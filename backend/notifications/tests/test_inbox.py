from django.test import TestCase
from rest_framework.test import APIClient

from core.models import User, UserRole, Venue
from notifications.models import NotificationInboxEvent, NotificationInboxEventType, NotificationInboxRead
from notifications.services import record_venue_inbox_event


class NotificationInboxApiTests(TestCase):
    """Проверить ленту, личные отметки прочтения и границы доступа."""

    def setUp(self):
        """Подготовить сотрудника, его заведение и авторизованный API-клиент."""
        self.venue = Venue.objects.create(display_name="Cafe Inbox")
        self.user = User.objects.create(
            email="inbox-user@example.test",
            full_name="Cafe Employee",
            role=UserRole.VENUE_USER,
            venue=self.venue,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def create_event(self, key="order-submitted:inbox-1"):
        """Создать событие ленты с уникальным ключом идемпотентности."""
        return record_venue_inbox_event(
            venue_id=self.venue.id,
            event_type=NotificationInboxEventType.ORDER_SUBMITTED,
            payload={"order_number": "A-104"},
            idempotency_key=key,
            object_type="order",
            object_id="104",
        )

    def test_list_reports_unread_events_and_read_filter(self):
        """Вернуть доступные события и учитывать личный статус прочтения."""
        event = self.create_event()

        response = self.client.get("/api/v1/notifications/inbox")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1, response.data)
        self.assertEqual(response.data["unread_count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(event.id))
        self.assertFalse(response.data["results"][0]["is_read"])

        read_response = self.client.post(f"/api/v1/notifications/inbox/{event.id}/read")
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(NotificationInboxRead.objects.filter(notification=event, user=self.user).count(), 1)

        filtered = self.client.get("/api/v1/notifications/inbox", {"unread": "false"})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.data["count"], 1)
        self.assertEqual(filtered.data["unread_count"], 0)
        self.assertTrue(filtered.data["results"][0]["is_read"])

    def test_read_all_marks_only_events_for_accessible_venues(self):
        """Отметить все доступные сотруднику события прочитанными."""
        first = self.create_event()
        second = self.create_event("order-submitted:inbox-2")

        response = self.client.post("/api/v1/notifications/inbox/read-all", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["marked_read"], 2)
        self.assertEqual(NotificationInboxRead.objects.filter(user=self.user).count(), 2)
        self.assertEqual(
            set(NotificationInboxRead.objects.filter(user=self.user).values_list("notification_id", flat=True)),
            {first.id, second.id},
        )

    def test_user_cannot_read_event_from_another_venue(self):
        """Не раскрывать и не изменять уведомления чужого заведения."""
        other_venue = Venue.objects.create(display_name="Other Cafe")
        event = record_venue_inbox_event(
            venue_id=other_venue.id,
            event_type=NotificationInboxEventType.ORDER_SUBMITTED,
            payload={},
            idempotency_key="order-submitted:private-event",
        )

        response = self.client.post(f"/api/v1/notifications/inbox/{event.id}/read")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(NotificationInboxRead.objects.filter(notification=event, user=self.user).exists())

    def test_event_creation_is_idempotent(self):
        """Повторная запись того же события не создаёт вторую карточку."""
        first = self.create_event()
        duplicate = self.create_event()

        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(NotificationInboxEvent.objects.filter(idempotency_key="order-submitted:inbox-1").count(), 1)
