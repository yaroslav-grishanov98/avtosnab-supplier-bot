"""Tenders: lifecycle, items, offers and distribution."""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.v1.catalog.selectors import (
    SUPPLIER_PRODUCT_NOT_FOUND_MESSAGE,
    _owner_active_venue_ids,
    active_tender_holders,
    find_active_tender_conflicts,
    master_product_tender_offer_diff,
    owner_linked_venue_ids,
    supplier_can_access_tender,
    venue_label,
    with_offer_distribution,
)
from api.v1.catalog.tender_history import display_names, display_value, record_event, record_field_changes
from catalog.models import (
    SupplierProductVenuePriceSourceType,
    Tender,
    TenderAccess,
    TenderChangedField,
    TenderChangeType,
    TenderItem,
    TenderOffer,
    TenderStatus,
    VenueProductMatrixItem,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    Venue,
)

from .master_catalog import (
    bulk_assign_supplier_product_to_master_product,
    link_master_product_to_venue,
    master_product_bulk_supplier_product_targets,
)
from .supplier_prices import set_supplier_product_venue_price


def create_tender(
    *, owner, name, description=None, accepts_offers_until=None, access, invited_suppliers=None, actor=None
):
    """Raw ТЗ §6: only the tender shell — status stays draft (§14), publish
    is a separate not-yet-built action. §8: invited suppliers only make
    sense for access=invite_only; the serializer already rejects a
    non-empty list on access=open, and an empty one on invite_only, so
    this just persists whatever the serializer already validated."""
    tender = Tender.objects.create(
        created_by=owner,
        name=name,
        description=description,
        accepts_offers_until=accepts_offers_until,
        access=access,
    )
    if access == TenderAccess.INVITE_ONLY and invited_suppliers:
        tender.invited_suppliers.set(invited_suppliers)
    record_event(tender, TenderChangeType.CREATED, actor=actor)
    return tender


@transaction.atomic
def create_tender_with_items(*, items=None, **tender_kwargs):
    """TASK-66 (raw ТЗ §5.2-5.3, кнопка «Собрать предложения» — способ
    создания тендера сразу из выбранных Мастер-товаров/товаров компании).
    Тонкая обёртка над уже существующими create_tender + add_tender_items:
    новый тендер всегда создаётся draft (add_tender_items), поэтому §12
    (конфликт активных тендеров) здесь физически не может сработать — это
    не вопрос обработки ошибок, а просто следствие порядка вызовов.
    @transaction.atomic здесь — если хотя бы одна позиция не проходит
    валидацию add_tender_items, откатывается и уже созданный тендер."""
    tender = create_tender(**tender_kwargs)
    if items:
        add_tender_items(tender=tender, items=items, actor=tender_kwargs.get("actor"))
    return tender


class ActiveTenderConflict(Exception):
    """Raw ТЗ §12: a Мастер-товар + Заведение pair can't be active in two
    tenders at once — raised instead of silently creating/publishing the
    second one, carrying the colliding rows so the caller can build the
    "уже находятся в активном тендере №15" message."""

    def __init__(self, conflicts):
        self.conflicts = conflicts  # list[{"master_product", "venue", "tender"}]
        super().__init__(f"{len(conflicts)} master_product+venue combination(s) already active elsewhere")


