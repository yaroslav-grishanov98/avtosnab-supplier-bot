"""Master catalog reads and import matching."""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import connection
from django.db.models import Count, OuterRef, Q
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from api.v1.catalog.candidate_ranking_llm import CandidateRankingError, rerank_candidates, rerank_venue_candidates
from api.v1.catalog.embeddings import create_embedding, create_embeddings
from api.v1.catalog.search import apply_directory_product_search
from api.v1.catalog.unit_llm_client import UnitSuggestionError, suggest_unit_ids, suggest_venue_unit_ids
from api.v1.measure import round_net_value
from catalog.models import (
    MasterProduct,
    VenueProductImportAlias,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    Unit,
    User,
    Venue,
    VenueUser,
    VenueUserRole,
)

from .common import _owner_active_venue_ids
from .supplier_prices import resolve_supplier_product_price


def _is_owners_active_venue(*, owner_id, venue_id):
    """2026-09-01 fix wave (Fix 3): also requires the venue itself to be
    ACTIVE, not just the owner's VenueUser row — delete_venue_cabinet
    soft-archives a venue (Venue.status=ARCHIVED) while leaving VenueUser
    rows in place, so without this check an archived venue would still
    accept a new master-catalog link (same root cause as Fix 2's
    companies_count drift). Moved here from services.py
    (master-catalog-import-stage-2 plan) so selectors.py's own
    resolve_master_product_matches_for_import can use it without a
    circular import — services.py already imports from selectors.py, never
    the other way around."""
    return VenueUser.objects.filter(
        venue_id=venue_id,
        venue__status=EntityStatus.ACTIVE,
        user_id=owner_id,
        role=VenueUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    ).exists()


