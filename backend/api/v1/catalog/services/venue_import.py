"""Venue product import apply."""

from django.db import transaction
from django.utils import timezone

from catalog.models import (
    VenueProductImportAlias,
    VenueProductMatrixItem,
    VenueProductMatrixItemSource,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    Venue,
)

from ._shared import _decisions_error, _replace_audit_summary, _target_conflict_reason
from .master_core import _auto_assign_master_products_to_new_items


@transaction.atomic
def apply_venue_import(
    *, venue, user, decisions, present_matrix_item_ids=None, mark_missing=True, prepared_matching=None
):
    """Spec §3.7/§4.4 stage 1 apply: writes exactly what resolve_venue_import_rows
    (Task 2, selectors.py) would have shown as the diff for the same decisions.
    "update_existing" writes only the alias (identity resolution, no field
    overwrite on the existing VenueProductMatrixItem — see this plan's Scope
    Decision section, spec §6 rule 7). "create_new" creates a matrix item
    with requested_unit_label/note from file preview, tagged
    source=ACCOUNTING_SYSTEM (the existing choice written for exactly this
    case), runs it through the same §11.1 auto-match rule as a manual
    add (TASK-45 followup — see _auto_assign_master_products_to_new_items),
    plus the alias. "skip" writes nothing (spec §3.7: no
    resulting matrix_item to alias).

    Import is inherently a bulk operation (a real accounting export is
    hundreds of rows), so this runs in two passes instead of one query per
    decision: pass 1 batch-fetches every referenced matrix item and validates
    the whole batch with no writes at all; pass 2's own bulk writes are still
    just two statements regardless of batch size — one bulk_create for the
    new matrix items, one upserting bulk_create for the aliases — but pass 2
    also runs _auto_assign_master_products_to_new_items over every
    "create_new" row before that bulk_create, and that step is per-row (an
    embedding-similarity lookup, and possibly a MasterProduct create, for
    each new item), so pass 2's total write/query cost is proportional to the
    number of "create_new" rows, not constant. Because the whole function is
    atomic and pass 1 raises before pass 2 starts, an invalid decision
    anywhere still aborts the call with nothing written.

    The alias write is an ON CONFLICT upsert on UniqueConstraint(venue,
    raw_name), which keeps a retried "update_existing" decision idempotent
    (what update_or_create used to do row by row). Duplicate raw_names inside
    one batch are collapsed to their last decision before the write — Postgres
    refuses to let a single ON CONFLICT statement touch the same row twice,
    and last-wins is what the old sequential loop did anyway.

    Deliberate, reviewed tradeoff (reviewed and accepted by the project
    owner, 2026-09-05): the auto-match run above on "create_new" rows
    happens immediately, inline with the import, and does not go through
    this codebase's separate stage-2 human-confirmation flow
    (resolve_master_product_matches_for_import /
    apply_master_product_import_matches). A row that already got linked
    here arrives at stage 2 already resolved and is reported back as
    proposed_action="keep_existing" — it is never re-asked, however
    low-confidence the original auto-match was. This is a shipped decision,
    not an oversight or a TODO.

    Accepted performance cost of the same step: each new row's
    find_candidate_master_products call also triggers
    _ensure_master_product_embeddings, which backfills any of the owner's
    MasterProduct rows still missing an embedding — including the row the
    *previous* iteration of this same batch just created — so a large
    "create_new" batch makes roughly two embedding-model calls per new row,
    all while holding the Venue row lock (select_for_update, see
    _auto_assign_master_products_to_new_items) for the whole batch. This is
    an accepted cost of the current design, not something left to fix here.

    TASK-71: "replace" overwrites requested_name + requested_unit_label of
    the chosen matrix item in place (ID, note, master_product,
    supplier_product, lists untouched), then records the alias exactly like
    "update_existing" and counts the item as touched by this import. A
    replace that changes nothing is not written. TASK-79: if the replaced item
    is linked to a master_product, it is also flagged needs_review=True — the
    link itself is untouched (replace never repoints master_product), but a
    renamed/re-unit'd item may no longer be what that link assumed."""
    from types import SimpleNamespace

    Venue.objects.select_for_update().get(pk=venue.pk)

    # --- Pass 1: validate everything, write nothing (except per-row
    # conflicts, which are skipped rather than aborting the whole batch --
    # unified-import-architecture spec §9.1). ------------------------------
    replace_targets_seen = set()  # first replace on a target wins, later ones in this batch conflict
    conflicts = {}  # decision index -> conflict reason
    wanted_ids = [
        decision.get("matrix_item_id")
        for decision in decisions
        if decision["action"] in ("update_existing", "replace") and decision.get("matrix_item_id") is not None
    ]
    # Keyed by str() so a caller passing UUID strings resolves the same as one
    # passing UUID objects (the serializer always hands us UUID objects).
    existing_by_id = (
        {
            str(item.id): item
            for item in VenueProductMatrixItem.objects.select_for_update()
            .filter(pk__in=set(wanted_ids), venue=venue)
            .order_by("pk")
        }
        if wanted_ids
        else {}
    )

    # One entry per decision, aligned with `decisions`; None means "writes nothing".
    resolved = []
    new_items = []
    replacements = []  # (matrix item, new requested_name, new requested_unit_label)
    for index, decision in enumerate(decisions):
        raw_name = decision["raw_name"]
        action = decision["action"]

        if action in ("update_existing", "replace"):
            matrix_item_id = decision.get("matrix_item_id")
            if matrix_item_id is None:
                raise _decisions_error(f'"{raw_name}": для action={action} нужно указать matrix_item_id.')
            item = existing_by_id.get(str(matrix_item_id))
            if item is None:
                raise _decisions_error(
                    f'"{raw_name}": товара с matrix_item_id={matrix_item_id} нет в товарной матрице этого заведения.'
                )
            if item.status != EntityStatus.ACTIVE:
                raise _decisions_error(
                    f'"{raw_name}": товар matrix_item_id={matrix_item_id} архивный — '
                    "сопоставить импорт с ним нельзя, восстановите его или выберите другой."
                )
            conflict = _target_conflict_reason(decision, item, raw_name=raw_name)
            if action == "replace":
                new_unit_label = (decision.get("requested_unit_label") or "").strip()
                if not new_unit_label:
                    raise _decisions_error(f'"{raw_name}": для action=replace нужно указать requested_unit_label.')
                if str(matrix_item_id) in replace_targets_seen:
                    conflict = (
                        conflict
                        or f'"{raw_name}": несколько строк в этой пачке заменяют один и тот же товар компании '
                        f"matrix_item_id={matrix_item_id}."
                    )
                if conflict:
                    conflicts[index] = conflict
                else:
                    replace_targets_seen.add(str(matrix_item_id))
                    replacements.append((item, raw_name, new_unit_label, decision.get("unit_id")))
            elif conflict:
                conflicts[index] = conflict
            resolved.append(item)
        elif action == "create_new":
            # Not saved yet — bulk_create'd below. id/created_at/updated_at come
            # from UUIDTimestampedModel's own field defaults, which bulk_create
            # honours (neither model overrides save()).
            item = VenueProductMatrixItem(
                venue=venue,
                requested_name=raw_name,
                requested_unit_label=decision.get("requested_unit_label"),
                unit_id=decision.get("unit_id"),
                note=decision.get("note"),
                source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM,
                status=EntityStatus.ACTIVE,
            )
            resolved.append(item)
            new_items.append(item)
        else:
            # skip or unknown action: writes nothing.
            resolved.append(None)

    # --- Pass 2: the whole batch in a constant number of writes. ----------
    # TASK-45 followup: create_new rows get the same §11.1 auto-match rule
    # as a manually added row (create_product_matrix_item) — previously an
    # import batch skipped the Мастер-справочник for every new row.
    # update_existing rows are untouched: auto-match never runs on them.
    _auto_assign_master_products_to_new_items(venue=venue, items=new_items, prepared_matching=prepared_matching)
    if new_items:
        VenueProductMatrixItem.objects.bulk_create(new_items)

    now = timezone.now()
    changed_items = []
    for item, new_name, new_unit_label, new_unit_id in replacements:
        if (
            item.requested_name == new_name
            and item.requested_unit_label == new_unit_label
            and item.unit_id == new_unit_id
        ):
            continue
        summary = _replace_audit_summary(
            old_name=item.requested_name,
            old_unit=item.requested_unit_label,
            new_name=new_name,
            new_unit=new_unit_label,
        )
        item.requested_name = new_name
        item.requested_unit_label = new_unit_label
        item.unit_id = new_unit_id
        item.updated_at = now  # bulk_update skips auto_now
        # TASK-79: replace doesn't repoint master_product (ID/links are kept
        # on purpose), but a renamed/re-unit'd item may no longer match what
        # that master_product actually is — flag it for the same manual
        # review _auto_assign_master_products_to_new_items uses for a
        # low-confidence auto-match, rather than silently trusting a link
        # that predates the rename. No master_product means nothing to
        # review, so it's left untouched (never forced back to False here).
        if item.master_product_id is not None:
            item.needs_review = True
        changed_items.append((item, summary))
    if changed_items:
        VenueProductMatrixItem.objects.bulk_update(
            [item for item, _ in changed_items],
            ["requested_name", "requested_unit_label", "unit_id", "needs_review", "updated_at"],
        )
        for item, summary in changed_items:
            record_audit_log(
                actor=user,
                action=AuditLogAction.REPLACED,
                entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM,
                entity_id=item.id,
                summary=summary,
                scope_id=venue.id,
            )

    aliases_by_raw_name = {}
    for index, (decision, item) in enumerate(zip(decisions, resolved)):
        if item is None or index in conflicts:
            continue
        aliases_by_raw_name[decision["raw_name"]] = VenueProductImportAlias(
            venue=venue, raw_name=decision["raw_name"], matrix_item=item, confirmed_by=user
        )
    if aliases_by_raw_name:
        VenueProductImportAlias.objects.bulk_create(
            list(aliases_by_raw_name.values()),
            update_conflicts=True,
            unique_fields=["venue", "raw_name"],
            update_fields=["matrix_item", "confirmed_by", "updated_at"],
        )

    # §30 raw ТЗ: flag active accounting_system items this batch never
    # touched, and un-flag any touched item that was previously flagged —
    # never delete/archive automatically.
    #
    # TASK-74: a partial apply only carries decisions for the rows the user
    # has resolved so far — present_matrix_item_ids lets the caller also
    # name items preview matched to a row of THIS SAME file that has no
    # decision in this particular call (still pending review), so they
    # aren't wrongly swept up as "missing" just because they weren't
    # decided yet. present_matrix_item_ids is caller-supplied and otherwise
    # unvalidated, so an id that doesn't belong to this venue/isn't
    # ACTIVE+ACCOUNTING_SYSTEM can land in touched_ids — both queries below
    # are scoped to venue+ACTIVE+ACCOUNTING_SYSTEM so such an id is simply
    # never matched/written by either one.
    touched_ids = {item.id for item in resolved if item is not None}
    if present_matrix_item_ids:
        touched_ids |= set(present_matrix_item_ids)
    if mark_missing:
        VenueProductMatrixItem.objects.filter(
            venue=venue, status=EntityStatus.ACTIVE, source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM
        ).exclude(pk__in=touched_ids).update(missing_from_latest_import=True)
    if touched_ids:
        VenueProductMatrixItem.objects.filter(
            pk__in=touched_ids,
            venue=venue,
            status=EntityStatus.ACTIVE,
            source=VenueProductMatrixItemSource.ACCOUNTING_SYSTEM,
            missing_from_latest_import=True,
        ).update(missing_from_latest_import=False)

    return [
        SimpleNamespace(
            raw_name=decision["raw_name"],
            action=decision["action"],
            matrix_item_id=None if item is None else item.id,
            matrix_item_requested_name=None if item is None else item.requested_name,
            alias_created=item is not None,
            conflict=conflicts.get(index),
        )
        for index, (decision, item) in enumerate(zip(decisions, resolved))
    ]