@transaction.atomic
def add_tender_items(*, tender, items, actor=None):
    """Raw ТЗ §9: "можно выбрать сразу несколько" — one call adds every
    row or none. venue ownership check reuses _owner_active_venue_ids
    (already imported from selectors.py, same helper companies_count and
    master_product_venue_breakdown use) rather than duplicating the
    "owner's active venues" query. Duplicate detection is a pre-check
    (not a caught IntegrityError) to give one clean ValidationError instead
    of an unhandled 500 mid-batch — same style create_master_products_bulk
    already uses for its own duplicate handling. Caller is expected to have
    already run sync_tender_completion(tender) — not done here, since this
    function is @transaction.atomic and a later raise in the same call
    would roll back that write along with everything else."""
    if tender.status == TenderStatus.COMPLETED:
        raise ValidationError({"detail": "Тендер завершён — нельзя добавлять новые позиции."})
    owner_venue_ids = {row["venue_id"] for row in _owner_active_venue_ids(tender.created_by_id)}

    requested_master_product_ids = [entry["master_product"].id for entry in items]
    if len(requested_master_product_ids) != len(set(requested_master_product_ids)):
        raise ValidationError({"master_product_id": "Повторяющиеся master_product_id в одном запросе."})

    existing_ids = set(TenderItem.objects.filter(tender=tender).values_list("master_product_id", flat=True))
    dupes = existing_ids & set(requested_master_product_ids)
    if dupes:
        raise ValidationError({"master_product_id": f"Уже добавлены в этот тендер: {sorted(str(d) for d in dupes)}."})

    # TASK-68 (raw ТЗ §10, Павел подтвердил в Telegram 2026-09-17): если
    # покупатель явно не передал venue_ids, заведения подтягиваются сами —
    # из уже существующих VenueProductMatrixItem-связей мастер-товара с
    # заведениями владельца тендера. Резолвится ДО конфликт-чека, чтобы обе
    # проверки ниже видели один и тот же финальный список заведений.
    # TASK-93 (ТЗ «Мастер-справочник — создание тендера» §6-9, docs/adr/0007):
    # auto-selection also skips pairs another ACTIVE tender already holds —
    # the master product itself is still added, possibly with no venues.
    auto_ids = [entry["master_product"].id for entry in items if entry.get("venue_ids") is None]
    linked = owner_linked_venue_ids(tender.created_by_id, auto_ids) if auto_ids else {}
    held = active_tender_holders(auto_ids, exclude_tender_id=tender.id) if auto_ids else {}
    resolved_items = []
    for entry in items:
        venue_ids = entry.get("venue_ids")
        if venue_ids is None:
            mp_id = entry["master_product"].id
            venue_ids = sorted((v for v in linked[mp_id] if (mp_id, v) not in held), key=str)
        resolved_items.append({**entry, "venue_ids": venue_ids})

    _reject_archived_venues({venue_id for entry in resolved_items for venue_id in entry["venue_ids"]})
    empty = [entry["master_product"] for entry in resolved_items if not entry["venue_ids"]]
    if tender.status == TenderStatus.ACTIVE and empty:
        # A position added to an active tender is published at once (§13, §16).
        raise ValidationError({"detail": _no_venues_message(empty)})

    if tender.status == TenderStatus.ACTIVE:
        # §12 only forbids two ACTIVE tenders sharing a slot — a draft item
        # is invisible to suppliers (§14) and gets checked at publish time
        # instead, not here.
        conflicts = find_active_tender_conflicts(
            exclude_tender_id=tender.id,
            items=[
                {"master_product_id": entry["master_product"].id, "venue_ids": entry["venue_ids"]}
                for entry in resolved_items
            ],
        )
        if conflicts:
            raise ActiveTenderConflict(conflicts)

    created = []
    for entry in resolved_items:
        master_product = entry["master_product"]
        if master_product.owner_id != tender.created_by_id:
            raise ValidationError({"master_product_id": f'"{master_product.id}": не принадлежит владельцу тендера.'})
        venue_ids = entry["venue_ids"]
        bad_venue_ids = set(venue_ids) - owner_venue_ids
        if bad_venue_ids:
            raise ValidationError(
                {"venue_ids": f"Не принадлежат владельцу тендера: {sorted(str(v) for v in bad_venue_ids)}."}
            )
        item = TenderItem.objects.create(
            tender=tender,
            master_product=master_product,
            product_kind_label=entry.get("product_kind_label") or None,
            comment=entry.get("comment") or None,
        )
        item.venues.set(Venue.objects.filter(pk__in=venue_ids))
        created.append(item)
        if tender.status == TenderStatus.ACTIVE:
            record_event(tender, TenderChangeType.ITEM_ADDED, actor=actor, item=item)
    return created


