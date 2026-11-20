from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderOffer, TenderStatus
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct, SupplierUser, SupplierUserRole


class TenderOfferHistoryFixture(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="oh-owner@example.com", full_name="Анна", role=UserRole.VENUE_USER)
        self.cafe_1 = Venue.objects.create(display_name="Кафе 1")
        self.cafe_2 = Venue.objects.create(display_name="Кафе 2")
        for venue in (self.cafe_1, self.cafe_2):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 35%")
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Молочка", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=cream)
        self.item.venues.set([self.cafe_1, self.cafe_2])
        self.supplier_a, self.member_a, self.product_a = self.supplier("Поставщик А", "Сливки Parmalat 35%", "sa")
        self.supplier_b, self.member_b, self.product_b = self.supplier("Поставщик Б", "Сливки Петмол 33%", "sb")

    def supplier(self, name, product_name, key):
        supplier = Supplier.objects.create(legal_name=f"{name} LLC", display_name=name)
        member = User.objects.create(
            email=f"oh-{key}@example.com", full_name=f"Сотрудник {name}", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=supplier, user=member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        product = SupplierProduct.objects.create(
            supplier=supplier, supplier_name=product_name, normalized_name=product_name.lower()
        )
        return supplier, member, product

    def offer_url(self, supplier):
        return f"/api/v1/catalog/suppliers/{supplier.id}/tenders/{self.tender.id}/items/{self.item.id}/offer"

    def put_offer(self, supplier, member, product, price, venues, comment=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(member))
        body = {"supplier_product_id": str(product.id), "price": price, "venue_ids": [str(v.id) for v in venues]}
        if comment is not None:
            body["comment"] = comment
        response = self.client.put(self.offer_url(supplier), body, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def buyer_history(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/history")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def rows(self, history, change_type):
        return sorted(
            (row["changed_field"], row["old_value"], row["new_value"])
            for row in history
            if row["change_type"] == change_type
        )


class TenderOfferHistoryAPITests(TenderOfferHistoryFixture):
    """Raw ТЗ §76 «Предложение: исходный товар; изменения товара; цены; список заведений; комментарий; даты»."""

    def test_first_offer_records_what_was_offered(self):
        offer = self.put_offer(self.supplier_a, self.member_a, self.product_a, "320.00", [self.cafe_1], "свежие")
        history = self.buyer_history()
        self.assertEqual(
            self.rows(history, "offer_submitted"),
            [
                ("comment", None, "свежие"),
                ("price", None, "320.00"),
                ("supplier_product", None, "Сливки Parmalat 35%"),
                ("venues", None, "Кафе 1"),
            ],
        )
        row = next(row for row in history if row["change_type"] == "offer_submitted")
        self.assertEqual((row["tender_offer_id"], row["supplier_name"]), (offer["id"], "Поставщик А"))
        self.assertEqual((row["tender_item_id"], row["item_name"]), (str(self.item.id), "Сливки 35%"))
        self.assertEqual(row["actor_name"], "Сотрудник Поставщик А")

    def test_changed_offer_records_changed_fields_only(self):
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "320.00", [self.cafe_1])
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "300.00", [self.cafe_1, self.cafe_2])
        self.assertEqual(
            self.rows(self.buyer_history(), "offer_updated"),
            [("price", "320.00", "300.00"), ("venues", "Кафе 1", "Кафе 1, Кафе 2")],
        )

    def test_same_offer_again_records_nothing(self):
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "320.00", [self.cafe_1])
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "320.00", [self.cafe_1])
        self.assertEqual(self.rows(self.buyer_history(), "offer_updated"), [])

    def test_withdrawn_and_returned(self):
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "320.00", [self.cafe_1])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.member_a))
        self.assertEqual(self.client.delete(self.offer_url(self.supplier_a)).status_code, 200)
        self.assertEqual(self.client.delete(self.offer_url(self.supplier_a)).status_code, 200)
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "310.00", [self.cafe_1])

        history = self.buyer_history()
        self.assertEqual(
            [row["change_type"] for row in history if row["change_type"] in ("offer_withdrawn", "offer_returned")],
            ["offer_returned", "offer_withdrawn"],
        )
        self.assertEqual(self.rows(history, "offer_updated"), [("price", "320.00", "310.00")])

    def test_supplier_sees_own_offer_only(self):
        self.put_offer(self.supplier_a, self.member_a, self.product_a, "320.00", [self.cafe_1])
        self.put_offer(self.supplier_b, self.member_b, self.product_b, "300.00", [self.cafe_1])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.member_a))
        response = self.client.get(f"/api/v1/catalog/suppliers/{self.supplier_a.id}/tenders/{self.tender.id}/history")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertEqual({row["supplier_name"] for row in rows}, {"Поставщик А"})
        self.assertNotIn("Петмол", response.content.decode())
        self.assertEqual({(row["actor_id"], row["actor_name"]) for row in rows}, {(None, None)})


