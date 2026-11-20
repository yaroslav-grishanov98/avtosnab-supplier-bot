from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0046_catalogimportsession_parser_version_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="masterproduct",
            index=GinIndex(
                fields=["canonical_name"],
                name="mp_canonical_name_trgm_gin",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
