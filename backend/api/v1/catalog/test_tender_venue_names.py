from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import (
    MasterProduct,
    Tender,
    TenderAccess,
    TenderChange,
    TenderItem,
    TenderOffer,
    TenderStatus,
)
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct, SupplierUser, SupplierUserRole


class TenderVenueFixture(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="vn-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.mmk = Venue.objects.create(display_name='ПАО "ММК-1"', short_name='ПАО "ММК"')
        self.cafe = Venue.objects.create(display_name="Кафе 1")
        self.archived = Venue.objects.create(display_name="Старое кафе", status=EntityStatus.ARCHIVED)
        for venue in (self.mmk, self.cafe, self.archived):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%")
        self.cheese = MasterProduct.objects.create(owner=self.owner, canonical_name="Камамбер")
        self.supplier = Supplier.objects.create(legal_name="Vn LLC", display_name="Vn Co")
        self.member = User.objects.create(email="vn-sup@example.com", full_name="S", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.product = SupplierProduct.objects.create(
            supplier=self.supplier, supplier_name="Сливки 1л", normalized_name="сливки", current_price="300.00"
        )

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def tender(self, status=TenderStatus.ACTIVE, venues=None, name="Осеннее меню"):
        tender = Tender.objects.create(created_by=self.owner, name=name, access=TenderAccess.OPEN, status=status)
        item = TenderItem.objects.create(tender=tender, master_product=self.cream)
        item.venues.set(venues if venues is not None else [self.mmk])
        return tender, item


class ArchivedVenueAPITests(TenderVenueFixture):
    """A venue whose cabinet was deleted is archived, while its owner link stays active — it must not be added to
    a tender position; one already there stays."""

    def test_item_with_archived_venue_not_added(self):
        tender, _ = self.tender(status=TenderStatus.DRAFT)
        self.auth()
        response = self.client.post(
            f"/api/v1/catalog/tenders/{tender.id}/items",
            {"items": [{"master_product_id": str(self.cheese.id), "venue_ids": [str(self.archived.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["detail"], "Заведение «Старое кафе» в архиве.")
        self.assertFalse(TenderItem.objects.filter(master_product=self.cheese).exists())

    def test_archived_venue_not_added_to_item(self):
        tender, item = self.tender(status=TenderStatus.DRAFT)
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}",
            {"venue_ids": [str(self.mmk.id), str(self.archived.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("в архиве", response.json()["detail"])
        self.assertEqual(list(item.venues.all()), [self.mmk])

    def test_venue_archived_after_adding_stays(self):
        tender, item = self.tender(venues=[self.mmk])
        Venue.objects.filter(pk=self.mmk.pk).update(status=EntityStatus.ARCHIVED)
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}",
            {"venue_ids": [str(self.mmk.id), str(self.cafe.id)], "comment": "дополнили"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertCountEqual(response.json()["venue_ids"], [str(self.mmk.id), str(self.cafe.id)])


class TenderVenueNameAPITests(TenderVenueFixture):
    """The buyer's cabinet names a venue short_name, falling back to display_name; the tender module follows."""

    def test_availability_uses_short_name(self):
        self.auth()
        response = self.client.post(
            "/api/v1/catalog/tenders/venue-availability",
            {"owner_id": str(self.owner.id), "master_product_ids": [str(self.cream.id)]},
            format="json",
        )
        names = {row["venue_id"]: row["venue_name"] for row in response.json()[0]["venues"]}
        self.assertEqual(names[str(self.mmk.id)], 'ПАО "ММК"')
        self.assertEqual(names[str(self.cafe.id)], "Кафе 1")

    def test_conflict_uses_short_name(self):
        self.tender()
        draft, _ = self.tender(status=TenderStatus.DRAFT, name="Дубль")
        self.auth()
        response = self.client.post(f"/api/v1/catalog/tenders/{draft.id}/publish")
        self.assertEqual(response.status_code, 409, response.content)
        data = response.json()
        self.assertEqual(data["conflicts"][0]["venue_name"], 'ПАО "ММК"')
        self.assertIn('Сливки 33% + ПАО "ММК" уже участвует', data["detail"])

    def test_supplier_sees_short_name(self):
        tender, _ = self.tender()
        self.auth(self.member)
        header = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{tender.id}").json()
        self.assertEqual([venue["display_name"] for venue in header["customer_venues"]], ['ПАО "ММК"'])
        listing = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders", {"q": '"ммк"'}).json()
        self.assertEqual([row["id"] for row in listing["results"]], [str(tender.id)])

    def test_venue_prices_use_short_name(self):
        tender, item = self.tender()
        self.auth(self.member)
        response = self.client.get(
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{tender.id}/items/{item.id}/venue-prices",
            {"supplier_product_id": str(self.product.id)},
        )
        self.assertEqual(response.json()[0]["venue_name"], 'ПАО "ММК"')

    def test_history_records_short_name(self):
        tender, item = self.tender()
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}",
            {"venue_ids": [str(self.mmk.id), str(self.cafe.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        change = TenderChange.objects.get(tender=tender)
        self.assertEqual((change.old_value, change.new_value), ('ПАО "ММК"', 'Кафе 1, ПАО "ММК"'))

    def test_distribution_preview_uses_short_name(self):
        tender, item = self.tender()
        offer = TenderOffer.objects.create(
            tender_item=item, supplier=self.supplier, supplier_product=self.product, price="300.00"
        )
        offer.venues.set([self.mmk])
        self.auth()
        response = self.client.get(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}/offers/{offer.id}/distribution",
            {"venue_id": str(self.mmk.id)},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["venue_name"], 'ПАО "ММК"')

    def test_distributed_venue_message_uses_short_name(self):
        tender, item = self.tender(venues=[self.mmk, self.cafe])
        offer = TenderOffer.objects.create(
            tender_item=item, supplier=self.supplier, supplier_product=self.product, price="300.00"
        )
        offer.venues.set([self.mmk, self.cafe])
        self.auth()
        response = self.client.post(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}/offers/{offer.id}/distribution",
            {"venue_ids": [str(self.mmk.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.auth(self.member)
        response = self.client.put(
            f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{tender.id}/items/{item.id}/offer",
            {"supplier_product_id": str(self.product.id), "price": "300.00", "venue_ids": [str(self.cafe.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('ПАО "ММК"', str(response.json()["venue_ids"]))
