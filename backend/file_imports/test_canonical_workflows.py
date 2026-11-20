from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from openpyxl import Workbook, load_workbook

from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
from api.v1.suppliers.product_import_selectors import _create_import_items
from api.v1.suppliers.product_import_services import (
    commit_supplier_product_import_session,
    patch_supplier_product_import_item,
)
from api.v1.suppliers.serializers import SupplierProductImportItemSerializer
from api.v1.suppliers.tests import _make_supplier, _make_unit
from file_imports.canonical import parse_product_row
from file_imports.extraction import extract_rows_from_file
from file_imports.models import ImportRowReceipt
from file_imports.types import RawImportRow
from suppliers.models import SupplierProductImportSession


@override_settings(CANONICAL_SUPPLIER_PARSER_ENABLED=True, CANONICAL_IMPORT_LLM_ENABLED=False)
class CanonicalSupplierTests(TestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        from core.models import Unit

        self.unit = Unit.objects.filter(code="кг").first() or _make_unit(code="кг")
        self.session = SupplierProductImportSession.objects.create(supplier=self.supplier)

    def test_supplier_adapter_and_preview_preserve_raw(self):
        upload = SimpleUploadedFile(
            "price.csv", 'Название;Цена\nАджика "Балтийская" в ведре 1/5 кг, упак;210\n'.encode()
        )
        rows = parse_supplier_product_import_file(upload)
        self.assertEqual(rows[0]["name"], 'Аджика "Балтийская"')
        _create_import_items(self.session, rows)
        item = self.session.items.get()
        response = SupplierProductImportItemSerializer(item).data
        self.assertEqual(response["raw_name"], 'Аджика "Балтийская" в ведре 1/5 кг, упак')
        self.assertEqual(response["parser_version"], "canonical-products-v2")
        self.assertEqual(response["field_meta"]["unit_label"]["status"], "inferred")
        self.assertFalse(response["ready"])

    def ready_item(self, **extra):
        parsed = parse_product_row(RawImportRow(2, {"name": "Мука", "price": "0"}), lambda k: k)
        return self.session.items.create(
            row_number=2,
            raw_name="Мука",
            name="Мука",
            country_of_origin="Россия",
            unit=self.unit,
            net_unit=self.unit,
            price=Decimal(0),
            net_value=Decimal(1),
            delivery_multiple=Decimal(1),
            min_order_qty=Decimal(1),
            availability="in_stock",
            action="new",
            parsed_snapshot=parsed.snapshot(),
            **extra,
        )

    def test_commit_receipt_survives_deleted_session(self):
        item = self.ready_item()
        result = commit_supplier_product_import_session(session=self.session)
        self.assertEqual(result["imported_count"], 1)
        self.assertFalse(SupplierProductImportSession.objects.filter(id=self.session.id).exists())
        receipt = ImportRowReceipt.objects.get(source_item_id=item.id)
        self.assertEqual(receipt.snapshot["raw"]["cells"]["price"], "0")
        self.assertEqual(receipt.target_type, "supplier_product")

    def test_manual_price_override_retains_original(self):
        item = self.ready_item()
        with patch("api.v1.suppliers.product_import_services.find_supplier_product_import_candidates", return_value={}):
            item = patch_supplier_product_import_item(item=item, data={"price": Decimal("100")})
        self.assertEqual(item.parsed_snapshot["fields"]["price"]["value"], "0")
        self.assertEqual(item.parsed_snapshot["overrides"][0]["value"], "100")
        self.assertEqual(item.field_sources["price"], "user")


class CanonicalReaderTests(SimpleTestCase):
    def test_numeric_sku_format_and_original_cell(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Название", "Артикул", "Цена"])
        sheet.append(["Бифштекс", 123, "10"])
        sheet["B2"].number_format = "000000"
        stream = BytesIO()
        workbook.save(stream)
        rows = extract_rows_from_file(
            SimpleUploadedFile("p.xlsx", stream.getvalue()), header_alias_groups=({"название"}, {"артикул"}, {"цена"})
        )
        self.assertEqual(rows[0].cells["артикул"], "000123")
        self.assertEqual(rows[0].source["raw_cells"][1]["value"], 123)
        self.assertEqual(rows[0].source["raw_cells"][1]["number_format"], "000000")

    def test_export_text_cannot_become_formula(self):
        from file_imports.export_safety import escape_formula_cells

        workbook = Workbook()
        workbook.active.append(["=1+1", "@SUM(1)", "Нормальное имя"])
        escape_formula_cells(workbook.active)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        restored = load_workbook(stream)
        self.assertEqual(restored.active["A1"].data_type, "s")
        self.assertEqual(restored.active["A1"].value, "=1+1")


class CanonicalCatalogTests(TestCase):
    def test_catalog_snapshot_keeps_net_omitted_from_preview(self):
        from api.v1.catalog.import_sessions import create_catalog_import_items
        from api.v1.catalog.importers import parse_catalog_file
        from catalog.models import CatalogImportSession
        from core.models import User

        owner = User.objects.create(email="canonical-owner@example.com", full_name="Owner", role="venue_user")
        parsed = parse_catalog_file(
            SimpleUploadedFile("catalog.csv", "Название;Нетто\nБифштекс 1/0,295 кг;0,295\n".encode()),
            canonical_enabled=True,
        )
        session = CatalogImportSession.objects.create(owner=owner, domain="master")
        rows = [
            {"row_number": parsed.rows[0]["row_number"], "raw_name": parsed.rows[0]["name"], "action": "create_new"}
        ]
        create_catalog_import_items(session, rows, parsed_rows=parsed.rows)
        item = session.items.get()
        self.assertNotIn("net", item.raw_values)
        self.assertEqual(item.parsed_snapshot["fields"]["net_value"]["value"], "0.295")
        self.assertEqual(item.parsed_snapshot["raw"]["cells"]["название"], "Бифштекс 1/0,295 кг")

    @override_settings(CANONICAL_LEGACY_PARSER_ENABLED=True)
    def test_legacy_missing_availability_default_blocks_automatic_apply(self):
        from api.v1.suppliers.services import _ai_item_needs_review
        from core.models import Unit
        from file_imports.legacy_adapter import parse_legacy_row

        row = RawImportRow(2, {"название": "Мука", "цена": "100", "ед": "кг"})
        product = parse_legacy_row(row)
        snapshot = product["raw_payload"]["canonical"]
        self.assertEqual(snapshot["fields"]["availability_label"]["status"], "defaulted")
        self.assertEqual(snapshot["fields"]["currency"]["source"], "default")
        self.assertTrue(_ai_item_needs_review(product, Unit.objects.get(code="кг")))
