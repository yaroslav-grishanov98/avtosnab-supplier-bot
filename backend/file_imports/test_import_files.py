import shutil
import tempfile
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from file_imports.models import ImportFile, ImportFileScope
from file_imports.services import (
    ImportFileUnavailable,
    import_file_as_upload,
    import_file_storage_enabled,
    purge_expired_import_files,
    store_import_file,
)


class TemporaryMediaRootMixin:
    @classmethod
    def setUpClass(cls):
        cls._temporary_media_root = tempfile.mkdtemp(prefix="autosnab-test-media-")
        cls._temporary_media_override = override_settings(MEDIA_ROOT=cls._temporary_media_root)
        cls._temporary_media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._temporary_media_override.disable()
        shutil.rmtree(cls._temporary_media_root, ignore_errors=True)


class ImportFileModelTests(TestCase):
    def _make(self, **overrides):
        values = {
            "scope_type": ImportFileScope.SUPPLIER,
            "scope_id": uuid.uuid4(),
            "original_name": "price.xlsx",
            "content_type": "application/vnd.ms-excel",
            "size": 10,
            "sha256": "a" * 64,
            "storage_key": "import_files/supplier/x/y.xlsx",
            "expires_at": timezone.now() + timedelta(days=30),
        }
        values.update(overrides)
        return ImportFile.objects.create(**values)

    def test_live_file_is_unique_per_scope_and_hash(self):
        first = self._make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._make(scope_id=first.scope_id)

    def test_same_hash_in_another_scope_is_allowed(self):
        first = self._make()
        self._make(scope_id=uuid.uuid4())
        self._make(scope_type=ImportFileScope.OWNER, scope_id=first.scope_id)

    def test_deleted_file_does_not_block_a_new_one(self):
        first = self._make()
        first.deleted_at = timezone.now()
        first.save(update_fields=["deleted_at"])
        self._make(scope_id=first.scope_id)


class StoreImportFileTests(TemporaryMediaRootMixin, TestCase):
    def setUp(self):
        self.scope_id = uuid.uuid4()

    def _upload(self, content=b"name,price\nmilk,10\n", name="price.csv"):
        return SimpleUploadedFile(name, content, content_type="text/csv")

    def test_file_is_saved_with_hash_size_and_30_day_expiry(self):
        import_file = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
        )
        self.assertEqual(import_file.size, len(b"name,price\nmilk,10\n"))
        self.assertEqual(len(import_file.sha256), 64)
        self.assertEqual(import_file.original_name, "price.csv")
        self.assertTrue(default_storage.exists(import_file.storage_key))
        self.assertLess(abs(import_file.expires_at - (timezone.now() + timedelta(days=30))), timedelta(minutes=1))

    def test_store_and_reuse_are_logged_without_file_content(self):
        upload = self._upload()
        with self.assertLogs("autosnab.file_imports", level="INFO") as logs:
            first = store_import_file(uploaded_file=upload, scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id)
            store_import_file(uploaded_file=upload, scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id)
            import_file_as_upload(first)
        text = "\n".join(logs.output)
        self.assertIn("import_file.stored id=%s" % first.pk, text)
        self.assertIn("import_file.reused id=%s" % first.pk, text)
        self.assertIn("import_file.read id=%s" % first.pk, text)
        self.assertIn(first.storage_key, text)
        self.assertNotIn("milk", text)

    def test_storage_key_is_unguessable_and_has_no_original_name(self):
        import_file = store_import_file(
            uploaded_file=self._upload(name="Секретный прайс.csv"),
            scope_type=ImportFileScope.SUPPLIER,
            scope_id=self.scope_id,
        )
        self.assertRegex(import_file.storage_key, rf"^import_files/supplier/{self.scope_id}/[0-9a-f]{{32}}\.csv$")

    def test_pointer_is_reset_so_the_parser_can_read_the_file_again(self):
        upload = self._upload()
        store_import_file(uploaded_file=upload, scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id)
        self.assertEqual(upload.read(), b"name,price\nmilk,10\n")

    def test_same_file_in_same_scope_is_reused_and_expiry_is_extended(self):
        first = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
        )
        ImportFile.objects.filter(pk=first.pk).update(expires_at=timezone.now() + timedelta(days=1))
        second = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
        )
        self.assertEqual(first.pk, second.pk)
        self.assertGreater(second.expires_at, timezone.now() + timedelta(days=29))
        self.assertEqual(ImportFile.objects.count(), 1)

    def test_same_content_in_another_scope_is_a_separate_file(self):
        first = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
        )
        second = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=uuid.uuid4()
        )
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.storage_key, second.storage_key)

    def test_record_without_file_in_storage_is_replaced(self):
        first = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
        )
        default_storage.delete(first.storage_key)
        second = store_import_file(
            uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
        )
        self.assertNotEqual(first.pk, second.pk)
        first.refresh_from_db()
        self.assertIsNotNone(first.deleted_at)
        self.assertTrue(default_storage.exists(second.storage_key))

    def test_storage_failure_becomes_import_file_unavailable_and_leaves_no_record(self):
        with patch("file_imports.services.default_storage.save", side_effect=OSError("s3 down")):
            with self.assertRaises(ImportFileUnavailable):
                store_import_file(
                    uploaded_file=self._upload(), scope_type=ImportFileScope.SUPPLIER, scope_id=self.scope_id
                )
        self.assertEqual(ImportFile.objects.count(), 0)


