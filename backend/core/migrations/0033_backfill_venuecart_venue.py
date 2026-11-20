from django.db import migrations


def backfill_venuecart_venue(apps, schema_editor):
    """2026-09-18: VenueCart.venue goes back to being required (reversal of
    0024_venuecart_user, which made it optional when the cart became
    account-scoped). `venue` is always NULL in practice at this point — no
    code has set it since 0024. For every existing cart, try to resolve a
    real venue from its items' subdivision/off_system_supplier (the only
    per-item venue signal that exists — plain supplier_product items carry
    none), falling back to VenueUser membership when items give no signal
    at all. When resolution is ambiguous (items imply more than one venue,
    or the user manages zero/multiple venues with no item-level signal),
    the cart is deleted outright rather than guessed at — it's unsent,
    in-progress state, not a record of fact like Order; the next
    get_or_create_cart(user, venue) call rebuilds an empty one on demand.
    Guessing wrong here would risk silently attributing a future order to
    the wrong venue, which is strictly worse than losing an unsent draft."""
    VenueCart = apps.get_model("core", "VenueCart")
    VenueCartItem = apps.get_model("core", "VenueCartItem")
    VenueUser = apps.get_model("core", "VenueUser")

    for cart in VenueCart.objects.all():
        items = list(VenueCartItem.objects.filter(cart=cart).select_related("subdivision", "off_system_supplier"))

        venue_ids = set()
        for item in items:
            if item.subdivision_id:
                venue_ids.add(item.subdivision.venue_id)
            if item.off_system_supplier_id:
                venue_ids.add(item.off_system_supplier.venue_id)

        if len(venue_ids) == 1:
            (venue_id,) = venue_ids
        elif not venue_ids:
            managed = list(
                VenueUser.objects.filter(
                    user_id=cart.user_id, role__in=["owner", "manager"], status="active"
                ).values_list("venue_id", flat=True)
            )
            managed_venue_ids = set(managed)
            venue_id = managed_venue_ids.pop() if len(managed_venue_ids) == 1 else None
        else:
            venue_id = None

        if venue_id is None:
            cart.delete()
            continue

        cart.venue_id = venue_id
        cart.save(update_fields=["venue_id"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_venueimage"),
    ]

    operations = [
        migrations.RunPython(backfill_venuecart_venue, noop_reverse),
    ]
