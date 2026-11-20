from decimal import Decimal

from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import (
    MasterProduct,
    SupplierProductVenuePrice,
    Tender,
    TenderAccess,
    TenderItem,
    TenderOffer,
    TenderStatus,
    VenueProductMatrixItem,
)
from core.models import EntityStatus, Unit, UnitKind, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierProduct


class TenderOfferPricesFixture(APITestCase):
    def setUp(self):
        self.owner = User.objects.create(email="op-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.cafe_1 = Venue.objects.create(display_name="Кафе 1")
        self.cafe_2 = Venue.objects.create(display_name="Кафе 2")
        self.cafe_3 = Venue.objects.create(display_name="Кафе 3")
        for venue in (self.cafe_1, self.cafe_2, self.cafe_3):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.kg = Unit.objects.create(code="t-кг", name="кг", kind=UnitKind.WEIGHT, ratio_to_base=Decimal("1"))
        self.g = Unit.objects.create(code="t-г", name="г", kind=UnitKind.WEIGHT, ratio_to_base=Decimal("0.001"))
        self.l = Unit.objects.create(code="t-л", name="л", kind=UnitKind.VOLUME, ratio_to_base=Decimal("1"))
        self.pc = Unit.objects.create(code="t-шт", name="шт", kind=UnitKind.PIECE, ratio_to_base=Decimal("1"))
        self.pack = Unit.objects.create(code="t-уп", name="уп", kind=UnitKind.PACKAGE)
        self.cheese = MasterProduct.objects.create(owner=self.owner, canonical_name="Сыр", unit=self.kg)
        self.tender = Tender.objects.create(
            created_by=self.owner, name="Сыры", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        self.item = TenderItem.objects.create(tender=self.tender, master_product=self.cheese)
        self.item.venues.set([self.cafe_1, self.cafe_2, self.cafe_3])
        self.count = 0

    def product(self, unit, price=None, net_value=None, net_unit=None, supplier_name=None):
        self.count += 1
        supplier = Supplier.objects.create(
            legal_name=f"{supplier_name or 'Поставщик'} {self.count}", display_name=supplier_name or f"П{self.count}"
        )
        return SupplierProduct.objects.create(
            supplier=supplier,
            supplier_name=f"Сыр {self.count}",
            unit=unit,
            net_value=net_value,
            net_unit=net_unit,
            current_price=price,
        )

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))


class TenderOfferPricePerUnitAPITests(TenderOfferPricesFixture):
    """QA 01.10, the buyer's «Предложения по товару»: offers in different packs compare by the price per unit of the
    position (the master product's unit)."""

    def offer(self, price, unit, net_value=None, net_unit=None):
        product = self.product(unit, net_value=net_value, net_unit=net_unit)
        TenderOffer.objects.create(
            tender_item=self.item, supplier=product.supplier, supplier_product=product, price=price
        )
        self.auth()
        response = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/items/{self.item.id}/offers")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()[0]["price_per_unit"]

    def test_same_unit_as_is(self):
        self.assertEqual(self.offer("300.00", self.kg), "300.00")

    def test_same_kind_is_converted(self):
        self.assertEqual(self.offer("0.40", self.g), "400.00")

    def test_piece_with_net_in_position_kind(self):
        self.assertEqual(self.offer("160.00", self.pc, Decimal("0.5"), self.kg), "320.00")

    def test_pack_with_net_in_grams(self):
        self.assertEqual(self.offer("160.00", self.pack, Decimal("500"), self.g), "320.00")

    def test_rounded_half_up_to_kopecks(self):
        self.assertEqual(self.offer("100.00", self.pc, Decimal("0.3"), self.kg), "333.33")

    def test_net_of_other_kind_is_null(self):
        self.assertIsNone(self.offer("90.00", self.pc, Decimal("1"), self.l))

    def test_piece_without_net_is_null(self):
        self.assertIsNone(self.offer("160.00", self.pc))

    def test_position_without_unit_is_null(self):
        MasterProduct.objects.filter(pk=self.cheese.pk).update(unit=None)
        self.assertIsNone(self.offer("300.00", self.kg))


class TenderItemCurrentProductsAPITests(TenderOfferPricesFixture):
    """QA 01.10: what each venue of the position has in its directory right now — whose product and at what price."""

    def url(self, tender=None):
        return f"/api/v1/catalog/tenders/{(tender or self.tender).id}/items/{self.item.id}/current-products"

    def row(self, venue, product):
        return VenueProductMatrixItem.objects.create(
            venue=venue, requested_name="Сыр", master_product=self.cheese, supplier_product=product
        )

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_other_owner_gets_403(self):
        stranger = User.objects.create(email="op-str@example.com", full_name="Чужой", role=UserRole.VENUE_USER)
        self.auth(stranger)
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_item_of_another_tender_is_404(self):
        other = Tender.objects.create(created_by=self.owner, name="Другой", access=TenderAccess.OPEN)
        self.auth()
        self.assertEqual(self.client.get(self.url(tender=other)).status_code, 404)

    def test_directory_name_also_without_supplier_product(self):
        product = self.product(self.kg, price="300.00", supplier_name="Альфа")
        self.row(self.cafe_1, product)
        VenueProductMatrixItem.objects.create(
            venue=self.cafe_2, requested_name="Сыр твёрдый", master_product=self.cheese
        )

        self.auth()
        rows = self.client.get(self.url()).json()
        self.assertEqual(
            [(row["venue_name"], row["requested_name"], row["supplier_name"], row["price"]) for row in rows],
            [("Кафе 1", "Сыр", "Альфа", "300.00"), ("Кафе 2", "Сыр твёрдый", None, None), ("Кафе 3", None, None, None)],
        )

    def test_current_product_and_price_per_venue(self):
        individual = self.product(self.kg, price="310.00", supplier_name="Альфа")
        self.row(self.cafe_1, individual)
        SupplierProductVenuePrice.objects.create(supplier_product=individual, venue=self.cafe_1, price="290.00")
        base = self.product(self.kg, price="305.00", supplier_name="Бета")
        self.row(self.cafe_2, base)
        archived = self.row(self.cafe_3, self.product(self.kg, price="1.00"))
        VenueProductMatrixItem.objects.filter(pk=archived.pk).update(status=EntityStatus.ARCHIVED)

        self.auth()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [
                (
                    row["venue_name"],
                    row["supplier_name"],
                    row["supplier_product_name"],
                    row["price"],
                    row["currency"],
                    row["price_source"],
                )
                for row in response.json()
            ],
            [
                ("Кафе 1", "Альфа", individual.supplier_name, "290.00", "RUB", "individual"),
                ("Кафе 2", "Бета", base.supplier_name, "305.00", "RUB", "base"),
                ("Кафе 3", None, None, None, None, None),
            ],
        )
