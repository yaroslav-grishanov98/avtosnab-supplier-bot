from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0006_notificationdestination_connected_fields_and_action_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationdeliveryreceipt",
            name="comment",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="notificationdeliveryreceipt",
            name="result",
            field=models.CharField(
                choices=[
                    ("complete", "Получена полностью"),
                    ("discrepancy", "Получена с расхождением"),
                ],
                default="complete",
                max_length=16,
            ),
        ),
    ]
