"""Master product primitives used by both the product matrix and the master catalog."""

from django.db import transaction

from api.v1.catalog.selectors import (
    _venue_sole_active_owner_id,
    find_candidate_master_products,
    master_product_match_min_score,
)
from catalog.models import (
    MasterProduct,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    User,
    Venue,
)


def _auto_assign_master_products_to_new_items(*, venue, items, prepared_matching=None):
    """Batch-safe version of the §11.1 auto-match rule (TASK-38), shared by
    create_product_matrix_item (one row) and apply_venue_import's
    create_new rows (TASK-45 followup, services.py). `items` are unsaved
    VenueProductMatrixItem instances belonging to `venue`; each gets its
    .master_product/.needs_review resolved in place, in the given order, so
    two rows in the same call never claim the same MasterProduct — each
    claim updates `occupied_master_ids` before the next item is resolved.

    Locks the Venue row via select_for_update() before resolving anything:
    without it, two concurrent top-level calls for the same venue (e.g. two
    overlapping POST /product-matrix requests) can each pass the
    "not already linked" check before either commits, and the second
    .save() then violates vpmi_master_product_venue_uniq with an unhandled
    IntegrityError (500) — the per-call occupied_master_ids check alone
    only protects against that race within a single call, not across two
    callers. See CreateProductMatrixItemAutoMatchConcurrencyTests.

    Must be called from within an existing @transaction.atomic block —
    select_for_update() requires an open transaction and raises
    TransactionManagementError otherwise."""
    if not items:
        return
    owner_id = _venue_sole_active_owner_id(venue.id)
    if owner_id is None:
        return
    owner = User.objects.get(pk=owner_id)
    Venue.objects.select_for_update().get(pk=venue.pk)

    # vpmi_master_product_venue_uniq (catalog/models.py) has no status
    # filter, so an archived row still claims this (master_product, venue)
    # slot — matching the constraint's own condition here, not just ACTIVE.
    occupied_master_ids = set(
        VenueProductMatrixItem.objects.filter(venue=venue, master_product__isnull=False).values_list(
            "master_product_id", flat=True
        )
    )
    min_score = master_product_match_min_score()
    for item in items:
        if prepared_matching is None:
            candidates = find_candidate_master_products(name=item.requested_name, owner=owner)
        else:
            snapshot = prepared_matching["rows"].get(item.requested_name)
            if snapshot is None or owner.id != prepared_matching["owner_id"]:
                item.needs_review = True
                continue
            candidates = []
            changed = False
            current = MasterProduct.objects.filter(
                pk__in=[row[0] for row in snapshot["candidates"]], owner=owner, status=EntityStatus.ACTIVE
            ).in_bulk()
            for identifier, expected_version, score in snapshot["candidates"]:
                product = current.get(identifier)
                if product is None or product.updated_at != expected_version:
                    changed = True
                else:
                    candidates.append((product, float(score)))
            if changed:
                item.needs_review = True
                continue
            for product, vector in prepared_matching["created"]:
                numerator = sum(a * b for a, b in zip(snapshot["vector"], vector, strict=True))
                denominator = (sum(a * a for a in snapshot["vector"]) * sum(b * b for b in vector)) ** 0.5
                if denominator:
                    candidates.append((product, numerator / denominator))
            candidates.sort(key=lambda candidate: (-candidate[1], str(candidate[0].id)))
        top = candidates[0] if candidates else None
        if top is None:
            master = create_master_product(
                owner=owner,
                canonical_name=item.requested_name,
                embedding=snapshot["vector"] if prepared_matching is not None else None,
            )
            item.master_product = master
            if prepared_matching is not None:
                prepared_matching["created"].append((master, snapshot["vector"]))
            occupied_master_ids.add(master.id)
        elif top[1] >= min_score and top[0].id not in occupied_master_ids:
            item.master_product = top[0]
            occupied_master_ids.add(top[0].id)
        else:
            item.needs_review = True


@transaction.atomic
def create_master_product(*, owner, canonical_name, unit=None, net_value=None, note=None, embedding=None):
    return MasterProduct.objects.create(
        owner=owner,
        canonical_name=canonical_name,
        embedding=embedding,
        unit=unit,
        net_value=net_value,
        note=note,
        status=EntityStatus.ACTIVE,
    )


class MasterImportQueueUnavailable(Exception):
    """Задачу обработки импорта не удалось поставить в очередь; upload уже помечен failed."""


class MasterProductVenueLinkConflict(Exception):
    """Spec §16: linking a master product into a venue's own catalog must
    not silently create a duplicate row when the venue already has an
    unlinked item with the same name — raised by
    link_master_product_to_venue instead of creating, carrying the
    candidate row so the caller can offer "link existing / create new"."""

    def __init__(self, candidate):
        self.candidate = candidate
        super().__init__(f"Similar unlinked matrix item found: {candidate.id}")


class MasterProductAlreadyLinkedConflict(Exception):
    """The selected master product already occupies this venue's unique slot."""

    def __init__(self, existing_item):
        self.existing_item = existing_item
        super().__init__(f"Master product is already linked to matrix item {existing_item.id}")
