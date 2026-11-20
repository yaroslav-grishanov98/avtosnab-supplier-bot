import django.db.models.deletion
from django.db import migrations, models


def backfill_cart_user(apps, schema_editor):
    """2026-09-08: VenueCart becomes account-scoped (`user`), venue-scoped
    lookup (`venue`) is now optional. Existing rows only have `venue` set —
    resolve the most plausible owning user (active owner/manager VenueUser
    membership, falling back to the legacy core.User.venue single-FK) and
    merge any accidental collisions (a user who already owned another
    venue's cart) so the new OneToOneField unique constraint never trips."""
    VenueCart = apps.get_model("core", "VenueCart")
    VenueCartItem = apps.get_model("core", "VenueCartItem")
    VenueCartContact = apps.get_model("core", "VenueCartContact")
    VenueUser = apps.get_model("core", "VenueUser")
    User = apps.get_model("core", "User")

    claimed_user_ids = set()
    for cart in VenueCart.objects.filter(venue_id__isnull=False).order_by("created_at"):
        membership = (
            VenueUser.objects.filter(venue_id=cart.venue_id, role="owner", status="active")
            .order_by("created_at")
            .first()
            or VenueUser.objects.filter(venue_id=cart.venue_id, role="manager", status="active")
            .order_by("created_at")
            .first()
        )
        user_id = membership.user_id if membership else None
        if user_id is None:
            legacy_user = User.objects.filter(venue_id=cart.venue_id, role="venue_user").order_by("created_at").first()
            user_id = legacy_user.id if legacy_user else None

        if user_id is None or user_id in claimed_user_ids:
            continue

        existing = VenueCart.objects.filter(user_id=user_id).first()
        if existing is not None:
            VenueCartItem.objects.filter(cart=cart).update(cart=existing)
            VenueCartContact.objects.filter(cart=cart).update(cart=existing)
            cart.delete()
        else:
            cart.user_id = user_id
            cart.save(update_fields=["user_id"])
        claimed_user_ids.add(user_id)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_orderitem_applied_price"),
    ]

    operations = [
        migrations.AlterField(
            model_name="venuecart",
            name="venue",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="carts",
                to="core.venue",
            ),
        ),
        migrations.AddField(
            model_name="venuecart",
            name="user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="cart",
                to="core.user",
            ),
        ),
        migrations.RunPython(backfill_cart_user, noop_reverse),
    ]
