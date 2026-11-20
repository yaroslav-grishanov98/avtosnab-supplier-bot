from rest_framework.test import APITestCase

from api.v1.catalog.services import TENDER_DISTRIBUTION_EVENT
from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderOffer, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from notifications.models import (
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
    NotificationDestination,
    NotificationDestinationType,
    NotificationRule,
)
from suppliers.models import Supplier, SupplierProduct


class TenderDistributionNoticeAPITests(APITestCase):
    """Raw ТЗ §32-34, docs/adr/0013: after a distribution is applied, the chosen supplier's notification chats
    get «Ваш товар выбран для заведения…» — only for venues whose product or price actually changed; nothing on
    preview; nobody is told they were replaced (§34)."""

    def setUp(self):
        self.owner = User.objects.create(email="dn-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.cafe_1 = Venue.objects.create(display_name="Кафе 1-полное", short_name="Кафе 1")
        self.cafe_2 = Venue.objects.create(display_name="Кафе 2")
        for venue in (self.cafe_1, self.cafe_2):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 35%")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.cream)
        self.item.venues.set([self.cafe_1, self.cafe_2])
        self.offer = self.make_offer("Parmalat LLC", "Сливки Parmalat 35%", "320.00")
        self.rival_offer = self.make_offer("Petmol LLC", "Сливки Петмол 33%", "300.00")

    def make_offer(self, supplier_name, product_name, price):
        supplier = Supplier.objects.create(legal_name=supplier_name, display_name=supplier_name)
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name=product_name, normalized_name=product_name.lower()
        )
        offer = TenderOffer.objects.create(
            tender_item=self.item, supplier=supplier, supplier_product=product, price=price
        )
        offer.venues.set([self.cafe_1, self.cafe_2])
        chat = NotificationDestination.objects.create(
            created_by=self.owner,
            audience=NotificationAudience.SUPPLIER,
            supplier=supplier,
            channel=NotificationChannel.TELEGRAM,
            destination_type=NotificationDestinationType.PERSONAL,
            chat_id=f"chat-{supplier_name}",
        )
        NotificationRule.objects.create(destination=chat, event=TENDER_DISTRIBUTION_EVENT)
        return offer

    def distribute(self, offer, venues, preview=False):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers/{offer.id}/distribution"
        venue_ids = [str(venue.id) for venue in venues]
        if preview:
            response = self.client.get(url, {"venue_id": venue_ids})
        else:
            response = self.client.post(url, {"venue_ids": venue_ids}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

    def notices(self, offer):
        return NotificationDelivery.objects.filter(
            destination__supplier=offer.supplier, event=TENDER_DISTRIBUTION_EVENT
        ).order_by("created_at")

    def test_chosen_supplier_notified_with_tender_product_price_and_venues(self):
        self.distribute(self.offer, [self.cafe_1, self.cafe_2])
        notice = self.notices(self.offer).get()
        self.assertEqual(
            notice.payload,
            {
                "tender_id": str(self.tender.id),
                "tender_number": self.tender.number,
                "tender_name": "Молочка",
                "product_name": "Сливки Parmalat 35%",
                "price": "320.00",
                "currency": "RUB",
                "venues": ["Кафе 1", "Кафе 2"],
            },
        )

    def test_preview_sends_nothing(self):
        self.distribute(self.offer, [self.cafe_1], preview=True)
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_repeated_apply_without_changes_sends_nothing_new(self):
        self.distribute(self.offer, [self.cafe_1])
        self.distribute(self.offer, [self.cafe_1])
        self.assertEqual(self.notices(self.offer).count(), 1)

    def test_only_newly_assigned_venues_are_listed(self):
        self.distribute(self.offer, [self.cafe_1])
        self.distribute(self.offer, [self.cafe_1, self.cafe_2])
        self.assertEqual([notice.payload["venues"] for notice in self.notices(self.offer)], [["Кафе 1"], ["Кафе 2"]])

    def test_replaced_supplier_is_not_told(self):
        self.distribute(self.rival_offer, [self.cafe_1])
        self.distribute(self.offer, [self.cafe_1])
        self.assertEqual(self.notices(self.rival_offer).count(), 1)
        self.assertEqual(self.notices(self.offer).count(), 1)

    def test_without_enabled_event_nothing_is_queued(self):
        NotificationRule.objects.filter(destination__supplier=self.offer.supplier).delete()
        self.distribute(self.offer, [self.cafe_1])
        self.assertFalse(self.notices(self.offer).exists())

    def test_event_is_known_to_notifications(self):
        """docs/adr/0013: the event value is shared with the notifications module."""
        from notifications.services import SUPPLIER_EVENTS

        self.assertIn(TENDER_DISTRIBUTION_EVENT, SUPPLIER_EVENTS)
