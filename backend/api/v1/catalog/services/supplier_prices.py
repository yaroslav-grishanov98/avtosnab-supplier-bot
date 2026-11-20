"""Supplier product prices per venue."""

import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from catalog.models import (
    SupplierProductVenuePrice,
    SupplierProductVenuePriceSourceType,
    SupplierProductVenuePriceStatus,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
)

from ._shared import _UNSET


class SupplierProductVenuePriceConflict(Exception):
    """2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §18-20): creating
    a price that overlaps an existing ACTIVE one for a venue must not
    silently supersede it — the caller has to see what's currently in
    effect and explicitly confirm the replacement (confirm_replace=True on
    retry), mirroring the exact 409-then-retry shape
    MasterProductVenueLinkConflict already established for TASK-33."""

    def __init__(self, conflicts):
        self.conflicts = conflicts  # list[SupplierProductVenuePrice], one per colliding venue
        super().__init__(f"{len(conflicts)} venue(s) already have an overlapping active price")


def _locked_conflicting_active_prices_by_venue(*, supplier_product, venue_ids, valid_from, valid_to, exclude_id=None):
    """Locks (SELECT ... FOR UPDATE) the ACTIVE rows, one per venue at most
    (venue is now a plain FK, not an M2M), that time-overlap the new window.
    Returns {venue_id: row}.

    exclude_id (2026-09-21 final-review fix wave, Fix 1): when re-checking a
    row that is itself ACTIVE and already occupies the venue slot being
    edited (update_supplier_product_venue_price), the row would otherwise
    trivially "conflict" with its own unsaved-yet period — pass its own id
    to leave it out of both the lock and the result."""
    overlap = Q(valid_to__isnull=True) | Q(valid_to__gt=valid_from)
    if valid_to is not None:
        overlap &= Q(valid_from__lt=valid_to)
    rows = (
        SupplierProductVenuePrice.objects.select_for_update()
        .filter(
            supplier_product=supplier_product,
            status=SupplierProductVenuePriceStatus.ACTIVE,
            venue_id__in=venue_ids,
        )
        .filter(overlap)
    )
    if exclude_id is not None:
        rows = rows.exclude(id=exclude_id)
    return {row.venue_id: row for row in rows}


def _supersede_price(*, row, valid_from):
    """Spec §6 rule 13 / §3.5's own note: no two ACTIVE
    SupplierProductVenuePrice rows may overlap in time for the same
    (supplier_product, venue) pair — enforced here, not as a DB constraint.
    Mirrors _apply_price() (api/v1/suppliers/services.py) and venues'
    _demote_other_active_owners() (api/v1/venues/services.py).

    2026-09-01 final-review fix wave (C2), still applies post-refactor:
    valid_to may only be moved to a point strictly after the row's own
    valid_from — the sp_venue_prices_period CheckConstraint requires
    valid_to IS NULL OR valid_to > valid_from. A row that starts at or after
    the new valid_from has no legal truncation point and is only marked
    ARCHIVED; a row that started earlier is truncated at the new valid_from.

    2026-09-07 refactor: one venue per row means superseding this row can
    never affect any *other* venue — the M2M-era "collateral damage" concern
    (_assert_supersede_allowed) no longer applies and has been removed."""
    update_fields = ["status", "updated_at"]
    row.status = SupplierProductVenuePriceStatus.ARCHIVED
    if row.valid_from < valid_from:
        row.valid_to = valid_from
        update_fields.append("valid_to")
    row.save(update_fields=update_fields)


