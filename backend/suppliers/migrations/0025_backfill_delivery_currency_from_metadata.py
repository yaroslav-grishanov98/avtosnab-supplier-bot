# Data-only migration, no schema change.
#
# The legacy HTML cabinet (templates/cabinet.html, api/v1/views.py::cabinet_update_proxy)
# used to save the "Валюта мин. заказа" dropdown only into Supplier.metadata["min_order_currency"]
# (a JSONField), never into a real column — Supplier.currency/delivery_currency didn't exist
# for that form's amount field until 2026-07-28. Fixed to write Supplier.delivery_currency
# directly (see docs/wiki/logs/2026-07-28-delivery-pickup-currency-split.md), but suppliers who
# already saved a non-default currency through the old form would otherwise keep showing the
# migration-0024 default ("RUB") on delivery_currency forever, since nothing re-reads metadata
# after this fix ships. Backfill once: only touches suppliers still at the untouched default, so
# it never clobbers a value already set through the newer DRF API path (create_supplier_by_owner/
# _by_staff, which writes delivery_currency directly and could differ from a stale metadata value).

from django.db import migrations


def backfill_delivery_currency_from_metadata(apps, schema_editor):
    Supplier = apps.get_model("suppliers", "Supplier")
    for supplier in Supplier.objects.filter(delivery_currency="RUB").exclude(metadata={}):
        legacy_currency = (supplier.metadata or {}).get("min_order_currency")
        if legacy_currency and legacy_currency != "RUB":
            supplier.delivery_currency = legacy_currency
            supplier.save(update_fields=["delivery_currency"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0024_remove_supplier_currency_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_delivery_currency_from_metadata, noop_reverse),
    ]
