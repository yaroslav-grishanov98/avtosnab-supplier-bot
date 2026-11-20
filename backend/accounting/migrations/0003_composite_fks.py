from django.db import migrations

from accounting.migrations._composite import composite_fk

WAREHOUSE_KEY = ("id", "connection_id", "venue_id")
ROW_WAREHOUSE_KEY = ("warehouse_id", "connection_id", "venue_id")
PRODUCT_KEY = ("id", "connection_id")
ROW_PRODUCT_KEY = ("product_id", "connection_id")
LINK_KEY = ("connection_id", "venue_id")


class Migration(migrations.Migration):
    """docs/adr/0020: tenant isolation in the database.

    A stock, snapshot or consumption row carries the connection and venue of its
    warehouse (and its product's connection); a warehouse or department venue is
    linked to the connection. Link status and which venue owns a warehouse are
    not checked here; those are service decisions.
    """

    dependencies = [
        ("accounting", "0002_inventory"),
    ]

    operations = [
        composite_fk(
            "accounting_departments", "acc_dept_conn_venue_link_fk", LINK_KEY, "accounting_venue_links", LINK_KEY
        ),
        composite_fk(
            "accounting_warehouses", "acc_wh_conn_venue_link_fk", LINK_KEY, "accounting_venue_links", LINK_KEY
        ),
        composite_fk(
            "accounting_stock_snapshots",
            "acc_snap_wh_conn_venue_fk",
            ROW_WAREHOUSE_KEY,
            "accounting_warehouses",
            WAREHOUSE_KEY,
        ),
        composite_fk(
            "accounting_stock_levels",
            "acc_level_wh_conn_venue_fk",
            ROW_WAREHOUSE_KEY,
            "accounting_warehouses",
            WAREHOUSE_KEY,
        ),
        composite_fk(
            "accounting_stock_levels",
            "acc_level_snap_conn_venue_fk",
            ("snapshot_id", "connection_id", "venue_id"),
            "accounting_stock_snapshots",
            ("id", "connection_id", "venue_id"),
        ),
        composite_fk(
            "accounting_stock_levels", "acc_level_product_conn_fk", ROW_PRODUCT_KEY, "accounting_products", PRODUCT_KEY
        ),
        composite_fk(
            "accounting_consumption_daily",
            "acc_cons_wh_conn_venue_fk",
            ROW_WAREHOUSE_KEY,
            "accounting_warehouses",
            WAREHOUSE_KEY,
        ),
        composite_fk(
            "accounting_consumption_daily",
            "acc_cons_product_conn_fk",
            ROW_PRODUCT_KEY,
            "accounting_products",
            PRODUCT_KEY,
        ),
        composite_fk(
            "accounting_consumption_coverage",
            "acc_cov_wh_conn_venue_fk",
            ROW_WAREHOUSE_KEY,
            "accounting_warehouses",
            WAREHOUSE_KEY,
        ),
    ]
