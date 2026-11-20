from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from threading import Event
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.models import User, UserRole, Venue
from notifications.models import (
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationDestination,
    NotificationEvent,
    NotificationRule,
    NotificationVenueScope,
)
from notifications.serializers import NotificationBotConnectRequestSerializer, NotificationDestinationCreateSerializer
from notifications.services import (
    enqueue_daily_delivery_checklists,
    enqueue_for_venue,
    enqueue_notification,
    enqueue_pending_deliveries,
)
from notifications.tasks import dispatch_notification_delivery, kick_update_cancelled_supplier_messages


def create_destination():
    user = User.objects.create(email="notifications@example.test", full_name="Test", role=UserRole.VENUE_USER)
    return NotificationDestination.objects.create(
        created_by=user,
        audience=NotificationAudience.BUYER,
        channel=NotificationChannel.TELEGRAM,
        destination_type="personal",
        chat_id="test-chat",
    )


class NotificationCeleryConfigurationTests(SimpleTestCase):
    def test_tasks_are_routed_to_the_dedicated_acknowledged_queue(self):
        for task_name in (
            "notifications.dispatch_delivery",
            "notifications.enqueue_daily_delivery_checklists",
            "notifications.enqueue_pending_deliveries",
            "notifications.remove_supplier_action_buttons",
            "notifications.update_cancelled_supplier_messages",
        ):
            self.assertEqual(settings.CELERY_TASK_ROUTES[task_name]["queue"], "notifications")
            self.assertTrue(settings.CELERY_TASK_ANNOTATIONS[task_name]["acks_late"])
            self.assertTrue(settings.CELERY_TASK_ANNOTATIONS[task_name]["reject_on_worker_lost"])

    def test_cancellation_message_enqueue_failure_does_not_break_committed_action(self):
        with patch(
            "notifications.tasks.update_cancelled_supplier_messages.delay", side_effect=RuntimeError("broker down")
        ):
            with self.assertLogs("notifications.tasks", level="ERROR"):
                kick_update_cancelled_supplier_messages("request-id")