@transaction.atomic
def set_supplier_product_venue_price(
    *,
    supplier_product,
    venues,
    price,
    created_by,
    currency="RUB",
    valid_from=None,
    valid_to=None,
    source_type=SupplierProductVenuePriceSourceType.MANUAL,
    source_reference=None,
    source_tender_offer=None,
    comment=None,
    confirm_replace=False,
):
    """2026-09-07 refactor (Павел's "Поставщик Изменения цен - ТЗ", §12):
    one negotiated price assigned to several venues in one call now creates
    one row *per venue* (was a single row with a `venues` M2M) sharing a
    fresh `group_id` — so a later change to just one of those venues only
    ever touches that venue's own row (§12's "изменить только Кафе 2").

    Raises SupplierProductVenuePriceConflict (not a silent supersede) when
    any target venue already has an overlapping ACTIVE price and the caller
    hasn't passed confirm_replace=True — the view turns that into a 409
    carrying the conflicting rows, exactly like TASK-33's
    MasterProductVenueLinkConflict. apply_tender_offer calls this with
    confirm_replace=True unconditionally: its own preview/apply cycle
    (master_product_tender_offer_diff) already is the confirmation step."""
    valid_from = valid_from or timezone.now()
    if valid_to is not None and valid_to <= valid_from:
        raise ValidationError({"valid_to": "Должно быть позже valid_from."})

    venue_ids = [v.id for v in venues]
    conflicts_by_venue = _locked_conflicting_active_prices_by_venue(
        supplier_product=supplier_product, venue_ids=venue_ids, valid_from=valid_from, valid_to=valid_to
    )
    if conflicts_by_venue and not confirm_replace:
        raise SupplierProductVenuePriceConflict(list(conflicts_by_venue.values()))

    group_id = uuid.uuid4()
    for conflict in conflicts_by_venue.values():
        _supersede_price(row=conflict, valid_from=valid_from)
    rows = SupplierProductVenuePrice.objects.bulk_create(
        [
            SupplierProductVenuePrice(
                supplier_product=supplier_product,
                venue=venue,
                group_id=group_id,
                price=price,
                currency=currency,
                valid_from=valid_from,
                valid_to=valid_to,
                source_type=source_type,
                source_reference=source_reference,
                source_tender_offer=source_tender_offer,
                comment=comment,
                created_by=created_by,
                status=SupplierProductVenuePriceStatus.ACTIVE,
            )
            for venue in venues
        ]
    )
    return rows


@transaction.atomic
def archive_supplier_product_venue_price(price, actor=None):
    """2026-09-01 final-review fix wave (C2): valid_to is closed at "now"
    only when that is a legal value for this row — the
    sp_venue_prices_period CheckConstraint requires
    valid_to IS NULL OR valid_to > valid_from, so a not-yet-started
    (future valid_from) price used to crash here with an IntegrityError
    (500). Such a row is simply marked ARCHIVED and keeps its window:
    resolve_supplier_product_price only ever looks at ACTIVE rows, so
    status alone is what takes it out of resolution. An already-expired
    valid_to is likewise left alone rather than being pushed forward."""
    price = SupplierProductVenuePrice.objects.select_for_update().get(pk=price.pk)
    if price.status != SupplierProductVenuePriceStatus.ACTIVE:
        raise ValidationError({"detail": "Эта цена уже не активна."})
    now = timezone.now()
    price.status = SupplierProductVenuePriceStatus.ARCHIVED
    update_fields = ["status", "updated_at"]
    if price.valid_from < now and (price.valid_to is None or price.valid_to > now):
        price.valid_to = now
        update_fields.append("valid_to")
    price.save(update_fields=update_fields)
    record_audit_log(
        actor=actor,
        action=AuditLogAction.ARCHIVED,
        entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE,
        entity_id=price.id,
        summary=f"{price.price} {price.currency} — {price.supplier_product.normalized_name}",
    )
    return price