def _venue_sole_active_owner_id(venue_id):
    """§11.1 raw ТЗ: which owner's master catalog to auto-match a manually
    added company-catalog item against. Returns that owner's user id only
    when exactly one core.User currently holds an active OWNER VenueUser
    row at this venue — zero (e.g. a bot/service-token call before any
    owner is set up) or more than one (shared ownership) is deliberately
    treated as "don't know which catalog", never a guess."""
    owner_ids = list(
        VenueUser.objects.filter(venue_id=venue_id, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        .values_list("user_id", flat=True)
        .distinct()
    )
    return owner_ids[0] if len(owner_ids) == 1 else None


def _companies_count_annotation(owner_id_expr):
    return Count(
        "matrix_items",
        filter=Q(matrix_items__status=EntityStatus.ACTIVE)
        & Q(matrix_items__venue__status=EntityStatus.ACTIVE)
        & Q(matrix_items__venue_id__in=_owner_active_venue_ids(owner_id_expr)),
        distinct=True,
    )


def master_products_queryset(owner_id, *, q=None):
    queryset = (
        MasterProduct.objects.filter(owner_id=owner_id, status=EntityStatus.ACTIVE)
        .annotate(companies_count=_companies_count_annotation(owner_id))
        .select_related("unit")
    )
    if q:
        return apply_directory_product_search(
            queryset,
            q,
            fields=("canonical_name", "note", "unit__code", "unit__name"),
            name_field="canonical_name",
        )
    return queryset.order_by("canonical_name", "id")


def master_product_suggestions_for_venue(*, venue, name=None, q=None, limit=10, offset=0):
    """Frontend picker for the prototype's "Мастер-товар" field.

    ``name`` produces semantic recommendations before the user searches;
    ``q`` produces explicit text-search results. Both are scoped to the sole
    active owner of the venue and mark master products whose one-per-venue
    slot is already occupied, so the frontend never has to discover that only
    after a failed save.

    ``offset`` only pages through ``search_results`` (the ``q`` branch).
    ``recommended`` (the ``name`` branch) is always the top ``limit`` matches
    ranked by score — offsetting a ranked top-N list doesn't have a sane
    meaning, so it's ignored there by design (TASK-53, 2026-09-11).
    """
    from types import SimpleNamespace

    owner_id = _venue_sole_active_owner_id(venue.id)
    if owner_id is None:
        raise ValidationError(
            {"venue_id": "Для компании не удалось однозначно определить владельца Мастер-справочника."}
        )
    owner = get_object_or_404(User, pk=owner_id)
    occupied_ids = set(
        VenueProductMatrixItem.objects.filter(venue=venue, master_product__isnull=False).values_list(
            "master_product_id", flat=True
        )
    )

    def as_row(master_product, score):
        occupied = master_product.id in occupied_ids
        return SimpleNamespace(
            master_product_id=master_product.id,
            canonical_name=master_product.canonical_name,
            unit_id=master_product.unit_id,
            unit_code=master_product.unit.code if master_product.unit_id else None,
            net_value=master_product.net_value,
            note=master_product.note,
            companies_count=master_product.companies_count,
            score=score,
            already_linked_to_venue=occupied,
            can_link=not occupied,
        )

    recommended = []
    if name:
        candidates = find_candidate_master_products(name=name, owner=owner, limit=limit)
        candidate_scores = {
            product.id: score for product, score in candidates if score >= master_product_match_min_score()
        }
        hydrated = {
            product.id: product for product in master_products_queryset(owner_id).filter(id__in=candidate_scores)
        }
        recommended = [
            as_row(hydrated[product.id], score)
            for product, score in candidates
            if product.id in hydrated and score >= master_product_match_min_score()
        ]

    search_results = []
    search_results_count = 0
    if q:
        search_queryset = master_products_queryset(owner_id, q=q)
        search_results_count = search_queryset.count()
        search_results = [as_row(product, None) for product in search_queryset[offset : offset + limit]]

    return SimpleNamespace(
        owner_id=owner_id,
        mode="search" if q else "recommended",
        query=q or name,
        recommended=recommended,
        search_results=search_results,
        search_results_count=search_results_count,
    )


def master_product_detail_queryset():
    return (
        MasterProduct.objects.filter(status=EntityStatus.ACTIVE)
        .annotate(companies_count=_companies_count_annotation(OuterRef("owner_id")))
        .select_related("unit")
    )


def master_product_venue_breakdown(master_product):
    from types import SimpleNamespace

    owner_venue_ids = _owner_active_venue_ids(master_product.owner_id)
    venues = Venue.objects.filter(id__in=owner_venue_ids, status=EntityStatus.ACTIVE).order_by("display_name", "id")
    linked_items = {
        item.venue_id: item
        for item in VenueProductMatrixItem.objects.filter(
            master_product=master_product, venue_id__in=owner_venue_ids, status=EntityStatus.ACTIVE
        ).select_related("supplier_product", "supplier_product__supplier")
    }

    rows = []
    for venue in venues:
        item = linked_items.get(venue.id)
        sp = item.supplier_product if item else None
        rows.append(
            SimpleNamespace(
                venue_id=venue.id,
                venue_name=venue.display_name,
                linked=item is not None,
                matrix_item_id=item.id if item else None,
                requested_name=item.requested_name if item else None,
                supplier_product_id=sp.id if sp else None,
                supplier_product_name=sp.normalized_name if sp else None,
                price=resolve_supplier_product_price(supplier_product=sp, venue_id=venue.id)[0] if sp else None,
            )
        )
    return rows


def master_product_venue_batch_current(*, master_product, venue_ids):
    """TASK-52: same shape as master_product_venue_breakdown, but scoped to
    an explicit venue_id set (the rows a batch save actually touches, not
    every owner venue) and carrying live model instances (venue, matrix_item)
    alongside the display fields — the batch apply function (Task 6) needs
    to act on the objects, not just show their ids."""
    from types import SimpleNamespace

    venues = {v.id: v for v in Venue.objects.filter(id__in=venue_ids, status=EntityStatus.ACTIVE)}
    linked_items = {
        item.venue_id: item
        for item in VenueProductMatrixItem.objects.filter(
            master_product=master_product, venue_id__in=venue_ids, status=EntityStatus.ACTIVE
        ).select_related("supplier_product", "supplier_product__supplier")
    }

    result = {}
    for venue_id in venue_ids:
        venue = venues.get(venue_id)
        item = linked_items.get(venue_id)
        sp = item.supplier_product if item else None
        result[venue_id] = SimpleNamespace(
            venue=venue,
            venue_name=venue.display_name if venue else None,
            linked=item is not None,
            matrix_item=item,
            matrix_item_id=item.id if item else None,
            requested_name=item.requested_name if item else None,
            supplier_product_id=sp.id if sp else None,
            supplier_product_name=sp.normalized_name if sp else None,
            price=resolve_supplier_product_price(supplier_product=sp, venue_id=venue_id)[0] if sp else None,
        )
    return result


def master_product_bulk_supplier_product_diff(*, items, new_supplier_product):
    """ "Сейчас → Будет" rows for a proposed bulk supplier_product assignment
    (spec §4.4/§6 rule 9) — same shape and same inputs whether called for a
    read-only preview or right before committing the write, so the two can
    never show the caller a different diff than what apply() actually
    does."""
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            venue_id=item.venue_id,
            venue_name=item.venue.display_name,
            matrix_item_id=item.id,
            requested_name=item.requested_name,
            current_supplier_product_id=item.supplier_product_id,
            current_supplier_product_name=(item.supplier_product.normalized_name if item.supplier_product_id else None),
            new_supplier_product_id=new_supplier_product.id,
            new_supplier_product_name=new_supplier_product.normalized_name,
            changed=item.supplier_product_id != new_supplier_product.id,
        )
        for item in items
    ]