class NotificationDestinationSerializerTests(SimpleTestCase):
    def test_telegram_group_requires_numeric_chat_id(self):
        """Отклонить текст команды вместо числового ID Telegram-группы."""
        payload = {
            "audience": NotificationAudience.BUYER,
            "channel": NotificationChannel.TELEGRAM,
            "destination_type": "group",
            "chat_id": "/getgroupid",
            "venue_ids": ["00000000-0000-0000-0000-000000000001"],
        }

        serializer = NotificationDestinationCreateSerializer(data=payload)

        self.assertFalse(serializer.is_valid())
        self.assertIn("chat_id", serializer.errors)

    def test_bot_connect_rejects_command_as_telegram_group_id(self):
        """Не создавать подключение группы, если бот передал текст команды вместо ID."""
        serializer = NotificationBotConnectRequestSerializer(
            data={
                "channel": NotificationChannel.TELEGRAM,
                "destination_type": "group",
                "chat_id": "/getgroupid",
                "token": "token",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("chat_id", serializer.errors)


class NotificationDestinationLifecycleTests(TestCase):
    def test_same_chat_can_be_reconnected_after_old_destination_is_deactivated(self):
        user = User.objects.create(email="chat-owner@example.test", full_name="Owner", role=UserRole.VENUE_USER)
        original = NotificationDestination.objects.create(
            created_by=user,
            audience=NotificationAudience.BUYER,
            channel=NotificationChannel.TELEGRAM,
            destination_type="group",
            chat_id="-1001234567890",
            title="Old role",
        )

        original.is_active = False
        original.save(update_fields=["is_active", "updated_at"])
        replacement = NotificationDestination.objects.create(
            created_by=user,
            audience=NotificationAudience.BUYER,
            channel=NotificationChannel.TELEGRAM,
            destination_type="group",
            chat_id="-1001234567890",
            title="Cafe group",
        )

        self.assertFalse(original.is_active)
        self.assertTrue(replacement.is_active)
        self.assertEqual(
            NotificationDestination.objects.filter(channel="telegram", chat_id="-1001234567890").count(), 2
        )


class NotificationOutboxTests(TestCase):
    def setUp(self):
        self.destination = create_destination()
        NotificationRule.objects.create(destination=self.destination, event=NotificationEvent.ORDER_SUBMITTED)

    def test_enqueue_is_idempotent_and_does_not_replace_original_payload(self):
        with patch("notifications.services.kick_notification_delivery") as kick:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                first = enqueue_notification(
                    destination=self.destination,
                    event=NotificationEvent.ORDER_SUBMITTED,
                    payload={"order_number": "A-1"},
                    idempotency_key="order-submitted:A-1:destination-1",
                )
                duplicate = enqueue_notification(
                    destination=self.destination,
                    event=NotificationEvent.ORDER_SUBMITTED,
                    payload={"order_number": "overwritten"},
                    idempotency_key="order-submitted:A-1:destination-1",
                )

        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(first.payload, {"order_number": "A-1"})
        self.assertEqual(len(callbacks), 1)
        kick.assert_called_once_with(first.pk)

    def test_disabled_rule_does_not_create_outbox_row(self):
        NotificationRule.objects.filter(destination=self.destination).update(is_enabled=False)

        delivery = enqueue_notification(
            destination=self.destination,
            event=NotificationEvent.ORDER_SUBMITTED,
            payload={"order_number": "A-2"},
            idempotency_key="order-submitted:A-2:destination-1",
        )

        self.assertIsNone(delivery)
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    def test_periodic_sweep_does_not_enqueue_an_already_queued_delivery_again(self):
        delivery = NotificationDelivery.objects.create(
            destination=self.destination,
            event=NotificationEvent.ORDER_SUBMITTED,
            payload={"order_number": "A-4"},
            idempotency_key="order-submitted:A-4:destination-1",
        )

        with patch("notifications.tasks.dispatch_notification_delivery.delay") as send_task:
            self.assertEqual(enqueue_pending_deliveries(), 1)
            self.assertEqual(enqueue_pending_deliveries(), 0)

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.queued_at)
        send_task.assert_called_once_with(str(delivery.pk))

    def test_destination_fanout_uses_a_bounded_number_of_queries(self):
        venue = Venue.objects.create(display_name="Fanout Cafe")
        destinations = [self.destination]
        for number in range(4):
            destinations.append(
                NotificationDestination.objects.create(
                    created_by=self.destination.created_by,
                    audience=NotificationAudience.BUYER,
                    channel=NotificationChannel.TELEGRAM,
                    destination_type="group",
                    chat_id=f"test-group-{number}",
                )
            )
        NotificationVenueScope.objects.bulk_create(
            [NotificationVenueScope(destination=destination, venue=venue) for destination in destinations]
        )
        NotificationRule.objects.bulk_create(
            [
                NotificationRule(destination=destination, event=NotificationEvent.SUPPLIER_RESPONSE)
                for destination in destinations
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            deliveries = enqueue_for_venue(
                venue_id=venue.id,
                event=NotificationEvent.SUPPLIER_RESPONSE,
                payload={"order_number": "FANOUT"},
                idempotency_key="supplier-response:fanout",
            )

        self.assertEqual(len(deliveries), 5)
        self.assertTrue(all(deliveries))
        self.assertLessEqual(len(queries), 5)

    def test_daily_checklist_waits_for_configured_moscow_time_and_enqueues_empty_day(self):
        venue = Venue.objects.create(display_name="Test Cafe")
        NotificationVenueScope.objects.create(destination=self.destination, venue=venue)
        NotificationRule.objects.create(
            destination=self.destination,
            event=NotificationEvent.DAILY_DELIVERY,
            send_time=time(9, 0),
        )
        for number in range(4):
            destination = NotificationDestination.objects.create(
                created_by=self.destination.created_by,
                audience=NotificationAudience.BUYER,
                channel=NotificationChannel.TELEGRAM,
                destination_type="group",
                chat_id=f"daily-group-{number}",
            )
            NotificationVenueScope.objects.create(destination=destination, venue=venue)
            NotificationRule.objects.create(
                destination=destination,
                event=NotificationEvent.DAILY_DELIVERY,
                send_time=time(9, 0),
            )
        today = date(2026, 9, 25)

        with patch(
            "notifications.services.timezone.localtime",
            return_value=datetime(2026, 9, 25, 8, 59, tzinfo=ZoneInfo("Europe/Moscow")),
        ):
            self.assertEqual(enqueue_daily_delivery_checklists(today=today), 0)

        with patch(
            "notifications.services.timezone.localtime",
            return_value=datetime(2026, 9, 25, 9, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ):
            with CaptureQueriesContext(connection) as queries:
                enqueue_daily_delivery_checklists(today=today)
            enqueue_daily_delivery_checklists(today=today)

        deliveries = NotificationDelivery.objects.filter(event=NotificationEvent.DAILY_DELIVERY)
        self.assertEqual(deliveries.count(), 5)
        self.assertTrue(
            all(
                delivery.payload == {"kind": "daily_delivery", "date": "2026-09-25", "deliveries": []}
                for delivery in deliveries
            )
        )
        self.assertLessEqual(len(queries), 10)

    def test_daily_checklist_does_not_send_late_after_configured_time(self):
        """Не отправлять ежедневный чек-лист при позднем запуске задачи."""
        venue = Venue.objects.create(display_name="Late Checklist Cafe")
        NotificationVenueScope.objects.create(destination=self.destination, venue=venue)
        NotificationRule.objects.create(
            destination=self.destination,
            event=NotificationEvent.DAILY_DELIVERY,
            send_time=time(9, 0),
        )

        with patch(
            "notifications.services.timezone.localtime",
            return_value=datetime(2026, 9, 25, 14, 9, tzinfo=ZoneInfo("Europe/Moscow")),
        ):
            self.assertEqual(enqueue_daily_delivery_checklists(today=date(2026, 9, 25)), 0)

        self.assertFalse(NotificationDelivery.objects.filter(event=NotificationEvent.DAILY_DELIVERY).exists())


class NotificationDeliveryConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.destination = create_destination()
        self.delivery = NotificationDelivery.objects.create(
            destination=self.destination,
            event=NotificationEvent.ORDER_SUBMITTED,
            payload={"order_number": "A-3"},
            idempotency_key="order-submitted:A-3:destination-1",
        )

    @override_settings(NOTIFICATION_DELIVERY_DRY_RUN=True)
    def test_repeated_dispatch_only_claims_and_sends_once(self):
        dispatch_notification_delivery(str(self.delivery.pk))
        dispatch_notification_delivery(str(self.delivery.pk))

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDeliveryStatus.SENT)
        self.assertEqual(self.delivery.attempts, 1)
        self.assertIsNone(self.delivery.lease_token)

    @override_settings(NOTIFICATION_DELIVERY_DRY_RUN=False)
    def test_expired_lease_cannot_be_overwritten_by_old_worker(self):
        def simulate_reclaim(delivery, text, keyboard):
            NotificationDelivery.objects.filter(pk=delivery.pk).update(
                status=NotificationDeliveryStatus.PENDING,
                lease_token=None,
            )
            return "provider-message-1"

        with patch("notifications.tasks.send_provider_message", side_effect=simulate_reclaim):
            dispatch_notification_delivery(str(self.delivery.pk))

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDeliveryStatus.PENDING)
        self.assertIsNone(self.delivery.provider_message_id)

    @override_settings(NOTIFICATION_DELIVERY_DRY_RUN=False)
    def test_parallel_dispatch_does_not_send_same_active_lease_twice(self):
        sending = Event()
        release = Event()

        def slow_send(delivery, text, keyboard):
            sending.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test send was not released")
            return "provider-message-2"

        with patch("notifications.tasks.send_provider_message", side_effect=slow_send) as send:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(dispatch_notification_delivery, str(self.delivery.pk))
                self.assertTrue(sending.wait(timeout=5), "first worker did not reach provider call")
                second = executor.submit(dispatch_notification_delivery, str(self.delivery.pk))
                second.result(timeout=5)
                release.set()
                first.result(timeout=10)

        self.delivery.refresh_from_db()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.delivery.status, NotificationDeliveryStatus.SENT)
        self.assertEqual(self.delivery.attempts, 1)
