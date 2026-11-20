"""Tender reads: lists, items, offers, distribution and availability."""

import re
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Exists, F, Func, OuterRef, Prefetch, Q, TextField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from catalog.models import (
    SupplierProductVenuePrice,
    SupplierProductVenuePriceSourceType,
    SupplierProductVenuePriceStatus,
    SupplierTenderMyWork,
    Tender,
    TenderAccess,
    TenderItem,
    TenderOffer,
    TenderOfferAutofillMatchStatus,
    TenderStatus,
    TenderVenuePriceSource,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    Venue,
)
from suppliers.models import Supplier, SupplierProduct

from .common import _owner_active_venue_ids, owner_linked_venue_ids, venue_label
from .master_catalog import master_product_bulk_supplier_product_diff
from .supplier_prices import resolve_supplier_product_price


def tenders_with_counters_queryset():
    """Every Tender response is read through here (TASK-83, docs/adr/0002):
    items_count/offers_count come from this annotation, so TenderSerializer
    fails on a Tender loaded any other way. Both counts share one
    items-offers join, hence distinct=True. prefetch_related avoids N+1 on
    invited_supplier_ids for invite_only tenders."""
    return Tender.objects.annotate(
        items_count=Count("items", distinct=True),
        offers_count=Count("items__offers", filter=Q(items__offers__withdrawn_at__isnull=True), distinct=True),
    ).prefetch_related("invited_suppliers", "files")


def tenders_queryset(owner_id, *, q=None):
    """Mirrors master_products_queryset — same account-wide owner scoping
    (§2 raw ТЗ: "Покупатель — пользователь учётной записи АвтоСнаб"). The
    order is explicit: Django drops Meta.ordering in a GROUP BY query (§45).
    q finds the tender number («22», «№22») or a part of the name."""
    queryset = tenders_with_counters_queryset().filter(created_by_id=owner_id)
    if q:
        queryset = queryset.annotate(name_folded=_Casefold("name")).filter(
            Q(name_folded__contains=q.translate(_CASEFOLD_TABLE)) | _tender_number_match(q)
        )
    return queryset.order_by("-created_at", "-id")


def _tender_number_match(q):
    """«22», «№22», «№ 22» → Tender №22; anything else matches no number."""
    match = _TENDER_NUMBER_QUERY.match(q)
    return Q(number=int(match.group(1))) if match else Q(pk__in=[])


def supplier_tenders_queryset(supplier_ids):
    """Supplier-side Tender rows (TASK-84 header; the TASK-60 list reuses it)
    for one supplier company or several of one user. Unpublished positions
    are invisible to suppliers (§75), so both counters and the customer
    venues cover published ones only — my_offers_count counts positions any
    of these companies answered, never more than published_items_count.
    Visibility itself (draft, invite list) is still the caller's
    supplier_can_view_tender check."""
    published = Q(items__published=True)
    return Tender.objects.annotate(
        published_items_count=Count("items", filter=published, distinct=True),
        my_offers_count=Count(
            "items",
            filter=published & Q(items__offers__supplier_id__in=supplier_ids, items__offers__withdrawn_at__isnull=True),
            distinct=True,
        ),
    ).prefetch_related(
        Prefetch(
            "items",
            queryset=TenderItem.objects.filter(published=True).prefetch_related("venues"),
            to_attr="published_items",
        ),
        "files",
    )


# TASK-92: the stand's Postgres runs with LC_CTYPE=C, where UPPER()/ILIKE
# (what icontains compiles to) fold only ASCII. translate() with explicit
# alphabets folds case the same way under any locale.
_UPPER_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"


_LOWER_LETTERS = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя"


_CASEFOLD_TABLE = str.maketrans(_UPPER_LETTERS, _LOWER_LETTERS)


_TENDER_NUMBER_QUERY = re.compile(r"^\s*№?\s*(\d+)\s*$")


_ANSWERED = Q(my_offers_count__gt=0)


_MY_WORK_FILTERS = {
    SupplierTenderMyWork.NOT_STARTED: Q(my_offers_count=0),
    SupplierTenderMyWork.IN_PROGRESS: _ANSWERED & Q(my_offers_count__lt=F("published_items_count")),
    SupplierTenderMyWork.DONE: _ANSWERED & Q(my_offers_count=F("published_items_count")),
    SupplierTenderMyWork.PARTICIPATING: _ANSWERED,
}


