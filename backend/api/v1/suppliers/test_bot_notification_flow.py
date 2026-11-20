"""Exercise the real order/notification/reply path without sending messages."""

from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from rest_framework.test import APITestCase

from api.v1.catalog.services import add_cart_item, get_or_create_cart
from api.v1.catalog.tests import _jwt_bearer, _make_supplier_product, _make_venue_owner
from core.models import Order, User, UserRole, Venue
from notifications.models import (
    NotificationAudience,
    NotificationDelivery,
    NotificationDestination,
    NotificationEvent,
    NotificationRule,
    NotificationSupplierAction,
    NotificationVenueScope,
)
from notifications.tasks import dispatch_notification_delivery
from suppliers.models import BotUserBinding, Supplier, SupplierUser


@override_settings(SERVICE_API_TOKEN="offline-test-token", NOTIFICATION_DELIVERY_DRY_RUN=False)
class SupplierBotNotificationFlowTests(APITestCase):
    def setUp(self):
        self.venue = Venue.objects.create(display_name="Cafe")
        self.buyer = _make_venue_owner(self.venue)
        self.user = User.objects.create(
            email="supplier-flow@example.test", full_name="Supplier", role=UserRole.SUPPLIER
        )
        self.supplier = Supplier.objects.create(legal_name="Flow LLC", display_name="Flow Supplier")
        SupplierUser.objects.create(supplier=self.supplier, user=self.user, role="owner")
        BotUserBinding.objects.create(
            supplier=self.supplier, user=self.user, channel="telegram", chat_id="11", external_user_id="22"
        )
        self.destination = NotificationDestination.objects.create(
            created_by=self.user,
            audience=NotificationAudience.SUPPLIER,
            supplier=self.supplier,
            channel="telegram",
            destination_type="personal",
            chat_id="11",
        )
        NotificationRule.objects.create(destination=self.destination, event=NotificationEvent.SUPPLIER_NEW_REQUEST)
        buyer_destination = NotificationDestination.objects.create(
            created_by=self.buyer,
            audience=NotificationAudience.BUYER,
            channel="telegram",
            destination_type="personal",
            chat_id="33",
        )
        NotificationVenueScope.objects.create(destination=buyer_destination, venue=self.venue)
        NotificationRule.objects.create(destination=buyer_destination, event=NotificationEvent.SUPPLIER_RESPONSE)
        product = _make_supplier_product(supplier=self.supplier, price="100.00")
        cart = get_or_create_cart(self.buyer, self.venue)
        add_cart_item(cart=cart, supplier_product=product, requested_name="Milk", requested_qty=10)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.buyer))
        with patch("notifications.services.kick_notification_deliveries"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/v1/catalog/venues/{self.venue.pk}/cart/submit",
                {"command_id": str(uuid4()), "expected_settings_version": 1},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.order = Order.objects.get(pk=response.data["id"])
        self.request = self.order.supplier_requests.get()
        self.delivery = NotificationDelivery.objects.get(event=NotificationEvent.SUPPLIER_NEW_REQUEST)
        self.identity = {
            "platform": "telegram",
            "chat_id": "11",
            "telegram_user_id": "22",
            "supplier_id": str(self.supplier.id),
        }
        self.client.credentials(HTTP_AUTHORIZATION="Bearer offline-test-token")
        self.detail_url = f"/api/v1/bot-user-bindings/orders/{self.request.id}"
        self.reply_url = f"/api/v1/notifications/bot/supplier-requests/{self.request.id}/respond"

    def reply(self, **changes):
        with (
            patch("notifications.services.kick_notification_deliveries"),
            patch("notifications.tasks.kick_remove_supplier_action_buttons"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            return self.client.post(
                self.reply_url,
                {
                    "channel": "telegram",
                    "chat_id": "11",
                    "action": "accept",
                    "delivery_date_unknown": True,
                    **changes,
                },
                format="json",
            )

    def test_notification_and_incoming_card_use_same_request_and_reply_updates_buyer(self):
        self.assertEqual(self.delivery.payload["request_id"], str(self.request.id))
        detail = self.client.get(self.detail_url, self.identity)
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertTrue(detail.data["can_respond"])
        self.assertEqual(detail.data["supplier_name"], "Flow Supplier")
        self.assertEqual(detail.data["response_unavailable_reason"], "")
        # Keep rendering, claim and persistence real; mock only the provider boundary.
        with patch("notifications.tasks.send_provider_message", return_value="test-message") as send:
            dispatch_notification_delivery(str(self.delivery.id))
        send.assert_called_once()
        keyboard = send.call_args.args[2]
        callbacks = [b.get("callback_data", "") for line in keyboard for b in line]
        self.assertIn(f"supplier_action:{self.request.id}:accept", callbacks)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "sent")
        response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "confirmed")
        detail = self.client.get(self.detail_url, self.identity)
        self.assertEqual(detail.data["status"], "confirmed")
        self.assertFalse(detail.data["can_respond"])
        self.assertEqual(NotificationDelivery.objects.filter(event=NotificationEvent.SUPPLIER_RESPONSE).count(), 1)
        self.assertEqual(self.reply().status_code, 409)
        self.assertEqual(NotificationSupplierAction.objects.filter(supplier_request=self.request).count(), 1)

    def test_disabled_connection_removes_actions_and_rejects_old_callback(self):
        self.destination.is_active = False
        self.destination.save()
        self.assertFalse(self.client.get(self.detail_url, self.identity).data["can_respond"])
        self.assertEqual(
            self.client.get(self.detail_url, self.identity).data["response_unavailable_reason"], "chat_not_connected"
        )
        self.assertEqual(self.reply().status_code, 403)
        self.assertFalse(NotificationSupplierAction.objects.exists())

    def test_connection_for_another_supplier_does_not_authorise_actions(self):
        self.destination.supplier = Supplier.objects.create(legal_name="Other LLC", display_name="Other")
        self.destination.save()
        self.assertFalse(self.client.get(self.detail_url, self.identity).data["can_respond"])
        self.assertEqual(self.reply().status_code, 403)
        self.assertFalse(NotificationSupplierAction.objects.exists())

    def test_partial_reply_is_visible_in_incoming_requests(self):
        item = self.request.items.get()
        response = self.reply(action="partial", item_quantities={str(item.id): "5"})
        self.assertEqual(response.status_code, 200, response.data)
        detail = self.client.get(self.detail_url, self.identity)
        self.assertEqual(detail.data["status"], "partially_confirmed")
        self.assertFalse(detail.data["can_respond"])

    def test_declined_reply_is_visible_in_incoming_requests(self):
        response = self.reply(action="decline", comment="Нет в наличии")
        self.assertEqual(response.status_code, 200, response.data)
        detail = self.client.get(self.detail_url, self.identity)
        self.assertEqual(detail.data["status"], "declined")
        self.assertFalse(detail.data["can_respond"])
        self.assertEqual(NotificationDelivery.objects.filter(event=NotificationEvent.SUPPLIER_RESPONSE).count(), 1)
