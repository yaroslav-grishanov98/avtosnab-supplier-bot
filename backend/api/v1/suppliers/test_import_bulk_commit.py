"""Applying an import creates the new cards in batches: few queries, and a conflict stays pinned to its own row."""

from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from api.v1.suppliers.product_import_services import commit_supplier_product_import_session
from core.models import EntityStatus, Unit, UnitKind
from file_imports.models import ImportRowReceipt
from suppliers.models import (
    PriceStatus,
    ProductAvailability,
    Supplier,
    SupplierProduct,
    SupplierProductImportAction,
    SupplierProductImportItem,
    SupplierProductImportSession,
    SupplierProductPrice,
)


def _unit():
    unit, _ = Unit.objects.get_or_create(code="bulk-pcs", defaults={"name": "bulk-pcs", "kind": UnitKind.PIECE})
    return unit


class BulkCommitTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(legal_name="Bulk LLC", display_name="Bulk LLC")
        self.session = SupplierProductImportSession.objects.create(supplier=self.supplier, processing_status="ready")
        self.unit = _unit()

    def _row(self, number, name=None, **extra):
        values = {
            "session": self.session,
            "row_number": number,
            "raw_name": name or f"Товар {number}",
            "name": name or f"Товар {number}",
            "unit": self.unit,
            "net_value": Decimal("1"),
            "net_unit": self.unit,
            "price": Decimal(100 + number),
            "country_of_origin": "Россия",
            "delivery_multiple": Decimal("1"),
            "min_order_qty": Decimal("1"),
            "availability": ProductAvailability.IN_STOCK,
            "action": SupplierProductImportAction.NEW,
        }
        values.update(extra)
        return SupplierProductImportItem.objects.create(**values)

    def _products(self):
        return SupplierProduct.objects.filter(supplier=self.supplier)

    def test_many_rows_become_drafts_with_prices_in_a_few_queries(self):
        for number in range(1, 451):
            self._row(number)

        with CaptureQueriesContext(connection) as queries:
            result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 450)
        self.assertEqual(self._products().count(), 450)
        self.assertEqual(set(self._products().values_list("status", flat=True)), {"draft"})
        self.assertEqual(SupplierProductPrice.objects.filter(supplier_product__supplier=self.supplier).count(), 450)
        sample = self._products().get(supplier_name="Товар 7")
        self.assertEqual((sample.current_price, sample.previous_price), (Decimal("107"), None))
        price = sample.prices.get()
        self.assertEqual((price.price, price.status), (Decimal("107"), PriceStatus.ACTIVE))
        # One system SKU per card, all different.
        self.assertEqual(self._products().values("system_sku").distinct().count(), 450)
        # It used to take about fourteen queries per card.
        self.assertLess(len(queries), 150, len(queries))

    def test_a_name_taken_in_the_catalog_fails_only_that_row(self):
        SupplierProduct.objects.create(supplier=self.supplier, supplier_name="Сахар", normalized_name="Сахар")
        self._row(1, "Сахар")
        self._row(2, "Мука")

        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["not_ready_row_numbers"], [1])
        self.assertTrue(self._products().filter(supplier_name="Мука", status="draft").exists())
        left = SupplierProductImportItem.objects.get(session=self.session)
        self.assertEqual(left.name, "Сахар")
        self.assertEqual(
            left.validation_errors, ["supplier_raw_name: Товар с таким названием уже есть у этого поставщика."]
        )

    def test_a_repeated_name_inside_the_file_creates_one_card(self):
        self._row(1, "Соль")
        self._row(2, "Соль")

        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(self._products().filter(supplier_name="Соль").count(), 1)
        self.assertEqual(result["not_ready_row_numbers"], [2])

    def test_a_taken_article_number_fails_that_row(self):
        self._row(1, "Чай А", supplier_sku="T-1")
        self._row(2, "Чай Б", supplier_sku="T-1")

        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(self._products().get(supplier_name="Чай А").supplier_sku, "T-1")
        self.assertFalse(self._products().filter(supplier_name="Чай Б").exists())
        self.assertEqual(
            SupplierProductImportItem.objects.get(session=self.session).validation_errors,
            ["Артикул T-1 уже занят другим товаром этого поставщика."],
        )

    def test_an_article_number_of_an_archived_card_fails_with_a_hint(self):
        SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Чай старый", supplier_sku="T-1", status=EntityStatus.ARCHIVED
        )
        self._row(1, "Чай новый", supplier_sku="T-1")

        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(
            SupplierProductImportItem.objects.get(session=self.session).validation_errors,
            ["Артикул T-1 занят товаром «Чай старый» в архиве. Восстановите товар из архива или исправьте артикул."],
        )

    def test_an_incomplete_pack_composition_fails_that_row_only(self):
        self._row(1, "Кофе", pack_items_count=6)
        self._row(2, "Какао")

        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["not_ready_row_numbers"], [1])
        self.assertTrue(self._products().filter(supplier_name="Какао").exists())

    def test_skipped_and_imported_rows_leave_the_session_and_leave_receipts(self):
        snapshot = {"parser_version": "canonical-test", "fields": {}, "parse_issues": []}
        kept = self._row(1, "Рис", parsed_snapshot=snapshot)
        self._row(2, "Не нужен", action=SupplierProductImportAction.SKIP)

        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 1)
        self.assertFalse(SupplierProductImportItem.objects.filter(session_id=self.session.pk).exists())
        receipt = ImportRowReceipt.objects.get(source_item_id=kept.id)
        self.assertEqual(receipt.parser_version, "canonical-test")
        self.assertEqual(str(receipt.target_id), str(self._products().get(supplier_name="Рис").id))

    def test_a_concurrent_name_clash_at_insert_falls_back_to_one_by_one(self):
        self._row(1, "Гречка")
        self._row(2, "Овёс")

        with patch(
            "api.v1.suppliers.product_import_services.SupplierProduct.objects.bulk_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(set(self._products().values_list("supplier_name", flat=True)), {"Гречка", "Овёс"})
        self.assertEqual(set(self._products().values_list("status", flat=True)), {"draft"})
        self.assertEqual(self._products().get(supplier_name="Гречка").current_price, Decimal("101"))