def master_product_match_min_score():
    return float(getattr(settings, "MASTER_PRODUCT_MATCH_MIN_SCORE", 0.5) or 0.5)


def master_product_match_top_k():
    return int(getattr(settings, "MASTER_PRODUCT_MATCH_TOP_K", 5) or 5)


def master_product_match_confident_score():
    return float(getattr(settings, "MASTER_PRODUCT_MATCH_CONFIDENT_SCORE", 0.8) or 0.8)


VENUE_IMPORT_MATCH_TOP_K = 5


def venue_import_match_min_similarity():
    return float(getattr(settings, "VENUE_IMPORT_MATCH_MIN_SIMILARITY", 0.3) or 0.3)


def venue_import_match_confident_similarity():
    return float(getattr(settings, "VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY", 0.8) or 0.8)


def _parse_import_decimal(value, *, row_number):
    if not value:
        return None
    try:
        return round_net_value(Decimal(str(value).strip().replace(" ", "").replace(",", ".")))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError({"file": f"Строка {row_number}: некорректное значение нетто: {value}."}) from exc


def _vector_literal(vector):
    return "[" + ",".join(str(float(component)) for component in vector) + "]"


def _ensure_master_product_embeddings(owner):
    """Computes and stores embeddings for this owner's active MasterProduct
    rows that don't have one yet — covers both rows created before this
    column existed (Plans 1-5) and rows created by
    apply_master_product_import_matches's create_new decisions (services.py;
    that function deliberately leaves embedding NULL at creation time so the
    write path doesn't depend on the embedding model — see its docstring).
    Writes raw SQL directly, never through the ORM's value-prep pipeline —
    see this plan's Global Constraints for why."""
    missing = list(
        MasterProduct.objects.filter(owner=owner, status=EntityStatus.ACTIVE, embedding__isnull=True).only(
            "id", "canonical_name"
        )
    )
    if not missing:
        return
    vectors = create_embeddings([mp.canonical_name for mp in missing])
    with connection.cursor() as cursor:
        cursor.executemany(
            f"UPDATE {MasterProduct._meta.db_table} SET embedding = %s::vector WHERE id = %s",
            [(_vector_literal(vector), str(mp.id)) for mp, vector in zip(missing, vectors, strict=True)],
        )


def find_candidate_master_products(*, name, owner, limit=None):
    """Spec §3.2 contract (frozen 2026-08-31):
    find_candidate_master_products(name, owner) -> [(MasterProduct, score)],
    sorted by score descending. Embeds `name` with the same local
    sentence-transformers model already used synchronously in the live
    catalog search request path (api/v1/catalog/embeddings.py), then finds
    this owner's nearest active MasterProduct rows by pgvector cosine
    similarity. Lazily backfills any of this owner's MasterProduct rows
    missing an embedding before searching (_ensure_master_product_embeddings)
    — self-healing, so no separate backfill management command is needed.
    "Found" (spec §3.2, п. 11.1 — top-score >= a configurable threshold) is
    the caller's job (see resolve_master_product_matches_for_import below);
    this function only returns the ranked candidates."""
    _ensure_master_product_embeddings(owner)
    vector_literal = _vector_literal(create_embedding(name))
    limit = limit or master_product_match_top_k()
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, 1 - (embedding <=> %s::vector) AS score
            FROM {MasterProduct._meta.db_table}
            WHERE owner_id = %s AND status = %s AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [vector_literal, str(owner.id), EntityStatus.ACTIVE, vector_literal, limit],
        )
        rows = cursor.fetchall()
    products_by_id = MasterProduct.objects.select_related("unit").in_bulk([row[0] for row in rows])
    return [(products_by_id[row[0]], float(row[1])) for row in rows if row[0] in products_by_id]