def _require_editable_future_manual_price(price):
    """§29 ТЗ «Карточка товара поставщика»: редактирование/удаление доступно
    только «Будущей ручной цене» — valid_from ещё не наступил, status=active,
    source_type=manual. Цену из тендера или уже действующую/завершённую
    трогать нельзя (историческую цену не редактировать)."""
    if price.status != SupplierProductVenuePriceStatus.ACTIVE:
        raise ValidationError({"detail": "Эта цена уже не активна."})
    if price.source_type != SupplierProductVenuePriceSourceType.MANUAL:
        raise ValidationError({"detail": "Цену, назначенную из тендера, нельзя редактировать вручную."})
    if price.valid_from <= timezone.now():
        raise ValidationError({"detail": "Действие доступно только для ещё не наступившей цены."})


@transaction.atomic
def update_supplier_product_venue_price(
    price, *, actor=None, price_value=None, valid_from=None, valid_to=_UNSET, comment=_UNSET
):
    """2026-09-21 final-review fix wave (Fix 1/2/4/5):
    - valid_to/comment default to the module-level _UNSET sentinel (not
      None) so the view's explicit `{"valid_to": null}` — which DOES flow
      through as valid_to=None in validated_data — can be told apart from
      the caller simply not mentioning the field at all. A plain `is not
      None` check (the old code) silently treated both the same and could
      never clear an end date or a comment back to null.
    - a new valid_from must still be in the future — a PATCH that would
      make the row already-started is rejected rather than silently
      accepted (it would otherwise become instantly "active" without ever
      passing through the create path's conflict check).
    - the resulting window is re-checked against sibling ACTIVE prices for
      this row's own venue, mirroring set_supplier_product_venue_price's
      own conflict guard — this row is excluded from that check (it's the
      row that already owns the slot), and this endpoint has no
      confirm_replace escape hatch: a genuine conflict with a *different*
      row is just rejected."""
    price = SupplierProductVenuePrice.objects.select_for_update().get(pk=price.pk)
    _require_editable_future_manual_price(price)

    if valid_from is not None and valid_from <= timezone.now():
        raise ValidationError({"valid_from": "Новая дата начала должна остаться в будущем."})

    resulting_valid_from = valid_from if valid_from is not None else price.valid_from
    resulting_valid_to = valid_to if valid_to is not _UNSET else price.valid_to
    if resulting_valid_to is not None and resulting_valid_to <= resulting_valid_from:
        raise ValidationError({"valid_to": "Должно быть позже valid_from."})

    conflicts_by_venue = _locked_conflicting_active_prices_by_venue(
        supplier_product=price.supplier_product,
        venue_ids=[price.venue_id],
        valid_from=resulting_valid_from,
        valid_to=resulting_valid_to,
        exclude_id=price.id,
    )
    if conflicts_by_venue:
        raise SupplierProductVenuePriceConflict(list(conflicts_by_venue.values()))

    update_fields = ["updated_at"]
    if price_value is not None:
        price.price = price_value
        update_fields.append("price")
    if valid_from is not None:
        price.valid_from = valid_from
        update_fields.append("valid_from")
    if valid_to is not _UNSET:
        price.valid_to = valid_to
        update_fields.append("valid_to")
    if comment is not _UNSET:
        price.comment = comment or None
        update_fields.append("comment")

    price.full_clean(exclude=[f.name for f in price._meta.fields if f.name not in ("price", "valid_from", "valid_to")])
    price.save(update_fields=update_fields)
    record_audit_log(
        actor=actor,
        action=AuditLogAction.UPDATED,
        entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE,
        entity_id=price.id,
        summary=f"{price.price} {price.currency} — {price.supplier_product.normalized_name}",
    )
    return price


@transaction.atomic
def delete_future_supplier_product_venue_price(price, *, actor=None):
    price = SupplierProductVenuePrice.objects.select_for_update().get(pk=price.pk)
    _require_editable_future_manual_price(price)
    summary = f"{price.price} {price.currency} — {price.supplier_product.normalized_name}"
    price_id = price.id
    price.delete()
    record_audit_log(
        actor=actor,
        action=AuditLogAction.DELETED,
        entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE,
        entity_id=price_id,
        summary=summary,
    )