class _Casefold(Func):
    function = "TRANSLATE"
    output_field = TextField()

    def __init__(self, expression):
        super().__init__(expression, Value(_UPPER_LETTERS), Value(_LOWER_LETTERS))


def tender_item_distribution_history(item):
    """Raw ТЗ §76 «Распределение»: every distribution of the position, newest
    first, read from the individual prices distribution creates — so it covers
    distributions made before this history existed. is_current: its price is
    still the one in force for the product and venue, and the venue's directory
    row still points at the product — the same rule as «Распределено»."""
    from types import SimpleNamespace

    prices = list(
        SupplierProductVenuePrice.objects.filter(
            source_type=SupplierProductVenuePriceSourceType.TENDER, source_tender_offer__tender_item=item
        )
        .select_related("venue", "supplier_product__supplier", "created_by")
        .order_by("-created_at", "-id")
    )
    directory = dict(
        VenueProductMatrixItem.objects.filter(
            master_product_id=item.master_product_id,
            venue_id__in={price.venue_id for price in prices},
            status=EntityStatus.ACTIVE,
        ).values_list("venue_id", "supplier_product_id")
    )
    in_force = {
        row.id
        for row in _active_individual_price_rows(
            product_ids={price.supplier_product_id for price in prices if price.supplier_product_id},
            venue_ids={price.venue_id for price in prices},
        ).values()
    }
    rows = []
    for price in prices:
        product = price.supplier_product
        rows.append(
            SimpleNamespace(
                venue_id=price.venue_id,
                venue_name=venue_label(price.venue),
                supplier_id=product.supplier_id if product else None,
                supplier_name=product.supplier.display_name if product else None,
                supplier_product_name=product.supplier_name if product else None,
                price=price.price,
                currency=price.currency,
                distributed_at=price.created_at,
                distributed_by_name=(price.created_by.full_name or price.created_by.email)
                if price.created_by
                else None,
                is_current=(
                    price.id in in_force and product is not None and directory.get(price.venue_id) == product.id
                ),
            )
        )
    return rows


def user_supplier_companies(user):
    """The supplier companies a login-token user acts for: active memberships
    in active suppliers, by name."""
    return list(
        Supplier.objects.filter(
            supplier_users__user=user,
            supplier_users__status=EntityStatus.ACTIVE,
            status=EntityStatus.ACTIVE,
        )
        .distinct()
        .order_by("display_name", "id")
    )


def with_supplier_companies(tenders, companies):
    """Sets tender.companies: which of the user's companies see the tender (an
    open one — all, an invite_only one — the invited) and how many published
    positions each answered. Two queries for the whole page."""
    from types import SimpleNamespace

    tender_ids = [tender.id for tender in tenders]
    company_ids = [company.id for company in companies]
    invited = set(
        Tender.invited_suppliers.through.objects.filter(
            tender_id__in=tender_ids, supplier_id__in=company_ids
        ).values_list("tender_id", "supplier_id")
    )
    answered = {
        (row["tender_item__tender_id"], row["supplier_id"]): row["count"]
        for row in TenderOffer.objects.filter(
            tender_item__tender_id__in=tender_ids,
            tender_item__published=True,
            supplier_id__in=company_ids,
            withdrawn_at__isnull=True,
        )
        .values("tender_item__tender_id", "supplier_id")
        .annotate(count=Count("tender_item_id", distinct=True))
    }
    for tender in tenders:
        tender.companies = [
            SimpleNamespace(
                id=company.id, name=company.display_name, my_offers_count=answered.get((tender.id, company.id), 0)
            )
            for company in companies
            if tender.access == TenderAccess.OPEN or (tender.id, company.id) in invited
        ]
    return tenders


