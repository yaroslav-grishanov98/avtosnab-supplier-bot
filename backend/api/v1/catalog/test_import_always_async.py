from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from api.v1.catalog.import_sessions import catalog_import_always_async
from api.v1.catalog.tests import TemporaryMediaRootMixin, _fake_embedding, _fake_embeddings, _jwt_bearer, _xlsx_upload
from catalog.models import CatalogImportSession, MasterProductImportUpload, MasterProductImportUploadStatus
from core.models import EntityStatus, Unit, User, UserRole, Venue, VenueUser, VenueUserRole


class CatalogImportAlwaysAsyncFlagTests(SimpleTestCase):
    @override_settings(CATALOG_IMPORT_ALWAYS_ASYNC=True, IMPORT_FILE_STORAGE_ENABLED=True)
    def test_on_when_flag_and_file_storage_are_enabled(self):
        self.assertTrue(catalog_import_always_async())

    @override_settings(CATALOG_IMPORT_ALWAYS_ASYNC=True, IMPORT_FILE_STORAGE_ENABLED=False)
    def test_off_without_file_storage(self):
        self.assertFalse(catalog_import_always_async())

    @override_settings(CATALOG_IMPORT_ALWAYS_ASYNC=False, IMPORT_FILE_STORAGE_ENABLED=True)
    def test_off_when_flag_is_off(self):
        self.assertFalse(catalog_import_always_async())


