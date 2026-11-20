import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0048_supplierproductimportitem_commit_guards"),
    ]

    operations = [
        migrations.AlterField(
            model_name="botuserbinding",
            name="supplier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="bot_bindings",
                to="suppliers.supplier",
            ),
        ),
    ]