def invitable_suppliers_queryset(q=None):
    """Raw ТЗ §8, docs/adr/0014: suppliers a buyer can invite — every active
    published one, with or without a price list (the catalog filter wants
    priced products; an invitation does not). Case-insensitive under any
    database locale, same as the supplier tender search."""
    queryset = Supplier.objects.filter(status=EntityStatus.ACTIVE, is_published=True)
    if q:
        needle = q.translate(_CASEFOLD_TABLE)
        queryset = queryset.annotate(
            name_folded=_Casefold("display_name"), legal_name_folded=_Casefold("legal_name")
        ).filter(Q(name_folded__contains=needle) | Q(legal_name_folded__contains=needle) | Q(inn__contains=q))
    return queryset.order_by("display_name", "id")


def supplier_visible_tenders_queryset(supplier_ids, *, status, q=None, my_work=None):
    """TASK-60 (raw ТЗ §44-51): open tenders plus invite_only ones any of
    these supplier companies is invited to, never drafts, newest published first. `status` is the
    EFFECTIVE status: an ACTIVE tender whose deadline has passed belongs to
    the completed tab even before sync_tender_completion stores it (same
    `<= now` boundary). Visibility and search use Exists, not joins, so no
    row is duplicated. A tender with no published position is left out, as in
    supplier_can_view_tender. q also finds the tender number; my_work filters
    by how many published positions these companies answered («Моя работа»)."""
    expired = Q(accepts_offers_until__isnull=False, accepts_offers_until__lte=timezone.now())
    if status == TenderStatus.ACTIVE:
        by_status = Q(status=TenderStatus.ACTIVE) & ~expired
    else:
        by_status = Q(status=TenderStatus.COMPLETED) | (Q(status=TenderStatus.ACTIVE) & expired)
    invited = Exists(
        Tender.invited_suppliers.through.objects.filter(tender_id=OuterRef("pk"), supplier_id__in=supplier_ids)
    )
    has_published_items = Exists(TenderItem.objects.filter(tender_id=OuterRef("pk"), published=True))
    queryset = (
        supplier_tenders_queryset(supplier_ids)
        .filter(by_status, has_published_items)
        .filter(Q(access=TenderAccess.OPEN) | invited)
    )
    if q:
        needle = q.translate(_CASEFOLD_TABLE)
        visible_match = (
            TenderItem.objects.filter(tender_id=OuterRef("pk"), published=True)
            .annotate(
                product_folded=_Casefold("master_product__canonical_name"),
                venue_folded=_Casefold("venues__display_name"),
                venue_short_folded=_Casefold("venues__short_name"),
            )
            .filter(
                Q(product_folded__contains=needle)
                | Q(venue_folded__contains=needle)
                | Q(venue_short_folded__contains=needle)
            )
        )
        queryset = queryset.annotate(name_folded=_Casefold("name")).filter(
            Q(name_folded__contains=needle) | Exists(visible_match) | _tender_number_match(q)
        )
    if my_work:
        queryset = queryset.filter(_MY_WORK_FILTERS[my_work])
    # §45 «новые сверху» for a supplier is by publication; tenders published before the date was kept fall back.
    return queryset.annotate(listed_at=Coalesce("published_at", "created_at")).order_by("-listed_at", "-id")


def tender_items_queryset(tender_id):
    """Mirrors tenders_queryset's simplicity — raw ТЗ §71 wants the whole
    position list on one page (no pagination), select_related/
    prefetch_related avoid N+1 on TenderItemSerializer's master_product_name/
    unit_code/venue_ids."""
    return (
        TenderItem.objects.filter(tender_id=tender_id)
        .select_related("master_product", "master_product__unit")
        .prefetch_related("venues")
    )


def tender_items_for_supplier_queryset(tender_id, supplier_id):
    """Same shape as tender_items_queryset, plus a Prefetch of this one
    supplier's own TenderOffer (if any) onto each item as `my_offers` — a
    list of 0 or 1 element (never more, per the model's own tender_item+
    supplier uniqueness constraint), so the serializer can read
    obj.my_offers[0] if obj.my_offers else None without a second query.

    published=False items are excluded (TASK-59, raw ТЗ §18/§75) — this is
    the one place unpublishing a position actually has a visible effect:
    hidden from suppliers, still intact for the buyer (tender_items_
    queryset, unfiltered) and for history."""
    my_offer_qs = (
        TenderOffer.objects.filter(supplier_id=supplier_id)
        .select_related(
            "supplier", "supplier_product__unit", "supplier_product__net_unit", "tender_item__master_product__unit"
        )
        .prefetch_related("venues")
    )
    return (
        TenderItem.objects.filter(tender_id=tender_id, published=True)
        .select_related("master_product", "master_product__unit")
        .prefetch_related("venues", Prefetch("offers", queryset=my_offer_qs, to_attr="my_offers"))
    )


