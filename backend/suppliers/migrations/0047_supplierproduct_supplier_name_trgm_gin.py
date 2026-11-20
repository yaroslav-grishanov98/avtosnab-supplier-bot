from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0046_supplierproductimportsession_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="supplierproduct",
            index=GinIndex(
                fields=["supplier_name"],
                name="sp_supplier_name_trgm_gin",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
