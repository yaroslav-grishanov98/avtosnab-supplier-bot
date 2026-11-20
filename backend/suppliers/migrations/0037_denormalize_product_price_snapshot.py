# 2026-08-10: current_price/previous_price/price_changed_at used to be correlated
# Subquery annotations, recomputed from supplier_product_prices on every list request.
# Sorting by them forced Postgres to evaluate the subquery for every SupplierProduct
# row before it could sort — no index can back that, and it got measurably slow as
# the product/price-history tables grew (Витя reported occasional multi-second lag
# on GET /suppliers/products?ordering=current_price, see docs/wiki/current-status.md).
# Denormalized into plain columns here, backfilled from existing history, and kept
# in sync going forward by _sync_product_price_snapshot() in
# api/v1/suppliers/services.py (called after every price status/valid_to change).

from django.db import migrations, models

BACKFILL_CURRENT_PRICE_SQL = """
    UPDATE supplier_products sp
    SET current_price = cur.price
    FROM (
        SELECT DISTINCT ON (supplier_product_id) supplier_product_id, price
        FROM supplier_product_prices
        WHERE price_type = 'price_list' AND status = 'active' AND valid_to IS NULL
        ORDER BY supplier_product_id, valid_from DESC, created_at DESC
    ) cur
    WHERE cur.supplier_product_id = sp.id;
"""

BACKFILL_PREVIOUS_PRICE_SQL = """
    UPDATE supplier_products sp
    SET previous_price = prev.price, price_changed_at = prev.valid_to
    FROM (
        SELECT DISTINCT ON (supplier_product_id) supplier_product_id, price, valid_to
        FROM supplier_product_prices
        WHERE price_type = 'price_list' AND status = 'superseded'
        ORDER BY supplier_product_id, valid_to DESC, created_at DESC
    ) prev
    WHERE prev.supplier_product_id = sp.id;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0005_wishlist_wishlistitem_and_more"),
        ("core", "0011_seed_base_units"),
        ("suppliers", "0036_price_change_comment"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierproduct",
            name="current_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="supplierproduct",
            name="previous_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="supplierproduct",
            name="price_changed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="supplierproduct",
            index=models.Index(fields=["current_price"], name="sp_current_price_idx"),
        ),
        migrations.RunSQL(BACKFILL_CURRENT_PRICE_SQL, migrations.RunSQL.noop),
        migrations.RunSQL(BACKFILL_PREVIOUS_PRICE_SQL, migrations.RunSQL.noop),
    ]
