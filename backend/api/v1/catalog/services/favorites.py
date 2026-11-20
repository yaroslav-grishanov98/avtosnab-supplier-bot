"""Venue favorites and wishlists."""

from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from catalog.models import (
    VenueSupplierProduct,
    Wishlist,
    WishlistItem,
)
from core.models import (
    EntityStatus,
)


@transaction.atomic
def add_venue_favorite(*, venue, supplier_product, role=None, custom_name=None):
    obj, _ = VenueSupplierProduct.objects.update_or_create(
        venue=venue,
        supplier_product=supplier_product,
        defaults={
            "is_favorite": True,
            "status": EntityStatus.ACTIVE,
            "role": role,
            "custom_name": custom_name,
        },
    )
    return obj


@transaction.atomic
def remove_venue_favorite(favorite):
    favorite.status = EntityStatus.ARCHIVED
    favorite.save(update_fields=["status", "updated_at"])


def _wishlist_name_taken_error():
    return ValidationError({"name": "Список с таким названием уже есть."})


def _check_wishlist_name_free(*, user, name, exclude_id=None):
    existing = Wishlist.objects.filter(user=user, name=name, status=EntityStatus.ACTIVE)
    if exclude_id:
        existing = existing.exclude(id=exclude_id)
    if existing.exists():
        raise _wishlist_name_taken_error()


@transaction.atomic
def create_wishlist(*, user, name):
    _check_wishlist_name_free(user=user, name=name)
    # DB has a partial unique constraint on (user, name) for active wishlists
    # — the exists() check above prevents a *pre-existing* name collision,
    # but two near-simultaneous calls with the same new name can both pass
    # it before either commits. Without catching this, the loser hits an
    # unhandled IntegrityError (500) instead of the same friendly 400.
    try:
        with transaction.atomic():
            return Wishlist.objects.create(user=user, name=name, status=EntityStatus.ACTIVE)
    except IntegrityError:
        raise _wishlist_name_taken_error() from None


@transaction.atomic
def rename_wishlist(wishlist, name):
    _check_wishlist_name_free(user=wishlist.user, name=name, exclude_id=wishlist.id)
    wishlist.name = name
    try:
        with transaction.atomic():
            wishlist.save(update_fields=["name", "updated_at"])
    except IntegrityError:
        raise _wishlist_name_taken_error() from None
    return wishlist


@transaction.atomic
def archive_wishlist(wishlist):
    wishlist.status = EntityStatus.ARCHIVED
    wishlist.save(update_fields=["status", "updated_at"])
    wishlist.items.filter(status=EntityStatus.ACTIVE).update(status=EntityStatus.ARCHIVED)


@transaction.atomic
def add_wishlist_item(*, wishlist, supplier_product):
    obj, _ = WishlistItem.objects.update_or_create(
        wishlist=wishlist,
        supplier_product=supplier_product,
        defaults={"status": EntityStatus.ACTIVE},
    )
    return obj


@transaction.atomic
def remove_wishlist_item(item):
    item.status = EntityStatus.ARCHIVED
    item.save(update_fields=["status", "updated_at"])
