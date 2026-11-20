from django.test import override_settings
from rest_framework.test import APITestCase

from core.models import EntityStatus, Order, OrderItem, SupplierOrderRequest, User, UserRole, Venue
from suppliers.models import BotUserBinding, SourceChannel, Supplier, SupplierUser, SupplierUserRole


@override_settings(SERVICE_API_TOKEN="offline-test-token")
class BotOrderAPITests(APITestCase):
    url = "/api/v1/bot-user-bindings/orders"

    def setUp(self):
        self.user = User.objects.create(email="bot-orders@example.test", full_name="Owner", role=UserRole.SUPPLIER)
        self.supplier = Supplier.objects.create(legal_name="Supplier", display_name="Supplier")
        self.other = Supplier.objects.create(legal_name="Other", display_name="Other")
        self.membership = SupplierUser.objects.create(
            supplier=self.supplier, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.binding = BotUserBinding.objects.create(
            supplier=self.supplier,
            user=self.user,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-a",
            external_user_id="user-a",
            status=EntityStatus.ACTIVE,
        )
        self.venue = Venue.objects.create(display_name="Buyer")
        self.order = Order.objects.create(venue=self.venue, order_number="ORDER-1")
        self.request = SupplierOrderRequest.objects.create(order=self.order, supplier=self.supplier, number="REQUEST-1")
        OrderItem.objects.create(
            order=self.order, supplier_request=self.request, requested_name="Молоко", requested_qty="2.5"
        )
        self.foreign = SupplierOrderRequest.objects.create(order=self.order, supplier=self.other, number="REQUEST-2")
        OrderItem.objects.create(
            order=self.order, supplier_request=self.foreign, requested_name="Секретный товар", requested_qty="1"
        )
        self.identity = {
            "platform": "telegram",
            "chat_id": "chat-a",
            "telegram_user_id": "user-a",
            "supplier_id": str(self.supplier.id),
        }
        self.client.credentials(HTTP_AUTHORIZATION="Bearer offline-test-token")

    def test_list_contains_only_selected_company_request(self):
        response = self.client.get(self.url, self.identity)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.request.id))
        self.assertEqual(response.data["results"][0]["item_count"], 1)
        self.request.refresh_from_db()
        self.assertIsNone(self.request.viewed_at)

    def test_detail_uses_request_id_and_never_includes_other_supplier_items(self):
        response = self.client.get(f"{self.url}/{self.request.id}", self.identity)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r["name"] for r in response.data["items"]], ["Молоко"])
        self.assertEqual(response.data["number"], "REQUEST-1")
        self.request.refresh_from_db()
        viewed_at = self.request.viewed_at
        self.assertIsNotNone(viewed_at)
        self.client.get(f"{self.url}/{self.request.id}", self.identity)
        self.request.refresh_from_db()
        self.assertEqual(self.request.viewed_at, viewed_at)

    def test_order_id_is_not_silently_treated_as_request_id(self):
        self.assertEqual(self.client.get(f"{self.url}/{self.order.id}", self.identity).status_code, 404)

    def test_foreign_request_cannot_be_opened(self):
        self.assertEqual(self.client.get(f"{self.url}/{self.foreign.id}", self.identity).status_code, 404)
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.viewed_at)

    def test_rejects_spoofed_chat_or_user_or_company(self):
        for replacement in (
            {"chat_id": "chat-other"},
            {"telegram_user_id": "user-other"},
            {"supplier_id": str(self.other.id)},
        ):
            with self.subTest(replacement=replacement):
                response = self.client.get(self.url, {**self.identity, **replacement})
                self.assertEqual(response.status_code, 404)

    def test_revoked_membership_revokes_list_and_detail(self):
        self.membership.status = EntityStatus.INACTIVE
        self.membership.save()
        self.assertEqual(self.client.get(self.url, self.identity).status_code, 404)
        self.assertEqual(self.client.get(f"{self.url}/{self.request.id}", self.identity).status_code, 404)

    def test_company_switch_invalidates_old_company_callbacks(self):
        SupplierUser.objects.create(supplier=self.other, user=self.user, role=SupplierUserRole.OWNER)
        self.binding.supplier = self.other
        self.binding.save()
        self.assertEqual(self.client.get(self.url, self.identity).status_code, 404)

    def test_inactive_account_is_denied(self):
        self.user.status = EntityStatus.INACTIVE
        self.user.save()
        self.assertEqual(self.client.get(self.url, self.identity).status_code, 403)

    def test_service_token_is_required(self):
        self.client.credentials()
        self.assertEqual(self.client.get(self.url, self.identity).status_code, 403)

    def test_unapproved_requests_are_hidden_even_if_an_old_record_exists(self):
        self.order.approval_status = "pending"
        self.order.save()
        self.assertEqual(self.client.get(self.url, self.identity).data["count"], 0)
        self.assertEqual(self.client.get(f"{self.url}/{self.request.id}", self.identity).status_code, 404)

    def test_pages_have_stable_order_and_no_repeated_ids(self):
        for number in range(12):
            order = Order.objects.create(venue=self.venue, order_number=f"ORDER-{number + 2}")
            SupplierOrderRequest.objects.create(order=order, supplier=self.supplier, number=f"REQUEST-{number + 3}")
        first = self.client.get(self.url, {**self.identity, "limit": 10, "offset": 0}).data
        second = self.client.get(self.url, {**self.identity, "limit": 10, "offset": 10}).data
        ids = [r["id"] for r in first["results"] + second["results"]]
        self.assertEqual(len(ids), 13)
        self.assertEqual(len(set(ids)), 13)
        self.assertEqual(first["count"], 13)

    def test_invalid_pagination_is_rejected(self):
        for params in ({"limit": 11}, {"offset": -1}, {"offset": "bad"}):
            self.assertEqual(self.client.get(self.url, {**self.identity, **params}).status_code, 400)

    def test_schema_exposes_identity_request_id_and_service_auth(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(public=True)
        listing = schema["paths"][self.url]["get"]
        detail = schema["paths"][self.url + "/{request_id}"]["get"]
        self.assertEqual(listing["operationId"], "bot_supplier_requests_list")
        self.assertEqual(detail["operationId"], "bot_supplier_requests_retrieve")
        self.assertIn({"ServiceApiToken": []}, detail["security"])
        required = {p["name"] for p in listing["parameters"] if p.get("required")}
        self.assertTrue({"supplier_id", "chat_id", "telegram_user_id", "platform"}.issubset(required))
