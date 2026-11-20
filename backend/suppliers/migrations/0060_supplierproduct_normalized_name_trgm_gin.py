from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0059_supplierproductimportitem_parsed_snapshot_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="supplierproduct",
            index=GinIndex(
                fields=["normalized_name"],
                name="sp_normalized_name_trgm_gin",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
