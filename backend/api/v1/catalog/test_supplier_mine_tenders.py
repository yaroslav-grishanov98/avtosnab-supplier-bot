from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderOffer, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct, SupplierUser, SupplierUserRole


class SupplierMineTendersAPITests(APITestCase):
    """Raw ТЗ §44-51 for a user of several supplier companies: one list across all of them, without picking a
    company first — each row tells which of his companies see the tender and how many positions each answered."""

    url = "/api/v1/catalog/suppliers/mine/tenders"

    def setUp(self):
        self.owner = User.objects.create(email="mt-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Кафе")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.user = User.objects.create(email="mt-sup@example.com", full_name="Сергей", role=UserRole.SUPPLIER)
        self.company_a = self.company("Альфа")
        self.company_b = self.company("Бета")
        self.stranger_company = Supplier.objects.create(legal_name="Гамма LLC", display_name="Гамма")
        self.age = 10

    def company(self, name, status=EntityStatus.ACTIVE):
        supplier = Supplier.objects.create(legal_name=f"{name} LLC", display_name=name)
        SupplierUser.objects.create(supplier=supplier, user=self.user, role=SupplierUserRole.MANAGER, status=status)
        return supplier

    def tender(self, name, access=TenderAccess.OPEN, invited=(), status=TenderStatus.ACTIVE, items=1):
        tender = Tender.objects.create(created_by=self.owner, name=name, access=access, status=status)
        Tender.objects.filter(pk=tender.pk).update(created_at=timezone.now() - timedelta(days=self.age))
        self.age -= 1
        tender.invited_suppliers.set(invited)
        for index in range(items):
            product = MasterProduct.objects.create(owner=self.owner, canonical_name=f"{name} товар {index}")
            TenderItem.objects.create(tender=tender, master_product=product).venues.set([self.venue])
        return tender

    def offer(self, tender, company, index=0, withdrawn=False):
        item = tender.items.order_by("master_product__canonical_name")[index]
        product = SupplierProduct.objects.create(
            supplier=company, supplier_name=f"{company.display_name} {tender.name} {index}"
        )
        TenderOffer.objects.create(
            tender_item=item,
            supplier=company,
            supplier_product=product,
            price="100.00",
            withdrawn_at=timezone.now() if withdrawn else None,
        )

    def rows(self, **params):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_open_and_invited_tenders_of_all_companies_once_newest_first(self):
        self.tender("Открытый")
        self.tender("Только А", TenderAccess.INVITE_ONLY, [self.company_a])
        self.tender("Обе", TenderAccess.INVITE_ONLY, [self.company_a, self.company_b])
        self.tender("Чужой", TenderAccess.INVITE_ONLY, [self.stranger_company])
        self.tender("Черновик", status=TenderStatus.DRAFT)

        rows = self.rows()
        self.assertEqual([row["name"] for row in rows], ["Обе", "Только А", "Открытый"])
        companies = {row["name"]: [company["name"] for company in row["companies"]] for row in rows}
        self.assertEqual(companies, {"Обе": ["Альфа", "Бета"], "Только А": ["Альфа"], "Открытый": ["Альфа", "Бета"]})

    def test_offers_counted_per_company_and_across_companies(self):
        tender = self.tender("Открытый", items=3)
        self.offer(tender, self.company_a, index=0)
        self.offer(tender, self.company_b, index=0)
        self.offer(tender, self.company_b, index=1)
        self.offer(tender, self.company_a, index=2, withdrawn=True)

        row = self.rows()[0]
        self.assertEqual(row["my_offers_count"], 2)
        self.assertEqual(row["published_items_count"], 3)
        self.assertEqual(
            [(company["id"], company["my_offers_count"]) for company in row["companies"]],
            [(str(self.company_a.id), 1), (str(self.company_b.id), 2)],
        )

    def test_inactive_membership_and_archived_company_are_left_out(self):
        self.company("Уволен", status=EntityStatus.ARCHIVED)
        archived = self.company("Архивная")
        Supplier.objects.filter(pk=archived.pk).update(status=EntityStatus.ARCHIVED)
        self.tender("Только им", TenderAccess.INVITE_ONLY, [archived])
        open_tender = self.tender("Открытый")

        rows = self.rows()
        self.assertEqual([row["id"] for row in rows], [str(open_tender.id)])
        self.assertEqual([company["name"] for company in rows[0]["companies"]], ["Альфа", "Бета"])

    def test_user_without_companies_gets_empty_list(self):
        self.tender("Открытый")
        nobody = User.objects.create(email="mt-nobody@example.com", full_name="Никто", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(nobody))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 0)

    def test_completed_tab_and_search(self):
        self.tender("Молочка")
        self.tender("Рыба завершена", status=TenderStatus.COMPLETED)
        self.assertEqual([row["name"] for row in self.rows(status="completed")], ["Рыба завершена"])
        self.assertEqual([row["name"] for row in self.rows(q="МОЛОЧ")], ["Молочка"])

    def test_single_company_list_unchanged(self):
        self.tender("Только Б", TenderAccess.INVITE_ONLY, [self.company_b])
        self.tender("Открытый")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.company_a.id}/tenders")
        self.assertEqual([row["name"] for row in response.json()["results"]], ["Открытый"])

    def test_queries_do_not_grow_with_tenders(self):
        def count_queries():
            self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(self.url).status_code, 200)
            return len(queries)

        tender = self.tender("Первый", TenderAccess.INVITE_ONLY, [self.company_a])
        self.offer(tender, self.company_a)
        one = count_queries()
        for index in range(3):
            more = self.tender(f"Ещё {index}", TenderAccess.INVITE_ONLY, [self.company_a, self.company_b], items=2)
            self.offer(more, self.company_b, index=1)
        self.assertEqual(count_queries(), one)