def tender_item_offers_queryset(item_id):
    """Buyer-side "Предложения по товару" (raw ТЗ §20-21) — every supplier's
    current offer on one position, newest-updated first (model's own
    ordering already does this, see TenderOffer.Meta.ordering). Offers the
    supplier withdrew (§67, docs/adr/0006) are not shown to the buyer."""
    return (
        TenderOffer.objects.filter(tender_item_id=item_id, withdrawn_at__isnull=True)
        .select_related(
            "supplier", "supplier_product__unit", "supplier_product__net_unit", "tender_item__master_product__unit"
        )
        .prefetch_related("venues")
    )


def _distributed_venue_ids_by_offer(offers, items_by_id):
    """Raw ТЗ §19/§23, docs/adr/0004: no distribution ledger exists — a venue
    of the position counts as distributed by an offer by CURRENT state: its
    directory row for the position's master product points at the offer's
    product AND the price now in force for that product and venue was fixed
    from this very offer. The same product put there by another tender or by
    hand is not this offer's distribution. `offers` are (offer id, item id,
    product id) triples; items must have `venues` prefetched. Two queries."""
    venue_ids = {venue.id for item in items_by_id.values() for venue in item.venues.all()}
    current_product = {
        (mp_id, v_id): sp_id
        for mp_id, v_id, sp_id in VenueProductMatrixItem.objects.filter(
            master_product_id__in={item.master_product_id for item in items_by_id.values()},
            venue_id__in=venue_ids,
            status=EntityStatus.ACTIVE,
        ).values_list("master_product_id", "venue_id", "supplier_product_id")
    }
    price_source = {
        pair: row.source_tender_offer_id
        for pair, row in _active_individual_price_rows(
            product_ids={product_id for _, _, product_id in offers if product_id}, venue_ids=venue_ids
        ).items()
    }
    distributed = {}
    for offer_id, item_id, product_id in offers:
        item = items_by_id[item_id]
        distributed[offer_id] = {
            venue.id
            for venue in item.venues.all()
            if product_id
            and current_product.get((item.master_product_id, venue.id)) == product_id
            and price_source.get((product_id, venue.id)) == offer_id
        }
    return distributed


def _distributed_venue_ids_by_item(items):
    """Raw ТЗ §19/§41: a position's venue is distributed when any offer of
    THIS position (withdrawn ones included) is distributed to it — the union
    of _distributed_venue_ids_by_offer. Three queries for the whole batch."""
    if not items:
        return {}
    offers = list(
        TenderOffer.objects.filter(tender_item_id__in=[item.id for item in items]).values_list(
            "id", "tender_item_id", "supplier_product_id"
        )
    )
    by_offer = _distributed_venue_ids_by_offer(offers, {item.id: item for item in items})
    distributed = {item.id: set() for item in items}
    for offer_id, item_id, _ in offers:
        distributed[item_id] |= by_offer[offer_id]
    return distributed


def tender_items_with_counters(tender_id, *, item_ids=None):
    """TASK-85: every TenderItem response carries offers_count and
    distributed_venues_count, so each is read through here (a plain
    TenderItem would fail TenderItemSerializer)."""
    queryset = tender_items_queryset(tender_id).annotate(
        offers_count=Count("offers", filter=Q(offers__withdrawn_at__isnull=True), distinct=True)
    )
    if item_ids is not None:
        queryset = queryset.filter(pk__in=item_ids)
    items = list(queryset)
    distributed = _distributed_venue_ids_by_item(items)
    for item in items:
        item.distributed_venues_count = len(distributed[item.id])
    return items


