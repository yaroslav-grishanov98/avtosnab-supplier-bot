"""Venue cart, orders and supplier order request reads."""

import uuid

from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404

from core.models import (
    EntityStatus,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderRevision,
    OrderStatus,
    SupplierOrderRequest,
    SupplierOrderRequestStatus,
    VenueCart,
    VenueUser,
)


def _group_items_by_supplier(items):
    """Общая для Order и Cart группировка позиций по поставщику (обычному/вне
    системы/без распознанного поставщика), в порядке появления первой позиции
    группы. Возвращает список dict'ов с сырыми объектами в "items" —
    сериализация конкретным ItemSerializer'ом остаётся на вызывающей стороне,
    т.к. у Order и Cart разные поля позиции (confirmed_*/status только у Order).
    2026-09-17: живёт в selectors.py (не serializers.py, где была раньше) —
    venue_supplier_order_rows ниже тоже на неё завязан, а serializers.py
    импортирует ИЗ selectors.py, не наоборот (иначе циклический импорт)."""
    groups = {}
    order = []
    for item in items:
        if item.supplier_product_id:
            key = ("supplier", item.supplier_product.supplier_id)
            supplier_id = item.supplier_product.supplier_id
            supplier_name = item.supplier_product.supplier.display_name
            is_off_system = False
        elif item.off_system_supplier_id:
            key = ("off_system", item.off_system_supplier_id)
            supplier_id = item.off_system_supplier_id
            supplier_name = item.off_system_supplier.display_name
            is_off_system = True
        else:
            key = ("none", None)
            supplier_id = None
            supplier_name = None
            is_off_system = False

        if key not in groups:
            groups[key] = {
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "is_off_system": is_off_system,
                "items": [],
            }
            order.append(key)
        groups[key]["items"].append(item)

    return [groups[key] for key in order]


def supplier_group_minimum_order(items):
    """2026-09-25 (Павел, задача #Лк1, подраздел "Корзину и Историю заявок"):
    "Мин. заказ" группы — читается с обычного Supplier.delivery_min_order_amount/
    delivery_currency, либо, для off-system группы, с
    OffSystemSupplier.delivery_min_order_amount/delivery_currency (задача #Лк1,
    TASK-86). Общая для CartSupplierGroupSerializer (корзина) и
    _venue_supplier_order_row (история заявок) — тот же смысл в обоих местах,
    не дублировать логику. items — непустой список позиций одной группы (из
    _group_items_by_supplier)."""
    first = items[0]
    if first.supplier_product_id:
        supplier = first.supplier_product.supplier
        return supplier.delivery_min_order_amount, supplier.delivery_currency
    if first.off_system_supplier_id:
        off_system_supplier = first.off_system_supplier
        return off_system_supplier.delivery_min_order_amount, off_system_supplier.delivery_currency
    return None, None


def _order_supplier_group_status(items, order_status):
    """2026-09-15, пользователь: "неверный статус у второго товара confirmed
    а должен быть partially-confirmed" на GET /venues/{venue_id}/orders.
    Order.status — грубая агрегация (submitted, пока хоть одна позиция
    любого поставщика не отвечена, иначе confirmed/cancelled, см.
    _advance_order_status_if_fully_resolved в services.py), поэтому заказ,
    где ОДИН поставщик принял позиции лишь частично, всё равно показывал
    только общий "confirmed" — заведение не видело, что конкретно этот
    поставщик не всё подтвердил. Точная по-поставщицкая логика уже была —
    services._recompute_supplier_order_request_status, — здесь та же самая
    логика, только вычисляется прямо из уже загруженных items (без похода
    к SupplierOrderRequest), чтобы работать одинаково для обычных и
    вне-системных поставщиков (у вне-системных заявки-сущности нет вовсе)."""
    if any(item.status == OrderItemStatus.SUBMITTED for item in items):
        return SupplierOrderRequestStatus.SUBMITTED
    fully_confirmed = 0
    partially_confirmed = 0
    for item in items:
        if item.status == OrderItemStatus.CONFIRMED:
            if item.confirmed_qty == item.requested_qty:
                fully_confirmed += 1
            else:
                partially_confirmed += 1
    if partially_confirmed:
        status = SupplierOrderRequestStatus.PARTIALLY_CONFIRMED
    elif fully_confirmed == 0:
        status = SupplierOrderRequestStatus.DECLINED
    elif fully_confirmed == len(items):
        status = SupplierOrderRequestStatus.CONFIRMED
    else:
        status = SupplierOrderRequestStatus.PARTIALLY_CONFIRMED
    if order_status == OrderStatus.COMPLETED and status != SupplierOrderRequestStatus.DECLINED:
        return SupplierOrderRequestStatus.COMPLETED
    return status


