"""api/v1/qa_views.py — must never be reachable outside DEBUG. The seed
endpoint has no authentication of its own (by design, it's a local dev
harness), so the DEBUG gate is the only thing standing between it and
being an open door to create Supplier/core.User rows on a real deploy."""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

QA_PAGE_URL = "/qa/share-access/"
QA_SEED_URL = "/api/v1/qa/share-access/seed"


class QAShareAccessDebugGateTests(TestCase):
    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_page_404s_outside_debug(self):
        response = self.client.get(QA_PAGE_URL)
        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_seed_404s_outside_debug(self):
        response = APIClient().post(QA_SEED_URL)
        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=True, ALLOWED_HOSTS=["testserver"])
    def test_page_renders_in_debug(self):
        response = self.client.get(QA_PAGE_URL)
        self.assertEqual(response.status_code, 200)

    @override_settings(DEBUG=True, ALLOWED_HOSTS=["testserver"])
    def test_seed_creates_owner_and_manager_with_working_jwts(self):
        from suppliers.models import EntityStatus, SupplierUser, SupplierUserRole

        response = APIClient().post(QA_SEED_URL)
        self.assertEqual(response.status_code, 201)
        data = response.data

        supplier_id = data["supplier_id"]
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier_id=supplier_id,
                user_id=data["owner"]["user_id"],
                role=SupplierUserRole.OWNER,
                status=EntityStatus.ACTIVE,
            ).exists()
        )
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier_id=supplier_id,
                user_id=data["manager"]["user_id"],
                role=SupplierUserRole.MANAGER,
                status=EntityStatus.ACTIVE,
            ).exists()
        )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['owner']['core_jwt']}")
        team_response = client.get(f"/api/v1/suppliers/{supplier_id}/users")
        self.assertEqual(team_response.status_code, 200)
        self.assertEqual(len(team_response.data), 2)
