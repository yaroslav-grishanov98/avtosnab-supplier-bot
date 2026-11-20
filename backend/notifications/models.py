import uuid

from django.db import models
from django.utils import timezone

from core.models import UUIDTimestampedModel


def generate_idempotency_key():
    return str(uuid.uuid4())


class NotificationAudience(models.TextChoices):
    BUYER = "buyer", "Покупатель"
    SUPPLIER = "supplier", "Поставщик"


class NotificationChannel(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    MAX = "max", "MAX"


class NotificationDestinationType(models.TextChoices):
    PERSONAL = "personal", "Личный чат"
    GROUP = "group", "Групповой чат"


class NotificationEvent(models.TextChoices):
    ORDER_SUBMITTED = "order_submitted", "Заявка отправлена"
    SUPPLIER_RESPONSE = "supplier_response", "Ответ поставщика"
    DAILY_DELIVERY = "daily_delivery", "Ежедневный чек-лист поставок"
    SUPPLIER_NEW_REQUEST = "supplier_new_request", "Новая заявка поставщику"
    SUPPLIER_REQUEST_CANCELLED = "supplier_request_cancelled", "Заявка отменена заведением"
    SUPPLIER_TENDER_DISTRIBUTION = "supplier_tender_distribution", "Товар выбран в тендере"


class NotificationReceiptResult(models.TextChoices):
    """Результат проверки фактически полученной поставки."""

    COMPLETE = "complete", "Получена полностью"
    DISCREPANCY = "discrepancy", "Получена с расхождением"


class NotificationDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Ожидает отправки"
    PROCESSING = "processing", "Отправляется"
    SENT = "sent", "Отправлено"
    FAILED = "failed", "Ошибка"


class NotificationInboxEventType(models.TextChoices):
    ORDER_SUBMITTED = "order_submitted", "Заявка отправлена поставщикам"
    SUPPLIER_RESPONSE = "supplier_response", "Ответ поставщика"
    SUPPLIER_REQUEST_CANCELLED = "supplier_request_cancelled", "Заявка отменена"
    DAILY_DELIVERY = "daily_delivery", "Поставка на сегодня"


class NotificationSupplierActionType(models.TextChoices):
    ACCEPT = "accept", "Принять"
    PARTIAL = "partial", "Принять частично"
    DECLINE = "decline", "Отказать"


class NotificationDestination(UUIDTimestampedModel):
    created_by = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="notification_destinations")
    audience = models.CharField(max_length=16, choices=NotificationAudience.choices)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.CASCADE, blank=True, null=True, related_name="notification_destinations"
    )
    channel = models.CharField(max_length=16, choices=NotificationChannel.choices)
    destination_type = models.CharField(max_length=16, choices=NotificationDestinationType.choices)
    chat_id = models.TextField()
    title = models.TextField(blank=True, default="")
    connected_user_id = models.CharField(max_length=255, blank=True, default="")
    connected_username = models.CharField(max_length=255, blank=True, default="")
    connected_user_name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    venues = models.ManyToManyField(
        "core.Venue", through="NotificationVenueScope", related_name="notification_destinations"
    )

    class Meta:
        db_table = "notification_destinations"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "chat_id"],
                condition=models.Q(is_active=True),
                name="notif_dest_active_channel_chat_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(audience=NotificationAudience.BUYER, supplier__isnull=True)
                    | models.Q(audience=NotificationAudience.SUPPLIER, supplier__isnull=False)
                ),
                name="notif_dest_audience_supplier_check",
            ),
        ]
        indexes = [models.Index(fields=["created_by", "audience", "is_active"], name="notif_dest_owner_idx")]


class NotificationVenueScope(UUIDTimestampedModel):
    destination = models.ForeignKey(NotificationDestination, on_delete=models.CASCADE, related_name="venue_scopes")
    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="notification_scopes")

    class Meta:
        db_table = "notification_venue_scopes"
        constraints = [
            models.UniqueConstraint(fields=["destination", "venue"], name="notif_scope_dest_venue_uniq"),
        ]
        indexes = [models.Index(fields=["venue", "destination"], name="notif_scope_venue_idx")]


class NotificationRule(UUIDTimestampedModel):
    destination = models.ForeignKey(NotificationDestination, on_delete=models.CASCADE, related_name="rules")
    event = models.CharField(max_length=40, choices=NotificationEvent.choices)
    is_enabled = models.BooleanField(default=True)
    send_time = models.TimeField(default="09:00")

    class Meta:
        db_table = "notification_rules"
        constraints = [
            models.UniqueConstraint(fields=["destination", "event"], name="notif_rule_dest_event_uniq"),
        ]


class NotificationLinkToken(UUIDTimestampedModel):
    created_by = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="notification_link_tokens")
    audience = models.CharField(max_length=16, choices=NotificationAudience.choices)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.CASCADE, blank=True, null=True, related_name="notification_link_tokens"
    )
    channel = models.CharField(max_length=16, choices=NotificationChannel.choices)
    destination_type = models.CharField(max_length=16, choices=NotificationDestinationType.choices, default="personal")
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "notification_link_tokens"
        indexes = [models.Index(fields=["token", "expires_at"], name="notif_link_token_idx")]