def resolve_master_product_matches_for_import(*, venue, owner_id, matrix_item_ids):
    """Spec §3.7/§4.4 stage 2: for a batch of this venue's
    VenueProductMatrixItem rows (normally the ones stage 1's
    apply_venue_import, Plan 5, just touched), decide whether each should
    link to an existing MasterProduct of owner_id or become a new one.
    Unlike stage 1, there is no VenueProductImportAlias-style memory for
    this decision (spec: that model is never tied to stage 2) — an item
    that already has a master_product simply reports its current link as
    proposed_action="keep_existing" (idempotent re-run, nothing to ask).
    Same "Сейчас → Будет" shape as master_product_bulk_supplier_product_diff
    (Plan 4): current_* is what's there now, proposed_* is what preview
    suggests — apply_master_product_import_matches (services.py) always
    requires the caller's own explicit decision, never applies a proposal
    just because it was found (spec §6 rule 9)."""
    from types import SimpleNamespace

    if not _is_owners_active_venue(owner_id=owner_id, venue_id=venue.id):
        raise ValidationError({"owner_id": "У этого владельца нет активного доступа к этой компании."})
    owner = get_object_or_404(User, pk=owner_id)

    items_by_id = {
        item.id: item
        for item in VenueProductMatrixItem.objects.filter(
            pk__in=set(matrix_item_ids), venue=venue, status=EntityStatus.ACTIVE
        ).select_related("master_product")
    }
    missing = [str(mid) for mid in matrix_item_ids if mid not in items_by_id]
    if missing:
        raise ValidationError(
            {"matrix_item_ids": f"Не найдены в товарной матрице этой компании: {', '.join(missing)}."}
        )

    min_score = master_product_match_min_score()
    confident_score = master_product_match_confident_score()

    # Position i corresponds to position i in matrix_item_ids. None for items
    # that already have a master_product (nothing to rank for those).
    raw_candidates_by_position = [None] * len(matrix_item_ids)
    pending_candidates = [None] * len(matrix_item_ids)
    for position, matrix_item_id in enumerate(matrix_item_ids):
        item = items_by_id[matrix_item_id]
        if item.master_product_id:
            continue
        raw_candidates = find_candidate_master_products(name=item.requested_name, owner=owner)
        raw_candidates_by_position[position] = raw_candidates
        pending_candidates[position] = [(mp, score) for mp, score in raw_candidates if score >= min_score]

    if getattr(settings, "MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED", False):
        rows_for_llm = [
            {
                "row_number": position + 1,
                "raw_name": items_by_id[matrix_item_ids[position]].requested_name,
                "candidates": [{"id": str(mp.id), "canonical_name": mp.canonical_name} for mp, score in candidates],
            }
            for position, candidates in enumerate(pending_candidates)
            if candidates and min_score <= candidates[0][1] < confident_score
        ]
        if rows_for_llm:
            try:
                rankings = rerank_candidates(rows=rows_for_llm)
            except CandidateRankingError:
                rankings = {}
            for row_number, ranked_ids in rankings.items():
                position = row_number - 1
                candidates = pending_candidates[position]
                candidates_by_id = {str(mp.id): (mp, score) for mp, score in candidates}
                reordered = [candidates_by_id[cid] for cid in ranked_ids if cid in candidates_by_id]
                if reordered:
                    pending_candidates[position] = reordered

    rows = []
    for position, matrix_item_id in enumerate(matrix_item_ids):
        item = items_by_id[matrix_item_id]
        if item.master_product_id:
            rows.append(
                SimpleNamespace(
                    matrix_item_id=item.id,
                    requested_name=item.requested_name,
                    current_master_product_id=item.master_product_id,
                    current_master_product_canonical_name=item.master_product.canonical_name,
                    proposed_master_product_id=None,
                    proposed_master_product_canonical_name=None,
                    proposed_action="keep_existing",
                    score=None,
                )
            )
            continue

        raw_candidates = raw_candidates_by_position[position]
        top_raw = raw_candidates[0] if raw_candidates else None
        found = top_raw is not None and top_raw[1] >= min_score
        # When found, use the (possibly LLM-reordered) pending_candidates top —
        # that's the whole point of the ranking feature. When not found, restore
        # the pre-ranking-feature behavior: report the real top score even
        # though it's below min_score, instead of None.
        top = pending_candidates[position][0] if found else top_raw
        rows.append(
            SimpleNamespace(
                matrix_item_id=item.id,
                requested_name=item.requested_name,
                current_master_product_id=None,
                current_master_product_canonical_name=None,
                proposed_master_product_id=top[0].id if found else None,
                proposed_master_product_canonical_name=top[0].canonical_name if found else None,
                proposed_action="link_existing" if found else "create_new",
                score=top[1] if top else None,
            )
        )
    return rows


