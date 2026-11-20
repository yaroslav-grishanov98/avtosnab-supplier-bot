"""Venue product matrix (company directory)."""

from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from api.v1.catalog.selectors import (
    _venue_sole_active_owner_id,
)
from catalog.models import (
    MasterProduct,
    VenueProductMatrixItem,
    VenueProductMatrixSupplierMatch,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    User,
)

from .master_core import (
    MasterProductAlreadyLinkedConflict,
    _auto_assign_master_products_to_new_items,
    create_master_product,
)


@transaction.atomic
def create_product_matrix_item(
    *, venue, requested_name, requested_unit_label=None, note=None, subdivision_id=None, auto_match=True
):
    """§11.1 raw ТЗ: adding a company-catalog item outside the import flow
    auto-resolves against the owner's master catalog — see
    _auto_assign_master_products_to_new_items for the matching rule.
    Skipped entirely (item still created, no auto-match attempted) when the
    venue doesn't resolve to exactly one active owner via
    _venue_sole_active_owner_id — e.g. a bot/service-token upload.

    2026-09-17: subdivision_id — по прямой просьбе пользователя. Caller
    (the view) is responsible for verifying it belongs to this venue before
    calling this — same convention as everywhere else subdivision_id is
    accepted (cart items, cart submit)."""
    item = VenueProductMatrixItem(
        venue=venue,
        requested_name=requested_name,
        requested_unit_label=requested_unit_label,
        note=note,
        subdivision_id=subdivision_id,
        status=EntityStatus.ACTIVE,
    )
    if auto_match:
        _auto_assign_master_products_to_new_items(venue=venue, items=[item])
    item.save()
    return item


@transaction.atomic
def update_product_matrix_item(item, **fields):
    for field, value in fields.items():
        setattr(item, field, value)
    item.save(update_fields=list(fields.keys()) + ["updated_at"])
    if fields.get("supplier_product") is not None:
        # TASK-148: товар выбран — состояние массового подбора («Нашли N»/«Не нашли») больше не актуально.
        VenueProductMatrixSupplierMatch.objects.filter(item=item).delete()
    return item


@transaction.atomic
def set_product_matrix_master_product(
    *,
    item,
    action,
    master_product_id=None,
    canonical_name=None,
    unit=None,
    net_value=None,
    note=None,
):
    """Apply the prototype's row-first Master-product picker decision.

    Existing master-first endpoints remain valid; this service gives the
    company-catalog screen one explicit operation for link/create/unlink and
    enforces the same owner and one-master-product-per-venue invariants.
    """
    from django.shortcuts import get_object_or_404

    locked_item = VenueProductMatrixItem.objects.select_for_update().get(pk=item.pk)
    owner_id = _venue_sole_active_owner_id(locked_item.venue_id)
    if owner_id is None:
        raise ValidationError(
            {"venue_id": "Для компании не удалось однозначно определить владельца Мастер-справочника."}
        )

    if action == "unlink":
        locked_item.master_product = None
        locked_item.needs_review = False
        locked_item.save(update_fields=["master_product", "needs_review", "updated_at"])
        return locked_item

    if action == "link_existing":
        master_product = get_object_or_404(
            MasterProduct.objects.filter(owner_id=owner_id, status=EntityStatus.ACTIVE), pk=master_product_id
        )
    elif action == "create_new":
        owner = get_object_or_404(User, pk=owner_id)
        master_product = create_master_product(
            owner=owner,
            canonical_name=canonical_name or locked_item.requested_name,
            unit=unit,
            net_value=net_value,
            note=note,
        )
    else:
        raise ValidationError({"action": "Неизвестное действие."})

    occupied = (
        VenueProductMatrixItem.objects.filter(
            venue_id=locked_item.venue_id,
            master_product=master_product,
        )
        .exclude(pk=locked_item.pk)
        .first()
    )
    if occupied is not None:
        raise MasterProductAlreadyLinkedConflict(occupied)

    locked_item.master_product = master_product
    locked_item.needs_review = False
    try:
        with transaction.atomic():
            locked_item.save(update_fields=["master_product", "needs_review", "updated_at"])
    except IntegrityError as exc:
        conflicting = (
            VenueProductMatrixItem.objects.filter(venue_id=locked_item.venue_id, master_product=master_product)
            .exclude(pk=locked_item.pk)
            .first()
        )
        if conflicting is not None:
            raise MasterProductAlreadyLinkedConflict(conflicting) from exc
        raise
    return locked_item


@transaction.atomic
def create_product_matrix_item_with_master_decision(*, venue, master_product_decision, **fields):
    """Create a company product and apply the user's explicit picker choice atomically."""
    item = create_product_matrix_item(venue=venue, auto_match=False, **fields)
    return set_product_matrix_master_product(item=item, **master_product_decision)


@transaction.atomic
def delete_product_matrix_item(item, actor=None):
    """Archive, not hard delete (2026-09-03, дополнение к ТЗ "безопасность
    данных и удаление товаров" — товар компании по умолчанию архивируется,
    не удаляется физически). A real delete used to CASCADE onto
    VenueProductImportAlias (Plan 5) and silently destroy the venue's
    confirmed import-matching memory."""
    item.status = EntityStatus.ARCHIVED
    item.save(update_fields=["status", "updated_at"])
    record_audit_log(
        actor=actor,
        action=AuditLogAction.ARCHIVED,
        entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM,
        entity_id=item.id,
        summary=item.requested_name,
    )