# 2026-09-17: стабильное пространство имён для синтетических id строк
# venue_supplier_order_rows — у вне-системных/без-распознанного поставщика
# групп нет персистентной сущности заявки (SupplierOrderRequest только для
# обычных поставщиков), а строка в списке всё равно должна иметь id.
_VENUE_SUPPLIER_ROW_NAMESPACE = uuid.UUID("6e6e1a8e-6f2b-4c1a-9f0f-6b6b1f6c2a3d")


def _venue_supplier_order_row(order, group):
    """2026-09-17, по прямой просьбе пользователя ("у тебя должны с бэка
    отдаваться на каждого поставщика заявки, а не одна заявка на двух и
    более поставщиков" — история заказов заведения раньше отдавала одну
    строку на Order с suppliers[] внутри, теперь одна строка на КАЖДУЮ
    заявку/группу поставщика, тем же паттерном, что уже был у поставщика
    (GET /suppliers/{supplier_id}/orders)). Обычные поставщики уже имеют
    персистентную SupplierOrderRequest (id/number/viewed_at) — читаем её
    прямо с первого item группы (все items группы делят один и тот же
    supplier_request по построению). У вне-системных/нераспознанных
    поставщиков такой сущности нет вовсе (§30 raw ТЗ не определяет её) —
    id синтезируется детерминированно (стабилен между запросами одного и
    того же заказа+поставщика), number/viewed_at остаются null."""
    items = group["items"]
    supplier_request = items[0].supplier_request if not group["is_off_system"] and group["supplier_id"] else None
    if supplier_request is not None:
        row_id = supplier_request.id
        number = supplier_request.number
        viewed_at = supplier_request.viewed_at
        created_at = supplier_request.created_at
        updated_at = supplier_request.updated_at
    else:
        row_id = uuid.uuid5(_VENUE_SUPPLIER_ROW_NAMESPACE, f"{order.id}:{group['supplier_id']}")
        number = None
        viewed_at = None
        created_at = order.created_at
        updated_at = order.updated_at
    minimum_order_amount, currency = supplier_group_minimum_order(items)
    return {
        "id": row_id,
        "number": number,
        "order_id": order.id,
        "order_number": order.order_number,
        "venue_id": order.venue_id,
        "venue_name": order.venue.display_name,
        "supplier_id": group["supplier_id"],
        "supplier_name": group["supplier_name"],
        "is_off_system": group["is_off_system"],
        "status": _order_supplier_group_status(items, order.status),
        "viewed_at": viewed_at,
        "items_count": len(items),
        "requested_delivery_date": order.requested_delivery_date,
        "minimum_order_amount": minimum_order_amount,
        "currency": currency,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _build_venue_supplier_order_rows(order_queryset, filters):
    """Flattens an Order queryset into one row per (order, supplier) group —
    see _venue_supplier_order_row. Materializes and sorts in Python rather
    than returning a lazy queryset: rows span two heterogeneous sources
    (persisted SupplierOrderRequest for real suppliers, computed groups for
    off-system/unrecognized ones) that can't be expressed as a single SQL
    query. Callers slice the returned list for pagination themselves."""
    rows = []
    for order in order_queryset:
        for group in _group_items_by_supplier(order.items.all()):
            rows.append(_venue_supplier_order_row(order, group))

    row_status = filters.get("status")
    if row_status:
        rows = [row for row in rows if row["status"] == row_status]

    rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
    return rows


def venue_supplier_order_rows(venue_id, filters):
    """2026-09-17: replaces venue_orders_queryset — see
    _build_venue_supplier_order_rows. select_related("venue") — _venue_
    supplier_order_row always reads order.venue.display_name (kept even
    though venue_id is already known from the URL here, for a single
    code path shared with venue_mine_supplier_order_rows below, where
    rows DO span multiple venues and venue_name is genuinely needed).
    Rows are summary-only (no items) — only supplier_product__supplier/
    off_system_supplier (grouping key + name) and supplier_request
    (id/number/viewed_at) are prefetched, unlike venue_order_detail_
    queryset's fuller set (unit/requested_unit/subdivision), which this
    list no longer needs."""
    order_queryset = (
        Order.objects.filter(venue_id=venue_id)
        .exclude(status="awaiting_dispatch")
        .select_related("venue")
        .prefetch_related(
            "items__supplier_product__supplier",
            "items__supplier_product__category__parent__parent",
            "items__off_system_supplier",
            "items__supplier_request",
        )
        .order_by("-created_at", "-id")
    )
    return _build_venue_supplier_order_rows(order_queryset, filters)


def venue_mine_supplier_order_rows(*, user_id, filters):
    """2026-09-17: mine-aggregate counterpart of venue_supplier_order_rows —
    same reasoning as venue_mine_orders_queryset used to have (mirrors the
    single-venue version, but not scoped to one venue_id).
    2026-09-18, Дима: "должен быть фильтр: Заведения (с мультиселектом)" —
    filters["venue_id"], when given, narrows further (AND, not OR, with the
    managed-venues list) — a venue_id the user doesn't manage is silently
    dropped rather than 403ing, same spirit as the existing status filter."""
    venue_ids = VenueUser.objects.filter(user_id=user_id, status=EntityStatus.ACTIVE).values_list("venue_id", flat=True)
    order_queryset = (
        Order.objects.filter(venue_id__in=venue_ids)
        .exclude(status="awaiting_dispatch")
        .select_related("venue")
        .prefetch_related(
            "items__supplier_product__supplier",
            "items__supplier_product__category__parent__parent",
            "items__off_system_supplier",
            "items__supplier_request",
        )
        .order_by("-created_at", "-id")
    )
    venue_id_filter = filters.get("venue_id")
    if venue_id_filter:
        order_queryset = order_queryset.filter(venue_id__in=venue_id_filter)
    return _build_venue_supplier_order_rows(order_queryset, filters)


def venue_order_detail_queryset(venue_id):
    # 2026-09-09: select_related("venue") — та же причина, что у
    # venue_orders_queryset выше.
    return (
        Order.objects.filter(venue_id=venue_id)
        .select_related("venue", "subdivision", "delivery_address")
        .prefetch_related(
            "items",
            "items__supplier_product",
            "items__supplier_product__supplier",
            "items__supplier_product__category__parent__parent",
            "items__supplier_product__unit",
            "items__requested_unit",
            "items__off_system_supplier",
            "items__subdivision",
            "items__supplier_request",
            "contacts",
        )
    )


def resolve_venue_order(queryset, venue_id, order_id):
    """2026-09-17: BUG на dev — "сломал в истории этот запрос", 404 на
    GET .../venues/{venue_id}/orders/{order_id}. Тот же класс регрессии,
    что уже чинил на стороне поставщика (resolve_supplier_order_request,
    2026-09-15) — venue_supplier_order_rows (список истории заказов
    заведения) отдаёт `id` заявки/группы поставщика отдельно от `order_id`
    заказа с 2026-09-17 (расхождение по прямой просьбе пользователя:
    "у тебя должны с бэка отдаваться на каждого поставщика заявки"), а
    фронт, скорее всего, продолжает переходить в детали заказа по
    `row.id`, который теперь не всегда совпадает с order_id.

    Резолвит `order_id` из URL в реальный `Order`, пробуя по очереди:
    1. Order.pk напрямую (старое поведение, обычные ссылки);
    2. SupplierOrderRequest.pk — самый частый случай (обычный поставщик);
    3. синтетический id вычисленной группы (вне-системный поставщик/без
       распознанного поставщика — у них нет персистентной сущности заявки
       вовсе, id вычисляется на лету в _venue_supplier_order_row) — редкий
       fallback, единственный способ узнать её заранее — пересчитать
       группы по всем заказам заведения, поэтому пробуется последним."""
    order = queryset.filter(pk=order_id).first()
    if order is not None:
        return order

    request_order_id = (
        SupplierOrderRequest.objects.filter(pk=order_id, order__venue_id=venue_id)
        .values_list("order_id", flat=True)
        .first()
    )
    if request_order_id is not None:
        order = queryset.filter(pk=request_order_id).first()
        if order is not None:
            return order

    candidates = Order.objects.filter(venue_id=venue_id).prefetch_related(
        "items__supplier_product__supplier",
        "items__supplier_product__category__parent__parent",
        "items__off_system_supplier",
    )
    for candidate in candidates:
        for group in _group_items_by_supplier(candidate.items.all()):
            row = _venue_supplier_order_row(candidate, group)
            if str(row["id"]) == str(order_id):
                order = queryset.filter(pk=candidate.id).first()
                if order is not None:
                    return order

    raise Http404("No Order matches the given query.")


def venue_unresolved_order_items_queryset(
    venue_id, *, q=None, subdivision_id=None, supplier_id=None, off_system_supplier_id=None
):
    """Raw ТЗ §3.5/§16-18 "Неразобранные": items this venue still needs to
    find a replacement for — a supplier fully declined (CANCELLED), or
    confirmed less than requested (CONFIRMED with confirmed_qty <
    requested_qty; only the unfulfilled remainder needs re-sourcing, §16).

    Deliberately excludes CANCELLED_BY_VENUE (§23 — the venue cancelled it
    on purpose, doesn't need a replacement) and REPLACED (§19 — its
    remainder was already re-routed via "Найти замену", the new item is
    what's outstanding now, not this one) simply by not matching either
    filter branch — no explicit .exclude() needed.

    2026-09-21: `q`/`subdivision_id` — same filter pattern as
    venue_product_matrix_queryset (requested_name__icontains / exact
    subdivision_id match).

    2026-09-28 (ТЗ панели «Неразобранные»): `supplier_id`/`off_system_supplier_id`
    — фильтр «Все поставщики»; позиции, убранные кнопкой «Убрать из списка»
    (unresolved_dismissed_at), больше не попадают никуда — ни в список, ни в счётчик."""
    return _unresolved_order_items(
        OrderItem.objects.filter(order__venue_id=venue_id),
        q=q,
        subdivision_id=subdivision_id,
        supplier_id=supplier_id,
        off_system_supplier_id=off_system_supplier_id,
    )


def venue_mine_unresolved_order_items_queryset(
    *, user_id, venue_ids=None, q=None, supplier_id=None, off_system_supplier_id=None
):
    """2026-09-28 (ТЗ панели «Неразобранные», фильтр «Все заведения»): те же
    «Неразобранные», но по всем заведениям, где пользователь — активный участник.
    venue_ids сужает (AND) — чужое заведение молча отбрасывается, тот же подход,
    что у venue_mine_supplier_order_rows."""
    managed_venue_ids = VenueUser.objects.filter(user_id=user_id, status=EntityStatus.ACTIVE).values_list(
        "venue_id", flat=True
    )
    queryset = OrderItem.objects.filter(order__venue_id__in=managed_venue_ids)
    if venue_ids:
        queryset = queryset.filter(order__venue_id__in=venue_ids)
    return _unresolved_order_items(
        queryset, q=q, supplier_id=supplier_id, off_system_supplier_id=off_system_supplier_id
    )


def _unresolved_order_items(queryset, *, q=None, subdivision_id=None, supplier_id=None, off_system_supplier_id=None):
    queryset = (
        queryset.filter(
            Q(status=OrderItemStatus.CANCELLED)
            | Q(status=OrderItemStatus.CONFIRMED, confirmed_qty__lt=F("requested_qty"))
        )
        .filter(unresolved_dismissed_at__isnull=True)
        .annotate(
            _revision_content=Subquery(
                OrderRevision.objects.filter(
                    order_id=OuterRef("order_id"), revision=OuterRef("order__revision")
                ).values("content")[:1]
            )
        )
        .select_related(
            "order__venue",
            "order__subdivision",
            "supplier_product__supplier",
            "supplier_product__category__parent__parent",
            "supplier_product__unit",
            "requested_unit",
            "subdivision",
            "supplier_request",
            "off_system_supplier",
        )
    )
    if q:
        queryset = queryset.filter(requested_name__icontains=q)
    if subdivision_id:
        # Older orders may have the subdivision only on Order. Preserve
        # explicit line overrides and fall back only for unassigned lines.
        queryset = queryset.filter(
            Q(subdivision_id=subdivision_id) | Q(subdivision__isnull=True, order__subdivision_id=subdivision_id)
        )
    if supplier_id:
        queryset = queryset.filter(supplier_product__supplier_id=supplier_id)
    if off_system_supplier_id:
        queryset = queryset.filter(off_system_supplier_id=off_system_supplier_id)
    return queryset.order_by("-created_at", "-id")


def cart_detail_queryset(user_id, venue_id):
    """Mirrors venue_order_detail_queryset for the Cart/Order split (2026-08-28).
    2026-09-09: no more select_related("subdivision", "delivery_address")/"contacts"
    prefetch — those moved off VenueCart entirely, onto POST /cart/submit params.
    2026-09-18: filters by (user, venue) again — the cart is (user, venue)-scoped
    once more (reverses 2026-09-08's account-only scoping)."""
    return VenueCart.objects.filter(user_id=user_id, venue_id=venue_id).prefetch_related(
        "items",
        "items__supplier_product",
        "items__supplier_product__supplier",
        "items__supplier_product__category__parent__parent",
        "items__supplier_product__unit",
        "items__requested_unit",
        "items__off_system_supplier",
        "items__subdivision",
    )


def supplier_order_requests_queryset(supplier_id, filters):
    """2026-09-15 (raw ТЗ "ЛК Заведения" §3.3): "заявка" is now the
    persisted SupplierOrderRequest, one row per (order, supplier) — replaces
    the old approach of filtering Order by `items__supplier_product__
    supplier_id`, which had no independent status/number/Просмотрено per
    supplier (see docs/wiki/logs/2026-09-14-venue-cabinet-cart-orders-tz-
    gap-analysis.md, расхождение №1). select_related("order__venue") for
    SupplierOrderRequestSerializer.venue_name, mirrors the reasoning
    venue_orders_queryset already had for the venue side."""
    queryset = (
        SupplierOrderRequest.objects.filter(supplier_id=supplier_id)
        .select_related("order__venue", "supplier")
        .annotate(items_count=Count("items"))
        .order_by("-created_at", "-id")
    )

    request_status = filters.get("status")
    if request_status:
        queryset = queryset.filter(status=request_status)

    venue_id_filter = filters.get("venue_id")
    if venue_id_filter:
        queryset = queryset.filter(order__venue_id__in=venue_id_filter)

    return queryset


def supplier_order_request_detail_queryset(supplier_id):
    """Mirrors venue_order_detail_queryset — items here are ALWAYS this
    supplier's own (a SupplierOrderRequest only ever holds one supplier's
    items by construction, unlike the old Order-based filter which needed
    an explicit `.filter(supplier_product__supplier_id=...)` on the
    prefetch to keep other suppliers' items out)."""
    return (
        SupplierOrderRequest.objects.filter(supplier_id=supplier_id)
        .select_related("order__venue", "supplier")
        .prefetch_related(
            Prefetch(
                "items",
                queryset=OrderItem.objects.select_related(
                    "supplier_product__supplier",
                    "supplier_product__category__parent__parent",
                    "supplier_product__unit",
                    "requested_unit",
                    "subdivision",
                    "supplier_request",
                ),
            )
        )
    )


def resolve_supplier_order_request(queryset, order_id):
    """2026-09-15: BUG on dev — "во всех заказах у поставщика не открываются
    заказы, 404". Root cause: the `/suppliers/{id}/orders/{order_id}` path
    param is (by design) the Order's pk, matched via `order_id=order_id`
    against SupplierOrderRequestSerializer's own `order_id` field — but that
    same list row's `id` field is the SupplierOrderRequest's OWN pk (a
    distinct value, §3.3). Before this session's SupplierOrderRequest
    refactor, list rows WERE Order objects, so `id` WAS the order id — a
    frontend built against that older contract is still plausibly
    navigating with `row.id`, which now silently means something else.
    Confirmed on dev via VictoriaLogs: a brand-new order, created well after
    the refactor deployed, 404s the exact same way as pre-existing orders —
    rules out stale backfilled data as the (sole) cause. Matching on EITHER
    the request's own pk OR order_id makes the lookup work regardless of
    which id the caller sends, without depending on a frontend redeploy."""
    return get_object_or_404(queryset, Q(pk=order_id) | Q(order_id=order_id))