def _reject_archived_venues(venue_ids):
    """Deleting a venue cabinet archives the venue but keeps its owner link, so ownership alone lets it through.
    Callers pass only venues being added — ones a position already has stay."""
    archived = sorted(Venue.objects.filter(pk__in=venue_ids).exclude(status=EntityStatus.ACTIVE), key=venue_label)
    if archived:
        names = ", ".join(f"«{venue_label(venue)}»" for venue in archived)
        noun = "Заведение" if len(archived) == 1 else "Заведения"
        raise ValidationError({"detail": f"{noun} {names} в архиве."})


def _no_venues_message(master_products):
    names = ", ".join(f"«{master_product.canonical_name}»" for master_product in master_products)
    noun = "товара" if len(master_products) == 1 else "товаров"
    return f"Выберите хотя бы одно заведение для {noun} {names}."


def _reject_published_items_without_venues(tender_items):
    """TASK-93 (ТЗ «Мастер-справочник — создание тендера» §13, §16): a
    published position needs at least one venue; auto-selection may leave it
    empty (§12-13). Unpublished positions are hidden from suppliers and do
    not block."""
    empty = [item for item in tender_items if item.published and not item.venues.all()]
    if empty:
        raise ValidationError(
            {
                "detail": _no_venues_message([item.master_product for item in empty]),
                "tender_item_ids": [item.id for item in empty],
            }
        )


def publish_tender(tender, actor=None):
    """Raw ТЗ §15: draft -> active is the only status transition this
    minimal slice implements — auto-completion by deadline (§7/§40) and
    manual completion (§40-42) are out of scope, see the plan's Scope &
    Deferred Items."""
    if tender.status != TenderStatus.DRAFT:
        raise ValidationError({"detail": "Тендер уже опубликован или завершён."})
    if tender.accepts_offers_until is not None and tender.accepts_offers_until <= timezone.now():
        raise ValidationError(
            {"detail": "Срок приёма предложений уже прошёл — продлите или снимите его перед публикацией."}
        )
    tender_items = list(tender.items.select_related("master_product").prefetch_related("venues"))
    if not tender_items:
        raise ValidationError({"detail": "Нельзя опубликовать тендер без товарных позиций."})
    _reject_published_items_without_venues(tender_items)
    conflicts = find_active_tender_conflicts(
        exclude_tender_id=tender.id,
        items=[
            {"master_product_id": item.master_product_id, "venue_ids": [v.id for v in item.venues.all()]}
            for item in tender_items
        ],
    )
    if conflicts:
        raise ActiveTenderConflict(conflicts)
    tender.status = TenderStatus.ACTIVE
    tender.published_at = timezone.now()
    tender.save(update_fields=["status", "published_at", "updated_at"])
    record_event(tender, TenderChangeType.PUBLISHED, actor=actor, occurred_at=tender.published_at)
    return tender


def publish_tender_item(item, actor=None):
    """TASK-59 (raw ТЗ §18/§75): противоположность unpublish_tender_item —
    возвращает позицию в состояние "Опубликован". Идемпотентна (повторный
    вызов на уже опубликованной позиции — не ошибка)."""
    if item.tender.status == TenderStatus.COMPLETED:
        raise ValidationError({"detail": "Тендер завершён — публикация позиций больше не меняется."})
    if item.tender.status == TenderStatus.ACTIVE and not item.venues.exists():
        raise ValidationError({"detail": _no_venues_message([item.master_product]), "tender_item_ids": [item.id]})
    if not item.published:
        item.published = True
        item.save(update_fields=["published", "updated_at"])
        if item.tender.status == TenderStatus.ACTIVE:
            record_event(item.tender, TenderChangeType.ITEM_PUBLISHED, actor=actor, item=item)
    return item


def unpublish_tender_item(item, actor=None):
    """TASK-59 (raw ТЗ §18/§75): скрывает позицию от поставщиков без
    физического удаления — единственный способ убрать позицию из
    активного тендера, раз DELETE-ручки для TenderItem не существует.
    Идемпотентна, как и publish_tender_item."""
    if item.tender.status == TenderStatus.COMPLETED:
        raise ValidationError({"detail": "Тендер завершён — публикация позиций больше не меняется."})
    if item.published:
        item.published = False
        item.save(update_fields=["published", "updated_at"])
        if item.tender.status == TenderStatus.ACTIVE:
            record_event(item.tender, TenderChangeType.ITEM_UNPUBLISHED, actor=actor, item=item)
    return item


