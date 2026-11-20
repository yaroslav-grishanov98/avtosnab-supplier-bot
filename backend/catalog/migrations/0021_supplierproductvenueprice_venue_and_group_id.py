import uuid

import django.db.models.deletion
from django.db import migrations, models


def split_venues_into_rows(apps, schema_editor):
    """2026-09-07: one venue per row (was M2M) — for each existing price row,
    keep the first linked venue on the row itself and create a sibling row
    (same group_id, same everything else) for every additional venue. A row
    with zero venues (shouldn't exist in practice — the API always required
    at least one) is deleted rather than left with venue=NULL, since the
    field is about to become non-nullable."""
    SupplierProductVenuePrice = apps.get_model("catalog", "SupplierProductVenuePrice")
    for price in SupplierProductVenuePrice.objects.prefetch_related("venues").all():
        venue_ids = list(price.venues.values_list("id", flat=True))
        if not venue_ids:
            price.delete()
            continue
        price.venue_id = venue_ids[0]
        price.save(update_fields=["venue"])
        for venue_id in venue_ids[1:]:
            SupplierProductVenuePrice.objects.create(
                supplier_product_id=price.supplier_product_id,
                venue_id=venue_id,
                group_id=price.group_id,
                price=price.price,
                currency=price.currency,
                valid_from=price.valid_from,
                valid_to=price.valid_to,
                source_type=price.source_type,
                source_reference=price.source_reference,
                comment=price.comment,
                created_by_id=price.created_by_id,
                status=price.status,
            )


def noop_reverse(apps, schema_editor):
    """Not reversible — the split rows can't be losslessly folded back into
    a single M2M row set (they may have since diverged independently)."""


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0020_venueproductmatrixitem_missing_from_latest_import"),
        ("core", "0022_merge_20260904_1442"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierproductvenueprice",
            name="group_id",
            field=models.UUIDField(
                default=uuid.uuid4,
                db_index=True,
                help_text="Связывает строки, созданные одной операцией назначения цены нескольким заведениям.",
            ),
        ),
        migrations.AddField(
            model_name="supplierproductvenueprice",
            name="venue",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="supplier_product_prices",
                to="core.venue",
            ),
        ),
        migrations.RunPython(split_venues_into_rows, noop_reverse),
        migrations.RemoveField(
            model_name="supplierproductvenueprice",
            name="venues",
        ),
        migrations.AlterField(
            model_name="supplierproductvenueprice",
            name="venue",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="supplier_product_prices",
                to="core.venue",
            ),
        ),
        migrations.AddIndex(
            model_name="supplierproductvenueprice",
            index=models.Index(
                fields=["supplier_product", "venue", "status"], name="spvp_product_venue_status_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="supplierproductvenueprice",
            index=models.Index(fields=["group_id"], name="spvp_group_idx"),
        ),
    ]
