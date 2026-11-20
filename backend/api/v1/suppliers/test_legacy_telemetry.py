import logging
import uuid
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from api.v1.suppliers.telemetry import classify_consumer, legacy_path_template

UPLOAD_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


class ClassifyConsumerTests(SimpleTestCase):
    def test_bot_is_service_token_with_aiohttp(self):
        self.assertEqual(classify_consumer(has_service_token=True, user_agent="Python/3.12 aiohttp/3.9.5"), "bot")

    def test_n8n_is_service_token_with_axios_or_n8n_agent(self):
        self.assertEqual(classify_consumer(has_service_token=True, user_agent="axios/1.6.0"), "n8n")
        self.assertEqual(classify_consumer(has_service_token=True, user_agent="n8n"), "n8n")

    def test_other_service_token_client(self):
        self.assertEqual(classify_consumer(has_service_token=True, user_agent="curl/8.0"), "service-other")

    def test_browser_without_service_token(self):
        self.assertEqual(
            classify_consumer(has_service_token=False, user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/120"),
            "browser",
        )

    def test_anything_else(self):
        self.assertEqual(classify_consumer(has_service_token=False, user_agent="curl/8.0"), "other")
        self.assertEqual(classify_consumer(has_service_token=False, user_agent=""), "other")


class LegacyPathTemplateTests(SimpleTestCase):
    def test_upload_routes_get_id_placeholders(self):
        self.assertEqual(legacy_path_template("/api/v1/suppliers/uploads"), "/api/v1/suppliers/uploads")
        self.assertEqual(legacy_path_template("/api/v1/suppliers/uploads/text"), "/api/v1/suppliers/uploads/text")
        self.assertEqual(
            legacy_path_template(f"/api/v1/suppliers/uploads/{UPLOAD_ID}/normalize"),
            "/api/v1/suppliers/uploads/{id}/normalize",
        )
        self.assertEqual(
            legacy_path_template(f"/api/v1/suppliers/uploads/{UPLOAD_ID}/items/{uuid.uuid4()}"),
            "/api/v1/suppliers/uploads/{id}/items/{id}",
        )

    def test_other_paths_are_not_legacy(self):
        self.assertIsNone(legacy_path_template("/api/v1/suppliers/products/import/analyze"))
        self.assertIsNone(legacy_path_template("/api/v1/suppliers/uploads-other"))
        self.assertIsNone(legacy_path_template("/api/v1/catalog/products"))


@override_settings(SERVICE_API_TOKEN="test-token", LEGACY_UPLOADS_TELEMETRY_ENABLED=True)
class LegacyUploadsTelemetryMiddlewareTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _call(self, path, *, token=True, agent="Python/3.12 aiohttp/3.9.5"):
        headers = {"HTTP_USER_AGENT": agent}
        if token:
            headers["HTTP_AUTHORIZATION"] = "Bearer test-token"
        return self.client.get(path, **headers)

    def test_legacy_call_is_logged_with_consumer_and_status(self):
        with self.assertLogs("autosnab.legacy_uploads", level="INFO") as logs:
            response = self._call(f"/api/v1/suppliers/uploads/{UPLOAD_ID}")
        line = next(entry for entry in logs.output if "legacy_uploads.call" in entry)
        self.assertIn("method=GET", line)
        self.assertIn("path=/api/v1/suppliers/uploads/{id}", line)
        self.assertIn(f"status={response.status_code}", line)
        self.assertIn("consumer=bot", line)

    def test_token_value_and_ids_are_never_logged(self):
        with self.assertLogs("autosnab.legacy_uploads", level="INFO") as logs:
            self._call(f"/api/v1/suppliers/uploads/{UPLOAD_ID}")
        self.assertNotIn("test-token", "\n".join(logs.output))
        self.assertNotIn(UPLOAD_ID, "\n".join(logs.output))

    def test_user_agent_is_truncated(self):
        with self.assertLogs("autosnab.legacy_uploads", level="INFO") as logs:
            self._call(f"/api/v1/suppliers/uploads/{UPLOAD_ID}", agent="A" * 500)
        line = next(entry for entry in logs.output if "legacy_uploads.call" in entry)
        self.assertNotIn("A" * 81, line)

    def test_non_legacy_path_is_not_logged(self):
        with self.assertNoLogs("autosnab.legacy_uploads", level="INFO"):
            self._call("/api/v1/suppliers/products/import/analyze")

    @override_settings(LEGACY_UPLOADS_TELEMETRY_ENABLED=False)
    def test_flag_off_logs_nothing(self):
        with self.assertNoLogs("autosnab.legacy_uploads", level="INFO"):
            self._call(f"/api/v1/suppliers/uploads/{UPLOAD_ID}")

    def test_logging_failure_does_not_break_the_response(self):
        with patch("api.v1.suppliers.telemetry.logger.info", side_effect=RuntimeError("log sink down")):
            response = self._call(f"/api/v1/suppliers/uploads/{UPLOAD_ID}")
        self.assertIn(response.status_code, (200, 401, 403, 404))

    def test_telemetry_logger_emits_info_with_project_logging_config(self):
        self.assertTrue(logging.getLogger("autosnab.legacy_uploads").isEnabledFor(logging.INFO))