def with_offer_distribution(offers):
    """TASK-91 (raw ТЗ §23, docs/adr/0004): sets offer.distributed_venue_ids —
    venues of the offer's position this offer is distributed to (see
    _distributed_venue_ids_by_offer). Every TenderOffer response is read
    through here (the serializer needs the attribute). Four queries for the
    whole batch."""
    offers = list(offers)
    if not offers:
        return offers
    item_ids = {offer.tender_item_id for offer in offers}
    items = {item.id: item for item in TenderItem.objects.filter(pk__in=item_ids).prefetch_related("venues")}
    distributed = _distributed_venue_ids_by_offer(
        [(offer.id, offer.tender_item_id, offer.supplier_product_id) for offer in offers], items
    )
    for offer in offers:
        offer.distributed_venue_ids = sorted(distributed[offer.id], key=str)
    return offers


def tender_completion_summary(tender):
    """Raw ТЗ §41: the "Завершить тендер?" modal's summary counts —
    distributed is the sum of the per-position counts (same helper)."""
    items = list(tender_items_queryset(tender.id))
    distributed = sum(len(venue_ids) for venue_ids in _distributed_venue_ids_by_item(items).values())
    total_slots = sum(len(item.venues.all()) for item in items)
    return {
        "items_count": len(items),
        "offers_count": TenderOffer.objects.filter(tender_item__tender=tender, withdrawn_at__isnull=True).count(),
        "distributed": distributed,
        "undistributed": total_slots - distributed,
    }


def _holding_tender_q():
    """Raw ТЗ §7/§12: a tender holds its master product + venue pairs while it
    is ACTIVE and its deadline has not passed — also before a request closes
    it lazily (sync_tender_completion), so an expired tender never causes 409."""
    return Q(tender__status=TenderStatus.ACTIVE) & (
        Q(tender__accepts_offers_until__isnull=True) | Q(tender__accepts_offers_until__gt=timezone.now())
    )


def active_tender_holders(master_product_ids, *, exclude_tender_id=None):
    """TASK-93 (§8, docs/adr/0007): which ACTIVE tender currently holds each
    master product + venue pair — the pair a new tender may not take. Drafts
    and completed tenders hold nothing. {(master_product_id, venue_id): Tender}."""
    items = (
        TenderItem.objects.filter(_holding_tender_q(), master_product_id__in=master_product_ids)
        .exclude(tender_id=exclude_tender_id)
        .select_related("tender")
        .prefetch_related("venues")
    )
    return {(item.master_product_id, venue.id): item.tender for item in items for venue in item.venues.all()}


def tender_venue_availability(owner_id, master_product_ids):
    """TASK-93 (§10-11, docs/adr/0007): for each master product, every active
    venue of the account with `linked` and the active tender holding the pair,
    if any. The client derives auto-selection (linked, not held), «N
    недоступны» (linked, held) and disabled manual choices (held)."""
    venues = sorted(
        Venue.objects.filter(id__in=_owner_active_venue_ids(owner_id), status=EntityStatus.ACTIVE),
        key=lambda venue: (venue_label(venue), str(venue.id)),
    )
    linked = owner_linked_venue_ids(owner_id, master_product_ids)
    holders = active_tender_holders(master_product_ids)
    return [
        {
            "master_product_id": mp_id,
            "venues": [
                {
                    "venue_id": venue.id,
                    "venue_name": venue_label(venue),
                    "linked": venue.id in linked[mp_id],
                    "busy_tender_id": holders[(mp_id, venue.id)].id if (mp_id, venue.id) in holders else None,
                    "busy_tender_name": holders[(mp_id, venue.id)].name if (mp_id, venue.id) in holders else None,
                    "busy_tender_number": (holders[(mp_id, venue.id)].number if (mp_id, venue.id) in holders else None),
                }
                for venue in venues
            ],
        }
        for mp_id in master_product_ids
    ]


