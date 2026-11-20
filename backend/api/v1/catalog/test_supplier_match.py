"""TASK-148: массовый подбор товаров поставщиков для справочника — состояние строки, открытие каталога
по строке (контекст + пересчёт N), «Выбрать»."""

from unittest.mock import patch

from rest_framework.test import APITestCase

from api.v1.catalog.supplier_match import save_supplier_match_result
from api.v1.catalog.tests import _jwt_bearer, _make_supplier_product_for, _make_venue_owner
from catalog.models import (
    MasterProduct,
    ProductCategory,
    SupplierMatchStatus,
    VenueProductMatrixItem,
    VenueProductMatrixSupplierMatch,
)
from core.models import Unit, Venue
from suppliers.models import Supplier, SupplierProduct


@patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
class SupplierMatchAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Базилик")
        self.owner = _make_venue_owner(self.venue)
        self.vegetables = ProductCategory.objects.create(name="Овощи", slug="vegetables")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Поставщик 1")
        self.supplier_b = Supplier.objects.create(legal_name="B LLC", display_name="Поставщик 2")
        self.tomato_a = _make_supplier_product_for(self.supplier_a, "Помидор розовый", "200.00")
        self.tomato_b = _make_supplier_product_for(self.supplier_b, "Помидор черри", "300.00")
        for product in (self.tomato_a, self.tomato_b):
            product.category = self.vegetables
            product.save(update_fields=["category"])
        self.unit = Unit.objects.filter(code="кг").first() or Unit.objects.create(code="кг", name="килограмм")
        self.master = MasterProduct.objects.create(owner=self.owner, canonical_name="Помидор")
        self.item = VenueProductMatrixItem.objects.create(
            venue=self.venue,
            requested_name="Помидор Розовый",
            unit=self.unit,
            note="для салата",
            master_product=self.master,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _url(self, item=None, suffix=""):
        return f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/{(item or self.item).id}/supplier-match{suffix}"

    def _row(self, item=None):
        rows = self.client.get(f"/api/v1/catalog/venues/{self.venue.id}/product-matrix").json()["results"]
        return next(row for row in rows if row["id"] == str((item or self.item).id))

    def test_row_state_in_matrix_list(self, _):
        self.assertIsNone(self._row()["supplier_match"])

        save_supplier_match_result(
            self.item, status=SupplierMatchStatus.FOUND, query="Помидор", filters={}, result_count=12
        )
        state = self._row()["supplier_match"]
        self.assertEqual(state["status"], "found")
        self.assertEqual(state["result_count"], 12)
        self.assertIsNotNone(state["searched_at"])
        self.assertIsNone(state["error"])

        save_supplier_match_result(self.item, status=SupplierMatchStatus.SEARCHING)
        self.assertEqual(self._row()["supplier_match"]["status"], "searching")
        self.assertIsNone(self._row()["supplier_match"]["searched_at"])

    def test_context_returns_source_and_saved_search_and_refreshes_count(self, _):
        save_supplier_match_result(
            self.item,
            status=SupplierMatchStatus.FOUND,
            query="Помидор",
            filters={"category_id": [str(self.vegetables.id)]},
            result_count=12,
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertEqual(
            body["source"],
            {
                "item_id": str(self.item.id),
                "requested_name": "Помидор Розовый",
                "unit_label": "кг",
                "master_product_id": str(self.master.id),
                "master_product_name": "Помидор",
                "note": "для салата",
            },
        )
        self.assertEqual(body["search"], {"q": "Помидор", "filters": {"category_id": [str(self.vegetables.id)]}})
        # N актуализирован по каталогу: было 12, реально 2 — и сохранено в строке справочника.
        self.assertEqual(body["match"]["status"], "found")
        self.assertEqual(body["match"]["result_count"], 2)
        self.assertEqual(self._row()["supplier_match"]["result_count"], 2)

        # Тот же поиск в каталоге даёт столько же товаров.
        catalog = self.client.get(
            "/api/v1/catalog/products", {"q": body["search"]["q"], **body["search"]["filters"]}
        ).json()
        self.assertEqual(catalog["count"], 2)

    def test_refresh_switches_to_not_found(self, _):
        self.tomato_a.status = self.tomato_b.status = "archived"
        self.tomato_a.save(update_fields=["status"])
        self.tomato_b.save(update_fields=["status"])
        save_supplier_match_result(self.item, status=SupplierMatchStatus.FOUND, query="Помидор", result_count=2)
        body = self.client.get(self._url()).json()
        self.assertEqual(body["match"]["status"], "not_found")
        self.assertEqual(body["match"]["result_count"], 0)

    def test_searching_row_is_not_recounted(self, _):
        save_supplier_match_result(self.item, status=SupplierMatchStatus.SEARCHING, query="Помидор")
        body = self.client.get(self._url()).json()
        self.assertEqual(body["match"]["status"], "searching")
        self.assertIsNone(body["match"]["result_count"])

    def test_without_match_search_starts_from_requested_name(self, _):
        body = self.client.get(self._url()).json()
        self.assertIsNone(body["match"])
        self.assertEqual(body["search"], {"q": "Помидор Розовый", "filters": {}})
        self.assertFalse(VenueProductMatrixSupplierMatch.objects.exists())

    def test_supplier_and_availability_are_never_applied_automatically(self, _):
        save_supplier_match_result(
            self.item,
            status=SupplierMatchStatus.FOUND,
            query="Помидор",
            filters={"supplier_id": [str(self.supplier_a.id)], "availability": ["in_stock"], "brand_id": []},
            result_count=1,
        )
        body = self.client.get(self._url()).json()
        self.assertEqual(body["search"]["filters"], {})
        self.assertEqual(body["match"]["result_count"], 2)

    def test_broken_saved_filter_is_dropped_not_500(self, _):
        save_supplier_match_result(
            self.item,
            status=SupplierMatchStatus.FOUND,
            query="Помидор",
            filters={"category_id": ["not-a-uuid"]},
            result_count=5,
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["match"]["result_count"], 2)

    def test_select_links_product_and_clears_match(self, _):
        save_supplier_match_result(self.item, status=SupplierMatchStatus.FOUND, query="Помидор", result_count=1)
        response = self.client.post(self._url(suffix="/select"), {"supplier_product_id": str(self.tomato_b.id)})
        self.assertEqual(response.status_code, 200, response.json())
        row = response.json()
        self.assertEqual(row["supplier_product_id"], str(self.tomato_b.id))
        self.assertEqual(row["supplier_name"], "Поставщик 2")
        self.assertIsNone(row["supplier_match"])
        self.assertFalse(VenueProductMatrixSupplierMatch.objects.filter(item=self.item).exists())

        response = self.client.post(self._url(suffix="/select"), {"supplier_product_id": str(self.tomato_a.id)})
        self.assertEqual(response.status_code, 409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.supplier_product_id, self.tomato_b.id)

    def test_select_rejects_product_hidden_from_catalog(self, _):
        hidden = SupplierProduct.objects.create(
            supplier=self.supplier_a,
            supplier_name="Помидор без цены",
            normalized_name="помидор без цены",
            unit=self.unit,
        )
        response = self.client.post(self._url(suffix="/select"), {"supplier_product_id": str(hidden.id)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("supplier_product_id", response.json())

    def test_patch_link_clears_match(self, _):
        save_supplier_match_result(self.item, status=SupplierMatchStatus.NOT_FOUND, query="x", result_count=0)
        response = self.client.patch(
            f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/{self.item.id}",
            {"supplier_product_id": str(self.tomato_a.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertFalse(VenueProductMatrixSupplierMatch.objects.filter(item=self.item).exists())

    def test_access(self, _):
        other_venue = Venue.objects.create(display_name="Чужое")
        foreign = VenueProductMatrixItem.objects.create(venue=other_venue, requested_name="Форель")
        response = self.client.get(self._url(foreign))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "No VenueProductMatrixItem matches the given query."})

        stranger = _make_venue_owner(other_venue, email="stranger@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        self.assertEqual(self.client.get(self._url()).status_code, 403)

        self.client.credentials()
        self.assertEqual(self.client.get(self._url()).status_code, 401)
        self.assertEqual(self.client.post(self._url(suffix="/select"), {}).status_code, 401)
