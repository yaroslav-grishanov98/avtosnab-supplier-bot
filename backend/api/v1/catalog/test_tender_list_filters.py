from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, ProductCategory, Tender, TenderAccess, TenderItem, TenderOffer, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct, SupplierUser, SupplierUserRole


class TenderListFixture(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="lf-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Кафе")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.member = User.objects.create(email="lf-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        self.supplier = Supplier.objects.create(legal_name="Lf LLC", display_name="Lf Co")
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.products = 0

    def tender(self, name, items=1, number=None):
        tender = Tender.objects.create(
            created_by=self.owner, name=name, access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        if number is not None:
            Tender.objects.filter(pk=tender.pk).update(number=number)
            tender.refresh_from_db()
        for index in range(items):
            product = MasterProduct.objects.create(owner=self.owner, canonical_name=f"{name} {index}")
            TenderItem.objects.create(tender=tender, master_product=product).venues.set([self.venue])
        return tender

    def answer(self, tender, positions):
        for item in tender.items.order_by("master_product__canonical_name")[:positions]:
            self.products += 1
            product = SupplierProduct.objects.create(supplier=self.supplier, supplier_name=f"Товар {self.products}")
            TenderOffer.objects.create(tender_item=item, supplier=self.supplier, supplier_product=product, price="1.00")

    def names(self, url, user, **params):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return sorted(row["name"] for row in response.json()["results"])


class TenderNumberSearchAPITests(TenderListFixture):
    """Board 01.10: a venue tells the supplier the tender number, the supplier finds it by search."""

    def setUp(self):
        super().setUp()
        self.t22 = self.tender("Молочка", number=900022)
        self.t220 = self.tender("Рыба", number=900220)
        self.named = self.tender("Партия 900022", number=900023)

    def test_supplier_lists_find_number(self):
        for url in (
            "/api/v1/catalog/suppliers/mine/tenders",
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.names(url, self.member, q="№900022"), ["Молочка"])
                self.assertEqual(self.names(url, self.member, q="900022"), ["Молочка", "Партия 900022"])
                self.assertEqual(self.names(url, self.member, q="№ 900220"), ["Рыба"])

    def test_buyer_list_finds_number_and_name(self):
        url = "/api/v1/catalog/tenders"
        owner = {"owner_id": str(self.owner.id)}
        self.assertEqual(self.names(url, self.owner, q="900022", **owner), ["Молочка", "Партия 900022"])
        self.assertEqual(self.names(url, self.owner, q="РЫБ", **owner), ["Рыба"])
        self.assertEqual(self.names(url, self.owner, q="нет такого", **owner), [])
        self.assertEqual(len(self.names(url, self.owner, **owner)), 3)


class SupplierMyWorkFilterAPITests(TenderListFixture):
    """Board «Моя работа»: not started / in progress / everything answered, filtered on the server so pages and
    count follow."""

    def setUp(self):
        super().setUp()
        self.answer(self.tender("Не начинал", items=2), 0)
        self.answer(self.tender("В работе", items=2), 1)
        self.answer(self.tender("Всё", items=2), 2)
        self.tender("Пустой", items=0)

    def test_each_state(self):
        url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders"
        self.assertEqual(self.names(url, self.member, my_work="not_started"), ["Не начинал"])
        self.assertEqual(self.names(url, self.member, my_work="in_progress"), ["В работе"])
        self.assertEqual(self.names(url, self.member, my_work="done"), ["Всё"])
        self.assertEqual(self.names(url, self.member, my_work="participating"), ["В работе", "Всё"])
        self.assertEqual(len(self.names(url, self.member)), 3)

    def test_mine_list_counts_filtered(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.member))
        response = self.client.get("/api/v1/catalog/suppliers/mine/tenders", {"my_work": "participating"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 2)

    def test_unknown_state_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.member))
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders", {"my_work": "maybe"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("my_work", response.json())


class TenderProductKindsAPITests(TenderListFixture):
    """Call 01.10: «Вид товара» of a tender position is chosen from the full product kind reference."""

    url = "/api/v1/catalog/tenders/product-kinds"

    def setUp(self):
        super().setUp()
        dairy = ProductCategory.objects.create(name="Молочная продукция", slug="t-dairy")
        cream = ProductCategory.objects.create(name="Сливки и сметана", slug="t-cream", parent=dairy)
        self.kind = ProductCategory.objects.create(name="Сливки", slug="t-cream-kind", parent=cream)
        ProductCategory.objects.create(name="Старый вид", slug="t-old-kind", parent=cream, status=EntityStatus.ARCHIVED)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_kinds_with_their_path_for_a_buyer(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        rows = [row for row in response.json() if row["group_name"] == "Молочная продукция"]
        self.assertEqual(
            rows,
            [
                {
                    "id": str(self.kind.id),
                    "name": "Сливки",
                    "subgroup_id": str(self.kind.parent_id),
                    "subgroup_name": "Сливки и сметана",
                    "group_id": str(self.kind.parent.parent_id),
                    "group_name": "Молочная продукция",
                }
            ],
        )
