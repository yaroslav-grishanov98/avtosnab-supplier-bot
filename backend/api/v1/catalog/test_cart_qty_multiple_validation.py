"""2026-09-29, пользователь (баг-репорт с фронта, скриншоты): «Мин сумма стоит 1.
В итоге при клике на + получается 17. Это не верно... если кратность заказа 16 штук
- то при +1 будет сразу 32 штуки». Кнопки +/- — фронтенд (в этом репозитории его
нет), но бэкенд вообще не проверял requested_qty против min_order_qty/
delivery_multiple товара поставщика — любое количество проходило. Эти тесты
закрывают именно серверную часть."""

from decimal import Decimal

from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer, _make_supplier_product, _make_venue_owner
from core.models import Venue
from suppliers.models import SupplierProduct


class CartItemQuantityMultipleValidationTests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Test Cafe")
        self.owner = _make_venue_owner(self.venue)
        self.sp = _make_supplier_product(price="172.00")
        SupplierProduct.objects.filter(pk=self.sp.id).update(
            min_order_qty=Decimal("1"), delivery_multiple=Decimal("16")
        )
        self.cart_url = f"/api/v1/catalog/venues/{self.venue.id}/cart"
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def _add(self, qty):
        return self.client.post(
            f"{self.cart_url}/items",
            {"supplier_product_id": str(self.sp.id), "requested_qty": qty},
            format="json",
        )

    def test_add_valid_multiple_succeeds(self):
        response = self._add("16")
        self.assertEqual(response.status_code, 201, response.json())
        response = self._add("32")
        self.assertEqual(response.status_code, 201, response.json())

    def test_add_non_multiple_rejected(self):
        response = self._add("17")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Количество должно быть кратно 16", response.json()["requested_qty"][0])

    def test_add_below_min_order_qty_rejected(self):
        SupplierProduct.objects.filter(pk=self.sp.id).update(min_order_qty=Decimal("20"), delivery_multiple=None)
        response = self._add("5")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Минимальное количество", response.json()["requested_qty"][0])

    def test_add_without_min_or_multiple_set_accepts_any_qty(self):
        SupplierProduct.objects.filter(pk=self.sp.id).update(min_order_qty=None, delivery_multiple=None)
        response = self._add("17")
        self.assertEqual(response.status_code, 201, response.json())

    def test_add_off_system_item_not_validated(self):
        """У товара вне системы нет ни supplier_product, ни min_order_qty/
        delivery_multiple — количество не должно проверяться вообще."""
        response = self.client.post(
            f"{self.cart_url}/items",
            {"requested_name": "Микрозелень", "requested_qty": "17"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())

    def test_patch_to_non_multiple_rejected(self):
        item_id = self._add("16").json()["id"]
        response = self.client.patch(f"{self.cart_url}/items/{item_id}", {"requested_qty": "17"}, format="json")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("Количество должно быть кратно 16", response.json()["requested_qty"][0])
        # позиция не изменилась после отказа
        self.assertEqual(self.client.get(self.cart_url).json()["items"][0]["requested_qty"], "16.0000")

    def test_patch_to_valid_multiple_succeeds(self):
        item_id = self._add("16").json()["id"]
        response = self.client.patch(f"{self.cart_url}/items/{item_id}", {"requested_qty": "48"}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["requested_qty"], "48.0000")
