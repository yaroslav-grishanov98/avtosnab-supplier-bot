"""Changing the sale unit must not leave a net quantity that was only derived from the old sale unit."""

from decimal import Decimal

from django.test import TestCase

from api.v1.suppliers.product_import_services import _item_errors, patch_supplier_product_import_item
from core.models import Unit, UnitKind
from suppliers.models import (
    Supplier,
    SupplierProductImportAction,
    SupplierProductImportItem,
    SupplierProductImportSession,
)


def _snapshot(net_method):
    candidate = {"value": "1", "method": net_method, "source": "rule", "status": "inferred"}
    return {
        "parser_version": "canonical-products-v2",
        "fields": {
            "unit_label": {"value": "шт", "status": "defaulted", "candidates": []},
            "net_value": {"value": "1", "status": "inferred", "candidates": [candidate]},
            "net_unit_label": {"value": "шт", "status": "inferred", "candidates": [{**candidate, "value": "шт"}]},
        },
        "parse_issues": [],
    }


class UnitChangeRecomputeTests(TestCase):
    def setUp(self):
        supplier = Supplier.objects.create(legal_name="Unit LLC", display_name="Unit LLC")
        self.session = SupplierProductImportSession.objects.create(supplier=supplier)
        self.piece, _ = Unit.objects.get_or_create(code="шт", defaults={"name": "Штука", "kind": UnitKind.PIECE})
        self.kg, _ = Unit.objects.get_or_create(code="кг", defaults={"name": "Килограмм", "kind": UnitKind.WEIGHT})
        self.pack, _ = Unit.objects.get_or_create(code="упак", defaults={"name": "Упаковка", "kind": UnitKind.PACKAGE})

    def _item(self, net_method="price-per-unit", **kwargs):
        values = {
            "session": self.session,
            "row_number": 1,
            "raw_name": "Манго",
            "name": "Манго",
            "unit": self.piece,
            "price": Decimal("10"),
            "net_value": Decimal("1"),
            "net_unit": self.piece,
            "action": SupplierProductImportAction.NEW,
            "parsed_snapshot": _snapshot(net_method),
        }
        values.update(kwargs)
        return SupplierProductImportItem.objects.create(**values)

    def test_derived_net_is_dropped_when_the_unit_no_longer_implies_one(self):
        item = patch_supplier_product_import_item(item=self._item(), data={"unit_id": self.pack.pk})
        item.refresh_from_db()
        self.assertEqual(item.unit_id, self.pack.pk)
        self.assertIsNone(item.net_value)
        self.assertIsNone(item.net_unit_id)
        self.assertIn("Не заполнено поле net_value.", _item_errors(item))

    def test_derived_net_follows_the_new_unit_when_it_implies_one(self):
        item = patch_supplier_product_import_item(item=self._item(), data={"unit_id": self.kg.pk})
        item.refresh_from_db()
        self.assertEqual((item.net_value, item.net_unit_id), (Decimal("1"), self.kg.pk))

    def test_net_given_in_the_same_patch_is_kept(self):
        item = patch_supplier_product_import_item(
            item=self._item(), data={"unit_id": self.pack.pk, "net_value": Decimal("5"), "net_unit_id": self.kg.pk}
        )
        item.refresh_from_db()
        self.assertEqual((item.net_value, item.net_unit_id), (Decimal("5"), self.kg.pk))

    def test_net_taken_from_the_file_is_never_replaced(self):
        item = self._item(net_method="measure-expression")
        item = patch_supplier_product_import_item(item=item, data={"unit_id": self.pack.pk})
        item.refresh_from_db()
        self.assertEqual((item.net_value, item.net_unit_id), (Decimal("1"), self.piece.pk))

    def test_net_edited_by_the_user_is_never_replaced(self):
        item = self._item(field_sources={"net_value": "user"})
        item = patch_supplier_product_import_item(item=item, data={"unit_id": self.pack.pk})
        item.refresh_from_db()
        self.assertEqual((item.net_value, item.net_unit_id), (Decimal("1"), self.piece.pk))

    def test_snapshot_override_stores_the_unit_code_not_its_id(self):
        item = patch_supplier_product_import_item(
            item=self._item(), data={"unit_id": self.pack.pk, "net_unit_id": self.kg.pk}
        )
        overrides = {entry["field"]: entry["value"] for entry in item.parsed_snapshot["overrides"]}
        self.assertEqual(overrides["unit_label"], "упак")
        self.assertEqual(overrides["net_unit_label"], "кг")

    def test_snapshot_override_stores_pack_unit_codes(self):
        item = patch_supplier_product_import_item(
            item=self._item(),
            data={"pack_items_count": 6, "pack_item_value": Decimal("2"), "pack_item_unit": self.kg},
        )
        overrides = {entry["field"]: entry["value"] for entry in item.parsed_snapshot["overrides"]}
        self.assertEqual(overrides["pack_item_unit_label"], "кг")

    def test_other_edits_do_not_touch_a_derived_net(self):
        item = patch_supplier_product_import_item(item=self._item(), data={"price": Decimal("12")})
        item.refresh_from_db()
        self.assertEqual((item.net_value, item.net_unit_id), (Decimal("1"), self.piece.pk))