def sync_tender_completion(tender):
    """Raw ТЗ §7/§40: "После наступления срока тендер автоматически
    переходит в статус: Завершен". No background job exists anywhere in
    this project (no celery beat schedule, see the Завершение-тендера
    design decision) — deadline expiry is applied lazily, the first time
    any tender-scoped request touches this tender after the deadline
    passes. Idempotent no-op otherwise."""
    now = timezone.now()
    if (
        tender.status == TenderStatus.ACTIVE
        and tender.accepts_offers_until is not None
        and tender.accepts_offers_until <= now
    ):
        # Conditional UPDATE: two requests racing past the deadline record the completion once (docs/adr/0010).
        closed = Tender.objects.filter(pk=tender.pk, status=TenderStatus.ACTIVE).update(
            status=TenderStatus.COMPLETED, updated_at=now
        )
        tender.status, tender.updated_at = TenderStatus.COMPLETED, now
        if closed:
            record_event(tender, TenderChangeType.COMPLETED, occurred_at=tender.accepts_offers_until)
    return tender


def complete_tender(tender, actor=None):
    """Raw ТЗ §40-42: manual completion — the buyer's "Завершить тендер"
    button. Rejects from draft (nothing to close) and from an
    already-completed tender (including one sync_tender_completion just
    auto-closed for having passed its own deadline) — mirrors
    publish_tender's own status guard."""
    sync_tender_completion(tender)
    if tender.status != TenderStatus.ACTIVE:
        raise ValidationError({"detail": "Тендер не активен — завершить можно только активный тендер."})
    tender.status = TenderStatus.COMPLETED
    tender.save(update_fields=["status", "updated_at"])
    record_event(tender, TenderChangeType.COMPLETED, actor=actor)
    return tender


@transaction.atomic
def _reject_removing_distributed_venues(*, tender_item, supplier, venue_ids):
    """Raw ТЗ §39: a venue the buyer already distributed this offer to stays
    in «Готов поставлять» — a regular edit cannot uncheck it."""
    current = TenderOffer.objects.filter(tender_item=tender_item, supplier=supplier).first()
    if current is None:
        return
    locked = set(with_offer_distribution([current])[0].distributed_venue_ids) - set(venue_ids)
    if locked:
        names = ", ".join(sorted(venue_label(venue) for venue in Venue.objects.filter(pk__in=locked)))
        raise ValidationError({"venue_ids": f"Уже назначены покупателем, убрать нельзя: {names}."})


def _offer_snapshot(offer):
    """Raw ТЗ §76: what history shows of an offer — display strings, as in TenderChange."""
    if offer is None:
        return {}
    product = offer.supplier_product
    return {
        TenderChangedField.SUPPLIER_PRODUCT: product.supplier_name if product is not None else None,
        TenderChangedField.PRICE: display_value(offer.price),
        TenderChangedField.VENUES: display_names(venue_label(venue) for venue in offer.venues.all()),
        TenderChangedField.COMMENT: display_value(offer.comment),
    }


def _record_offer_history(*, offer, before, was_withdrawn, actor):
    """Raw ТЗ §76 (docs/adr/0010): a first offer records every offered field,
    a later one only the changed fields; resubmitting after «Не предлагаем»
    (§69) records the return."""
    tender = offer.tender_item.tender
    after = _offer_snapshot(offer)
    if was_withdrawn:
        record_event(tender, TenderChangeType.OFFER_RETURNED, actor=actor, item=offer.tender_item, offer=offer)
    change_type = TenderChangeType.OFFER_UPDATED if before else TenderChangeType.OFFER_SUBMITTED
    record_field_changes(
        tender,
        [(field, before.get(field), value) for field, value in after.items()],
        actor=actor,
        item=offer.tender_item,
        offer=offer,
        change_type=change_type,
    )