def resolve_master_product_import_rows(*, owner, rows):
    """TASK-51 preview, reworked by TASK-71 (ТЗ «Проверка перед импортом» §46):
    for each raw file row (from parse_catalog_file), report up to
    MASTER_PRODUCT_MATCH_TOP_K candidates with score >=
    master_product_match_min_score(), best first. Preview never picks an
    action on the user's behalf when a candidate exists: action is None
    ("требует проверки") and the user chooses replace / skip / create_new;
    only a row with no candidate at all defaults to create_new. A candidate
    below the cutoff is not reported — vector search almost always returns
    *some* top-1, so the cutoff is the only meaningful "found a similar one"
    signal. candidate_master_product_id / candidate_canonical_name / score
    mirror candidates[0] for older clients. Unit labels are resolved up
    front; a label not found in the units reference flags that row's
    unit_unrecognized=True (unit_id stays None) instead of aborting the
    whole preview (TASK-77, ТЗ §52) — every other row is still matched
    normally.
    A flagged row is then offered to unit_llm_client.suggest_unit_ids
    (TASK-75, ТЗ §35-39) as a last attempt to resolve it automatically —
    on a confident match from the closed Unit list the row's
    unit_unrecognized flips back to False and unit_source is set to
    "llm"; any failure or low-confidence non-answer leaves the row exactly
    as it was before this call existed."""
    from types import SimpleNamespace

    units_by_key = {}
    for unit in Unit.objects.filter(status=EntityStatus.ACTIVE):
        units_by_key.setdefault(unit.code.casefold(), unit)
        units_by_key.setdefault(unit.name.casefold(), unit)

    resolved_units = {}
    unrecognized_row_numbers = set()
    for row in rows:
        unit_label = row.get("unit")
        if not unit_label:
            continue
        unit = units_by_key.get(str(unit_label).strip().casefold())
        if unit is None:
            # TASK-77 (ТЗ §52): an unrecognized unit no longer aborts the
            # whole preview — the row is flagged unit_unrecognized so the
            # user can pick one manually, the rest of the file still gets
            # matched. apply still requires a real unit_id for replace
            # (TASK-71) and doesn't require one for create_new (TASK-81,
            # decision: left optional) — this only softens preview.
            unrecognized_row_numbers.add(row.get("row_number"))
            continue
        resolved_units[row.get("row_number")] = unit

    llm_assigned_row_numbers = set()
    if (
        unrecognized_row_numbers
        and not any(row.get("_parsed") for row in rows)
        and getattr(settings, "MASTER_CATALOG_UNIT_LLM_ENABLED", True)
    ):
        rows_by_number = {row.get("row_number"): row for row in rows}
        unique_units = {unit.id: unit for unit in units_by_key.values()}.values()
        units_by_id = {str(unit.id): unit for unit in unique_units}
        llm_input_rows = [
            {
                "row_number": row_number,
                "raw_name": str(rows_by_number[row_number].get("name") or "").strip(),
                "unit_label": rows_by_number[row_number].get("unit"),
            }
            for row_number in sorted(unrecognized_row_numbers)
        ]
        units_payload = [{"id": unit_id, "code": unit.code, "name": unit.name} for unit_id, unit in units_by_id.items()]
        # Capture the row numbers actually offered to the LLM *before* the
        # call, since the results loop below mutates unrecognized_row_numbers
        # via .discard(...) — without this snapshot a row_number the model
        # invents/renumbers (never offered) could silently overwrite an
        # already-correctly-resolved row's unit and mark it unit_source="llm",
        # letting it skip manual review entirely (final-review finding).
        offered_row_numbers = set(unrecognized_row_numbers)
        try:
            suggestions = suggest_unit_ids(rows=llm_input_rows, units=units_payload)
        except UnitSuggestionError:
            suggestions = {}
        for row_number, unit_id in suggestions.items():
            if row_number not in offered_row_numbers:
                continue
            unit = units_by_id.get(unit_id)
            if unit is None:
                continue
            unrecognized_row_numbers.discard(row_number)
            resolved_units[row_number] = unit
            llm_assigned_row_numbers.add(row_number)

    min_score = master_product_match_min_score()
    confident_score = master_product_match_confident_score()

    # Pass 1: compute each row's candidates from the vector search, exactly as before.
    candidates_by_row = {}
    for row in rows:
        row_number = row.get("row_number")
        raw_name = str(row.get("name") or "").strip()
        candidates_by_row[row_number] = (
            raw_name,
            [
                SimpleNamespace(
                    master_product_id=master_product.id,
                    canonical_name=master_product.canonical_name,
                    unit_id=master_product.unit_id,
                    unit_name=master_product.unit.name if master_product.unit_id else None,
                    updated_at=master_product.updated_at,
                    score=score,
                )
                for master_product, score in find_candidate_master_products(name=raw_name, owner=owner)
                if score >= min_score
            ],
        )

    # TASK-75 §40-42: LLM re-ranks only the "ambiguous zone" — a top vector
    # score that cleared MIN_SCORE but isn't confidently above
    # CONFIDENT_SCORE. Confident or absent candidates skip the LLM call
    # entirely (cost/latency). The LLM never gets veto power over
    # found/not-found (§46) — that decision already happened above, purely
    # from the vector score.
    if getattr(settings, "MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED", False):
        rows_for_llm = [
            {
                "row_number": row_number,
                "raw_name": raw_name,
                "candidates": [
                    {"id": str(candidate.master_product_id), "canonical_name": candidate.canonical_name}
                    for candidate in candidates
                ],
            }
            for row_number, (raw_name, candidates) in candidates_by_row.items()
            if candidates and min_score <= candidates[0].score < confident_score
        ]
        if rows_for_llm:
            try:
                rankings = rerank_candidates(rows=rows_for_llm)
            except CandidateRankingError:
                rankings = {}
            for row_number, ranked_ids in rankings.items():
                raw_name, candidates = candidates_by_row[row_number]
                candidates_by_id = {str(candidate.master_product_id): candidate for candidate in candidates}
                reordered = [candidates_by_id[cid] for cid in ranked_ids if cid in candidates_by_id]
                if reordered:
                    candidates_by_row[row_number] = (raw_name, reordered)

    # Pass 2: build the result rows from (possibly LLM-reordered) candidates.
    result = []
    for row in rows:
        row_number = row.get("row_number")
        raw_name, candidates = candidates_by_row[row_number]
        unit = resolved_units.get(row_number)
        top = candidates[0] if candidates else None
        result.append(
            SimpleNamespace(
                row_number=row_number,
                raw_name=raw_name,
                unit_label=row.get("unit"),
                unit_id=unit.id if unit else None,
                unit_unrecognized=row_number in unrecognized_row_numbers,
                unit_source="llm" if row_number in llm_assigned_row_numbers else None,
                net_value=_parse_import_decimal(row.get("net"), row_number=row_number),
                note=str(row["note"]).strip() if row.get("note") else None,
                action=None if candidates else "create_new",
                candidates=candidates,
                candidate_master_product_id=top.master_product_id if top else None,
                candidate_canonical_name=top.canonical_name if top else None,
                score=top.score if top else None,
                # Final-review finding: an unrecognized unit (present in the
                # file but not matched to the units reference) must also
                # require manual review before apply, per raw ТЗ §52
                # ("отсутствует/не определена → требовать ручной выбор") and
                # this field's own help_text — even when there are no
                # name-similarity candidates. Distinct from TASK-81 (unit
                # simply absent from the file, which is fine for
                # create_new): this is a unit that IS present but unknown.
                needs_attention=bool(candidates) or row_number in unrecognized_row_numbers,
            )
        )
    return result