class TenderDistributionHistoryAPITests(TenderOfferHistoryFixture):
    """Raw ТЗ §76 «Распределение: какой поставщик; какой товар; какая цена; какое заведение; когда назначено; кем»."""

    def distribute(self, supplier, product, price, item=None):
        item = item or self.item
        offer = TenderOffer.objects.create(tender_item=item, supplier=supplier, supplier_product=product, price=price)
        offer.venues.set([self.cafe_1])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        response = self.client.post(
            f"/api/v1/catalog/tenders/{item.tender_id}/items/{item.id}/offers/{offer.id}/distribution",
            {"venue_ids": [str(self.cafe_1.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def url(self, tender=None):
        return f"/api/v1/catalog/tenders/{(tender or self.tender).id}/items/{self.item.id}/distributions"

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_other_owner_gets_403(self):
        stranger = User.objects.create(email="oh-str@example.com", full_name="Чужой", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_item_of_another_tender_is_404(self):
        other = Tender.objects.create(created_by=self.owner, name="Другой", access=TenderAccess.OPEN)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.assertEqual(self.client.get(self.url(tender=other)).status_code, 404)

    def test_empty_before_any_distribution(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.assertEqual(self.client.get(self.url()).json(), [])

    def test_replaced_distribution_stays_in_history(self):
        self.distribute(self.supplier_a, self.product_a, "320.00")
        self.distribute(self.supplier_b, self.product_b, "300.00")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        rows = self.client.get(self.url()).json()
        self.assertEqual(
            [
                (row["venue_name"], row["supplier_name"], row["supplier_product_name"], row["price"], row["is_current"])
                for row in rows
            ],
            [
                ("Кафе 1", "Поставщик Б", "Сливки Петмол 33%", "300.00", True),
                ("Кафе 1", "Поставщик А", "Сливки Parmalat 35%", "320.00", False),
            ],
        )
        self.assertEqual({row["distributed_by_name"] for row in rows}, {"Анна"})
        self.assertTrue(all(row["distributed_at"] for row in rows))

    def test_same_product_redistributed_by_another_tender_is_no_longer_current(self):
        """A later tender fixes its own price for the same product and venue — this position's row stops being
        current, as its «Распределено» does (docs/adr/0004)."""
        self.distribute(self.supplier_a, self.product_a, "320.00")
        later = Tender.objects.create(
            created_by=self.owner, name="Позже", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        later_item = TenderItem.objects.create(tender=later, master_product=self.item.master_product)
        later_item.venues.set([self.cafe_1])
        self.distribute(self.supplier_a, self.product_a, "300.00", item=later_item)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        self.assertEqual([row["is_current"] for row in self.client.get(self.url()).json()], [False])
        later_url = f"/api/v1/catalog/tenders/{later.id}/items/{later_item.id}/distributions"
        self.assertEqual([row["is_current"] for row in self.client.get(later_url).json()], [True])