def submit_tender_offer(*, tender_item, supplier, supplier_product, price, currency, venue_ids, comment, actor=None):
    """Raw ТЗ §35: "не создаётся новое предложение, обновляется текущее" —
    update_or_create keyed on the model's own uniqueness constraint. Caller
    is expected to have already run sync_tender_completion(tender) — not
    done here, since this function is @transaction.atomic and a later
    raise in the same call would roll back that write too."""
    tender = tender_item.tender
    if not supplier_can_access_tender(supplier=supplier, tender=tender):
        raise ValidationError({"detail": "Тендер недоступен этому поставщику."})
    if supplier_product.supplier_id != supplier.id:
        raise ValidationError({"supplier_product_id": [SUPPLIER_PRODUCT_NOT_FOUND_MESSAGE]})

    item_venue_ids = set(tender_item.venues.values_list("id", flat=True))
    bad_venue_ids = set(venue_ids) - item_venue_ids
    if bad_venue_ids:
        raise ValidationError(
            {"venue_ids": f"Не входят в заведения этой позиции: {sorted(str(v) for v in bad_venue_ids)}."}
        )
    _reject_removing_distributed_venues(tender_item=tender_item, supplier=supplier, venue_ids=venue_ids)

    current = TenderOffer.objects.filter(tender_item=tender_item, supplier=supplier).first()
    before, was_withdrawn = _offer_snapshot(current), current is not None and current.withdrawn_at is not None
    offer, _created = TenderOffer.objects.update_or_create(
        tender_item=tender_item,
        supplier=supplier,
        defaults={
            "supplier_product": supplier_product,
            "price": price,
            "currency": currency,
            "comment": comment,
            "withdrawn_at": None,
        },
    )
    offer.venues.set(venue_ids)
    _record_offer_history(offer=offer, before=before, was_withdrawn=was_withdrawn, actor=actor)
    return offer


def withdraw_tender_offer(offer, actor=None):
    """Raw ТЗ §67 «Не предлагаем» for an already sent offer (docs/adr/0006):
    the row stays, withdrawn_at marks it; submit_tender_offer revives it.
    Same access rule as submitting — only while the tender is active. The
    caller has already run sync_tender_completion. Idempotent: the first
    withdrawal moment is kept."""
    if not supplier_can_access_tender(supplier=offer.supplier, tender=offer.tender_item.tender):
        raise ValidationError({"detail": "Тендер недоступен этому поставщику."})
    if offer.withdrawn_at is None:
        offer.withdrawn_at = timezone.now()
        offer.save(update_fields=["withdrawn_at", "updated_at"])
        tender_item = offer.tender_item
        record_event(tender_item.tender, TenderChangeType.OFFER_WITHDRAWN, actor=actor, item=tender_item, offer=offer)
    return offer


# docs/adr/0013: rendered by the notifications module. Until it enables this
# event for supplier chats, enqueue_for_supplier finds no rule and sends nothing.
TENDER_DISTRIBUTION_EVENT = "supplier_tender_distribution"


def _notify_distributed_supplier(*, offer, rows):
    """Raw ТЗ §32-34: the chosen supplier hears about the venues whose product
    or fixed price this apply actually changed; a replaced supplier hears
    nothing (§34)."""
    venues = sorted(row.venue_name for row in rows if row.changed or row.current_price != row.new_price)
    if not venues:
        return
    from notifications.services import enqueue_for_supplier

    tender = offer.tender_item.tender
    enqueue_for_supplier(
        supplier_id=offer.supplier_id,
        event=TENDER_DISTRIBUTION_EVENT,
        payload={
            "tender_id": str(tender.id),
            "tender_number": tender.number,
            "tender_name": tender.name,
            "product_name": offer.supplier_product.supplier_name,
            "price": str(offer.price),
            "currency": offer.currency,
            "venues": venues,
        },
        idempotency_key=f"tender-distribution:{offer.id}:{timezone.now().isoformat()}",
    )


