"""Master catalog: products, imports, venue links and bulk actions."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.v1.catalog.selectors import (
    _is_owners_active_venue,
    _owner_active_venue_ids,
    master_product_bulk_supplier_product_diff,
    master_product_venue_batch_current,
)
from api.v1.catalog.tasks import process_master_product_import_upload_task
from catalog.models import (
    MasterProduct,
    VenueProductMatrixItem,
    VenueProductMatrixItemSource,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    Unit,
    User,
    Venue,
)
from suppliers.models import SupplierProduct

from ._shared import _UNSET, _decisions_error, _replace_audit_summary, _target_conflict_reason
from .master_core import MasterImportQueueUnavailable, MasterProductVenueLinkConflict
from .product_matrix import update_product_matrix_item


def start_master_product_import(*, owner, source_file_name, rows, parsed_meta, import_file=None):
    from catalog.models import MasterProductImportUpload, MasterProductImportUploadStatus
    from file_imports.versions import parser_version_for

    upload = MasterProductImportUpload.objects.create(
        owner=owner,
        source_file_name=source_file_name,
        rows_total=len(rows or []) + parsed_meta.get("rows_without_name", 0),
        parser_version=parser_version_for(getattr(settings, "CANONICAL_MASTER_PARSER_ENABLED", False)),
        parsed_rows=rows if rows and any(row.get("_parsed") for row in rows) else [],
        import_file=import_file,
    )
    try:
        process_master_product_import_upload_task.delay(
            upload_id=str(upload.id),
            owner_id=str(owner.id),
            # Файл сохранён -- воркер читает его сам, в Redis строки не кладём (на 10 000+ строк это мегабайты).
            rows=None if import_file is not None else rows,
            parsed_meta=parsed_meta,
        )
    except Exception as error:
        MasterProductImportUpload.objects.filter(pk=upload.pk).update(
            status=MasterProductImportUploadStatus.FAILED,
            error_message="Не удалось поставить обработку в очередь.",
        )
        raise MasterImportQueueUnavailable() from error
    return upload


@transaction.atomic
def create_master_products_bulk(*, owner, items):
    """§11.3 raw ТЗ: import a ready-made list straight into the Master
    Catalog, without creating anything in any company. Skips (never
    duplicates) any canonical_name that already matches an active
    MasterProduct of this owner, case-insensitively — same no-silent-
    duplicates principle as §16/link_master_product_to_venue."""
    from types import SimpleNamespace

    existing_by_name = {
        mp.canonical_name.casefold(): mp
        for mp in MasterProduct.objects.filter(owner=owner, status=EntityStatus.ACTIVE).only("id", "canonical_name")
    }
    results = []
    for row in items:
        canonical_name = row["canonical_name"]
        existing = existing_by_name.get(canonical_name.casefold())
        if existing is not None:
            results.append(
                SimpleNamespace(
                    canonical_name=canonical_name, outcome="skipped_duplicate", master_product_id=existing.id
                )
            )
            continue
        created = MasterProduct.objects.create(
            owner=owner,
            canonical_name=canonical_name,
            unit=row.get("unit"),
            net_value=row.get("net_value"),
            note=row.get("note"),
            status=EntityStatus.ACTIVE,
        )
        existing_by_name[canonical_name.casefold()] = created
        results.append(SimpleNamespace(canonical_name=canonical_name, outcome="created", master_product_id=created.id))
    return results


@transaction.atomic
def apply_master_product_import(*, owner, decisions, actor=None):
    """TASK-51 apply: writes exactly what resolve_master_product_import_rows
    (Task 1, selectors.py) would have shown for the same decisions.
    update_existing never overwrites the existing MasterProduct (same
    no-silent-overwrite rule as elsewhere in this codebase — see this
    plan's Global Constraints). create_new rows are deduplicated by
    casefolded raw_name WITHIN this batch before bulk_create, so two file
    rows naming the same product still resolve to one MasterProduct (see
    this task's design note) — the old sequential import_master_products
    got this for free from its single for-loop; splitting into preview +
    bulk-write apply does not, so it is done explicitly here.

    All-or-nothing: pass 1 validates every decision (existing
    master_product_id belongs to this owner and is ACTIVE) before pass 2
    writes anything, so an invalid decision anywhere aborts with nothing
    written — same guarantee the old direct-write endpoint had.

    TASK-71: replace overwrites canonical_name + unit of the chosen
    MasterProduct in place (ID, links, net_value, note untouched) and clears
    its embedding so _ensure_master_product_embeddings recomputes it from the
    new name on the next match. A replace that changes nothing is not
    written. update_existing keeps its no-overwrite rule.

    TASK-105: if a changed MasterProduct is linked to any ACTIVE
    VenueProductMatrixItem, those items are flagged needs_review=True —
    mirrors TASK-79's protection in the other direction (apply_venue_import)."""
    from types import SimpleNamespace

    # --- Pass 1: validate, write nothing (except per-row conflicts, which
    # are skipped rather than aborting the whole batch -- spec §9.1). -------
    replace_targets_seen = set()  # first replace on a target wins, later ones in this batch conflict
    wanted_ids = [
        d.get("master_product_id")
        for d in decisions
        if d["action"] in ("update_existing", "replace") and d.get("master_product_id")
    ]
    existing_by_id = (
        {
            str(mp.id): mp
            for mp in MasterProduct.objects.select_for_update(of=("self",))
            .select_related("unit")
            .filter(pk__in=set(wanted_ids), owner=owner, status=EntityStatus.ACTIVE)
        }
        if wanted_ids
        else {}
    )

    wanted_unit_ids = [
        d.get("unit_id") for d in decisions if d["action"] in ("create_new", "replace") and d.get("unit_id")
    ]
    active_units = (
        {unit.id: unit for unit in Unit.objects.filter(pk__in=set(wanted_unit_ids), status=EntityStatus.ACTIVE)}
        if wanted_unit_ids
        else {}
    )

    resolved = []  # one entry per decision: existing MasterProduct, or a key into new_by_name, or None (skip)
    conflicts = {}  # decision index -> conflict reason (row not applied, rest of batch continues)
    new_by_name = {}  # casefolded raw_name -> unsaved MasterProduct (dedup within this batch)
    replacements = []  # (existing MasterProduct, new canonical_name, new unit_id)
    for index, decision in enumerate(decisions):
        action = decision["action"]
        raw_name = decision["raw_name"]
        if action == "update_existing":
            master_product_id = decision.get("master_product_id")
            if master_product_id is None:
                raise _decisions_error(f'"{raw_name}": для action=update_existing нужно указать master_product_id.')
            existing = existing_by_id.get(str(master_product_id))
            if existing is None:
                raise _decisions_error(
                    f'"{raw_name}": мастер-товара с id={master_product_id} нет у этого владельца, либо он архивен.'
                )
            conflict = _target_conflict_reason(decision, existing, raw_name=raw_name)
            if conflict:
                conflicts[index] = conflict
            resolved.append(existing)
        elif action == "create_new":
            unit_id = decision.get("unit_id")
            if unit_id is not None and unit_id not in active_units:
                raise _decisions_error(f'"{raw_name}": единицы измерения с id={unit_id} нет, либо она архивна.')
            key = raw_name.strip().casefold()
            if key not in new_by_name:
                new_by_name[key] = MasterProduct(
                    owner=owner,
                    canonical_name=raw_name.strip(),
                    unit_id=unit_id,
                    net_value=decision.get("net_value"),
                    note=decision.get("note"),
                    status=EntityStatus.ACTIVE,
                )
            resolved.append(key)
        elif action == "replace":
            master_product_id = decision.get("master_product_id")
            unit_id = decision.get("unit_id")
            if master_product_id is None or unit_id is None:
                raise _decisions_error(f'"{raw_name}": для action=replace нужно указать master_product_id и unit_id.')
            existing = existing_by_id.get(str(master_product_id))
            if existing is None:
                raise _decisions_error(
                    f'"{raw_name}": мастер-товара с id={master_product_id} нет у этого владельца, либо он архивен.'
                )
            if unit_id not in active_units:
                raise _decisions_error(f'"{raw_name}": единицы измерения с id={unit_id} нет, либо она архивна.')
            conflict = _target_conflict_reason(decision, existing, raw_name=raw_name)
            if str(master_product_id) in replace_targets_seen:
                conflict = (
                    conflict
                    or f'"{raw_name}": несколько строк в этой пачке заменяют один и тот же мастер-товар '
                    f"id={master_product_id}."
                )
            if conflict:
                conflicts[index] = conflict
                resolved.append(existing)
            else:
                replace_targets_seen.add(str(master_product_id))
                replacements.append((existing, raw_name.strip(), unit_id))
                resolved.append(existing)
        else:
            resolved.append(None)

    # --- Pass 2: constant number of writes regardless of batch size. ------
    if new_by_name:
        MasterProduct.objects.bulk_create(new_by_name.values())

    now = timezone.now()
    changed = []
    for master_product, new_name, new_unit_id in replacements:
        if master_product.canonical_name == new_name and master_product.unit_id == new_unit_id:
            continue
        summary = _replace_audit_summary(
            old_name=master_product.canonical_name,
            old_unit=master_product.unit.name if master_product.unit_id else None,
            new_name=new_name,
            new_unit=active_units[new_unit_id].name,
        )
        master_product.canonical_name = new_name
        master_product.unit_id = new_unit_id
        master_product.embedding = None
        master_product.updated_at = now  # bulk_update skips auto_now
        changed.append((master_product, summary))
    if changed:
        MasterProduct.objects.bulk_update(
            [master_product for master_product, _ in changed], ["canonical_name", "unit", "embedding", "updated_at"]
        )
        for master_product, summary in changed:
            record_audit_log(
                actor=actor,
                action=AuditLogAction.REPLACED,
                entity_type=AuditLogEntityType.MASTER_PRODUCT,
                entity_id=master_product.id,
                summary=summary,
                scope_id=owner.id,
            )
        VenueProductMatrixItem.objects.filter(
            master_product_id__in=[master_product.id for master_product, _ in changed],
            status=EntityStatus.ACTIVE,
        ).update(needs_review=True)

    def _as_row(decision, target, conflict):
        if target is None:
            return SimpleNamespace(
                raw_name=decision["raw_name"],
                action=decision["action"],
                master_product_id=None,
                canonical_name=None,
                unit_id=None,
                net_value=None,
                note=None,
                conflict=conflict,
            )
        mp = new_by_name[target] if isinstance(target, str) else target
        return SimpleNamespace(
            raw_name=decision["raw_name"],
            action=decision["action"],
            master_product_id=mp.id,
            canonical_name=mp.canonical_name,
            unit_id=mp.unit_id,
            net_value=mp.net_value,
            note=mp.note,
            conflict=conflict,
        )

    return [
        _as_row(decision, target, conflicts.get(index))
        for index, (decision, target) in enumerate(zip(decisions, resolved))
    ]


@transaction.atomic
def update_master_product(master_product, **fields):
    for field, value in fields.items():
        setattr(master_product, field, value)
    master_product.save(update_fields=list(fields.keys()) + ["updated_at"])
    return master_product


@transaction.atomic
def delete_master_product(master_product, actor=None):
    """Hard delete (spec §4.5/§8, rule 15): VenueProductMatrixItem.master_product
    is SET_NULL, so links clear automatically at the DB level — no separate
    cleanup call needed, same pattern as delete_product_matrix_item above."""
    entity_id, owner_id, summary = master_product.id, master_product.owner_id, master_product.canonical_name
    master_product.delete()
    record_audit_log(
        actor=actor,
        action=AuditLogAction.DELETED,
        entity_type=AuditLogEntityType.MASTER_PRODUCT,
        entity_id=entity_id,
        summary=summary,
        scope_id=owner_id,
    )


@transaction.atomic
def delete_master_products_bulk(*, owner, master_product_ids, actor=None):
    """§49 raw ТЗ: "Удалить" applied to several selected Master Catalog rows
    at once. An id not found among this owner's active MasterProduct rows
    is reported not_found rather than raising — a stale id in a multi-
    select batch shouldn't abort the rest of the batch. Reuses
    delete_master_product per row (same SET_NULL/audit-log behavior as the
    single-item DELETE); a duplicate id within one request is only deleted
    once but reported for every occurrence."""
    from types import SimpleNamespace

    existing_by_id = {
        mp.id: mp
        for mp in MasterProduct.objects.filter(
            pk__in=set(master_product_ids), owner=owner, status=EntityStatus.ACTIVE
        ).only("id", "owner_id", "canonical_name")
    }
    deleted_ids = set()
    results = []
    for master_product_id in master_product_ids:
        mp = existing_by_id.get(master_product_id)
        if mp is None:
            results.append(SimpleNamespace(master_product_id=master_product_id, outcome="not_found"))
            continue
        if master_product_id not in deleted_ids:
            delete_master_product(mp, actor=actor)
            deleted_ids.add(master_product_id)
        results.append(SimpleNamespace(master_product_id=master_product_id, outcome="deleted"))
    return results


@transaction.atomic
def link_master_product_to_venue(*, master_product, venue, link_existing_item_id=None, force_create=False):
    if not _is_owners_active_venue(owner_id=master_product.owner_id, venue_id=venue.id):
        raise ValidationError({"detail": "Эта компания не принадлежит владельцу мастер-справочника."})

    # TASK-46: lock the Venue row before checking occupancy, matching
    # _auto_assign_master_products_to_new_items — without it, a concurrent
    # auto-match call for the same venue can pass its own occupancy check
    # before either transaction commits, and one of the two .create() calls
    # then violates vpmi_master_product_venue_uniq with an unhandled
    # IntegrityError (500) instead of raising this function's own
    # ValidationError below.
    Venue.objects.select_for_update().get(pk=venue.pk)

    # vpmi_master_product_venue_uniq (catalog/models.py) has no status
    # filter, so an archived row still claims this (master_product, venue)
    # slot at the DB level — the occupancy check here must match that, not
    # just ACTIVE rows, or force_create's .create() below can hit the same
    # unhandled IntegrityError.
    if VenueProductMatrixItem.objects.filter(master_product=master_product, venue=venue).exists():
        raise ValidationError({"detail": "Этот мастер-товар уже привязан к этой компании."})

    if link_existing_item_id is not None:
        try:
            item = VenueProductMatrixItem.objects.get(
                id=link_existing_item_id,
                venue=venue,
                status=EntityStatus.ACTIVE,
                master_product__isnull=True,
            )
        except VenueProductMatrixItem.DoesNotExist as exc:
            raise ValidationError(
                {
                    "link_existing_item_id": (
                        "Строка не найдена, принадлежит другой компании или уже привязана к мастер-товару."
                    )
                }
            ) from exc
        item.master_product = master_product
        item.save(update_fields=["master_product", "updated_at"])
        return item

    if not force_create:
        candidate = (
            VenueProductMatrixItem.objects.filter(
                venue=venue,
                status=EntityStatus.ACTIVE,
                master_product__isnull=True,
                requested_name__iexact=master_product.canonical_name,
            )
            .order_by("created_at")
            .first()
        )
        if candidate is not None:
            raise MasterProductVenueLinkConflict(candidate)

    return VenueProductMatrixItem.objects.create(
        venue=venue,
        master_product=master_product,
        requested_name=master_product.canonical_name,
        requested_unit_label=master_product.unit.code if master_product.unit_id else None,
        source=VenueProductMatrixItemSource.MASTER_CATALOG,
        status=EntityStatus.ACTIVE,
    )


@transaction.atomic
def unlink_master_product_from_venue(*, master_product, venue_id):
    from django.shortcuts import get_object_or_404

    item = get_object_or_404(
        VenueProductMatrixItem, master_product=master_product, venue_id=venue_id, status=EntityStatus.ACTIVE
    )
    item.master_product = None
    item.save(update_fields=["master_product", "updated_at"])


def reset_master_product_links_for_venue(*, venue_id):
    """Spec §3.1: completing a VenueAccountTransfer must clear every
    VenueProductMatrixItem.master_product link for the transferred venue —
    the new owner must never be able to see or edit the previous owner's
    Мастер-справочник through a stale link. Same SET_NULL-only semantics as
    delete_master_product (no other field/row is touched), applied in bulk
    to every matrix item of this venue that has a link, regardless of the
    item's own status — an archived matrix item can still be un-archived
    later and must not keep pointing at the old owner's master product."""
    VenueProductMatrixItem.objects.filter(venue_id=venue_id, master_product__isnull=False).update(
        master_product=None, updated_at=timezone.now()
    )


def master_product_bulk_supplier_product_targets(*, master_product, venue_ids=None):
    """Spec §4.4/§6 rule 9: the set of matrix items a bulk supplier_product
    assignment is allowed to touch — active VenueProductMatrixItem rows
    ALREADY linked to `master_product`. This never creates a new link (that
    is link_master_product_to_venue's job, with its own §16 duplicate
    check) and never silently drops a company the caller explicitly named:
    a truthy venue_ids that includes an unlinked venue raises instead of
    quietly excluding it. A falsy venue_ids (None or []) means "every
    company currently linked"."""
    queryset = VenueProductMatrixItem.objects.filter(
        master_product=master_product,
        status=EntityStatus.ACTIVE,
        venue__status=EntityStatus.ACTIVE,
        venue_id__in=_owner_active_venue_ids(master_product.owner_id),
    ).select_related("venue", "supplier_product")
    if venue_ids:
        queryset = queryset.filter(venue_id__in=venue_ids)
    items = list(queryset.order_by("venue__display_name", "id"))
    if venue_ids:
        found = {str(item.venue_id) for item in items}
        missing = [str(v) for v in venue_ids if str(v) not in found]
        if missing:
            raise ValidationError({"venue_ids": f"Мастер-товар не привязан к этим компаниям: {', '.join(missing)}."})
    return items


@transaction.atomic
def bulk_assign_supplier_product_to_master_product(*, master_product, supplier_product, venue_ids=None):
    """Spec §4.4/§6 rule 9: applies the exact diff master_product_bulk_
    supplier_product_diff would show for the same inputs — targets are
    resolved/validated the same way the read-only preview does (Task 2),
    then locked and updated together so a concurrent unlink can't leave a
    half-applied result. Returns the diff rows as they were BEFORE the
    write (current_supplier_product_id still shows the old value), so the
    caller can render a confirmation of what just changed."""
    items = master_product_bulk_supplier_product_targets(master_product=master_product, venue_ids=venue_ids)

    locked_ids = list(
        VenueProductMatrixItem.objects.select_for_update()
        .filter(pk__in=[item.id for item in items], master_product=master_product, status=EntityStatus.ACTIVE)
        .values_list("pk", flat=True)
    )

    # Only compute diff from items that survived the post-lock re-check
    locked_ids_set = set(locked_ids)
    locked_items = [item for item in items if item.id in locked_ids_set]
    rows = master_product_bulk_supplier_product_diff(items=locked_items, new_supplier_product=supplier_product)

    VenueProductMatrixItem.objects.filter(pk__in=locked_ids).update(
        supplier_product=supplier_product, updated_at=timezone.now()
    )
    return rows


def _apply_master_product_venue_batch(*, master_product, changes, commit):
    """TASK-52: shared implementation for master_product_venue_batch_preview
    (commit=False) and apply_master_product_venue_batch (commit=True).

    There is ONE code path instead of a separate read-only diff builder:
    every validation, lock and write runs exactly as apply would, and the
    preview simply rolls the whole thing back (transaction.set_rollback)
    before returning. That costs the preview a real (discarded) write, but
    it makes it structurally impossible for the preview to show a diff the
    apply wouldn't produce — the drift risk the flat bulk-supplier-product
    diff avoids by being a single pure function, which this batch can't be:
    each row picks its own already-validated service call (link / unlink /
    re-point supplier_product), each with its own §16 duplicate check and
    error cases.

    TASK-46: link_master_product_to_venue/unlink_master_product_from_venue
    take no select_for_update lock on Venue. This function locks every
    touched Venue up front, closing that race for this batch path; the
    single-item PUT .../venues/{venue_id} endpoint is unchanged and TASK-46
    stays open for it (confirmed 2026-09-08).
    """
    from types import SimpleNamespace

    from django.shortcuts import get_object_or_404

    venue_ids = [change["venue_id"] for change in changes]
    # Same scoping as _is_owners_active_venue (the check
    # link_master_product_to_venue itself applies): an active OWNER row AND
    # an active Venue — "keep"/"unlink" rows never reach that check, so an
    # archived venue would otherwise slip through here.
    allowed_venue_ids = set(
        Venue.objects.filter(id__in=venue_ids, status=EntityStatus.ACTIVE)
        .filter(id__in=_owner_active_venue_ids(master_product.owner_id))
        .values_list("id", flat=True)
    )
    missing = [str(v) for v in venue_ids if v not in allowed_venue_ids]
    if missing:
        raise ValidationError(
            {"changes": f"Эти компании не принадлежат владельцу мастер-справочника: {', '.join(missing)}."}
        )

    with transaction.atomic():
        # Lock every touched Venue up front — closes TASK-46 for this path
        # (see the docstring); forced via list() so the lock is actually
        # acquired before any per-venue service call below.
        list(Venue.objects.select_for_update().filter(pk__in=venue_ids))

        before = master_product_venue_batch_current(master_product=master_product, venue_ids=venue_ids)
        rows = []
        for change in changes:
            venue_id = change["venue_id"]
            link_action = change.get("link_action", "keep")
            supplier_product_id = change.get("supplier_product_id", _UNSET)
            current = before[venue_id]

            if link_action == "unlink" and supplier_product_id is not _UNSET:
                raise ValidationError(
                    {"changes": f'"{venue_id}": supplier_product_id несовместим с link_action=unlink.'}
                )
            if link_action == "keep" and (change.get("link_existing_item_id") or change.get("force_create")):
                raise ValidationError(
                    {"changes": f'"{venue_id}": link_existing_item_id/force_create несовместимы с link_action=keep.'}
                )

            item = current.matrix_item
            if link_action == "link":
                item = link_master_product_to_venue(
                    master_product=master_product,
                    venue=current.venue,
                    link_existing_item_id=change.get("link_existing_item_id"),
                    force_create=change.get("force_create", False),
                )
            elif link_action == "unlink":
                if item is not None:
                    unlink_master_product_from_venue(master_product=master_product, venue_id=venue_id)
                item = None

            if supplier_product_id is not _UNSET:
                if item is None:
                    raise ValidationError(
                        {"changes": f'"{venue_id}": строка не привязана — нечему назначать товар поставщика.'}
                    )
                supplier_product = get_object_or_404(
                    SupplierProduct.objects.filter(status=EntityStatus.ACTIVE), pk=supplier_product_id
                )
                item = update_product_matrix_item(item, supplier_product=supplier_product)

            rows.append(
                SimpleNamespace(
                    venue_id=venue_id,
                    venue_name=current.venue_name,
                    current_linked=current.linked,
                    current_matrix_item_id=current.matrix_item_id,
                    current_requested_name=current.requested_name,
                    current_supplier_product_id=current.supplier_product_id,
                    current_supplier_product_name=current.supplier_product_name,
                    current_price=current.price,
                    link_action=link_action,
                    new_matrix_item_id=item.id if item else None,
                    new_supplier_product_id=item.supplier_product_id if item else None,
                    new_supplier_product_name=(
                        item.supplier_product.normalized_name if item and item.supplier_product_id else None
                    ),
                    changed=(
                        current.matrix_item_id != (item.id if item else None)
                        or current.supplier_product_id != (item.supplier_product_id if item else None)
                    ),
                )
            )

        if not commit:
            transaction.set_rollback(True)
        return rows


def master_product_venue_batch_preview(*, master_product, changes):
    """Read-only "Сейчас → Будет" for a batch save: runs the real apply and
    rolls it back, so no write survives. See _apply_master_product_venue_batch."""
    return _apply_master_product_venue_batch(master_product=master_product, changes=changes, commit=False)


def apply_master_product_venue_batch(*, master_product, changes):
    """Atomically applies a whole "Компании: N" modal batch — every row or
    none. See _apply_master_product_venue_batch."""
    return _apply_master_product_venue_batch(master_product=master_product, changes=changes, commit=True)


@transaction.atomic
def apply_master_product_import_matches(*, venue, owner_id, decisions):
    """Spec §3.7/§4.4 stage 2 apply: writes exactly what
    resolve_master_product_matches_for_import (Task 2, selectors.py) would
    have shown as the diff for the same decisions. link_existing attaches an
    existing MasterProduct of this owner to the matrix item (validated: the
    master product belongs to owner_id, is active, and isn't already linked
    to a DIFFERENT active item of this same venue — the partial-unique
    constraint from Plan 1 on VenueProductMatrixItem(master_product, venue),
    checked here so a violation surfaces as a clean 400 instead of a leaked
    IntegrityError). create_new creates a MasterProduct for this owner
    (canonical_name = the matrix item's own requested_name) and links it —
    its embedding is deliberately left NULL (Task 1's
    _ensure_master_product_embeddings lazily backfills it on the next
    match), so this write path never depends on the embedding model. skip
    writes nothing — the item's existing master_product (if any) is left
    exactly as it was. link_existing/create_new also clear needs_review
    (TASK-44 followup) — a human just confirmed the match, and nothing
    else in this codebase ever clears that flag once auto-match sets it.

    Runs as two passes like apply_venue_import (Plan 5, fix wave a5d763f):
    pass 1 batch-fetches and validates the whole decision list with no
    writes; pass 2 does a constant number of writes regardless of batch
    size."""
    from types import SimpleNamespace

    from django.shortcuts import get_object_or_404

    if not _is_owners_active_venue(owner_id=owner_id, venue_id=venue.id):
        raise ValidationError({"owner_id": "У этого владельца нет активного доступа к этой компании."})
    owner = get_object_or_404(User, pk=owner_id)
    Venue.objects.select_for_update().get(pk=venue.pk)

    matrix_item_ids = [decision["matrix_item_id"] for decision in decisions]
    items_by_id = {
        item.id: item
        for item in VenueProductMatrixItem.objects.filter(
            pk__in=set(matrix_item_ids), venue=venue, status=EntityStatus.ACTIVE
        ).select_related("master_product")
    }

    wanted_master_product_ids = {
        decision.get("master_product_id")
        for decision in decisions
        if decision["action"] == "link_existing" and decision.get("master_product_id") is not None
    }
    master_products_by_id = {
        mp.id: mp
        for mp in MasterProduct.objects.filter(
            pk__in=wanted_master_product_ids, owner_id=owner_id, status=EntityStatus.ACTIVE
        )
    }
    # Seeded from the DB, then updated as pass 1 processes each link_existing
    # decision — so a later decision in the same batch that names a
    # master_product_id already claimed by an earlier decision here is caught
    # the same way as one already linked in the DB before this call started
    # (see this function's docstring: both must be a clean ValidationError,
    # never a leaked IntegrityError from the partial-unique constraint).
    claimed_by = {
        item.master_product_id: item.id
        for item in VenueProductMatrixItem.objects.filter(
            venue=venue, status=EntityStatus.ACTIVE, master_product_id__in=wanted_master_product_ids
        )
    }

    # --- Pass 1: validate everything, write nothing. -----------------------
    resolved = []  # (item, resulting_master_product_or_None, action)
    new_master_products = []
    for decision in decisions:
        matrix_item_id = decision["matrix_item_id"]
        action = decision["action"]
        item = items_by_id.get(matrix_item_id)
        if item is None:
            raise ValidationError(
                {"decisions": f"Товара с matrix_item_id={matrix_item_id} нет в товарной матрице этой компании."}
            )

        if action == "link_existing":
            master_product_id = decision.get("master_product_id")
            master_product = master_products_by_id.get(master_product_id) if master_product_id else None
            if master_product_id is None or master_product is None:
                raise ValidationError(
                    {
                        "decisions": f"master_product_id={master_product_id} не найден в мастер-справочнике "
                        "этого владельца."
                    }
                )
            conflicting_item_id = claimed_by.get(master_product_id)
            if conflicting_item_id is not None and conflicting_item_id != item.id:
                raise ValidationError(
                    {"decisions": f"Мастер-товар {master_product_id} уже привязан к другому товару этой компании."}
                )
            claimed_by[master_product_id] = item.id
            resolved.append((item, master_product, action))
        elif action == "create_new":
            new_master_product = MasterProduct(
                owner=owner, canonical_name=item.requested_name, status=EntityStatus.ACTIVE
            )
            new_master_products.append(new_master_product)
            resolved.append((item, new_master_product, action))
        else:
            resolved.append((item, None, "skip"))

    # --- Pass 2: constant number of writes regardless of batch size. -------
    if new_master_products:
        MasterProduct.objects.bulk_create(new_master_products)

    to_update = []
    for item, master_product, action in resolved:
        if action == "skip":
            continue
        item.master_product = master_product
        # TASK-44 followup: a human just confirmed this match (link_existing
        # or create_new), so it no longer needs review — needs_review was
        # never cleared anywhere before this, so a row auto-flagged once
        # stayed flagged forever even after manual confirmation.
        item.needs_review = False
        to_update.append(item)
    if to_update:
        VenueProductMatrixItem.objects.bulk_update(to_update, ["master_product", "needs_review"])

    return [
        SimpleNamespace(
            matrix_item_id=item.id,
            action=action,
            master_product_id=(master_product.id if master_product else item.master_product_id),
            master_product_canonical_name=(
                master_product.canonical_name
                if master_product
                else (item.master_product.canonical_name if item.master_product_id else None)
            ),
        )
        for item, master_product, action in resolved
    ]
