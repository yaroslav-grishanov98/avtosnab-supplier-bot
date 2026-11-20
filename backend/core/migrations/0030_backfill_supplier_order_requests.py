import uuid

from django.db import migrations


def _generate_number():
    return f"REQ-{uuid.uuid4().hex[:10].upper()}"


def backfill_supplier_order_requests(apps, schema_editor):
    """2026-09-15: core.0027 added SupplierOrderRequest but only wired
    OrderItem.supplier_request on the CREATE path (submit_venue_cart) — any
    Order submitted before this migration's own deploy has items with
    supplier_product set but supplier_request still null, so GET
    /suppliers/{id}/orders/{order_id} (looks up SupplierOrderRequest by
    order_id+supplier_id) 404s for every pre-existing order. Confirmed live
    on dev via VictoriaLogs: order 534e869c-... 404ing repeatedly right
    after the 0027-0029 deploy. This backfills one SupplierOrderRequest per
    (order, supplier) for every item still missing one, with status derived
    from the items' CURRENT state — mirrors _recompute_supplier_order_
    request_status/complete_order's cascade in api/v1/catalog/services.py,
    since these orders may already be fully resolved or even completed."""
    Order = apps.get_model("core", "Order")
    OrderItem = apps.get_model("core", "OrderItem")
    SupplierOrderRequest = apps.get_model("core", "SupplierOrderRequest")

    items = list(
        OrderItem.objects.filter(supplier_request__isnull=True, supplier_product__isnull=False).select_related(
            "order", "supplier_product"
        )
    )
    if not items:
        return

    items_by_order = {}
    for item in items:
        items_by_order.setdefault(item.order_id, []).append(item)

    orders_by_id = {order.id: order for order in Order.objects.filter(id__in=items_by_order.keys())}

    for order_id, order_items in items_by_order.items():
        order = orders_by_id[order_id]
        items_by_supplier = {}
        for item in order_items:
            items_by_supplier.setdefault(item.supplier_product.supplier_id, []).append(item)

        for supplier_id, supplier_items in items_by_supplier.items():
            if any(item.status == "submitted" for item in supplier_items):
                status = "submitted"
            else:
                fully_confirmed = sum(
                    1
                    for item in supplier_items
                    if item.status == "confirmed" and item.confirmed_qty == item.requested_qty
                )
                partially_confirmed = sum(
                    1
                    for item in supplier_items
                    if item.status == "confirmed" and item.confirmed_qty != item.requested_qty
                )
                if partially_confirmed:
                    status = "partially_confirmed"
                elif fully_confirmed == 0:
                    status = "declined"
                elif fully_confirmed == len(supplier_items):
                    status = "confirmed"
                else:
                    status = "partially_confirmed"
                # mirrors complete_order's cascade — a completed order's
                # non-declined requests are all "completed"
                if order.status == "completed" and status != "declined":
                    status = "completed"

            request = SupplierOrderRequest.objects.create(
                order_id=order_id,
                supplier_id=supplier_id,
                number=_generate_number(),
                status=status,
            )
            OrderItem.objects.filter(pk__in=[item.pk for item in supplier_items]).update(supplier_request=request)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_audit_log_order_events"),
    ]

    operations = [
        migrations.RunPython(backfill_supplier_order_requests, noop),
    ]