@override_settings(IMPORT_FILE_STORAGE_ENABLED=True, CATALOG_IMPORT_ALWAYS_ASYNC=True)
class MasterImportAlwaysAsyncTests(TemporaryMediaRootMixin, APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="master-async@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        Unit.objects.create(code="kg", name="Килограмм", kind="weight", status=EntityStatus.ACTIVE)
        self.url = "/api/v1/catalog/master-products/import/preview"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        for target, fake in (
            ("api.v1.catalog.selectors.master_catalog.create_embedding", _fake_embedding),
            ("api.v1.catalog.selectors.master_catalog.create_embeddings", _fake_embeddings),
        ):
            patcher = patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        close_patcher = patch("api.v1.catalog.tasks.close_old_connections")
        close_patcher.start()
        self.addCleanup(close_patcher.stop)

    def _post(self, rows):
        return self.client.post(
            self.url,
            {"owner_id": str(self.owner.id), "file": _xlsx_upload([["Название", "Нетто"], *rows])},
            format="multipart",
        )

    def test_small_file_returns_202_and_enqueues_task_without_rows(self):
        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            response = self._post([["Мука", ""], ["Сахар", ""]])
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        upload = MasterProductImportUpload.objects.get(pk=body["upload_id"])
        self.assertEqual((upload.rows_total, upload.status), (0, MasterProductImportUploadStatus.PROCESSING))
        self.assertIsNotNone(upload.import_file)
        self.assertEqual(body["status_url"], f"/api/v1/catalog/master-products/import/uploads/{upload.id}")
        mock_delay.assert_called_once()
        self.assertIsNone(mock_delay.call_args.kwargs["rows"])

    def test_worker_result_is_reviewable_through_the_session_api(self):
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            body = self._post([["Мука", ""], ["Сахар", ""]]).json()
        process_master_product_import_upload_task.apply(kwargs=mock_delay.call_args.kwargs, throw=True)

        status = self.client.get(body["status_url"], {"owner_id": str(self.owner.id)}).json()
        self.assertEqual((status["status"], status["percent"]), ("ready", 100))
        session_id = status["result"]["session_id"]
        session = CatalogImportSession.objects.get(pk=session_id)
        self.assertEqual(session.items.count(), 2)

    def test_bad_net_value_is_reported_by_the_worker(self):
        from api.v1.catalog.tasks import process_master_product_import_upload_task

        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            response = self._post([["Мука", "не число"]])
        self.assertEqual(response.status_code, 202, response.content)
        with self.assertRaises(ValidationError):
            process_master_product_import_upload_task.apply(kwargs=mock_delay.call_args.kwargs, throw=True)
        upload = MasterProductImportUpload.objects.get(pk=response.json()["upload_id"])
        self.assertEqual(upload.status, MasterProductImportUploadStatus.FAILED)
        self.assertTrue(upload.error_message)

    @override_settings(CATALOG_IMPORT_ALWAYS_ASYNC=False)
    def test_flag_off_keeps_the_synchronous_200(self):
        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            response = self._post([["Мука", ""]])
        self.assertEqual(response.status_code, 200, response.content)
        mock_delay.assert_not_called()

    @override_settings(IMPORT_FILE_STORAGE_ENABLED=False)
    def test_flag_without_file_storage_keeps_the_synchronous_200(self):
        with patch("api.v1.catalog.tasks.process_master_product_import_upload_task.delay") as mock_delay:
            response = self._post([["Мука", ""]])
        self.assertEqual(response.status_code, 200, response.content)
        mock_delay.assert_not_called()

    def test_queue_unavailable_returns_503_and_marks_upload_failed(self):
        with patch(
            "api.v1.catalog.tasks.process_master_product_import_upload_task.delay",
            side_effect=Exception("broker down"),
        ):
            response = self._post([["Мука", ""]])
        self.assertEqual(response.status_code, 503, response.content)
        upload = MasterProductImportUpload.objects.get()
        self.assertEqual(upload.status, MasterProductImportUploadStatus.FAILED)
        self.assertTrue(upload.error_message)


@override_settings(
    IMPORT_FILE_STORAGE_ENABLED=True,
    VENUE_IMPORT_ASYNC_ENABLED=True,
    CATALOG_IMPORT_ALWAYS_ASYNC=True,
    VENUE_IMPORT_BATCH_SIZE=2,
)
class VenueImportAlwaysAsyncTests(TemporaryMediaRootMixin, APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="venue-always@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Cafe Always")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.url = f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/file/preview"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        patcher = patch("api.v1.catalog.tasks.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, names):
        upload = _xlsx_upload([["Название"], *[[name] for name in names]], name="venue.xlsx")
        return self.client.post(self.url, {"file": upload}, format="multipart")

    @patch("api.v1.catalog.tasks.process_venue_import_session_task.delay")
    def test_small_file_returns_202_and_creates_pending_session(self, mock_delay):
        response = self._post(["Мука", "Сахар"])
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        session = CatalogImportSession.objects.get(pk=body["session_id"])
        self.assertEqual((body["status"], session.processing_status, session.rows_total), ("pending", "pending", 0))
        self.assertEqual(body["status_url"], f"/api/v1/catalog/import-sessions/{session.id}")
        mock_delay.assert_called_once_with(session_id=str(session.id))

    def test_worker_makes_the_small_session_ready_and_reviewable(self):
        from api.v1.catalog.tasks import process_venue_import_session_task

        with patch("api.v1.catalog.tasks.process_venue_import_session_task.delay"):
            session_id = self._post(["Мука", "Сахар"]).json()["session_id"]
        process_venue_import_session_task.apply(kwargs={"session_id": session_id}, throw=True)

        payload = self.client.get(f"/api/v1/catalog/import-sessions/{session_id}").json()
        self.assertEqual((payload["processing_status"], payload["percent"], len(payload["items"])), ("ready", 100, 2))

    @override_settings(CATALOG_IMPORT_ALWAYS_ASYNC=False)
    @patch("api.v1.catalog.tasks.process_venue_import_session_task.delay")
    def test_flag_off_keeps_the_synchronous_200_for_a_small_file(self, mock_delay):
        response = self._post(["Мука", "Сахар"])
        self.assertEqual(response.status_code, 200, response.content)
        mock_delay.assert_not_called()

    @override_settings(VENUE_IMPORT_ASYNC_ENABLED=False)
    @patch("api.v1.catalog.tasks.process_venue_import_session_task.delay")
    def test_without_venue_async_flag_stays_synchronous(self, mock_delay):
        response = self._post(["Мука", "Сахар"])
        self.assertEqual(response.status_code, 200, response.content)
        mock_delay.assert_not_called()
