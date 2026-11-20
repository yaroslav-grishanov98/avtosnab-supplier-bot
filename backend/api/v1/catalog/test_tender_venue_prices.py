from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import (
    MasterProduct,
    SupplierProductVenuePrice,
    Tender,
    TenderAccess,
    TenderItem,
    TenderStatus,
    VenueProductMatrixItem,
)
from core.models import EntityStatus, Unit, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct, SupplierUser, SupplierUserRole


class TenderVenuePricesFixture(APITestCase):
    """Raw ТЗ §58-60: the supplier's current price per venue of a position — individual when an active one
    covers the venue, base otherwise — behind «Есть разные цены по заведениям»."""

    def setUp(self):
        self.owner = User.objects.create(email="vp-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.cafe_1 = Venue.objects.create(display_name="Кафе 1")
        self.cafe_2 = Venue.objects.create(display_name="Кафе 2")
        self.cafe_3 = Venue.objects.create(display_name="Кафе 3")
        for venue in (self.cafe_1, self.cafe_2, self.cafe_3):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        unit = Unit.objects.create(code="l-vp", name="литр")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%", unit=unit)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.master_product)
        self.item.venues.set([self.cafe_3, self.cafe_1, self.cafe_2])

        self.supplier = Supplier.objects.create(legal_name="Milk LLC", display_name="Milk Co")
        self.supplier_user = User.objects.create(email="vp-milk@example.com", full_name="Milk", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.supplier_user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки 33% 1л", normalized_name="сливки 33%", current_price="330.00"
        )
        self.individual(self.product, self.cafe_1, "305.00")
        self.individual(self.product, self.cafe_2, "315.00")
        self.prefix = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}"

    def individual(self, product, venue, price, **kwargs):
        return SupplierProductVenuePrice.objects.create(supplier_product=product, venue=venue, price=price, **kwargs)

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def venue_price(self, venue, price, source):
        return {
            "venue_id": str(venue.id),
            "venue_name": venue.display_name,
            "price": price,
            "currency": "RUB" if price else None,
            "price_source": source,
        }


class TenderOfferAutofillVenuePricesAPITests(TenderVenuePricesFixture):
    def autofill_row(self):
        self.auth(self.supplier_user)
        response = self.client.get(f"{self.prefix}/offer-autofill")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()[0]

    def link_product(self):
        VenueProductMatrixItem.objects.create(
            venue=self.cafe_1,
            requested_name="Сливки",
            master_product=self.master_product,
            supplier_product=self.product,
        )

    def test_price_per_venue_behind_the_minimum(self):
        self.link_product()
        row = self.autofill_row()
        self.assertEqual(row["price"], "305.00")
        self.assertEqual(
            row["venue_prices"],
            [
                self.venue_price(self.cafe_1, "305.00", "individual"),
                self.venue_price(self.cafe_2, "315.00", "individual"),
                self.venue_price(self.cafe_3, "330.00", "base"),
            ],
        )

    def test_venue_without_any_price(self):
        SupplierProduct.objects.filter(pk=self.product.pk).update(current_price=None)
        self.link_product()
        row = self.autofill_row()
        self.assertEqual(row["price"], "305.00")
        self.assertEqual(row["venue_prices"][2], self.venue_price(self.cafe_3, None, None))

    def test_not_found_has_no_venue_prices(self):
        row = self.autofill_row()
        self.assertEqual(row["match_status"], "not_found")
        self.assertEqual(row["venue_prices"], [])


class TenderItemVenuePricesAPITests(TenderVenuePricesFixture):
    """The same breakdown for a product the supplier picks by hand (not the autofill suggestion)."""

    def url(self, product=None, item=None):
        product_id = (product or self.product).id
        return f"{self.prefix}/items/{(item or self.item).id}/venue-prices?supplier_product_id={product_id}"

    def get(self, url, user=None):
        self.auth(user or self.supplier_user)
        return self.client.get(url)

    def test_requires_authentication(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)

    def test_non_member_gets_403(self):
        stranger = User.objects.create(email="vp-stranger@example.com", full_name="S", role=UserRole.SUPPLIER)
        response = self.get(self.url(), user=stranger)
        self.assertEqual(response.status_code, 403)

    def test_prices_of_a_hand_picked_product(self):
        response = self.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(),
            [
                self.venue_price(self.cafe_1, "305.00", "individual"),
                self.venue_price(self.cafe_2, "315.00", "individual"),
                self.venue_price(self.cafe_3, "330.00", "base"),
            ],
        )

    def test_expired_individual_price_falls_back_to_base(self):
        now = timezone.now()
        SupplierProductVenuePrice.objects.filter(venue=self.cafe_1).update(
            valid_from=now - timedelta(days=2), valid_to=now - timedelta(minutes=1)
        )
        SupplierProductVenuePrice.objects.filter(venue=self.cafe_2).update(
            valid_from=now - timedelta(days=2), valid_to=now + timedelta(minutes=1)
        )
        response = self.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data[0], self.venue_price(self.cafe_1, "330.00", "base"))
        self.assertEqual(data[1], self.venue_price(self.cafe_2, "315.00", "individual"))

    def test_missing_product_rejected(self):
        response = self.get(f"{self.prefix}/items/{self.item.id}/venue-prices")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("supplier_product_id", response.json())

    def test_other_suppliers_product_rejected(self):
        other = Supplier.objects.create(legal_name="Other LLC", display_name="Other")
        foreign = SupplierProduct.objects.create(
            supplier=other, supplier_name="Чужие сливки", normalized_name="сливки", current_price="1.00"
        )
        response = self.get(self.url(product=foreign))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("supplier_product_id", response.json())

    def test_unpublished_item_is_404(self):
        TenderItem.objects.filter(pk=self.item.pk).update(published=False)
        response = self.get(self.url())
        self.assertEqual(response.status_code, 404, response.content)

    def test_item_of_another_tender_is_404(self):
        other = Tender.objects.create(
            created_by=self.owner, name="Другой", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        response = self.get(
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{other.id}/items/{self.item.id}/venue-prices"
            f"?supplier_product_id={self.product.id}"
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_draft_tender_is_404(self):
        Tender.objects.filter(pk=self.tender.pk).update(status=TenderStatus.DRAFT)
        response = self.get(self.url())
        self.assertEqual(response.status_code, 404, response.content)

    def test_not_invited_supplier_gets_404(self):
        Tender.objects.filter(pk=self.tender.pk).update(access=TenderAccess.INVITE_ONLY)
        response = self.get(self.url())
        self.assertEqual(response.status_code, 404, response.content)
