import uuid
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import (
    MasterProduct,
    SupplierProductVenuePrice,
    Tender,
    TenderAccess,
    TenderChange,
    TenderItem,
    TenderOffer,
    TenderStatus,
)
from core.models import EntityStatus, Unit, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct, SupplierUser, SupplierUserRole


class TenderServerRulesFixture(APITestCase):
    """Raw ТЗ §77 and nearby sections: rules the server enforces, not only the interface."""

    def setUp(self):
        self.owner = User.objects.create(email="rules-owner@example.com", full_name="Анна", role=UserRole.VENUE_USER)
        self.cafe_1 = Venue.objects.create(display_name="Кафе 1")
        self.cafe_2 = Venue.objects.create(display_name="Кафе 2")
        for venue in (self.cafe_1, self.cafe_2):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        unit = Unit.objects.create(code="l-rules", name="литр")
        self.cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%", unit=unit)
        self.supplier = Supplier.objects.create(legal_name="Rules LLC", display_name="Rules Co")
        self.supplier_user = User.objects.create(email="rules-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки 33% 1л", normalized_name="сливки", current_price="300.00"
        )

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def tender(self, name="Тендер", status=TenderStatus.ACTIVE, deadline=None, venues=None, published=True):
        tender = Tender.objects.create(
            created_by=self.owner, name=name, access=TenderAccess.OPEN, status=status, accepts_offers_until=deadline
        )
        item = TenderItem.objects.create(tender=tender, master_product=self.cream, published=published)
        item.venues.set(venues if venues is not None else [self.cafe_1])
        return tender, item

    def expire(self, tender, minutes=1):
        Tender.objects.filter(pk=tender.pk).update(accepts_offers_until=timezone.now() - timedelta(minutes=minutes))


class ExpiredTenderHoldsNothingAPITests(TenderServerRulesFixture):
    """§12, §7: a tender whose deadline has passed holds no master product + venue pair, even before a request
    closes it lazily."""

    def test_expired_unclosed_tender_does_not_block_publish(self):
        old, _ = self.tender("Старый")
        self.expire(old)
        draft, _ = self.tender("Новый", status=TenderStatus.DRAFT)
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 200, response.content)

    def test_tender_a_minute_before_deadline_still_blocks(self):
        Tender.objects.filter(pk=self.tender("Старый")[0].pk).update(
            accepts_offers_until=timezone.now() + timedelta(minutes=1)
        )
        draft, _ = self.tender("Новый", status=TenderStatus.DRAFT)
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 409, response.content)

    def test_expired_unclosed_tender_venue_is_free(self):
        old, _ = self.tender("Старый")
        self.expire(old)
        self.auth()
        response = self.client.post(
            "/api/v1/catalog/tenders/venue-availability",
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.cream.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        venues = {row["venue_id"]: row for row in response.json()[0]["venues"]}
        self.assertIsNone(venues[str(self.cafe_1.id)]["busy_tender_id"])

    def test_item_publication_on_expired_tender_rejected(self):
        tender, item = self.tender(published=False)
        self.expire(tender)
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}/publish")
        self.assertEqual(response.status_code, 400, response.content)
        item.refresh_from_db()
        self.assertFalse(item.published)
        tender.refresh_from_db()
        self.assertEqual(tender.status, TenderStatus.COMPLETED)
        self.assertFalse(TenderChange.objects.filter(change_type="item_published").exists())


