"""Venue order templates."""

from django.db import IntegrityError, transaction

from core.models import (
    EntityStatus,
    OrderTemplate,
    OrderTemplateItem,
)

from ._shared import _name_taken_error
from .orders import add_cart_item, get_or_create_cart


@transaction.atomic
def save_cart_as_template(*, cart, name):
    """2026-09-18: `venue` is `cart.venue` now, not a separate param — the
    cart is (user, venue)-scoped again, so it already carries the venue
    OrderTemplate needs."""
    if OrderTemplate.objects.filter(venue_id=cart.venue_id, name=name, status=EntityStatus.ACTIVE).exists():
        raise _name_taken_error()
    try:
        with transaction.atomic():
            template = OrderTemplate.objects.create(venue=cart.venue, name=name, status=EntityStatus.ACTIVE)
    except IntegrityError:
        raise _name_taken_error() from None

    OrderTemplateItem.objects.bulk_create(
        [
            OrderTemplateItem(
                template=template,
                supplier_product=item.supplier_product,
                off_system_supplier=item.off_system_supplier,
                subdivision=item.subdivision,
                requested_name=item.requested_name,
                requested_qty=item.requested_qty,
                requested_unit=item.requested_unit,
                comment=item.comment,
            )
            for item in cart.items.all()
        ]
    )
    return template


@transaction.atomic
def apply_order_template(*, template, user):
    """2026-08-27 (Vitya): adds the template's items into the user's cart
    (get_or_create_cart) instead of always spinning up a brand new Order —
    "точно нужно создавать заявку а не добавлять в корзину?".
    2026-09-18: cart is (user, venue)-scoped again — `venue` is derived
    from `template.venue` (the template is, and always was, venue-scoped),
    so the caller only needs to pass `user`; this also guarantees the
    template's items land in that same venue's cart, never another one."""
    cart = get_or_create_cart(user, template.venue)
    for item in template.items.all():
        add_cart_item(
            cart=cart,
            supplier_product=item.supplier_product,
            requested_name=item.requested_name,
            requested_qty=item.requested_qty,
            requested_unit=item.requested_unit,
            comment=item.comment,
            subdivision=item.subdivision,
            off_system_supplier=item.off_system_supplier,
        )
    return cart


@transaction.atomic
def delete_order_template(template):
    """Real hard delete (2026-08-27, Vitya) — OrderTemplateItem.template is
    CASCADE, so this removes the template's own items with it. Nothing else
    references an OrderTemplate."""
    template.delete()
