from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from core.models import EntityStatus, User, UserRole
from suppliers.models import Supplier


class TenderSupplierSearchAPITests(APITestCase):
    """Raw ТЗ §8, docs/adr/0014: a buyer invites any active supplier by name or INN — also one without a price
    list, unlike the catalog filter."""

    url = "/api/v1/catalog/tenders/supplier-search"

    def setUp(self):
        self.buyer = User.objects.create(email="ss-buyer@example.com", full_name="Buyer", role=UserRole.VENUE_USER)
        self.no_prices = Supplier.objects.create(
            legal_name="ООО «Молоко»", display_name="Молочный двор", inn="7701000001"
        )
        self.other = Supplier.objects.create(legal_name="ООО «Хлеб»", display_name="Булочная", inn="7702000002")
        Supplier.objects.create(
            legal_name="ООО «Архив»", display_name="Архивный", inn="7703000003", status=EntityStatus.ARCHIVED
        )
        Supplier.objects.create(
            legal_name="ООО «Скрытый»", display_name="Скрытый", inn="7704000004", is_published=False
        )

    def search(self, **params):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.buyer))
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["name"] for row in response.json()]

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_active_published_suppliers_without_prices_by_name(self):
        self.assertEqual(self.search(), ["Булочная", "Молочный двор"])

    def test_row_shape_matches_catalog_supplier(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.buyer))
        row = self.client.get(self.url, {"q": "молочный"}).json()[0]
        self.assertEqual(
            row,
            {"id": str(self.no_prices.id), "name": "Молочный двор", "legal_name": "ООО «Молоко»", "inn": "7701000001"},
        )

    def test_name_matches_in_any_case(self):
        self.assertEqual(self.search(q="МОЛОЧНЫЙ"), ["Молочный двор"])

    def test_legal_name_matches(self):
        self.assertEqual(self.search(q="хлеб"), ["Булочная"])

    def test_inn_matches(self):
        self.assertEqual(self.search(q="770200"), ["Булочная"])

    def test_nothing_found(self):
        self.assertEqual(self.search(q="рыба"), [])
