from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.v1.catalog.importers import parse_catalog_file
from api.v1.suppliers.product_import_parsing import _field_for_header_key, parse_supplier_product_import_file
from api.v1.suppliers.tests import _make_supplier
from file_imports.extraction import extract_rows_from_txt
from file_imports.models import ImportFile
from file_imports.task_locks import import_task_lock
from file_imports.tasks import recover_stalled_imports_task
from file_imports.types import RawImportRow
from suppliers.models import SupplierProductImportSession


class ImportParserRegressionTests(SimpleTestCase):
    def test_plain_text_supplier_rows_survive_without_inferred_price(self):
        rows = parse_supplier_product_import_file(SimpleUploadedFile("items.txt", "Мука\nСоль".encode()))
        self.assertEqual([row["name"] for row in rows], ["Мука", "Соль"])
        self.assertTrue(all(row["price"] is None for row in rows))

    def test_plain_text_commas_do_not_create_fake_columns(self):
        rows = parse_supplier_product_import_file(SimpleUploadedFile("items.txt", "Молоко, 3.2%\nСоль".encode()))
        self.assertEqual([row["name"] for row in rows], ["Молоко, 3.2%", "Соль"])

    def test_two_column_text_table_is_recognized(self):
        rows = extract_rows_from_txt("Название;Цена\nМука;100".encode(), header_alias_groups=({"название"}, {"цена"}))
        self.assertEqual(rows[0].cells["цена"], "100")

    def test_net_unit_header_wins_over_generic_unit_header(self):
        self.assertEqual(_field_for_header_key("единица нетто"), "net_unit_label")

    def test_extended_catalog_headers_and_limits(self):
        extracted = [RawImportRow(1, {"наименование товара": "Мука", "ед. изм": "кг"})]
        with patch("api.v1.catalog.importers.extract_rows_from_file", return_value=extracted):
            rows = parse_catalog_file(SimpleUploadedFile("items.pdf", b"test")).rows
        self.assertEqual(rows[0], {"row_number": 1, "name": "Мука", "unit": "кг"})
        with patch("api.v1.catalog.importers.extract_rows_from_file", return_value=extracted * 3):
            with self.assertRaises(ValidationError):
                parse_catalog_file(SimpleUploadedFile("items.pdf", b"test"), max_rows=2)


class ImportTaskLockRegressionTests(TransactionTestCase):
    def test_parallel_claim_is_rejected_and_release_allows_retry(self):
        with import_task_lock("supplier", "test-job") as first:
            self.assertTrue(first)
            with import_task_lock("supplier", "test-job") as second:
                self.assertFalse(second)
        with import_task_lock("supplier", "test-job") as retry:
            self.assertTrue(retry)

    def test_process_death_releases_import_claim(self):
        import os
        import select
        import subprocess
        import sys
        import time

        from django.db import connections

        child_code = """import django, signal, os
 django.setup()
 from django.db import connections
 connections["default"].settings_dict["NAME"] = os.environ["AUTOSNAB_LOCK_TEST_DB"]
 from file_imports.task_locks import import_task_lock
 with import_task_lock('supplier', 'crashed-worker') as acquired:
     print(acquired, flush=True)
     signal.pause()
""".replace("\n ", "\n")
        child_env = dict(
            os.environ,
            DJANGO_SETTINGS_MODULE=os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings"),
            AUTOSNAB_LOCK_TEST_DB=connections["default"].settings_dict["NAME"],
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code], env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            self.assertTrue(select.select([child.stdout], [], [], 15)[0], "Child did not claim in time")
            line = child.stdout.readline().strip()
            if line != "True":
                self.fail(child.communicate(timeout=5)[1])
            with import_task_lock("supplier", "crashed-worker") as concurrent:
                self.assertFalse(concurrent)
            child.kill()
            child.communicate(timeout=5)
            deadline = time.monotonic() + 5
            reclaimed = False
            while time.monotonic() < deadline:
                with import_task_lock("supplier", "crashed-worker") as available:
                    if available:
                        reclaimed = True
                        break
                time.sleep(0.02)
            self.assertTrue(reclaimed)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


@override_settings(IMPORT_RECOVERY_ENABLED=True)
class ImportRecoveryRegressionTests(TestCase):
    def setUp(self):
        self.supplier = _make_supplier()
        self.source = ImportFile.objects.create(
            scope_type="supplier",
            scope_id=self.supplier.id,
            original_name="items.txt",
            size=4,
            sha256="a" * 64,
            storage_key="test-source",
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.session = SupplierProductImportSession.objects.create(
            supplier=self.supplier, import_file=self.source, processing_status="processing"
        )
        self.stale()

    def stale(self):
        SupplierProductImportSession.objects.filter(pk=self.session.pk).update(
            updated_at=timezone.now() - timedelta(hours=3)
        )

    def test_stalled_job_is_requeued_at_most_twice(self):
        with patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay") as enqueue:
            for _ in range(2):
                self.assertEqual(recover_stalled_imports_task()["requeued"], 1)
                self.stale()
            self.assertEqual(recover_stalled_imports_task()["failed"], 1)
        self.session.refresh_from_db()
        self.assertEqual((self.session.processing_status, self.session.recovery_attempts), ("failed", 2))
        self.assertEqual(enqueue.call_count, 2)

    def test_live_job_is_not_requeued(self):
        with import_task_lock("supplier", self.session.id) as acquired:
            self.assertTrue(acquired)
            with patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay") as enqueue:
                self.assertEqual(recover_stalled_imports_task()["requeued"], 0)
                enqueue.assert_not_called()
