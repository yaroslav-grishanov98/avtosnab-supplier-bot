from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0029_alter_supplierproductvenueprice_supplier_product_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="venueproductmatrixitem",
            index=GinIndex(
                fields=["requested_name"],
                name="vpmi_requested_name_trgm_gin",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