def _venue_import_candidates(*, venue, raw_names):
    """TASK-71: up to VENUE_IMPORT_MATCH_TOP_K of this venue's ACTIVE matrix
    items per raw name, in ONE query for the whole batch (unnest + LATERAL).
    A case-insensitive exact name match always ranks first with score 1.0
    (pg_trgm ignores punctuation, so e.g. "Молоко 3,2%" and "молоко 3.2%" both
    score 1.0 against "Молоко 3.2%" — the exact one must still win); the rest
    by pg_trgm similarity >= venue_import_match_min_similarity(), then lowest
    id. Ids are uuid4, so this tie-break is deterministic but arbitrary (the
    lowest UUID, not the earliest-created item). Returns {0-based position in
    raw_names: [SimpleNamespace(matrix_item_id, requested_name,
    requested_unit_label, score)]}; positions without candidates are absent.

    TASK-78: the WHERE clause also filters on the `%` trigram operator
    (item.requested_name %% r.raw_name) so Postgres can use the
    vpmi_requested_name_trgm_gin GIN index — this only narrows what the
    engine has to run the real similarity() >= threshold check against, it
    never changes which rows qualify, as long as
    VENUE_IMPORT_MATCH_MIN_SIMILARITY stays >= 0.3 (pg_trgm's own default
    threshold for %) — see this task's spec for what happens below that.
    """
    from types import SimpleNamespace

    if not raw_names:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT r.idx, m.id, m.requested_name, m.requested_unit_label, m.updated_at, m.score
            FROM unnest(%s::text[]) WITH ORDINALITY AS r(raw_name, idx)
            CROSS JOIN LATERAL (
                SELECT
                    item.id,
                    item.requested_name,
                    item.requested_unit_label,
                    item.updated_at,
                    lower(item.requested_name) = lower(r.raw_name) AS is_exact,
                    CASE
                        WHEN lower(item.requested_name) = lower(r.raw_name) THEN 1.0::float8
                        ELSE similarity(item.requested_name, r.raw_name)::float8
                    END AS score
                FROM {VenueProductMatrixItem._meta.db_table} AS item
                WHERE item.venue_id = %s
                  AND item.status = %s
                  AND (
                      lower(item.requested_name) = lower(r.raw_name)
                      OR (
                          item.requested_name %% r.raw_name
                          AND similarity(item.requested_name, r.raw_name) >= %s
                      )
                  )
                ORDER BY is_exact DESC, score DESC, item.id ASC
                LIMIT %s
            ) AS m
            ORDER BY r.idx, m.is_exact DESC, m.score DESC, m.id ASC
            """,
            [
                list(raw_names),
                str(venue.id),
                EntityStatus.ACTIVE,
                venue_import_match_min_similarity(),
                VENUE_IMPORT_MATCH_TOP_K,
            ],
        )
        fetched = cursor.fetchall()
    candidates = {}
    for idx, item_id, requested_name, requested_unit_label, updated_at, score in fetched:
        candidates.setdefault(idx - 1, []).append(
            SimpleNamespace(
                matrix_item_id=item_id,
                requested_name=requested_name,
                requested_unit_label=requested_unit_label,
                updated_at=updated_at,
                score=float(score),
            )
        )
    return candidates


def resolve_venue_import_rows(*, venue, raw_names, unit_labels=None, allow_unit_llm=True):
    """Spec §3.7/§4.4 stage 1, reworked by TASK-71 (ТЗ «Проверка перед
    импортом» §46): for each raw name from an import batch, report up to 5
    similar ACTIVE matrix items of this venue as candidates
    (_venue_import_candidates — exact case-insensitive match first, then
    pg_trgm similarity). A found candidate never becomes an action by itself:
    action is None ("требует проверки") and the user chooses replace /
    update_existing / skip / create_new; no candidate -> create_new. An
    existing VenueProductImportAlias for (venue, raw_name) still wins and is
    reported as action="update_existing", auto_resolved=True, candidates=[]
    (no re-asking, spec §3.7) — the one legitimate auto-resolution.
    matrix_item_id / matrix_item_requested_name are the alias target or
    candidates[0]. Does not deduplicate raw_names — each occurrence is
    resolved independently, so preview/apply always agree row-for-row with
    the caller's input.

    An alias whose matrix_item has since been archived is deliberately NOT
    honoured (matrix_item__status filter below): apply_venue_import only
    accepts ACTIVE targets, so trusting such an alias here would hand the
    caller an auto_resolved=True row — one that never surfaces a dialog —
    that apply then rejects, with no way for the user to fix it. Falling
    through to the normal name-matching path keeps preview and apply in
    agreement, which is this feature's core invariant.

    TASK-122 п.1 (raw ТЗ §1/§35-39): when unit_labels is given (positionally
    aligned with raw_names, same length, any element may be None), each
    non-aliased row's label is resolved against the closed core.Unit list
    (case-insensitive code/name match); a label present but unmatched is
    offered to unit_llm_client.suggest_venue_unit_ids as a last attempt —
    on a confident match the row's unit_unrecognized flips back to False and
    unit_source is set to "llm". unit_labels=None (the default, and the only
    mode the raw_names-only "без файла" preview ever uses) skips this whole
    step: every row gets unit_id=None, unit_unrecognized=False,
    unit_source=None, identical to this function's behaviour before this
    parameter existed. An aliased row never gets a unit decision either
    (action is already "update_existing", nothing to suggest)."""
    from types import SimpleNamespace

    aliases = {
        alias.raw_name: alias
        for alias in VenueProductImportAlias.objects.filter(
            venue=venue, raw_name__in=raw_names, matrix_item__status=EntityStatus.ACTIVE
        ).select_related("matrix_item")
    }

    unaliased_pairs = [
        (raw_name, (unit_labels[position] if unit_labels is not None else None))
        for position, raw_name in enumerate(raw_names)
        if raw_name not in aliases
    ]
    unaliased_names = [raw_name for raw_name, _ in unaliased_pairs]
    unaliased_unit_labels = [unit_label for _, unit_label in unaliased_pairs]
    candidates_by_position = _venue_import_candidates(venue=venue, raw_names=unaliased_names)

    resolved_units = {}
    unrecognized_positions = set()
    llm_assigned_positions = set()
    if unit_labels is not None:
        units_by_key = {}
        for unit in Unit.objects.filter(status=EntityStatus.ACTIVE):
            units_by_key.setdefault(unit.code.casefold(), unit)
            units_by_key.setdefault(unit.name.casefold(), unit)

        for position, unit_label in enumerate(unaliased_unit_labels):
            if not unit_label:
                continue
            unit = units_by_key.get(str(unit_label).strip().casefold())
            if unit is None:
                unrecognized_positions.add(position)
                continue
            resolved_units[position] = unit

        if unrecognized_positions and allow_unit_llm and getattr(settings, "VENUE_IMPORT_UNIT_LLM_ENABLED", True):
            unique_units = {unit.id: unit for unit in units_by_key.values()}.values()
            units_by_id = {str(unit.id): unit for unit in unique_units}
            llm_input_rows = [
                {
                    "row_number": position + 1,
                    "raw_name": unaliased_names[position],
                    "unit_label": unaliased_unit_labels[position],
                }
                for position in sorted(unrecognized_positions)
            ]
            units_payload = [
                {"id": unit_id, "code": unit.code, "name": unit.name} for unit_id, unit in units_by_id.items()
            ]
            offered_positions = set(unrecognized_positions)
            try:
                suggestions = suggest_venue_unit_ids(rows=llm_input_rows, units=units_payload)
            except UnitSuggestionError:
                suggestions = {}
            for row_number, unit_id in suggestions.items():
                position = row_number - 1
                if position not in offered_positions:
                    continue
                unit = units_by_id.get(unit_id)
                if unit is None:
                    continue
                unrecognized_positions.discard(position)
                resolved_units[position] = unit
                llm_assigned_positions.add(position)

    # TASK-122 (raw ТЗ §1/§40-42): same ambiguous-zone LLM re-ranking as
    # resolve_master_product_import_rows (TASK-75 stage 1), now for this
    # справочник too — LLM only reorders/narrows the pg_trgm candidate list
    # already computed above, never adds a candidate or decides found/not-found
    # (§46 unaffected). Aliased rows never reach this: they short-circuited
    # above with action="update_existing" and no candidates to rank.
    if getattr(settings, "VENUE_IMPORT_CANDIDATE_RANKING_ENABLED", False):
        min_score = venue_import_match_min_similarity()
        confident_score = venue_import_match_confident_similarity()
        rows_for_llm = [
            {
                "row_number": position + 1,  # CandidateRanking.row_number requires >= 1
                "raw_name": unaliased_names[position],
                "candidates": [
                    {"id": str(candidate.matrix_item_id), "canonical_name": candidate.requested_name}
                    for candidate in candidates
                ],
            }
            for position, candidates in candidates_by_position.items()
            if candidates and min_score <= candidates[0].score < confident_score
        ]
        if rows_for_llm:
            try:
                rankings = rerank_venue_candidates(rows=rows_for_llm)
            except CandidateRankingError:
                rankings = {}
            for row_number, ranked_ids in rankings.items():
                position = row_number - 1
                candidates = candidates_by_position[position]
                candidates_by_id = {str(candidate.matrix_item_id): candidate for candidate in candidates}
                reordered = [candidates_by_id[cid] for cid in ranked_ids if cid in candidates_by_id]
                if reordered:
                    candidates_by_position[position] = reordered

    rows = []
    unaliased_position = 0
    for raw_name in raw_names:
        alias = aliases.get(raw_name)
        if alias is not None:
            rows.append(
                SimpleNamespace(
                    raw_name=raw_name,
                    action="update_existing",
                    matrix_item_id=alias.matrix_item_id,
                    matrix_item_requested_name=alias.matrix_item.requested_name,
                    matrix_item_updated_at=alias.matrix_item.updated_at,
                    auto_resolved=True,
                    candidates=[],
                    unit_id=None,
                    unit_unrecognized=False,
                    unit_source=None,
                )
            )
            continue

        candidates = candidates_by_position.get(unaliased_position, [])
        unit = resolved_units.get(unaliased_position)
        row_unit_unrecognized = unaliased_position in unrecognized_positions
        row_unit_source = "llm" if unaliased_position in llm_assigned_positions else None
        unaliased_position += 1
        top = candidates[0] if candidates else None
        rows.append(
            SimpleNamespace(
                raw_name=raw_name,
                action=None if candidates else "create_new",
                matrix_item_id=top.matrix_item_id if top else None,
                matrix_item_requested_name=top.requested_name if top else None,
                matrix_item_updated_at=top.updated_at if top else None,
                auto_resolved=False,
                candidates=candidates,
                unit_id=unit.id if unit else None,
                unit_unrecognized=row_unit_unrecognized,
                unit_source=row_unit_source,
            )
        )
    return rows
