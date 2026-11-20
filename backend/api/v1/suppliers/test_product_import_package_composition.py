"""«Состав упаковки» в унифицированном импорте товаров поставщика: разбор колонки, строка сессии, PATCH, commit."""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
from api.v1.suppliers.tests import (
    _analyze_ready,
    _make_supplier,
    _supplier_cabinet_bearer,
    _xlsx_upload,
)
from core.models import Unit
from suppliers.models import SupplierProduct, SupplierProductImportItem, SupplierProductImportSession

BASE = "/api/v1/suppliers/products/import"


def _unit(code):
    return Unit.objects.get(code=code)


class ParseCompositionColumnTests(TestCase):
    def test_composition_column_is_parsed_into_row(self):
        rows = parse_supplier_product_import_file(
            _xlsx_upload([["Название", "Состав упаковки"], ["Мука", "8 шт. × 2 кг = 16 кг/кор."]])
        )
        self.assertEqual(rows[0]["package_composition"], "8 шт. × 2 кг = 16 кг/кор.")

    def test_row_without_composition_column_has_none(self):
        rows = parse_supplier_product_import_file(_xlsx_upload([["Название", "Цена"], ["Мука", "10"]]))
        self.assertIsNone(rows[0]["package_composition"])


class ImportCompositionFlowTests(APITestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.client.credentials(HTTP_AUTHORIZATION=_supplier_cabinet_bearer(self.supplier))

    def _analyze(self, rows):
        accepted, response = _analyze_ready(self.client, _xlsx_upload(rows))
        self.assertEqual(response.status_code, 200, response.content)
        return accepted.json()["id"], response.json()["items"]

    def test_analyze_stores_composition_on_item(self):
        _, items = self._analyze([["Название", "Состав упаковки"], ["Мука пшеничная", "8 шт. × 2 кг = 16 кг/кор."]])
        item = items[0]
        self.assertEqual(item["pack_items_count"], 8)
        self.assertEqual(Decimal(item["pack_item_value"]), Decimal("2"))
        self.assertEqual(item["pack_item_unit_id"], str(_unit("кг").id))
        self.assertEqual(item["pack_type_id"], str(_unit("кор").id))
        self.assertEqual(item["package_composition"]["display"], "8 шт. × 2 кг = 16 кг/кор.")

    def test_unparsable_composition_leaves_item_empty_and_row_kept(self):
        _, items = self._analyze([["Название", "Состав упаковки"], ["Мука", "как получится"]])
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["pack_items_count"])
        self.assertIsNone(items[0]["package_composition"])

    def test_patch_sets_clears_and_validates_composition(self):
        session_id, items = self._analyze([["Название"], ["Мука"]])
        url = f"{BASE}/{session_id}/items/{items[0]['id']}"

        partial = self.client.patch(url, {"pack_items_count": 6}, format="json")
        self.assertEqual(partial.status_code, 400)
        self.assertIn("package_composition", partial.json())

        full = self.client.patch(
            url,
            {
                "pack_items_count": 6,
                "pack_item_value": "0.75",
                "pack_item_unit_id": str(_unit("л").id),
                "pack_type_id": str(_unit("кор").id),
            },
            format="json",
        )
        self.assertEqual(full.status_code, 200, full.content)
        self.assertEqual(full.json()["package_composition"]["display"], "6 шт. × 0,75 л = 4,5 л/кор.")

        bad_type = self.client.patch(url, {"pack_type_id": str(_unit("кг").id)}, format="json")
        self.assertEqual(bad_type.status_code, 400)

        only_core_cleared = self.client.patch(
            url, {"pack_items_count": None, "pack_item_value": None, "pack_item_unit_id": None}, format="json"
        )
        self.assertEqual(only_core_cleared.status_code, 400)
        self.assertIn("pack_type_id", only_core_cleared.json())

        cleared = self.client.patch(
            url,
            {"pack_items_count": None, "pack_item_value": None, "pack_item_unit_id": None, "pack_type_id": None},
            format="json",
        )
        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.assertIsNone(cleared.json()["package_composition"])

    def _commit_ready_item(self, session_id, item_id, *, replace_target=None):
        unit = _unit("кг")
        payload = {
            "country_of_origin": "Россия",
            "unit_id": str(unit.id),
            "price": "10.00",
            "delivery_multiple": "1",
            "net_value": "1",
            "net_unit_id": str(unit.id),
            "min_order_qty": "1",
            "availability": "in_stock",
        }
        response = self.client.patch(f"{BASE}/{session_id}/items/{item_id}", payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        if replace_target is not None:
            # PATCH unit_id сбрасывает совпадение в review, поэтому replace выбираем после правок полей.
            replace = self.client.put(
                f"{BASE}/{session_id}/items/{item_id}/action",
                {"action": "replace", "target_supplier_product_id": str(replace_target.id)},
                format="json",
            )
            self.assertEqual(replace.status_code, 200, replace.content)
        with patch("api.v1.suppliers.views.product_import.notify_linked_supplier_chats"):
            commit = self.client.post(f"{BASE}/{session_id}/commit")
        self.assertEqual(commit.status_code, 200, commit.content)
        return commit.json()

    def test_commit_creates_product_with_composition(self):
        session_id, items = self._analyze(
            [["Название", "Состав упаковки"], ["Уникальная мука для состава", "8 шт. × 2 кг = 16 кг/кор."]]
        )
        result = self._commit_ready_item(session_id, items[0]["id"])
        self.assertEqual(result["imported_count"], 1)
        product = SupplierProduct.objects.get(supplier=self.supplier, supplier_name="Уникальная мука для состава")
        self.assertEqual(product.pack_items_count, 8)
        self.assertEqual(product.pack_item_value, Decimal("2"))
        self.assertEqual(product.pack_item_unit, _unit("кг"))
        self.assertEqual(product.pack_type, _unit("кор"))

    def test_replace_without_composition_keeps_existing_one(self):
        product = SupplierProduct.objects.create(
            supplier=self.supplier,
            supplier_name="Сахар песок",
            unit=_unit("кг"),
            pack_items_count=10,
            pack_item_value=Decimal("1"),
            pack_item_unit=_unit("кг"),
        )
        session_id, items = self._analyze([["Название"], ["Сахар песок"]])
        item_id = items[0]["id"]
        self._commit_ready_item(session_id, item_id, replace_target=product)
        product.refresh_from_db()
        self.assertEqual(product.pack_items_count, 10)
        self.assertEqual(product.pack_item_unit, _unit("кг"))
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=session_id).exists())
        self.assertFalse(SupplierProductImportItem.objects.filter(pk=item_id).exists())