def _tender_item_distribution(*, offer, venue_ids, created_by, commit):
    """Raw ТЗ §23-27: the buyer can only distribute an offer to venues that
    are BOTH in the position's venue list (§11) AND in the offer's own
    "готов поставлять" list (§21/§27) — the intersection, not either alone.

    TASK-65 (raw ТЗ §30 "назначает товар поставщика для каждого выбранного
    заведения"): eligibility (above) never required the venue to already
    have a VenueProductMatrixItem for this master_product — neither
    add_tender_items (§10) nor submit_tender_offer check that — so a venue
    can legitimately reach distribution unlinked. Any such venue_id is
    auto-linked here via link_master_product_to_venue(force_create=True)
    before resolving targets, instead of master_product_bulk_supplier_
    product_targets raising its own "not linked" ValidationError.
    force_create=True deliberately skips the interactive name-conflict
    check that flow uses elsewhere (MasterProductVenueLinkConflict) — the
    buyer already made an explicit choice by distributing to this venue, no
    manual resolution step exists in this flow (confirmed with user).

    preview (commit=False) and apply (commit=True) share this single
    implementation — same pattern as _apply_master_product_venue_batch
    (TASK-52): the write actually happens, then preview rolls it back via
    transaction.set_rollback, so the two paths can never disagree about
    what applying would do, including which venues get auto-linked.

    supplier_product is SET_NULL on TenderOffer (see the model docstring) —
    a supplier can hard-delete their own product after submitting an offer,
    leaving offer.supplier_product_id null. Guarded explicitly here, first,
    rather than relying on this function always running before the writes
    below: that ordering is an implementation detail, not a safety
    guarantee, and bulk_assign_supplier_product_to_master_product(
    supplier_product=None) would otherwise silently null out every target
    venue's matrix row."""
    if offer.supplier_product_id is None:
        raise ValidationError({"detail": "Товар поставщика в этом предложении удалён — распределение невозможно."})
    if offer.withdrawn_at is not None:
        raise ValidationError({"detail": "Поставщик снял это предложение — распределение невозможно."})

    tender_item = offer.tender_item
    # Caller (view) is expected to have already run
    # sync_tender_completion(tender_item.tender) — not done here, since
    # this function is wrapped in @transaction.atomic and a later raise
    # would roll that write back too.
    if tender_item.tender.status == TenderStatus.COMPLETED:
        raise ValidationError({"detail": "Тендер завершён — распределение больше не производится."})
    eligible_ids = set(tender_item.venues.values_list("id", flat=True)) & set(offer.venues.values_list("id", flat=True))
    bad_ids = set(venue_ids) - eligible_ids
    if bad_ids:
        raise ValidationError(
            {
                "venue_ids": f"Поставщик не готов поставлять, либо заведение не входит в позицию: {sorted(str(v) for v in bad_ids)}."
            }
        )

    with transaction.atomic():
        _restore_archived_directory_rows(tender_item=tender_item, venue_ids=venue_ids, actor=created_by, commit=commit)
        linked_venue_ids = set(
            VenueProductMatrixItem.objects.filter(
                master_product=tender_item.master_product, venue_id__in=venue_ids, status=EntityStatus.ACTIVE
            ).values_list("venue_id", flat=True)
        )
        unlinked_venues = Venue.objects.filter(pk__in=set(venue_ids) - linked_venue_ids)
        for venue in unlinked_venues:
            link_master_product_to_venue(master_product=tender_item.master_product, venue=venue, force_create=True)

        items = master_product_bulk_supplier_product_targets(
            master_product=tender_item.master_product, venue_ids=venue_ids
        )
        rows = master_product_tender_offer_diff(
            items=items, new_supplier_product=offer.supplier_product, new_price=offer.price, new_currency=offer.currency
        )

        if commit:
            bulk_assign_supplier_product_to_master_product(
                master_product=tender_item.master_product, supplier_product=offer.supplier_product, venue_ids=venue_ids
            )
            set_supplier_product_venue_price(
                supplier_product=offer.supplier_product,
                venues=list(Venue.objects.filter(pk__in=venue_ids)),
                price=offer.price,
                currency=offer.currency,
                created_by=created_by,
                source_type=SupplierProductVenuePriceSourceType.TENDER,
                source_reference=f"Тендер №{tender_item.tender.number}",
                source_tender_offer=offer,
                # Own preview/apply cycle (this same function, commit=False)
                # is already the confirmation step — same reasoning
                # apply_tender_offer documents for its own confirm_replace=True.
                confirm_replace=True,
            )
            _notify_distributed_supplier(offer=offer, rows=rows)
        else:
            transaction.set_rollback(True)
        return rows


