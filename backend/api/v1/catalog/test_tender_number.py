from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


class TenderNumberAPITests(APITestCase):
    """Product owner 2026-09-28, docs/adr/0012: a platform-wide tender number — «уже участвует в Тендере №15»."""

    def setUp(self):
        self.owner = User.objects.create(email="num-owner@example.com", full_name="Анна", role=UserRole.VENUE_USER)
        self.cafe = Venue.objects.create(display_name="Кафе 2")
        VenueUser.objects.create(venue=self.cafe, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%")

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def create(self, name):
        self.auth()
        response = self.client.post(
            "/api/v1/catalog/tenders",
            {"owner_id": str(self.owner.id), "name": name, "access": TenderAccess.OPEN},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def holding_tender(self):
        tender = Tender.objects.create(
            created_by=self.owner, name="Осеннее меню", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        TenderItem.objects.create(tender=tender, master_product=self.cream).venues.set([self.cafe])
        return tender

    def test_numbers_follow_each_other(self):
        first = self.create("Первый")
        second = self.create("Второй")
        self.assertEqual(second["number"], first["number"] + 1)
        self.assertEqual(Tender.objects.get(pk=first["id"]).number, first["number"])

    def test_card_has_number(self):
        tender = self.holding_tender()
        self.auth()
        card = self.client.get(f"/api/v1/catalog/tenders/{tender.id}").json()
        self.assertEqual(card["number"], tender.number)

    def test_conflict_names_the_number(self):
        holder = self.holding_tender()
        draft = Tender.objects.create(created_by=self.owner, name="Дубль", access=TenderAccess.OPEN)
        TenderItem.objects.create(tender=draft, master_product=self.cream).venues.set([self.cafe])
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 409, response.content)
        data = response.json()
        self.assertEqual(data["conflicts"][0]["tender_number"], holder.number)
        self.assertIn(f"Тендере №{holder.number} «Осеннее меню»", data["detail"])

    def test_venue_availability_names_the_number(self):
        holder = self.holding_tender()
        self.auth()
        response = self.client.post(
            "/api/v1/catalog/tenders/venue-availability",
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.cream.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        venue = response.json()[0]["venues"][0]
        self.assertEqual((venue["busy_tender_id"], venue["busy_tender_number"]), (str(holder.id), holder.number))

    def test_free_venue_has_no_number(self):
        self.auth()
        response = self.client.post(
            "/api/v1/catalog/tenders/venue-availability",
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.cream.id)]},
            format="json",
        )
        self.assertIsNone(response.json()[0]["venues"][0]["busy_tender_number"])

    def test_supplier_header_has_number(self):
        tender = self.holding_tender()
        supplier = Supplier.objects.create(legal_name="Num LLC", display_name="Num Co")
        member = User.objects.create(email="num-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=supplier, user=member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.auth(member)
        response = self.client.get(f"/api/v1/catalog/suppliers/{supplier.id}/tenders/{tender.id}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["number"], tender.number)


class TenderNumberMigrationTests(TransactionTestCase):
    """docs/adr/0012: tenders created before the number existed are numbered by creation date."""

    migrate_from = [("catalog", "0034_masterproductimportupload")]
    migrate_to = [("catalog", "0035_tender_number")]

    def setUp(self):
        # TASK-122 п.1 final-review finding: this test deliberately reverses
        # the catalog app to an old migration state and only forwards it to
        # migrate_to (0035) — never back to HEAD. TransactionTestCase's
        # teardown truncates data but never reverses schema, so without this
        # cleanup the *real* test database schema was left at 0035 for every
        # test that runs afterwards in the same process (alphabetically,
        # that's most of api/v1/catalog/tests.py) — invisible for migrations
        # 0036/0037+ that don't touch a column any later test happens to
        # query, but a guaranteed "column does not exist" for one that does
        # (first surfaced by 0037_venueproductmatrixitem_unit). Restore to
        # the latest migration graph state so this test is self-contained.
        self.addCleanup(self._migrate_to_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldUser = old_apps.get_model("core", "User")
        OldTender = old_apps.get_model("catalog", "Tender")
        owner = OldUser.objects.create(email="mig-owner@example.com", full_name="Owner", role="venue_user")
        now = timezone.now()
        self.later = OldTender.objects.create(created_by=owner, name="Позже", access="open")
        self.earlier = OldTender.objects.create(created_by=owner, name="Раньше", access="open")
        OldTender.objects.filter(pk=self.earlier.pk).update(created_at=now - timedelta(days=2))
        OldTender.objects.filter(pk=self.later.pk).update(created_at=now - timedelta(days=1))

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_existing_tenders_numbered_by_creation_date_and_sequence_continues(self):
        NewTender = self.apps.get_model("catalog", "Tender")
        earlier = NewTender.objects.get(pk=self.earlier.pk)
        later = NewTender.objects.get(pk=self.later.pk)
        self.assertEqual((earlier.number, later.number), (1, 2))
        fresh = NewTender.objects.create(created_by_id=earlier.created_by_id, name="Новый", access="open")
        fresh.refresh_from_db()
        self.assertEqual(fresh.number, 3)

    @staticmethod
    def _migrate_to_latest():
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