class PublishedItemNeedsVenuesAPITests(TenderServerRulesFixture):
    """«Мастер-справочник — создание тендера» §13, §16: a published position needs a venue — adding to an active
    tender publishes at once."""

    def test_item_without_venues_not_added_to_active_tender(self):
        tender, _ = self.tender()
        cheese = MasterProduct.objects.create(owner=self.owner, canonical_name="Камамбер")
        self.auth()
        response = self.client.post(
            f"/api/v1/catalog/tenders/{tender.id}/items",
            {"items": [{"master_product_id": str(cheese.id), "venue_ids": []}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Камамбер", response.json()["detail"])
        self.assertFalse(TenderItem.objects.filter(master_product=cheese).exists())

    def test_unpublished_item_without_venues_not_published(self):
        tender, item = self.tender(venues=[], published=False)
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}/publish")
        self.assertEqual(response.status_code, 400, response.content)
        item.refresh_from_db()
        self.assertFalse(item.published)


class DeadlineInThePastAPITests(TenderServerRulesFixture):
    """§7: the deadline is a future date or none at all."""

    def create(self, deadline):
        self.auth()
        return self.client.post(
            "/api/v1/catalog/tenders",
            {
                "owner_id": str(self.owner.id),
                "name": "Срок",
                "access": "open",
                "accepts_offers_until": deadline.isoformat(),
            },
            format="json",
        )

    def test_create_a_minute_ago_rejected(self):
        response = self.create(timezone.now() - timedelta(minutes=1))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("accepts_offers_until", response.json())

    def test_create_a_minute_ahead_accepted(self):
        response = self.create(timezone.now() + timedelta(minutes=1))
        self.assertEqual(response.status_code, 201, response.content)

    def test_draft_edit_to_past_rejected(self):
        draft, _ = self.tender(status=TenderStatus.DRAFT)
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{draft.id}",
            {"accepts_offers_until": (timezone.now() - timedelta(minutes=1)).isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_publish_after_deadline_rejected(self):
        draft, _ = self.tender(status=TenderStatus.DRAFT)
        self.expire(draft)
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 400, response.content)
        draft.refresh_from_db()
        self.assertEqual(draft.status, TenderStatus.DRAFT)


class SupplierOfferRulesAPITests(TenderServerRulesFixture):
    def offer_url(self, tender, item):
        return f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{tender.id}/items/{item.id}/offer"

    def put_offer(self, tender, item, venues):
        self.auth(self.supplier_user)
        return self.client.put(
            self.offer_url(tender, item),
            {"supplier_product_id": str(self.product.id), "price": "300.00", "venue_ids": [str(v.id) for v in venues]},
            format="json",
        )

    def test_product_outside_the_companys_catalog_gets_a_readable_message(self):
        """Seen on the 03.10 demo: an offer with another company's product answered with raw DRF text."""
        tender, item = self.tender()
        other = Supplier.objects.create(legal_name="Other LLC", display_name="Other Co")
        foreign = SupplierProduct.objects.create(supplier=other, supplier_name="Чужие сливки")
        archived = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сняты с продажи", status=EntityStatus.ARCHIVED
        )
        self.auth(self.supplier_user)
        for label, product_id in (("foreign", foreign.id), ("archived", archived.id), ("missing", uuid.uuid4())):
            with self.subTest(label):
                response = self.client.put(
                    self.offer_url(tender, item),
                    {"supplier_product_id": str(product_id), "price": "300.00", "venue_ids": [str(self.cafe_1.id)]},
                    format="json",
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json(), {"supplier_product_id": ["Товар не найден в каталоге компании."]})
        self.assertFalse(TenderOffer.objects.exists())

    def test_offer_on_unpublished_item_is_404(self):
        tender, item = self.tender(published=False)
        response = self.put_offer(tender, item, [self.cafe_1])
        self.assertEqual(response.status_code, 404, response.content)
        self.assertFalse(TenderOffer.objects.exists())

    def test_withdraw_on_unpublished_item_is_404(self):
        tender, item = self.tender()
        self.assertEqual(self.put_offer(tender, item, [self.cafe_1]).status_code, 200)
        TenderItem.objects.filter(pk=item.pk).update(published=False)
        response = self.client.delete(self.offer_url(tender, item))
        self.assertEqual(response.status_code, 404, response.content)
        self.assertIsNone(TenderOffer.objects.get().withdrawn_at)

    def test_distributed_venue_stays_in_ready_to_supply(self):
        """§39: a venue the buyer already distributed to cannot be unchecked by a regular edit."""
        tender, item = self.tender(venues=[self.cafe_1, self.cafe_2])
        self.assertEqual(self.put_offer(tender, item, [self.cafe_1, self.cafe_2]).status_code, 200)
        self.auth()
        distribution = f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}/offers/{TenderOffer.objects.get().id}"
        response = self.client.post(f"{distribution}/distribution", {"venue_ids": [str(self.cafe_1.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        response = self.put_offer(tender, item, [self.cafe_2])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Кафе 1", str(response.json()["venue_ids"]))
        self.assertEqual(
            sorted(TenderOffer.objects.get().venues.values_list("display_name", flat=True)), ["Кафе 1", "Кафе 2"]
        )

        response = self.put_offer(tender, item, [self.cafe_1])
        self.assertEqual(response.status_code, 200, response.content)


class TenderWithoutPublishedItemsAPITests(TenderServerRulesFixture):
    """§75: unpublished positions are hidden from suppliers — a tender with none left published is hidden whole."""

    def setUp(self):
        super().setUp()
        self.active, self.cream_item = self.tender("Молочка")
        cheese = MasterProduct.objects.create(owner=self.owner, canonical_name="Камамбер")
        self.cheese_item = TenderItem.objects.create(tender=self.active, master_product=cheese)
        self.cheese_item.venues.set([self.cafe_1])

    def toggle(self, item, action):
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{self.active.id}/items/{item.id}/{action}")
        self.assertEqual(response.status_code, 200, response.content)

    def listed(self, status="active"):
        self.auth(self.supplier_user)
        ids = []
        for url in (f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders", "/api/v1/catalog/suppliers/mine/tenders"):
            response = self.client.get(url, {"status": status})
            self.assertEqual(response.status_code, 200, response.content)
            ids.append([row["id"] for row in response.json()["results"]])
        return ids

    def supplier_get(self, path=""):
        self.auth(self.supplier_user)
        return self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.active.id}{path}")

    def test_one_of_two_positions_unpublished_keeps_the_tender(self):
        self.toggle(self.cream_item, "unpublish")
        self.assertEqual(self.listed(), [[str(self.active.id)]] * 2)
        self.assertEqual(self.supplier_get().status_code, 200)

    def test_all_positions_unpublished_hides_the_tender(self):
        self.toggle(self.cream_item, "unpublish")
        self.toggle(self.cheese_item, "unpublish")
        self.assertEqual(self.listed(), [[], []])
        for path in ("", "/items", "/history", "/offer-autofill"):
            with self.subTest(path):
                self.assertEqual(self.supplier_get(path).status_code, 404)

    def test_completed_tender_without_published_positions_is_hidden_too(self):
        self.toggle(self.cream_item, "unpublish")
        self.toggle(self.cheese_item, "unpublish")
        self.expire(self.active)
        self.assertEqual(self.listed("completed"), [[], []])
        self.assertEqual(self.supplier_get().status_code, 404)

    def test_republished_position_brings_the_tender_and_the_offer_back(self):
        self.auth(self.supplier_user)
        response = self.client.put(
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.active.id}/items/{self.cream_item.id}/offer",
            {"supplier_product_id": str(self.product.id), "price": "300.00", "venue_ids": [str(self.cafe_1.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.toggle(self.cream_item, "unpublish")
        self.toggle(self.cheese_item, "unpublish")
        self.toggle(self.cream_item, "publish")

        self.assertEqual(self.listed(), [[str(self.active.id)]] * 2)
        items = self.supplier_get("/items").json()
        self.assertEqual([item["id"] for item in items], [str(self.cream_item.id)])
        self.assertEqual(items[0]["my_offer"]["price"], "300.00")


class DistributionPriceSourceAPITests(TenderServerRulesFixture):
    def test_price_source_names_the_tender(self):
        """§30: the individual price keeps its source as the tender, readable by people."""
        tender, item = self.tender("Осеннее меню")
        offer = TenderOffer.objects.create(
            tender_item=item, supplier=self.supplier, supplier_product=self.product, price="300.00"
        )
        offer.venues.set([self.cafe_1])
        self.auth()
        response = self.client.post(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}/offers/{offer.id}/distribution",
            {"venue_ids": [str(self.cafe_1.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        price = SupplierProductVenuePrice.objects.get(supplier_product=self.product, venue=self.cafe_1)
        tender.refresh_from_db()
        self.assertEqual(price.source_reference, f"Тендер №{tender.number}")
        self.assertEqual(price.source_tender_offer_id, offer.id)