def find_active_tender_conflicts(*, exclude_tender_id, items):
    """Raw ТЗ §12: "Связка Мастер-товар + Заведение — не может одновременно
    участвовать в двух активных тендерах". `items` is an iterable of
    {"master_product_id", "venue_ids"} describing the (about to be) active
    positions of one tender (exclude_tender_id) — returns one
    {master_product, venue, tender} dict per colliding combination found in
    OTHER tenders currently ACTIVE. One query for all requested master
    products, matched against requested venue_ids in Python (no N+1)."""
    master_product_ids = {entry["master_product_id"] for entry in items}
    if not master_product_ids:
        return []

    other_rows = (
        TenderItem.objects.filter(_holding_tender_q(), master_product_id__in=master_product_ids)
        .exclude(tender_id=exclude_tender_id)
        .select_related("tender", "master_product")
        .prefetch_related("venues")
    )
    rows_by_master_product = {}
    for row in other_rows:
        rows_by_master_product.setdefault(row.master_product_id, []).append(row)

    conflicts = []
    for entry in items:
        venue_ids = set(entry["venue_ids"])
        for row in rows_by_master_product.get(entry["master_product_id"], []):
            for venue in row.venues.all():
                if venue.id in venue_ids:
                    conflicts.append({"master_product": row.master_product, "venue": venue, "tender": row.tender})
    return conflicts


def supplier_can_access_tender(*, supplier, tender):
    """Raw ТЗ §8/§35/§42: a supplier can only submit/change an offer on an
    ACTIVE tender that's either open, or invite_only with this supplier on
    the invite list. A completed tender rejects new/changed offers too
    (§42) — callers are expected to have already run
    sync_tender_completion(tender) so a merely-expired-but-stale-ACTIVE
    tender doesn't slip through here."""
    if tender.status != TenderStatus.ACTIVE:
        return False
    if tender.access == TenderAccess.OPEN:
        return True
    return tender.invited_suppliers.filter(pk=supplier.pk).exists()


def supplier_can_view_tender(*, supplier, tender):
    """Raw ТЗ §42: unlike supplier_can_access_tender (submit/change offer),
    a COMPLETED tender stays visible to suppliers — "могут просматривать
    историю". Only DRAFT (§14: never shown to suppliers at all) is
    excluded here, and a tender with no published position left (§75 hides
    them all, nothing remains to show)."""
    if tender.status == TenderStatus.DRAFT:
        return False
    if not tender.items.filter(published=True).exists():
        return False
    if tender.access == TenderAccess.OPEN:
        return True
    return tender.invited_suppliers.filter(pk=supplier.pk).exists()


def tender_offer_autofill_suggestions(*, tender_id, supplier):
    """TASK-67 (raw ТЗ §62-64), минимальный v1-срез — не полный matching-
    движок из §62 ("название; вид товара; единицу измерения; нетто;
    характеристики; требования; разрешение аналогов"), а только прямая
    связь через уже существующие VenueProductMatrixItem (master_product +
    этот supplier). Fuzzy-поиск по названию — сознательно отложенный
    отдельный гэп, не блокирует то, что подтвердил Павел.

    venue_ids подсказки — всегда ПОЛНЫЙ список заведений позиции, никогда
    подмножество (follow-up Павла в Telegram 2026-09-17,
    src_telegram_pavel_tenders_venue_autoselect_clarification_20260917:
    "заведения выбираются системой сама, все проставлены галочками, а
    поставщик руками убирает лишние") — расходится с буквальным §63/§83.

    price — минимум resolve_supplier_product_price по всем заведениям
    позиции (§58-59, та же логика, что уже использует
    master_product_tender_offer_diff). Если позиция без заведений, нет
    связанного товара поставщика, или для него нигде не резолвится цена —
    NOT_FOUND (§66 требует товар+цена+хотя бы одно заведение, чтобы
    предложение считалось «готово»)."""
    items = list(tender_items_for_supplier_queryset(tender_id, supplier.id))
    products = _autofill_products(items, supplier)
    prices = _active_individual_prices(
        product_ids=[product.id for product in products.values()],
        venue_ids={venue.id for item in items for venue in item.venues.all()},
    )
    return [
        _tender_item_offer_autofill_suggestion(
            item=item, supplier_product=products.get(item.master_product_id), individual_prices=prices
        )
        for item in items
    ]


