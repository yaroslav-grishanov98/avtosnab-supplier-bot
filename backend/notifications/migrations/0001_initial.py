import django.db.models.deletion
import django.utils.timezone
import notifications.models
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("core", "0038_auditlog_action_replaced"),
        ("suppliers", "0044_alter_supplierproductprice_supplier_product"),
    ]

    operations = [
        migrations.CreateModel(
            name="NotificationDestination",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("audience", models.CharField(choices=[("buyer", "Покупатель"), ("supplier", "Поставщик")], max_length=16)),
                ("channel", models.CharField(choices=[("telegram", "Telegram"), ("max", "MAX")], max_length=16)),
                (
                    "destination_type",
                    models.CharField(choices=[("personal", "Личный чат"), ("group", "Групповой чат")], max_length=16),
                ),
                ("chat_id", models.TextField()),
                ("title", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_destinations",
                        to="core.user",
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_destinations",
                        to="suppliers.supplier",
                    ),
                ),
            ],
            options={"db_table": "notification_destinations"},
        ),
        migrations.CreateModel(
            name="NotificationLinkToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("audience", models.CharField(choices=[("buyer", "Покупатель"), ("supplier", "Поставщик")], max_length=16)),
                ("channel", models.CharField(choices=[("telegram", "Telegram"), ("max", "MAX")], max_length=16)),
                ("destination_type", models.CharField(choices=[("personal", "Личный чат"), ("group", "Групповой чат")], default="personal", max_length=16)),
                ("token", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_link_tokens",
                        to="core.user",
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_link_tokens",
                        to="suppliers.supplier",
                    ),
                ),
            ],
            options={"db_table": "notification_link_tokens"},
        ),
        migrations.CreateModel(
            name="NotificationDelivery",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event",
                    models.CharField(
                        choices=[
                            ("order_submitted", "Заявка отправлена"),
                            ("supplier_response", "Ответ поставщика"),
                            ("daily_delivery", "Ежедневный чек-лист поставок"),
                            ("supplier_new_request", "Новая заявка поставщику"),
                            ("supplier_request_cancelled", "Заявка отменена заведением"),
                        ],
                        max_length=40,
                    ),
                ),
                ("idempotency_key", models.CharField(default=notifications.models.generate_idempotency_key, max_length=160, unique=True)),
                ("payload", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("pending", "Ожидает отправки"), ("processing", "Отправляется"), ("sent", "Отправлено"), ("failed", "Ошибка")], default="pending", max_length=16)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("available_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("provider_message_id", models.TextField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                (
                    "destination",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
                        to="notifications.notificationdestination",
                    ),
                ),
            ],
            options={"db_table": "notification_deliveries"},
        ),
        migrations.CreateModel(
            name="NotificationRule",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event",
                    models.CharField(
                        choices=[
                            ("order_submitted", "Заявка отправлена"),
                            ("supplier_response", "Ответ поставщика"),
                            ("daily_delivery", "Ежедневный чек-лист поставок"),
                            ("supplier_new_request", "Новая заявка поставщику"),
                            ("supplier_request_cancelled", "Заявка отменена заведением"),
                        ],
                        max_length=40,
                    ),
                ),
                ("is_enabled", models.BooleanField(default=True)),
                ("send_time", models.TimeField(default="09:00")),
                (
                    "destination",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rules",
                        to="notifications.notificationdestination",
                    ),
                ),
            ],
            options={"db_table": "notification_rules"},
        ),
        migrations.CreateModel(
            name="NotificationSupplierAction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action", models.CharField(choices=[("accept", "Принять"), ("partial", "Принять частично"), ("decline", "Отказать")], max_length=16)),
                ("response", models.JSONField(default=dict)),
                (
                    "supplier_request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_action",
                        to="core.supplierorderrequest",
                    ),
                ),
            ],
            options={"db_table": "notification_supplier_actions"},
        ),
        migrations.CreateModel(
            name="NotificationVenueScope",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "destination",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="venue_scopes",
                        to="notifications.notificationdestination",
                    ),
                ),
                (
                    "venue",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_scopes",
                        to="core.venue",
                    ),
                ),
            ],
            options={"db_table": "notification_venue_scopes"},
        ),
        migrations.AddField(
            model_name="notificationdestination",
            name="venues",
            field=models.ManyToManyField(
                related_name="notification_destinations",
                through="notifications.NotificationVenueScope",
                to="core.venue",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdestination",
            constraint=models.UniqueConstraint(fields=("channel", "chat_id"), name="notif_dest_channel_chat_uniq"),
        ),
        migrations.AddConstraint(
            model_name="notificationdestination",
            constraint=models.CheckConstraint(
                condition=models.Q(("audience", "buyer"), ("supplier__isnull", True))
                | models.Q(("audience", "supplier"), ("supplier__isnull", False)),
                name="notif_dest_audience_supplier_check",
            ),
        ),
        migrations.AddIndex(
            model_name="notificationdestination",
            index=models.Index(fields=["created_by", "audience", "is_active"], name="notif_dest_owner_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationlinktoken",
            index=models.Index(fields=["token", "expires_at"], name="notif_link_token_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationdelivery",
            index=models.Index(fields=["status", "available_at"], name="notif_delivery_pending_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationdelivery",
            index=models.Index(fields=["destination", "event", "created_at"], name="notif_delivery_dest_idx"),
        ),
        migrations.AddConstraint(
            model_name="notificationrule",
            constraint=models.UniqueConstraint(fields=("destination", "event"), name="notif_rule_dest_event_uniq"),
        ),
        migrations.AddConstraint(
            model_name="notificationvenuescope",
            constraint=models.UniqueConstraint(fields=("destination", "venue"), name="notif_scope_dest_venue_uniq"),
        ),
        migrations.AddIndex(
            model_name="notificationvenuescope",
            index=models.Index(fields=["venue", "destination"], name="notif_scope_venue_idx"),
        ),
    ]
