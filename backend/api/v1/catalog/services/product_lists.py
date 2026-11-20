"""Venue product lists."""

from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from catalog.models import (
    VenueProductList,
)
from core.models import (
    EntityStatus,
)

from ._shared import _name_taken_error


def _check_venue_product_list_name_free(*, venue, name, exclude_id=None):
    existing = VenueProductList.objects.filter(venue=venue, name=name, status=EntityStatus.ACTIVE)
    if exclude_id:
        existing = existing.exclude(id=exclude_id)
    if existing.exists():
        raise _name_taken_error()


@transaction.atomic
def create_venue_product_list(*, venue, name):
    _check_venue_product_list_name_free(venue=venue, name=name)
    try:
        with transaction.atomic():
            return VenueProductList.objects.create(venue=venue, name=name, status=EntityStatus.ACTIVE)
    except IntegrityError:
        raise _name_taken_error() from None


@transaction.atomic
def rename_venue_product_list(product_list, name):
    _check_venue_product_list_name_free(venue=product_list.venue, name=name, exclude_id=product_list.id)
    product_list.name = name
    try:
        with transaction.atomic():
            product_list.save(update_fields=["name", "updated_at"])
    except IntegrityError:
        raise _name_taken_error() from None
    return product_list


@transaction.atomic
def archive_venue_product_list(product_list):
    """Only flips this list's own status — see this plan's Global
    Constraints for why this must NOT cascade to VenueProductMatrixItem
    rows or their `lists` M2M, unlike archive_wishlist()."""
    product_list.status = EntityStatus.ARCHIVED
    product_list.save(update_fields=["status", "updated_at"])


@transaction.atomic
def add_matrix_item_to_list(*, product_list, item):
    if item.venue_id != product_list.venue_id:
        raise ValidationError({"item_id": "Товар принадлежит другому заведению."})
    item.lists.add(product_list)
    return item


@transaction.atomic
def remove_matrix_item_from_list(*, product_list, item):
    item.lists.remove(product_list)
