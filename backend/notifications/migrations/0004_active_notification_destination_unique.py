from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0003_notificationdelivery_queued_at"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="notificationdestination",
            name="notif_dest_channel_chat_uniq",
        ),
        migrations.AddConstraint(
            model_name="notificationdestination",
            constraint=models.UniqueConstraint(
                fields=("channel", "chat_id"),
                condition=models.Q(is_active=True),
                name="notif_dest_active_channel_chat_uniq",
            ),
        ),
    ]