class NotificationDelivery(UUIDTimestampedModel):
    destination = models.ForeignKey(NotificationDestination, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=40, choices=NotificationEvent.choices)
    idempotency_key = models.CharField(max_length=160, unique=True, default=generate_idempotency_key)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=NotificationDeliveryStatus.choices, default=NotificationDeliveryStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    queued_at = models.DateTimeField(blank=True, null=True)
    lease_token = models.UUIDField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    provider_message_id = models.TextField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "notification_deliveries"
        indexes = [
            models.Index(fields=["status", "available_at"], name="notif_delivery_pending_idx"),
            models.Index(fields=["destination", "event", "created_at"], name="notif_delivery_dest_idx"),
        ]


class NotificationInboxEvent(UUIDTimestampedModel):
    """Бизнес-событие заведения для ленты уведомлений сотрудников."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="notification_inbox_events")
    event_type = models.CharField(max_length=40, choices=NotificationInboxEventType.choices)
    idempotency_key = models.CharField(max_length=200, unique=True)
    payload = models.JSONField(default=dict)
    object_type = models.CharField(max_length=40, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    deliveries = models.ManyToManyField(
        NotificationDelivery,
        through="NotificationInboxDelivery",
        related_name="inbox_events",
    )

    class Meta:
        db_table = "notification_inbox_events"
        indexes = [models.Index(fields=["venue", "created_at"], name="notif_inbox_venue_created_idx")]


class NotificationInboxRead(UUIDTimestampedModel):
    """Хранит отметку о прочтении события конкретным сотрудником."""

    notification = models.ForeignKey(NotificationInboxEvent, on_delete=models.CASCADE, related_name="read_states")
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="notification_inbox_reads")
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "notification_inbox_reads"
        constraints = [
            models.UniqueConstraint(fields=["notification", "user"], name="notif_inbox_read_user_uniq"),
        ]
        indexes = [models.Index(fields=["user", "read_at"], name="notif_inbox_read_user_idx")]


class NotificationInboxDelivery(UUIDTimestampedModel):
    """Связывает бизнес-событие с конкретной отправкой в мессенджер."""

    notification = models.ForeignKey(NotificationInboxEvent, on_delete=models.CASCADE, related_name="delivery_links")
    delivery = models.ForeignKey(NotificationDelivery, on_delete=models.CASCADE, related_name="inbox_links")

    class Meta:
        db_table = "notification_inbox_deliveries"
        constraints = [
            models.UniqueConstraint(fields=["notification", "delivery"], name="notif_inbox_delivery_uniq"),
        ]


class NotificationSupplierAction(UUIDTimestampedModel):
    supplier_request = models.OneToOneField(
        "core.SupplierOrderRequest", on_delete=models.CASCADE, related_name="notification_action"
    )
    action = models.CharField(max_length=16, choices=NotificationSupplierActionType.choices)
    response = models.JSONField(default=dict)

    class Meta:
        db_table = "notification_supplier_actions"


class NotificationDeliveryReceipt(UUIDTimestampedModel):
    """Идемпотентная отметка о получении поставки из чек-листа чата."""

    destination = models.ForeignKey(NotificationDestination, on_delete=models.CASCADE, related_name="delivery_receipts")
    supplier_request = models.ForeignKey(
        "core.SupplierOrderRequest", on_delete=models.CASCADE, related_name="notification_delivery_receipts"
    )
    result = models.CharField(
        max_length=16,
        choices=NotificationReceiptResult.choices,
        default=NotificationReceiptResult.COMPLETE,
    )
    comment = models.TextField(blank=True, default="")
    received_at = models.DateTimeField(default=timezone.now)
    telegram_user_id = models.CharField(max_length=255, blank=True, default="")
    telegram_user_name = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "notification_delivery_receipts"
        constraints = [
            models.UniqueConstraint(
                fields=["destination", "supplier_request"],
                name="notif_receipt_destination_request_uniq",
            )
        ]
        indexes = [
            models.Index(fields=["supplier_request", "received_at"], name="notif_receipt_request_idx"),
        ]


class NotificationActionLog(UUIDTimestampedModel):
    """История подключений чатов и действий пользователей в уведомлениях."""

    destination = models.ForeignKey(
        NotificationDestination,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="action_logs",
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="notification_action_logs",
    )
    channel = models.CharField(max_length=16, choices=NotificationChannel.choices)
    destination_type = models.CharField(max_length=16, choices=NotificationDestinationType.choices)
    chat_id = models.TextField()
    action = models.CharField(max_length=64)
    actor_id = models.CharField(max_length=255, blank=True, default="")
    actor_name = models.CharField(max_length=255, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "notification_action_logs"
        indexes = [
            models.Index(fields=["created_by", "created_at"], name="notif_action_owner_idx"),
            models.Index(fields=["chat_id", "created_at"], name="notif_action_chat_idx"),
        ]
