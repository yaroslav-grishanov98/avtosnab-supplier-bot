import logging

from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class NotificationOpenApiSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        spectacular_logger = logging.getLogger("drf_spectacular")
        previous_level = spectacular_logger.level
        spectacular_logger.setLevel(logging.ERROR)
        try:
            cls.schema = SchemaGenerator().get_schema(public=True)
        finally:
            spectacular_logger.setLevel(previous_level)

    def operation(self, path, method):
        return self.schema["paths"][path][method]

    def test_mutating_notification_operations_document_request_bodies(self):
        operations = (
            ("/api/v1/notifications/destinations", "post"),
            ("/api/v1/notifications/destinations/{destination_id}", "patch"),
            ("/api/v1/notifications/destinations/{destination_id}/rules", "put"),
            ("/api/v1/notifications/link-tokens", "post"),
            ("/api/v1/notifications/bot/connect", "post"),
            ("/api/v1/notifications/bot/test", "post"),
            ("/api/v1/notifications/bot/supplier-requests/{request_id}/respond", "post"),
            ("/api/v1/notifications/miniapp/supplier-request", "post"),
        )

        for path, method in operations:
            with self.subTest(path=path, method=method):
                self.assertIn("application/json", self.operation(path, method)["requestBody"]["content"])

    def test_destination_verification_has_no_request_body(self):
        """Описать POST проверки подключённых чатов без фиктивного тела запроса."""
        operation = self.operation("/api/v1/notifications/destinations/verify", "post")

        self.assertNotIn("requestBody", operation)

    def test_notification_success_and_error_statuses_match_views(self):
        expected_statuses = {
            ("/api/v1/notifications/destinations", "get"): {"200", "401"},
            ("/api/v1/notifications/destinations", "post"): {"201", "400", "401", "403"},
            ("/api/v1/notifications/destinations/{destination_id}", "get"): {"200", "401", "404"},
            ("/api/v1/notifications/destinations/{destination_id}", "patch"): {"200", "400", "401", "404"},
            ("/api/v1/notifications/destinations/{destination_id}", "delete"): {"204", "401", "404"},
            ("/api/v1/notifications/destinations/{destination_id}/rules", "get"): {"200", "401", "404"},
            ("/api/v1/notifications/destinations/{destination_id}/rules", "put"): {"200", "400", "401", "404"},
            ("/api/v1/notifications/link-tokens", "post"): {"201", "400", "401", "403"},
            ("/api/v1/notifications/bot/connect", "post"): {"200", "201", "400", "403"},
            ("/api/v1/notifications/bot/status", "get"): {"200", "400", "403"},
            ("/api/v1/notifications/bot/test", "post"): {"200", "400", "403", "404", "409"},
            ("/api/v1/notifications/bot/supplier-requests/{request_id}", "get"): {"200", "400", "403", "404"},
            ("/api/v1/notifications/bot/supplier-requests/{request_id}/respond", "post"): {
                "200",
                "400",
                "403",
                "404",
                "409",
            },
            ("/api/v1/notifications/miniapp/supplier-request", "get"): {"200", "403", "409"},
            ("/api/v1/notifications/miniapp/supplier-request", "post"): {"200", "400", "403", "409"},
        }

        for (path, method), statuses in expected_statuses.items():
            with self.subTest(path=path, method=method):
                self.assertEqual(set(self.operation(path, method)["responses"]), statuses)

    def test_user_and_bot_operations_show_their_correct_bearer_schemes(self):
        user_operation = self.operation("/api/v1/notifications/link-tokens", "post")
        bot_operation = self.operation("/api/v1/notifications/bot/connect", "post")
        bot_request_operation = self.operation("/api/v1/notifications/bot/supplier-requests/{request_id}", "get")

        self.assertEqual(user_operation["security"], [{"CoreUserJWT": []}])
        self.assertEqual(bot_operation["security"], [{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}])
        self.assertEqual(bot_request_operation["security"], [{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}])
        self.assertIn("CoreUserJWT", self.schema["components"]["securitySchemes"])
        self.assertIn("ServiceApiToken", self.schema["components"]["securitySchemes"])
        self.assertIn("ServiceApiTokenHeader", self.schema["components"]["securitySchemes"])