class ImportFileAsUploadTests(TemporaryMediaRootMixin, TestCase):
    def _stored(self):
        return store_import_file(
            uploaded_file=SimpleUploadedFile("price.csv", b"a,b\n1,2\n"),
            scope_type=ImportFileScope.OWNER,
            scope_id=uuid.uuid4(),
        )

    def test_returns_readable_upload_with_original_name(self):
        upload = import_file_as_upload(self._stored())
        self.assertEqual(upload.name, "price.csv")
        self.assertEqual(upload.read(), b"a,b\n1,2\n")

    def test_expired_file_is_unavailable(self):
        stored = self._stored()
        ImportFile.objects.filter(pk=stored.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        stored.refresh_from_db()
        with self.assertRaises(ImportFileUnavailable):
            import_file_as_upload(stored)

    def test_deleted_file_is_unavailable(self):
        stored = self._stored()
        stored.deleted_at = timezone.now()
        stored.save(update_fields=["deleted_at"])
        with self.assertRaises(ImportFileUnavailable):
            import_file_as_upload(stored)

    def test_missing_from_storage_is_unavailable(self):
        stored = self._stored()
        default_storage.delete(stored.storage_key)
        with self.assertRaises(ImportFileUnavailable):
            import_file_as_upload(stored)


class ImportFileStorageFlagTests(TestCase):
    def test_flag_is_off_by_default(self):
        self.assertFalse(import_file_storage_enabled())

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=True)
    def test_flag_can_be_enabled(self):
        self.assertTrue(import_file_storage_enabled())


class PurgeExpiredImportFilesTests(TemporaryMediaRootMixin, TestCase):
    def _stored(self, content):
        return store_import_file(
            uploaded_file=SimpleUploadedFile("p.csv", content),
            scope_type=ImportFileScope.SUPPLIER,
            scope_id=uuid.uuid4(),
        )

    def test_expired_file_is_removed_from_storage_and_marked_deleted(self):
        stored = self._stored(b"a\n1\n")
        ImportFile.objects.filter(pk=stored.pk).update(expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(purge_expired_import_files(), 1)
        stored.refresh_from_db()
        self.assertIsNotNone(stored.deleted_at)
        self.assertFalse(default_storage.exists(stored.storage_key))

    def test_fresh_file_is_kept(self):
        stored = self._stored(b"a\n2\n")
        self.assertEqual(purge_expired_import_files(), 0)
        stored.refresh_from_db()
        self.assertIsNone(stored.deleted_at)
        self.assertTrue(default_storage.exists(stored.storage_key))

    def test_already_deleted_record_is_not_processed_again(self):
        stored = self._stored(b"a\n3\n")
        ImportFile.objects.filter(pk=stored.pk).update(
            expires_at=timezone.now() - timedelta(days=1), deleted_at=timezone.now()
        )
        self.assertEqual(purge_expired_import_files(), 0)

    def test_storage_error_on_one_file_does_not_stop_the_others(self):
        first = self._stored(b"a\n4\n")
        second = self._stored(b"a\n5\n")
        ImportFile.objects.update(expires_at=timezone.now() - timedelta(days=1))
        real_delete = default_storage.delete

        def flaky(name):
            if name == first.storage_key:
                raise OSError("boom")
            return real_delete(name)

        with patch("file_imports.services.default_storage.delete", side_effect=flaky):
            self.assertEqual(purge_expired_import_files(), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.deleted_at)  # повторим при следующем запуске
        self.assertIsNotNone(second.deleted_at)

    def test_celery_task_is_routed_and_scheduled(self):
        from django.conf import settings

        from file_imports.tasks import purge_expired_import_files_task

        self.assertEqual(
            settings.CELERY_TASK_ROUTES[purge_expired_import_files_task.name]["queue"], settings.AI_CLEANUP_QUEUE
        )
        scheduled = [entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()]
        self.assertIn(purge_expired_import_files_task.name, scheduled)
