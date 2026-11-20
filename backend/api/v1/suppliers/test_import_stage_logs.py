from unittest.mock import patch

from django.test import TestCase

from api.v1.suppliers import product_import_selectors as selectors
from api.v1.suppliers.product_import_llm import SupplierProductEnrichmentError
from core.models import Unit
from suppliers.models import Supplier, SupplierProductImportItem, SupplierProductImportSession


class ImportStageLogTests(TestCase):
    def _session(self, rows=3):
        supplier = Supplier.objects.create(display_name="Stage log supplier")
        session = SupplierProductImportSession.objects.create(
            supplier=supplier, rows_total=rows, processing_status="pending"
        )
        unit = Unit.objects.get(code="шт")
        for number in range(1, rows + 1):
            SupplierProductImportItem.objects.create(
                session=session, row_number=number, raw_name=f"Товар {number}", name=f"Товар {number}", unit_id=unit.id
            )
        return session

    def test_process_logs_start_every_batch_and_totals(self):
        session = self._session(rows=3)
        with (
            patch.object(selectors, "_batch_size", return_value=2),
            patch.object(selectors, "enrich_supplier_product_import_rows", return_value={}),
            self.assertLogs("autosnab.supplier_import", level="INFO") as logs,
        ):
            selectors.process_supplier_product_import_session(session_id=session.id)
        text = "\n".join(logs.output)
        self.assertIn(f"import.process.start session={session.pk} rows=3 queue_wait_s=", text)
        self.assertIn("import.process.batch session=%s rows=1-2 of=3" % session.pk, text)
        self.assertIn("import.process.batch session=%s rows=3-3 of=3" % session.pk, text)
        self.assertIn(f"import.process.done session={session.pk} rows=3 total_s=", text)
        for key in ("enrich_s=", "match_s=", "save_s="):
            self.assertIn(key, text)

    def test_failed_enrichment_is_logged_as_a_warning_and_the_import_continues(self):
        session = self._session(rows=2)
        with (
            patch.object(
                selectors, "enrich_supplier_product_import_rows", side_effect=SupplierProductEnrichmentError("boom")
            ),
            self.assertLogs("autosnab.supplier_import", level="WARNING") as logs,
        ):
            result = selectors.process_supplier_product_import_session(session_id=session.id)
        self.assertEqual(result.processing_status, "ready")
        self.assertIn("import.enrich_failed", "\n".join(logs.output))
