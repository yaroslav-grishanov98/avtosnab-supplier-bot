from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory, APITestCase
from rest_framework.views import APIView

from api.v1.permissions import HasServiceToken


class HealthEndpointTests(APITestCase):
    def test_health_endpoint_returns_ok(self):
        response = self.client.get("/api/v1/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_endpoint_is_reversible_by_namespace(self):
        self.assertEqual(reverse("v1:health"), "/api/v1/health")


class ServiceTokenPermissionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.permission = HasServiceToken()
        self.view = APIView()

    @override_settings(SERVICE_API_TOKEN="secret-token")
    def test_allows_bearer_service_token(self):
        request = self.factory.post(
            "/api/v1/suppliers/uploads",
            HTTP_AUTHORIZATION="Bearer secret-token",
        )

        self.assertTrue(self.permission.has_permission(request, self.view))

    @override_settings(SERVICE_API_TOKEN="secret-token")
    def test_allows_x_service_token_header(self):
        request = self.factory.post(
            "/api/v1/suppliers/uploads",
            HTTP_X_SERVICE_TOKEN="secret-token",
        )

        self.assertTrue(self.permission.has_permission(request, self.view))

    @override_settings(SERVICE_API_TOKEN="secret-token")
    def test_rejects_invalid_service_token(self):
        request = self.factory.post(
            "/api/v1/suppliers/uploads",
            HTTP_AUTHORIZATION="Bearer wrong-token",
        )

        self.assertFalse(self.permission.has_permission(request, self.view))

    @override_settings(SERVICE_API_TOKEN="")
    def test_rejects_when_service_token_is_not_configured(self):
        request = self.factory.post(
            "/api/v1/suppliers/uploads",
            HTTP_AUTHORIZATION="Bearer any-token",
        )

        self.assertFalse(self.permission.has_permission(request, self.view))
