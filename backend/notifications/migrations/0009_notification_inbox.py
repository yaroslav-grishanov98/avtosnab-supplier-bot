import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0040_order_item_unresolved_dismissed"),
        ("notifications", "0008_supplier_tender_distribution_event"),
    ]

    operations = [
        migrations.CreateModel(
            name="NotificationInboxEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("order_submitted", "Заявка отправлена поставщикам"),
                            ("supplier_response", "Ответ поставщика"),
                            ("supplier_request_cancelled", "Заявка отменена"),
                            ("daily_delivery", "Поставка на сегодня"),
                        ],
                        max_length=40,
                    ),
                ),
                ("idempotency_key", models.CharField(max_length=200, unique=True)),
                ("payload", models.JSONField(default=dict)),
                ("object_type", models.CharField(blank=True, default="", max_length=40)),
                ("object_id", models.CharField(blank=True, default="", max_length=64)),
                (
                    "venue",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_inbox_events",
                        to="core.venue",
                    ),
                ),
            ],
            options={
                "db_table": "notification_inbox_events",
                "indexes": [models.Index(fields=["venue", "created_at"], name="notif_inbox_venue_created_idx")],
            },
        ),
        migrations.CreateModel(
            name="NotificationInboxRead",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("read_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "notification",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="read_states",
                        to="notifications.notificationinboxevent",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_inbox_reads",
                        to="core.user",
                    ),
                ),
            ],
            options={"db_table": "notification_inbox_reads"},
        ),
        migrations.CreateModel(
            name="NotificationInboxDelivery",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "delivery",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inbox_links",
                        to="notifications.notificationdelivery",
                    ),
                ),
                (
                    "notification",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="delivery_links",
                        to="notifications.notificationinboxevent",
                    ),
                ),
            ],
            options={"db_table": "notification_inbox_deliveries"},
        ),
        migrations.AddField(
            model_name="notificationinboxevent",
            name="deliveries",
            field=models.ManyToManyField(
                related_name="inbox_events",
                through="notifications.NotificationInboxDelivery",
                to="notifications.notificationdelivery",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationinboxread",
            constraint=models.UniqueConstraint(
                fields=("notification", "user"), name="notif_inbox_read_user_uniq"
            ),
        ),
        migrations.AddIndex(
            model_name="notificationinboxread",
            index=models.Index(fields=["user", "read_at"], name="notif_inbox_read_user_idx"),
        ),
        migrations.AddConstraint(
            model_name="notificationinboxdelivery",
            constraint=models.UniqueConstraint(
                fields=("notification", "delivery"), name="notif_inbox_delivery_uniq"
            ),
        ),
    ]
