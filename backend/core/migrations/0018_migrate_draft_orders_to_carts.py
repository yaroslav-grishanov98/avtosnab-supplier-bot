# Data-only migration, no schema change (schema already added in 0017).
#
# 2026-08-28: Cart/Order split (Vitya — "cart и order это разные бизнес
# сущности... это архитектурное нарушение"). Before OrderStatus.DRAFT is
# retired from the app-level choices, any existing Order rows still sitting
# in status="draft" (the old Order-as-cart representation, e.g. on dev from
# earlier testing) need to become VenueCart/VenueCartItem instead of just
# being silently orphaned with an out-of-choices status string.
#
# Multiple draft Orders for the same venue (shouldn't happen via the API's
# own get_or_create_venue_cart, but the now-removed POST /venues/{id}/orders
# could have created more than one over time) all merge into that venue's
# single VenueCart — first draft Order's comment/requested_delivery_date
# wins for the cart-level fields, all items from every draft Order land in
# the same cart.
from django.db import migrations


def migrate_draft_orders_to_carts(apps, schema_editor):
    Order = apps.get_model("core", "Order")
    VenueCart = apps.get_model("core", "VenueCart")
    VenueCartItem = apps.get_model("core", "VenueCartItem")

    draft_orders = Order.objects.filter(status="draft").prefetch_related("items")
    for order in draft_orders:
        cart, _ = VenueCart.objects.get_or_create(
            venue_id=order.venue_id,
            defaults={
                "comment": order.comment,
                "requested_delivery_date": order.requested_delivery_date,
            },
        )
        for item in order.items.all():
            VenueCartItem.objects.create(
                cart=cart,
                supplier_product_id=item.supplier_product_id,
                off_system_supplier_id=item.off_system_supplier_id,
                subdivision_id=item.subdivision_id,
                requested_name=item.requested_name,
                requested_qty=item.requested_qty,
                requested_unit_id=item.requested_unit_id,
                comment=item.comment,
            )
        order.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_alter_order_status_alter_orderitem_status_venuecart_and_more"),
    ]

    operations = [
        migrations.RunPython(migrate_draft_orders_to_carts, migrations.RunPython.noop),
    ]
