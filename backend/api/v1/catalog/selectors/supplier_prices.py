"""Supplier product price reads."""

from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from catalog.models import (
    SupplierProductVenuePrice,
    SupplierProductVenuePriceStatus,
)


def resolve_supplier_product_price(*, supplier_product, venue_id):
    """Spec §6 rule 12: read-time price resolution, never denormalized —
    individual price wins over SupplierProduct.current_price when an ACTIVE
    SupplierProductVenuePrice covers this venue and
    valid_from <= now < valid_to (or valid_to is null).

    2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §39-45): returns the
    matched row's id as a third element (None when falling back to the base
    price) — callers that need to snapshot *which* price was applied
    (submit_venue_cart) use it; callers that only display a number
    (master_product_venue_breakdown/_tender_offer_diff) can keep indexing/
    unpacking just the first two."""
    now = timezone.now()
    individual = (
        SupplierProductVenuePrice.objects.filter(
            supplier_product=supplier_product,
            venue_id=venue_id,
            status=SupplierProductVenuePriceStatus.ACTIVE,
            valid_from__lte=now,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .order_by("-valid_from")
        .first()
    )
    if individual is not None:
        return individual.price, individual.currency, individual.id
    return supplier_product.current_price, "RUB", None


def supplier_product_venue_price_history_queryset(*, supplier_product_id, venue_id):
    return (
        SupplierProductVenuePrice.objects.filter(supplier_product_id=supplier_product_id, venue_id=venue_id)
        .select_related("venue", "source_tender_offer__tender_item")
        .order_by("-valid_from", "-created_at")
    )


def supplier_product_individual_prices_queryset(*, supplier_product_id, venue_id=None, group_id=None):
    """2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §8): the ЛК
    поставщика table lists every individual price of one товар across every
    заведение it's been given to — unlike supplier_product_venue_price_
    history_queryset (venue-side, always scoped to exactly one venue). A row
    created by one "assign to several venues" batch shares a group_id (§12/
    §46) — the frontend groups by it to show one line until conditions for
    one venue diverge from the rest; group_id is an optional extra filter
    here so the same query can also answer "just this one group"."""
    qs = SupplierProductVenuePrice.objects.filter(supplier_product_id=supplier_product_id).select_related(
        "venue", "source_tender_offer__tender_item"
    )
    if venue_id is not None:
        qs = qs.filter(venue_id=venue_id)
    if group_id is not None:
        qs = qs.filter(group_id=group_id)
    return qs.order_by("venue_id", "-valid_from", "-created_at")


def supplier_product_venue_price_confirmed_qty(price_id):
    """§42-45 of the doc: "Подтверждено по этой цене" — sum of confirmed_qty
    across every OrderItem snapshotted to this exact price row, using the
    same per-status rule already encoded by confirmed_qty itself (accept_
    supplier_order_items writes confirmed_qty=None for a cancelled/still-
    submitted item, the full or partial confirmed amount otherwise) — no
    separate status table needed, just sum what's already there. Deliberately
    keyed by applied_individual_price_id, never by date/amount matching
    (§45: "не пытаться восстанавливать применение цены по датам")."""
    from core.models import OrderItem

    return OrderItem.objects.filter(applied_individual_price_id=price_id).aggregate(
        total=Coalesce(Sum("confirmed_qty"), Decimal("0"))
    )["total"]


def individual_price_order_history_queryset(
    *, price_id, venue_id=None, date_from=None, date_to=None, order_number=None
):
    """§26-27 ТЗ «Карточка товара поставщика»: построчная история заявок по
    ОДНОЙ индивидуальной цене — тот же принцип, что и агрегат
    supplier_product_venue_price_confirmed_qty (строго по прямой ссылке
    applied_individual_price_id, не по датам/суммам). §27: фильтр по
    заведению, периоду, поиск по номеру заявки — новых статусов заявки не
    придумывать."""
    from core.models import OrderItem

    qs = OrderItem.objects.filter(applied_individual_price_id=price_id).select_related("order", "order__venue")
    if venue_id is not None:
        qs = qs.filter(order__venue_id=venue_id)
    if date_from is not None:
        qs = qs.filter(order__created_at__gte=date_from)
    if date_to is not None:
        qs = qs.filter(order__created_at__lte=date_to)
    if order_number:
        qs = qs.filter(order__order_number__icontains=order_number)
    return qs.order_by("-order__created_at")