def _autofill_products(items, supplier):
    """BE-61: the best linked product per position in one query — the one
    linked to the position's master product in the most directory rows, then
    the lowest base price (NULLs last, as Postgres sorts), then by id."""
    counts = (
        VenueProductMatrixItem.objects.filter(
            master_product_id__in={item.master_product_id for item in items}, supplier_product__supplier=supplier
        )
        .values("master_product_id", "supplier_product_id", "supplier_product__current_price")
        .annotate(link_count=Count("id"))
    )
    best = {}
    for row in counts:
        price = row["supplier_product__current_price"]
        key = (-row["link_count"], price is None, price or 0, str(row["supplier_product_id"]))
        mp_id = row["master_product_id"]
        if mp_id not in best or key < best[mp_id][0]:
            best[mp_id] = (key, row["supplier_product_id"])
    products = SupplierProduct.objects.in_bulk([product_id for _, product_id in best.values()])
    return {mp_id: products[product_id] for mp_id, (_, product_id) in best.items()}


def _active_individual_price_rows(*, product_ids, venue_ids):
    """BE-61: the same rule as resolve_supplier_product_price, for many
    products and venues in one query (the product matrix page does the same):
    the latest ACTIVE individual price valid now. {(product_id, venue_id): row}."""
    now = timezone.now()
    rows = (
        SupplierProductVenuePrice.objects.filter(
            supplier_product_id__in=product_ids,
            venue_id__in=venue_ids,
            status=SupplierProductVenuePriceStatus.ACTIVE,
            valid_from__lte=now,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .order_by("-valid_from")
        .only("supplier_product_id", "venue_id", "price", "currency", "source_tender_offer_id")
    )
    active = {}
    for row in rows:
        active.setdefault((row.supplier_product_id, row.venue_id), row)
    return active


def _active_individual_prices(*, product_ids, venue_ids):
    """{(product_id, venue_id): (price, currency)} of _active_individual_price_rows."""
    rows = _active_individual_price_rows(product_ids=product_ids, venue_ids=venue_ids)
    return {pair: (row.price, row.currency) for pair, row in rows.items()}


def _resolved_price(supplier_product, venue_id, individual_prices):
    """Raw ТЗ §58-60: (price, currency, source) of a product in one venue — the
    latest active individual price valid now, else the base price, else all
    None. individual_prices comes from _active_individual_prices."""
    individual = individual_prices.get((supplier_product.id, venue_id))
    if individual is not None:
        return (*individual, TenderVenuePriceSource.INDIVIDUAL)
    if supplier_product.current_price is not None:
        return supplier_product.current_price, "RUB", TenderVenuePriceSource.BASE
    return None, None, None


def _venue_prices(*, item, supplier_product, individual_prices):
    """Raw ТЗ §58-60: the product's current price in each venue of the
    position, sorted by venue name; price and source are null when neither
    an individual nor a base price exists."""
    from types import SimpleNamespace

    rows = []
    for venue in sorted(item.venues.all(), key=lambda v: (venue_label(v), str(v.id))):
        price, currency, source = _resolved_price(supplier_product, venue.id, individual_prices)
        rows.append(
            SimpleNamespace(
                venue_id=venue.id, venue_name=venue_label(venue), price=price, currency=currency, price_source=source
            )
        )
    return rows


def tender_item_current_products(item):
    """QA 01.10, docs/adr/0016: what each venue of the position has in its
    directory now — the active row for the position's master product (its
    name), the supplier product on it and its price there; nulls for what the
    venue has not."""
    from types import SimpleNamespace

    venues = sorted(item.venues.all(), key=lambda v: (venue_label(v), str(v.id)))
    rows = {
        row.venue_id: row
        for row in VenueProductMatrixItem.objects.filter(
            master_product_id=item.master_product_id,
            venue_id__in=[venue.id for venue in venues],
            status=EntityStatus.ACTIVE,
        ).select_related("supplier_product__supplier")
    }
    prices = _active_individual_prices(
        product_ids={row.supplier_product_id for row in rows.values() if row.supplier_product_id},
        venue_ids=[venue.id for venue in venues],
    )
    result = []
    for venue in venues:
        row = rows.get(venue.id)
        product = row.supplier_product if row else None
        price, currency, source = _resolved_price(product, venue.id, prices) if product else (None, None, None)
        result.append(
            SimpleNamespace(
                venue_id=venue.id,
                venue_name=venue_label(venue),
                requested_name=row.requested_name if row else None,
                supplier_id=product.supplier_id if product else None,
                supplier_name=product.supplier.display_name if product else None,
                supplier_product_id=product.id if product else None,
                supplier_product_name=product.supplier_name if product else None,
                price=price,
                currency=currency,
                price_source=source,
            )
        )
    return result


def price_per_position_unit(price, supplier_product, position_unit):
    """QA 01.10, docs/adr/0016: an offer's price per unit of the position (the
    master product's unit), so offers in different packs compare. The sale
    unit converts within its kind (g→kg, ml→l); otherwise the pack's net does;
    otherwise None. Rounded half up to kopecks."""
    if price is None or supplier_product is None or position_unit is None:
        return None

    def position_units_in(unit):
        if unit is None:
            return None
        if unit.id == position_unit.id:
            return Decimal(1)
        if unit.kind == position_unit.kind and unit.ratio_to_base and position_unit.ratio_to_base:
            return unit.ratio_to_base / position_unit.ratio_to_base
        return None

    per_sale_unit = position_units_in(supplier_product.unit)
    if per_sale_unit is None and supplier_product.net_value:
        net_factor = position_units_in(supplier_product.net_unit)
        if net_factor is not None:
            per_sale_unit = supplier_product.net_value * net_factor
    if not per_sale_unit:
        return None
    return (Decimal(price) / per_sale_unit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def tender_item_venue_prices(*, item, supplier_product):
    """The §60 breakdown for a product the supplier picked by hand."""
    prices = _active_individual_prices(
        product_ids=[supplier_product.id], venue_ids=[venue.id for venue in item.venues.all()]
    )
    return _venue_prices(item=item, supplier_product=supplier_product, individual_prices=prices)


def _tender_item_offer_autofill_suggestion(*, item, supplier_product, individual_prices):
    from types import SimpleNamespace

    venue_ids = [v.id for v in item.venues.all()]

    def not_found():
        return SimpleNamespace(
            tender_item_id=item.id,
            match_status=TenderOfferAutofillMatchStatus.NOT_FOUND,
            supplier_product_id=None,
            supplier_product_name=None,
            supplier_product_raw_name=None,
            price=None,
            currency=None,
            venue_ids=[],
            venue_prices=[],
        )

    if not venue_ids or supplier_product is None:
        return not_found()

    venue_prices = _venue_prices(item=item, supplier_product=supplier_product, individual_prices=individual_prices)
    priced = [row for row in venue_prices if row.price is not None]
    if not priced:
        return not_found()
    cheapest = min(priced, key=lambda row: row.price)

    return SimpleNamespace(
        tender_item_id=item.id,
        match_status=TenderOfferAutofillMatchStatus.FOUND,
        supplier_product_id=supplier_product.id,
        supplier_product_name=supplier_product.normalized_name,
        supplier_product_raw_name=supplier_product.supplier_name,
        price=cheapest.price,
        currency=cheapest.currency,
        venue_ids=venue_ids,
        venue_prices=venue_prices,
    )


def master_product_tender_offer_diff(*, items, new_supplier_product, new_price, new_currency):
    """Raw ТЗ §45-47 / spec §3.6 п.3: applying a winning tender offer
    changes supplier_product AND price together, and §46 requires showing
    both before applying — same shape as master_product_bulk_supplier_
    product_diff, extended with the price each row would show before/after.
    current_price is resolved through resolve_supplier_product_price (the
    same read-time resolution the company catalog already uses), so it
    reflects an existing individual price if one is active, falling back to
    the current supplier_product's base price — null when the row has no
    supplier_product assigned yet. new_price/new_currency are simply the
    caller's inputs, echoed onto every row: §43 - "одна цена действует для
    всех компаний, выбранных поставщиком в одном предложении"."""
    rows = master_product_bulk_supplier_product_diff(items=items, new_supplier_product=new_supplier_product)
    items_by_id = {item.id: item for item in items}
    for row in rows:
        item = items_by_id[row.matrix_item_id]
        if item.supplier_product_id:
            current_price, current_currency, _current_individual_price_id = resolve_supplier_product_price(
                supplier_product=item.supplier_product, venue_id=item.venue_id
            )
        else:
            current_price, current_currency = None, None
        row.venue_name = venue_label(item.venue)
        row.current_price = current_price
        row.current_price_currency = current_currency
        row.new_price = new_price
        row.new_price_currency = new_currency
    return rows
