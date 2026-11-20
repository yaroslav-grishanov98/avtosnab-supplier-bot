"""Venue subdivisions and off-system suppliers."""

from django.db import IntegrityError, transaction

from core.models import (
    EntityStatus,
    OffSystemSupplier,
    VenueSubdivision,
)

from ._shared import _name_taken_error


@transaction.atomic
def create_venue_subdivision(*, venue, name):
    if VenueSubdivision.objects.filter(venue=venue, name=name, status=EntityStatus.ACTIVE).exists():
        raise _name_taken_error()
    try:
        with transaction.atomic():
            return VenueSubdivision.objects.create(venue=venue, name=name, status=EntityStatus.ACTIVE)
    except IntegrityError:
        raise _name_taken_error() from None


@transaction.atomic
def delete_venue_subdivision(subdivision):
    """Real hard delete (2026-08-27, Vitya) — mirrors delete_supplier_product's
    resolution of the same question for products. OrderItem.subdivision is
    SET_NULL, so historical orders keep their requested_name/qty text, just
    lose the department tag."""
    subdivision.delete()


@transaction.atomic
def create_off_system_supplier(
    *, venue, display_name, phone=None, comment=None, delivery_min_order_amount=None, delivery_currency="RUB"
):
    if OffSystemSupplier.objects.filter(venue=venue, display_name=display_name, status=EntityStatus.ACTIVE).exists():
        raise _name_taken_error("display_name")
    try:
        with transaction.atomic():
            return OffSystemSupplier.objects.create(
                venue=venue,
                display_name=display_name,
                phone=phone,
                comment=comment,
                delivery_min_order_amount=delivery_min_order_amount,
                delivery_currency=delivery_currency,
                status=EntityStatus.ACTIVE,
            )
    except IntegrityError:
        raise _name_taken_error("display_name") from None


@transaction.atomic
def update_off_system_supplier(supplier, **fields):
    if "display_name" in fields:
        conflict = OffSystemSupplier.objects.filter(
            venue_id=supplier.venue_id, display_name=fields["display_name"], status=EntityStatus.ACTIVE
        ).exclude(id=supplier.id)
        if conflict.exists():
            raise _name_taken_error("display_name")
    for field, value in fields.items():
        setattr(supplier, field, value)
    try:
        with transaction.atomic():
            supplier.save(update_fields=list(fields.keys()) + ["updated_at"])
    except IntegrityError:
        raise _name_taken_error("display_name") from None
    return supplier


@transaction.atomic
def delete_off_system_supplier(supplier):
    """Real hard delete (2026-08-27, Vitya) — same resolution as
    delete_supplier_product. OrderItem.off_system_supplier is SET_NULL."""
    supplier.delete()
