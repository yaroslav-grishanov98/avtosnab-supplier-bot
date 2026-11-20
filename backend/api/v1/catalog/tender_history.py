"""Raw ТЗ §16/§76, docs/adr/0010: the tender's own change log. Lifecycle
events are always written; field edits only once the tender is published —
a draft is seen by its buyer alone."""

from datetime import datetime
from datetime import timezone as dt_timezone

from django.db.models import Q

from catalog.models import TenderChange, TenderChangedField, TenderChangeType, TenderStatus


def display_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(dt_timezone.utc).isoformat()
    return str(value)


def display_names(names):
    return ", ".join(sorted(names)) or None


def record_event(
    tender, change_type, *, actor=None, item=None, offer=None, occurred_at=None, old_value=None, new_value=None
):
    extra = {"occurred_at": occurred_at} if occurred_at is not None else {}
    TenderChange.objects.create(
        tender=tender,
        tender_item=item,
        tender_offer=offer,
        change_type=change_type,
        actor=actor,
        old_value=old_value,
        new_value=new_value,
        **extra,
    )


def record_field_changes(tender, changes, *, actor=None, item=None, offer=None, change_type=None):
    """changes: [(field, old display value, new display value)]. change_type
    defaults to updated / item_updated by whether an item is given."""
    changes = [(field, old, new) for field, old, new in changes if old != new]
    if tender.status == TenderStatus.DRAFT or not changes:
        return
    if change_type is None:
        change_type = TenderChangeType.ITEM_UPDATED if item is not None else TenderChangeType.UPDATED
    TenderChange.objects.bulk_create(
        TenderChange(
            tender=tender,
            tender_item=item,
            tender_offer=offer,
            change_type=change_type,
            changed_field=field,
            old_value=old,
            new_value=new,
            actor=actor,
        )
        for field, old, new in changes
    )


def tender_history(tender_id):
    return TenderChange.objects.filter(tender_id=tender_id).select_related(
        "actor", "tender_item__master_product", "tender_offer__supplier"
    )


def supplier_tender_history(tender_id, supplier_id):
    """§51: a supplier learns nothing about other suppliers — invite list changes
    and other suppliers' offers are left out; §75: nor about positions hidden
    from it — events of currently unpublished items are left out too."""
    return (
        tender_history(tender_id)
        .exclude(changed_field=TenderChangedField.INVITED_SUPPLIERS)
        .exclude(tender_item__published=False)
        .filter(Q(tender_offer__isnull=True) | Q(tender_offer__supplier_id=supplier_id))
    )
