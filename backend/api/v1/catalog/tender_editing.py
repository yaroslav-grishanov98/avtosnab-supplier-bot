"""Raw ТЗ §14/§16, docs/adr/0009: edits of an existing tender. A draft is
invisible to suppliers and is edited freely (§14), keeping the create-time
access/invites rule. An active tender is only extended (§16) — the deadline
moves later, invited suppliers and item venues are only added. A value equal
to the current one is not an edit: the buyer's form sends everything it
shows. Callers run sync_tender_completion first, so a passed deadline is
already COMPLETED."""

from django.db import transaction
from rest_framework.exceptions import ValidationError

from api.v1.catalog.selectors import _owner_active_venue_ids, find_active_tender_conflicts, venue_label
from api.v1.catalog.services import ActiveTenderConflict, _reject_archived_venues
from api.v1.catalog.tender_history import display_names, display_value, record_field_changes
from catalog.models import TenderAccess, TenderChangedField, TenderStatus

COMPLETED_TENDER_MESSAGE = "Тендер завершён — редактировать нельзя."
DEADLINE_MESSAGE = "Срок приёма можно только продлить или снять; сократить или поставить срок тендеру без срока нельзя."
ACCESS_MESSAGE = "Доступ опубликованного тендера не меняется."
OPEN_INVITES_MESSAGE = "Недопустимо при access=open."
INVITES_REQUIRED_MESSAGE = "Обязательно и непусто при access=invite_only."
INVITES_REMOVED_MESSAGE = "Приглашённых поставщиков можно только добавить: передайте всех уже приглашённых."
VENUES_REMOVED_MESSAGE = "Заведения позиции можно только добавить: передайте все текущие."
DELETE_PUBLISHED_ITEM_MESSAGE = (
    "Удалить товар можно только из черновика; в опубликованном тендере снимите его с публикации."
)


def _reject_completed(tender):
    if tender.status == TenderStatus.COMPLETED:
        raise ValidationError({"detail": COMPLETED_TENDER_MESSAGE})


def _check_published_edit(tender, data):
    if "access" in data and data["access"] != tender.access:
        raise ValidationError({"access": ACCESS_MESSAGE})
    deadline, current = data.get("accepts_offers_until"), tender.accepts_offers_until
    if deadline is None or deadline == current:
        return
    if current is None or deadline < current:
        raise ValidationError({"accepts_offers_until": DEADLINE_MESSAGE})


def _resulting_invites(tender, data, current, *, extend_only):
    """None when neither access nor the invite list is in the request."""
    if "access" not in data and "invited_suppliers" not in data:
        return None
    if data.get("access", tender.access) == TenderAccess.OPEN:
        if data.get("invited_suppliers"):
            raise ValidationError({"invited_suppliers": OPEN_INVITES_MESSAGE})
        return []
    requested = list(data["invited_suppliers"]) if "invited_suppliers" in data else current
    if not requested:
        raise ValidationError({"invited_suppliers": INVITES_REQUIRED_MESSAGE})
    if extend_only and {s.id for s in current} - {s.id for s in requested}:
        raise ValidationError({"invited_suppliers": INVITES_REMOVED_MESSAGE})
    return requested


@transaction.atomic
def update_tender(*, tender, data, actor=None):
    _reject_completed(tender)
    is_draft = tender.status == TenderStatus.DRAFT
    if not is_draft:
        _check_published_edit(tender, data)
    current_invites = list(tender.invited_suppliers.all())
    invites = _resulting_invites(tender, data, current_invites, extend_only=not is_draft)

    changed = [
        field
        for field in ("name", "description", "accepts_offers_until", "access")
        if field in data and data[field] != getattr(tender, field)
    ]
    history = [
        (field, display_value(getattr(tender, field)), display_value(data[field]))
        for field in changed
        if field in TenderChangedField.values
    ]
    for field in changed:
        setattr(tender, field, data[field])
    if changed:
        tender.save(update_fields=[*changed, "updated_at"])
    if invites is not None and {s.id for s in invites} != {s.id for s in current_invites}:
        tender.invited_suppliers.set(invites)
        history.append(
            (
                TenderChangedField.INVITED_SUPPLIERS,
                display_names(s.display_name for s in current_invites),
                display_names(s.display_name for s in invites),
            )
        )
    record_field_changes(tender, history, actor=actor)
    return tender


def _resulting_venue_ids(item, venue_ids, current, *, extend_only):
    tender = item.tender
    current_ids = {venue.id for venue in current}
    requested = set(venue_ids)
    if extend_only and current_ids - requested:
        raise ValidationError({"venue_ids": VENUES_REMOVED_MESSAGE})
    added = sorted(requested - current_ids, key=str)
    owner_venue_ids = {row["venue_id"] for row in _owner_active_venue_ids(tender.created_by_id)}
    foreign = [venue_id for venue_id in added if venue_id not in owner_venue_ids]
    if foreign:
        raise ValidationError({"venue_ids": f"Не принадлежат владельцу тендера: {[str(v) for v in foreign]}."})
    _reject_archived_venues(added)
    # §12 is checked at publish time for a draft, same as add_tender_items.
    if added and tender.status == TenderStatus.ACTIVE:
        conflicts = find_active_tender_conflicts(
            exclude_tender_id=tender.id,
            items=[{"master_product_id": item.master_product_id, "venue_ids": added}],
        )
        if conflicts:
            raise ActiveTenderConflict(conflicts)
    return requested


@transaction.atomic
def update_tender_item(*, item, data, actor=None):
    _reject_completed(item.tender)
    extend_only = item.tender.status != TenderStatus.DRAFT
    current_venues = list(item.venues.all())
    venue_ids = (
        _resulting_venue_ids(item, data["venue_ids"], current_venues, extend_only=extend_only)
        if "venue_ids" in data
        else None
    )

    changed, history = [], []
    for field in ("comment", "product_kind_label"):
        if field not in data:
            continue
        value = data[field] or None
        if value != getattr(item, field):
            history.append((field, display_value(getattr(item, field)), display_value(value)))
            setattr(item, field, value)
            changed.append(field)
    if changed:
        item.save(update_fields=[*changed, "updated_at"])
    if venue_ids is not None and venue_ids != {venue.id for venue in current_venues}:
        item.venues.set(venue_ids)
        history.append(
            (
                TenderChangedField.VENUES,
                display_names(venue_label(venue) for venue in current_venues),
                display_names(venue_label(venue) for venue in item.venues.all()),
            )
        )
    record_field_changes(item.tender, history, actor=actor, item=item)
    return item


def delete_tender_item(item):
    """Raw ТЗ §14: only a draft's position is deleted — a draft never had
    offers. A published one is hidden by unpublish_tender_item instead."""
    if item.tender.status != TenderStatus.DRAFT:
        raise ValidationError({"detail": DELETE_PUBLISHED_ITEM_MESSAGE})
    item.delete()
