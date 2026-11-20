"""ТЗ панели «Неразобранные» (2026-09-28): фильтры по заведению/поставщику,
список по всем заведениям пользователя, «Убрать из списка», поля строки
(order_id/venue_id/order_comment), карточка заведения для popup."""

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer, _make_supplier_product, _make_supplier_product_for, _make_venue_owner
from catalog.models import ProductCategory
from core.models import (
    EntityStatus,
    OffSystemSupplier,
    Order,
    OrderItem,
    OrderItemStatus,
    Venue,
    VenueUser,
    VenueUserRole,
)
from suppliers.models import Supplier


def _item(venue, supplier_product=None, *, status=OrderItemStatus.CANCELLED, requested="10", confirmed=None, **extra):
    order = Order.objects.create(venue=venue, order_number=f"T-{uuid4().hex[:10]}", comment="Для сока, до утра")
    return OrderItem.objects.create(
        order=order,
        supplier_product=supplier_product,
        requested_name=extra.pop("name", "Апельсин бразильский"),
        requested_qty=Decimal(requested),
        confirmed_qty=Decimal(confirmed) if confirmed is not None else None,
        status=status,
        **extra,
    )


class UnresolvedItemsPanelAPITests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Базилик")
        self.other_venue = Venue.objects.create(display_name="Чужое")
        self.owner = _make_venue_owner(self.venue)
        self.second_venue = Venue.objects.create(display_name="Базилик-2")
        VenueUser.objects.create(
            venue=self.second_venue, user=self.owner, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Поставщик 1")
        self.supplier_b = Supplier.objects.create(legal_name="B LLC", display_name="Поставщик 2")
        self.sp_a = _make_supplier_product(supplier=self.supplier_a, price="152.00", normalized_name="a")
        self.sp_b = _make_supplier_product(supplier=self.supplier_b, price="10.00", normalized_name="b")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _venue_list(self, venue=None, **params):
        return self.client.get(f"/api/v1/catalog/venues/{(venue or self.venue).id}/orders/unresolved-items", params)

    def test_row_has_order_venue_ids_and_order_comment(self):
        item = _item(
            self.venue, self.sp_a, status=OrderItemStatus.CONFIRMED, requested="160", confirmed="24", comment="поз"
        )
        row = self._venue_list().json()["results"][0]
        self.assertEqual(row["order_id"], str(item.order_id))
        self.assertEqual(row["venue_id"], str(self.venue.id))
        self.assertEqual(row["order_comment"], "Для сока, до утра")
        self.assertEqual(row["comment"], "поз")
        self.assertEqual(Decimal(row["remaining_qty"]), Decimal("136"))
        self.assertEqual(row["status"], "partially_confirmed")

    def test_supplier_filters(self):
        _item(self.venue, self.sp_a, name="из A")
        _item(self.venue, self.sp_b, name="из B")
        off = OffSystemSupplier.objects.create(venue=self.venue, display_name="Рынок")
        _item(self.venue, None, name="вне системы", off_system_supplier=off)

        names = [r["requested_name"] for r in self._venue_list(supplier_id=str(self.supplier_a.id)).json()["results"]]
        self.assertEqual(names, ["из A"])
        names = [r["requested_name"] for r in self._venue_list(off_system_supplier_id=str(off.id)).json()["results"]]
        self.assertEqual(names, ["вне системы"])

    def test_mine_list_spans_my_venues_with_venue_filter(self):
        _item(self.venue, self.sp_a, name="первое")
        _item(self.second_venue, self.sp_a, name="второе")
        _item(self.other_venue, self.sp_a, name="чужое")
        _item(self.venue, self.sp_a, status=OrderItemStatus.CONFIRMED, requested="5", confirmed="5", name="принято")

        response = self.client.get("/api/v1/catalog/venues/mine/orders/unresolved-items")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({r["requested_name"] for r in response.json()["results"]}, {"первое", "второе"})

        response = self.client.get(
            "/api/v1/catalog/venues/mine/orders/unresolved-items",
            {"venue_id": [str(self.second_venue.id), str(self.other_venue.id)]},
        )
        self.assertEqual([r["requested_name"] for r in response.json()["results"]], ["второе"])

        response = self.client.get("/api/v1/catalog/venues/mine/orders/unresolved-items/count")
        self.assertEqual(response.json(), {"count": 2})

    def test_mine_list_rejects_service_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        response = self.client.get("/api/v1/catalog/venues/mine/orders/unresolved-items")
        self.assertEqual(response.status_code, 401)

    def test_dismiss_hides_item_permanently_without_changing_it(self):
        item = _item(self.venue, self.sp_a, status=OrderItemStatus.CONFIRMED, requested="160", confirmed="24")
        url = f"/api/v1/catalog/venues/{self.venue.id}/orders/{item.order_id}/items/{item.id}/dismiss"

        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.json())
        item.refresh_from_db()
        self.assertIsNotNone(item.unresolved_dismissed_at)
        self.assertEqual(item.unresolved_dismissed_by, self.owner)
        self.assertEqual(item.status, OrderItemStatus.CONFIRMED)
        self.assertEqual(item.confirmed_qty, Decimal("24"))

        self.assertEqual(self._venue_list().json()["count"], 0)
        count_url = f"/api/v1/catalog/venues/{self.venue.id}/orders/unresolved-items/count"
        self.assertEqual(self.client.get(count_url).json(), {"count": 0})
        self.assertEqual(
            self.client.get("/api/v1/catalog/venues/mine/orders/unresolved-items/count").json()["count"], 0
        )

        response = self.client.post(url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Позиция уже убрана из «Неразобранных».")

    def test_dismiss_rejects_item_not_in_unresolved(self):
        item = _item(self.venue, self.sp_a, status=OrderItemStatus.SUBMITTED)
        response = self.client.post(
            f"/api/v1/catalog/venues/{self.venue.id}/orders/{item.order_id}/items/{item.id}/dismiss"
        )
        self.assertEqual(response.status_code, 409)

    def test_dismiss_foreign_venue_forbidden(self):
        item = _item(self.other_venue, self.sp_a)
        response = self.client.post(
            f"/api/v1/catalog/venues/{self.other_venue.id}/orders/{item.order_id}/items/{item.id}/dismiss"
        )
        self.assertEqual(response.status_code, 403)

    def test_venue_card(self):
        response = self.client.get(f"/api/v1/venues/{self.second_venue.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_name"], "Базилик-2")
        self.assertEqual(response.json()["my_role"], VenueUserRole.MANAGER)

        self.assertEqual(self.client.get(f"/api/v1/venues/{self.other_venue.id}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/venues/{uuid4()}").status_code, 403)


@patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
class CatalogReplaceItemFilterAPITests(APITestCase):
    """2026-10-02: «Ищем замену» — GET /catalog/products?replace_item_id=<позиция заказа>."""

    url = "/api/v1/catalog/products"

    def setUp(self):
        self.venue = Venue.objects.create(display_name="Базилик")
        self.owner = _make_venue_owner(self.venue)
        self.fruits = ProductCategory.objects.create(name="Фрукты", slug="fruits")
        self.citrus = ProductCategory.objects.create(name="Цитрусовые", slug="citrus", parent=self.fruits)
        self.meat = ProductCategory.objects.create(name="Мясо", slug="meat")
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Поставщик 1")
        self.supplier_b = Supplier.objects.create(legal_name="B LLC", display_name="Поставщик 2")
        self.source = _make_supplier_product(supplier=self.supplier_a, price="152.00", category=self.fruits)
        self.same_supplier = _make_supplier_product_for(self.supplier_a, "Апельсин другой", "140.00")
        self.same_supplier.category = self.fruits
        self.same_supplier.save(update_fields=["category"])
        self.replacement = _make_supplier_product(supplier=self.supplier_b, price="150.00", category=self.fruits)
        self.sub_replacement = _make_supplier_product_for(self.supplier_b, "Апельсин Египет", "120.00")
        self.sub_replacement.category = self.citrus
        self.sub_replacement.save(update_fields=["category"])
        self.other_category = _make_supplier_product_for(self.supplier_b, "Говядина", "500.00")
        self.other_category.category = self.meat
        self.other_category.save(update_fields=["category"])
        self.item = _item(self.venue, self.source, status=OrderItemStatus.CONFIRMED, requested="160", confirmed="24")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _ids(self, response):
        self.assertEqual(response.status_code, 200, response.json())
        return {row["id"] for row in response.json()["results"]}

    def test_other_suppliers_same_category_tree(self, _):
        ids = self._ids(self.client.get(self.url, {"replace_item_id": str(self.item.id)}))
        self.assertEqual(ids, {str(self.replacement.id), str(self.sub_replacement.id)})

    def test_other_filters_apply_on_top(self, _):
        response = self.client.get(self.url, {"replace_item_id": str(self.item.id), "price_max": "130"})
        self.assertEqual(self._ids(response), {str(self.sub_replacement.id)})

    def test_without_category_searches_by_requested_name(self, _):
        item = _item(self.venue, None, name="Говядина")
        ids = self._ids(self.client.get(self.url, {"replace_item_id": str(item.id)}))
        self.assertIn(str(self.other_category.id), ids)
        self.assertNotIn(str(self.replacement.id), ids)

    def test_requires_jwt(self, _):
        self.client.credentials()
        response = self.client.get(self.url, {"replace_item_id": str(self.item.id)})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Для replace_item_id нужна авторизация."})

    def test_foreign_or_unknown_item_is_404(self, _):
        foreign = _item(Venue.objects.create(display_name="Чужое"), self.source)
        for item_id in (foreign.id, uuid4()):
            response = self.client.get(self.url, {"replace_item_id": str(item_id)})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "Позиция заказа для замены не найдена."})

    def test_invalid_uuid_is_400(self, _):
        response = self.client.get(self.url, {"replace_item_id": "nope"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("replace_item_id", response.json())
