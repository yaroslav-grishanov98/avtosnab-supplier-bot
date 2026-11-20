import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_merge_20260904_1442"),
        ("catalog", "0021_supplierproductvenueprice_venue_and_group_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="applied_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="applied_price_type",
            field=models.CharField(
                blank=True,
                choices=[("base", "Базовая"), ("individual", "Индивидуальная")],
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="applied_individual_price",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="order_items",
                to="catalog.supplierproductvenueprice",
            ),
        ),
        migrations.AddIndex(
            model_name="orderitem",
            index=models.Index(fields=["applied_individual_price"], name="oi_applied_ind_price_idx"),
        ),
    ]
