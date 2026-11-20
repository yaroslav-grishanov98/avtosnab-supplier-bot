from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


class TenderPublishedAtAPITests(APITestCase):
    """Raw ТЗ §3.1 «дата публикации», docs/adr/0018: set when a draft is published; a supplier's list puts newly
    published tenders first (§45), whenever their draft was created."""

    def setUp(self):
        self.owner = User.objects.create(email="pa-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.venue = Venue.objects.create(display_name="Кафе")
        VenueUser.objects.create(
            venue=self.venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.supplier = Supplier.objects.create(legal_name="Pa LLC", display_name="Pa Co")
        self.member = User.objects.create(email="pa-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def draft(self, name, created_days_ago=0):
        tender = Tender.objects.create(created_by=self.owner, name=name, access=TenderAccess.OPEN)
        Tender.objects.filter(pk=tender.pk).update(created_at=timezone.now() - timedelta(days=created_days_ago))
        product = MasterProduct.objects.create(owner=self.owner, canonical_name=f"{name} товар")
        TenderItem.objects.create(tender=tender, master_product=product).venues.set([self.venue])
        return tender

    def publish(self, tender):
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{tender.id}/publish")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_draft_has_no_publication_date(self):
        tender = self.draft("Черновик")
        self.auth()
        self.assertIsNone(self.client.get(f"/api/v1/catalog/tenders/{tender.id}").json()["published_at"])

    def test_publish_sets_the_date_for_buyer_and_supplier(self):
        tender = self.draft("Молочка")
        before = timezone.now()
        published = parse_datetime(self.publish(tender)["published_at"])
        self.assertTrue(before <= published <= timezone.now())

        self.auth(self.member)
        header = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{tender.id}").json()
        self.assertEqual(parse_datetime(header["published_at"]), published)

    def test_supplier_list_newest_published_first(self):
        old_draft = self.draft("Старый черновик, опубликован сейчас", created_days_ago=10)
        fresh = self.draft("Свежий, опубликован раньше", created_days_ago=1)
        self.publish(fresh)
        Tender.objects.filter(pk=fresh.pk).update(published_at=timezone.now() - timedelta(hours=1))
        self.publish(old_draft)
        legacy = self.draft("Опубликован до истории", created_days_ago=5)
        Tender.objects.filter(pk=legacy.pk).update(status=TenderStatus.ACTIVE)

        self.auth(self.member)
        for url in (
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders",
            "/api/v1/catalog/suppliers/mine/tenders",
        ):
            with self.subTest(url=url):
                rows = self.client.get(url).json()["results"]
                self.assertEqual(
                    [row["name"] for row in rows],
                    [
                        "Старый черновик, опубликован сейчас",
                        "Свежий, опубликован раньше",
                        "Опубликован до истории",
                    ],
                )


class TenderPublishedAtMigrationTests(TransactionTestCase):
    """docs/adr/0018: tenders published before the field existed take the time of their «published» history event;
    without one the date stays unknown."""

    migrate_from = [("catalog", "0040_catalog_import_session_lifecycle")]
    migrate_to = [("catalog", "0041_tender_published_at")]

    def setUp(self):
        self.addCleanup(self._migrate_to_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldUser = old_apps.get_model("core", "User")
        OldTender = old_apps.get_model("catalog", "Tender")
        OldChange = old_apps.get_model("catalog", "TenderChange")
        owner = OldUser.objects.create(email="pa-mig@example.com", full_name="Owner", role="venue_user")
        self.published_on = timezone.now() - timedelta(days=2)
        self.with_event = OldTender.objects.create(created_by=owner, name="С событием", access="open", status="active")
        OldChange.objects.create(tender=self.with_event, change_type="published", occurred_at=self.published_on)
        self.without_event = OldTender.objects.create(
            created_by=owner, name="Без события", access="open", status="active"
        )
        self.draft = OldTender.objects.create(created_by=owner, name="Черновик", access="open", status="draft")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_backfilled_from_history(self):
        NewTender = self.apps.get_model("catalog", "Tender")
        self.assertEqual(NewTender.objects.get(pk=self.with_event.pk).published_at, self.published_on)
        self.assertIsNone(NewTender.objects.get(pk=self.without_event.pk).published_at)
        self.assertIsNone(NewTender.objects.get(pk=self.draft.pk).published_at)

    @staticmethod
    def _migrate_to_latest():
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
