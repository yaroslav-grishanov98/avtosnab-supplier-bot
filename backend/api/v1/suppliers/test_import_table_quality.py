"""Table-quality checks are warnings only: they flag rows and sessions, never block an import."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
from api.v1.suppliers.product_import_selectors import (
    _create_import_items,
    process_supplier_product_import_session,
    supplier_product_import_session_payload,
)
from api.v1.suppliers.product_import_services import _item_errors
from api.v1.suppliers.serializers import SupplierProductImportSessionResponseSerializer
from api.v1.suppliers.tests import _make_supplier
from suppliers.models import SupplierProductImportAction, SupplierProductImportSession


def _session_for(csv_text, *, canonical):
    supplier = _make_supplier()
    upload = SimpleUploadedFile("price.csv", csv_text.encode("utf-8"), content_type="text/csv")
    rows = parse_supplier_product_import_file(upload, canonical_enabled=canonical)
    session = SupplierProductImportSession.objects.create(supplier=supplier, rows_total=len(rows))
    _create_import_items(session, rows)
    session.refresh_from_db()
    return session


class PriceShareWarningSessionTests(TestCase):
    def test_low_price_share_adds_session_warning_without_blocking_rows(self):
        lines = ["Номенклатура;Цена"] + [f"Товар {chr(1040 + i)};{'100' if i < 2 else ''}" for i in range(8)]

        session = _session_for("\n".join(lines) + "\n", canonical=True)

        self.assertEqual(session.items.count(), 8)
        self.assertEqual([w["code"] for w in session.warnings], ["low_price_share"])
        self.assertEqual(session.warnings[0]["rows_with_price"], 2)
        self.assertEqual(session.warnings[0]["rows_total"], 8)

    def test_normal_price_share_has_no_warning(self):
        lines = ["Номенклатура;Цена"] + [f"Товар {chr(1040 + i)};{100 + i}" for i in range(8)]

        session = _session_for("\n".join(lines) + "\n", canonical=True)

        self.assertEqual(session.warnings, [])

    def test_warnings_are_exposed_in_session_response(self):
        lines = ["Номенклатура;Цена"] + [f"Товар {chr(1040 + i)};" for i in range(6)]
        session = _session_for("\n".join(lines) + "\n", canonical=True)

        payload = SupplierProductImportSessionResponseSerializer(supplier_product_import_session_payload(session)).data

        self.assertEqual(payload["warnings"][0]["code"], "low_price_share")


class NumericNameRowTests(TestCase):
    def _check(self, canonical):
        session = _session_for(
            "Номенклатура;Цена\nМука 5 кг;500\n1250;77\nСахар 1 кг;70\n",
            canonical=canonical,
        )
        process_supplier_product_import_session(session_id=session.id)
        items = {item.raw_name: item for item in session.items.all()}

        flagged = items["1250"]
        self.assertEqual(flagged.action, SupplierProductImportAction.REVIEW)
        self.assertTrue(any("только из числа" in message for message in _item_errors(flagged)))
        self.assertFalse(
            any("только из числа" in message for message in _item_errors(items["Мука 5 кг"])),
        )
        return flagged

    def test_canonical_numeric_name_is_flagged_through_parse_issues(self):
        flagged = self._check(canonical=True)
        self.assertIn("numeric_name", [issue["code"] for issue in flagged.parsed_snapshot["parse_issues"]])

    def test_legacy_numeric_name_is_flagged_without_snapshot(self):
        self._check(canonical=False)

    def test_numeric_name_cannot_be_forced_into_commit(self):
        session = _session_for("Номенклатура;Цена\n1250;77\n", canonical=True)
        item = session.items.get()
        item.action = SupplierProductImportAction.NEW
        item.save(update_fields=["action"])

        self.assertTrue(_item_errors(item))
