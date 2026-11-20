from django.db import migrations

# Codes used to be "max existing code + 1": two suppliers importing at the same time got the same code, a hard-deleted
# code was handed out again, and string ordering put AS-999999 above AS-1000000. A sequence is atomic and never
# goes back. Its start is the largest numeric AS-code already stored.
CREATE_SEQUENCE = """
CREATE SEQUENCE IF NOT EXISTS suppliers_system_sku_seq;
SELECT setval(
    'suppliers_system_sku_seq',
    COALESCE(
        (
            SELECT MAX(substring(system_sku FROM 4)::bigint)
            FROM supplier_products
            WHERE system_sku ~ '^AS-[0-9]+$'
        ),
        0
    ) + 1,
    false
);
"""

DROP_SEQUENCE = "DROP SEQUENCE IF EXISTS suppliers_system_sku_seq;"


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0063_supplierproductimportsession_warnings"),
    ]

    operations = [
        migrations.RunSQL(CREATE_SEQUENCE, reverse_sql=DROP_SEQUENCE),
    ]
