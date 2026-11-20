from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


class TenderListOrderAPITests(APITestCase):
    """Raw ТЗ §45 «Всегда: новые тендеры сверху». Both lists aggregate counters (GROUP BY), where Django drops
    Meta.ordering — the order has to be explicit."""

    def setUp(self):
        self.owner = User.objects.create(email="lo-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        venue = Venue.objects.create(display_name="Кафе")
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.supplier = Supplier.objects.create(legal_name="Lo LLC", display_name="Lo Co")
        self.member = User.objects.create(email="lo-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        now = timezone.now()
        self.names = []
        for name, age_days in (("Старый", 3), ("Новый", 1), ("Средний", 2)):
            tender = Tender.objects.create(
                created_by=self.owner, name=name, access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
            )
            Tender.objects.filter(pk=tender.pk).update(created_at=now - timedelta(days=age_days))
            product = MasterProduct.objects.create(owner=self.owner, canonical_name=f"Товар {name}")
            TenderItem.objects.create(tender=tender, master_product=product).venues.set([venue])

    def test_buyer_list_newest_first(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get("/api/v1/catalog/tenders", {"owner_id": str(self.owner.id)})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["name"] for row in response.json()["results"]], ["Новый", "Средний", "Старый"])

    def test_supplier_list_newest_first(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.member))
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["name"] for row in response.json()["results"]], ["Новый", "Средний", "Старый"])