def _restore_archived_directory_rows(*, tender_item, venue_ids, actor, commit):
    """TASK-95 (docs/adr/0008): vpmi_master_product_venue_uniq ignores status,
    so an archived row still holds the (master product, venue) slot and
    link_master_product_to_venue refuses a new one. Restore that same row
    instead; the audit line is written on apply only (preview rolls back)."""
    rows = VenueProductMatrixItem.objects.filter(
        master_product=tender_item.master_product, venue_id__in=venue_ids
    ).exclude(status=EntityStatus.ACTIVE)
    for row in rows:
        row.status = EntityStatus.ACTIVE
        row.save(update_fields=["status", "updated_at"])
        if commit:
            record_audit_log(
                actor=actor,
                action=AuditLogAction.UPDATED,
                entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM,
                entity_id=row.id,
                summary=f"Восстановлено из архива при распределении тендера «{tender_item.tender.name}»",
                scope_id=row.venue_id,
            )


def tender_item_distribution_diff(*, offer, venue_ids):
    """Read-only "Сейчас / Будет" preview — see _tender_item_distribution."""
    return _tender_item_distribution(offer=offer, venue_ids=venue_ids, created_by=None, commit=False)


def apply_tender_item_distribution(*, offer, venue_ids, created_by):
    """Raw ТЗ §30-31: assigns the supplier product to the venue's own
    catalog row AND fixes the individual price, in the same transaction —
    see _tender_item_distribution."""
    return _tender_item_distribution(offer=offer, venue_ids=venue_ids, created_by=created_by, commit=True)


@transaction.atomic
def apply_tender_offer(*, master_product, supplier_product, price, currency, venue_ids, source_reference, created_by):
    """Raw ТЗ §45-47 / spec §3.6 п.3: applying a winning tender offer must,
    for exactly the venues the caller selected, simultaneously (1) assign
    supplier_product and (2) fix the individual price with tender
    attribution — reusing the same two services §19/§35 already implement
    for manual bulk-assign and manual pricing (bulk_assign_supplier_
    product_to_master_product, set_supplier_product_venue_price), so the
    still-unwritten future Tenders module (§37 of the raw doc) has no
    reason to duplicate either "no silent overwrite" rule. venue_ids is
    mandatory here — unlike manual bulk-assign's "every linked venue"
    default — because §45's example is an explicit per-venue checkbox
    selection, never "everyone".

    Returns the same before-write diff rows master_product_tender_offer_diff
    would show as a preview for the same inputs, so preview and apply can
    never disagree."""
    if not venue_ids:
        raise ValidationError({"venue_ids": "Нужно выбрать хотя бы одну компанию."})
    items = master_product_bulk_supplier_product_targets(master_product=master_product, venue_ids=venue_ids)
    rows = master_product_tender_offer_diff(
        items=items, new_supplier_product=supplier_product, new_price=price, new_currency=currency
    )
    bulk_assign_supplier_product_to_master_product(
        master_product=master_product, supplier_product=supplier_product, venue_ids=venue_ids
    )
    set_supplier_product_venue_price(
        supplier_product=supplier_product,
        venues=[item.venue for item in items],
        price=price,
        currency=currency,
        created_by=created_by,
        source_type=SupplierProductVenuePriceSourceType.TENDER,
        source_reference=source_reference,
        # Own preview/apply cycle (master_product_tender_offer_diff, shown
        # above) already is the confirmation step — no separate conflict gate.
        confirm_replace=True,
    )
    return rows
