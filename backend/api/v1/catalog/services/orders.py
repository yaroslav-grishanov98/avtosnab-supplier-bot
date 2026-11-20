"""Venue cart, orders and supplier order requests."""

import uuid
from datetime import date

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.v1.catalog.selectors import (
    resolve_supplier_product_price,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    Order,
    OrderContact,
    OrderItem,
    OrderItemAppliedPriceType,
    OrderItemStatus,
    OrderStatus,
    SupplierOrderRequest,
    SupplierOrderRequestStatus,
    User,
    VenueCart,
    VenueCartItem,
)


def _generate_order_number():
    return f"ORD-{uuid.uuid4().hex[:10].upper()}"


def _generate_supplier_order_request_number():
    return f"REQ-{uuid.uuid4().hex[:10].upper()}"


@transaction.atomic
def get_or_create_cart(user, venue):
    """2026-08-28: Cart/Order split (Vitya — "концептуально cart и order это
    разные бизнес сущности... это архитектурное нарушение"). VenueCart never
    carries order_number/status/confirmed_* fields that only make sense for
    an already-submitted Order.

    2026-09-18, пользователь: "нужно получать корзину по ид заведения" —
    reverses 2026-09-08's account-only singleton. One cart per (user, venue)
    pair now, not one shared cart per account — closes the "items from
    different venues silently mix in one cart" gap that came with the
    account-only design."""
    cart, _ = VenueCart.objects.get_or_create(user=user, venue=venue)
    return cart


@transaction.atomic
def update_venue_cart(cart, **fields):
    cart = VenueCart.objects.select_for_update().get(pk=cart.pk)
    for field, value in fields.items():
        setattr(cart, field, value)
    cart.save(update_fields=list(fields.keys()) + ["updated_at"])
    return cart


@transaction.atomic
def add_cart_item(
    *,
    cart,
    supplier_product=None,
    requested_name,
    requested_qty,
    requested_unit=None,
    comment=None,
    subdivision=None,
    off_system_supplier=None,
):
    VenueCart.objects.select_for_update().get(pk=cart.pk)
    return VenueCartItem.objects.create(
        cart=cart,
        supplier_product=supplier_product,
        requested_name=requested_name,
        requested_qty=requested_qty,
        requested_unit=requested_unit,
        comment=comment,
        subdivision=subdivision,
        off_system_supplier=off_system_supplier,
    )


@transaction.atomic
def update_cart_item(item, **fields):
    """2026-09-08 (Витя, баг с быстрым вводом количества): зажатая кнопка "+"
    шлёт несколько независимых PATCH подряд, и без блокировки строки они
    коммитятся в произвольном порядке — итоговое qty могло оказаться от
    более раннего запроса, а не от последнего. Клиент присылает абсолютное
    значение, так что классической потери апдейта (read-modify-write) тут
    нет, но порядок коммитов всё равно решал исход. select_for_update
    сериализует конкурентные записи по одной и той же позиции.

    Полностью гонку "чей ответ фронт применит последним" это не закрывает —
    это уже задача клиента, для чего CartItemSerializer отдаёт updated_at
    (см. его help_text)."""
    VenueCart.objects.select_for_update().get(pk=item.cart_id)
    locked = VenueCartItem.objects.select_for_update().filter(pk=item.pk).first()
    if locked is None:
        return item
    for field, value in fields.items():
        setattr(locked, field, value)
    locked.save(update_fields=list(fields.keys()) + ["updated_at"])
    return locked


@transaction.atomic
def remove_cart_item(item):
    VenueCart.objects.select_for_update().get(pk=item.cart_id)
    item.delete()


@transaction.atomic
def create_order_from_cart(cart, *, subdivision=None, delivery_address=None, contacts=None, actor=None):
    """Save the cart as an awaiting_dispatch Order and clear its item rows.

    Approval orchestration calls this inside the submit transaction. It
    snapshots the saved content and either starts approval or dispatches
    directly. This helper never creates supplier requests or notifications.
    The VenueCart row persists for reuse.

    2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §38-39): price is
    re-resolved here, right before the Order row is actually created — a
    cart item can sit for hours/days, so whatever price a preview showed
    earlier may be stale by now. The resolved price/type/individual-price-id
    is snapshotted onto the OrderItem (applied_price/_type/_individual_price)
    and never re-resolved afterwards — a later change to the individual
    price must not retroactively change an already-submitted order (§39's
    own example). Only items with a supplier_product resolve a price at
    all — off-system-supplier/free-text items have nothing to resolve.

    2026-09-18, пользователь: "Привязать по-настоящему" — cart is
    (user, venue)-scoped again (reverses 2026-09-08's account-only design),
    so `venue` is no longer a caller-supplied param — it's `cart.venue`,
    authoritative. The per-item subdivision/off_system_supplier cross-venue
    check that lived here (2026-09-08 through 09-09, defending against a
    cart holding items from more venues than the one being submitted to) is
    gone too: an item can now only ever be added to this cart with a
    subdivision/off_system_supplier belonging to `cart.venue` in the first
    place (enforced at add-item time, see views.VenueCartItemView), so a
    mismatch here is structurally unreachable now — leaving the check in
    would be dead code masking that invariant.

    `subdivision`/`delivery_address`/`contacts` stay explicit params
    (2026-09-09, Витя: "вынести вообще из корзины и принимать параметрами в
    /cart/submit") — resolved and ownership-checked by the caller against
    `cart.venue` (views.py). `contacts` is a list of {full_name, email,
    phone} dicts, copied into separate OrderContact rows."""
    items = list(cart.items.select_related("supplier_product", "subdivision", "off_system_supplier").all())
    if not items:
        return None, "Cannot submit an empty cart."

    order = Order.objects.create(
        venue=cart.venue,
        order_number=_generate_order_number(),
        status=OrderStatus.AWAITING_DISPATCH,
        created_by_user=actor,
        comment=cart.comment,
        requested_delivery_date=cart.requested_delivery_date,
        subdivision=subdivision,
        delivery_address=delivery_address,
        submitted_at=None,
    )
    order_items = []
    for item in items:
        applied_price = applied_price_type = applied_individual_price_id = _currency = None
        if item.supplier_product_id is not None:
            applied_price, _currency, applied_individual_price_id = resolve_supplier_product_price(
                supplier_product=item.supplier_product, venue_id=cart.venue_id
            )
            applied_price_type = (
                OrderItemAppliedPriceType.INDIVIDUAL if applied_individual_price_id else OrderItemAppliedPriceType.BASE
            )
        order_items.append(
            OrderItem(
                order=order,
                supplier_product=item.supplier_product,
                off_system_supplier=item.off_system_supplier,
                # The order form's selected subdivision is the default for
                # every line without its own explicit subdivision.
                subdivision=item.subdivision or subdivision,
                requested_name=item.requested_name,
                requested_qty=item.requested_qty,
                requested_unit=item.requested_unit,
                comment=item.comment,
                status=OrderItemStatus.SUBMITTED,
                applied_price=applied_price,
                applied_currency=_currency,
                applied_price_type=applied_price_type,
                applied_individual_price_id=applied_individual_price_id,
            )
        )
    order_items = OrderItem.objects.bulk_create(order_items)
    OrderContact.objects.bulk_create(
        [
            OrderContact(order=order, full_name=c.get("full_name"), email=c.get("email"), phone=c.get("phone"))
            for c in (contacts or [])
        ]
    )
    cart.items.all().delete()
    return order, None


@transaction.atomic
def accept_supplier_order_items(
    *,
    order,
    supplier_id,
    confirmed_delivery_date,
    item_quantities=None,
    comment=None,
    actor=None,
    delivery_date_unknown=False,
):
    """Принять позиции поставщика с датой поставки или без точного срока.
    Scoped to this supplier's own items only — an Order can span several
    suppliers (grouping is via OrderItem.supplier_product.supplier), other
    suppliers' items in the same order are untouched and may still be
    pending.

    2026-09-03 (Виктор/Павел board screenshot — closes the "частичный отказ"
    half of the open question left by the 2026-08-21 call, see below):
    `item_quantities` is an optional {item_id: confirmed_qty} map for
    per-line overrides within this one call. An item not present defaults to
    whole-item acceptance (confirmed_qty = requested_qty), same as before —
    plain "accept everything" calls are unaffected. A listed item with
    0 < confirmed_qty < requested_qty is a partial accept (the unfulfilled
    remainder can be re-routed via change_order_item_supplier()); a listed
    item with confirmed_qty = 0 is a targeted decline of just that line,
    without declining the rest of this supplier's items in the same call.
    Bad references/quantities raise ValidationError (400) — distinct from
    the (False, message) 409-conflict pairs below, which are about the
    order/items being in the wrong *state*, not about malformed input.

    2026-09-22, по просьбе пользователя: `comment` — один комментарий
    поставщика на весь этот вызов (не на отдельную позицию), сохраняется на
    SupplierOrderRequest.supplier_comment.

    The "смена поставщика" other half — reassigning a declined/partial
    remainder to a different supplier — is change_order_item_supplier()."""
    today = timezone.localdate()
    last_allowed_delivery_date = date(today.year, 12, 31)
    if (confirmed_delivery_date is None and not delivery_date_unknown) or (
        confirmed_delivery_date is not None and not today <= confirmed_delivery_date <= last_allowed_delivery_date
    ):
        raise ValidationError(
            {
                "confirmed_delivery_date": (
                    f"Дата поставки должна быть не раньше {today:%d.%m.%Y} "
                    f"и не позже {last_allowed_delivery_date:%d.%m.%Y}."
                )
            }
        )
    if order.status != OrderStatus.SUBMITTED:
        return False, f"Order is {order.status}, only submitted orders can be accepted."

    items = list(
        order.items.select_for_update().filter(
            supplier_product__supplier_id=supplier_id, status=OrderItemStatus.SUBMITTED
        )
    )
    if not items:
        return False, "No pending items for this supplier in this order."

    item_quantities = item_quantities or {}
    pending_ids = {item.id for item in items}
    unknown_ids = set(item_quantities) - pending_ids
    if unknown_ids:
        raise ValidationError(
            {
                "items": [
                    f"Item {item_id} is not a pending item of this supplier in this order." for item_id in unknown_ids
                ]
            }
        )

    now = timezone.now()
    for item in items:
        confirmed_qty = item_quantities.get(item.id, item.requested_qty)
        if confirmed_qty > item.requested_qty:
            raise ValidationError({"items": [f"confirmed_qty for item {item.id} exceeds requested_qty."]})
        if confirmed_qty == 0:
            item.status = OrderItemStatus.CANCELLED
            item.confirmed_qty = None
        else:
            item.status = OrderItemStatus.CONFIRMED
            item.confirmed_qty = confirmed_qty
            item.confirmed_delivery_date = confirmed_delivery_date
        item.supplier_responded_at = now
        item.updated_at = now
    OrderItem.objects.bulk_update(
        items, ["status", "confirmed_qty", "confirmed_delivery_date", "supplier_responded_at", "updated_at"]
    )
    changed_request = _recompute_supplier_order_request_status(
        order_id=order.id, supplier_id=supplier_id, supplier_comment=comment or None
    )
    _enqueue_supplier_response_notification(order=order, supplier_id=supplier_id, request=changed_request)
    if changed_request is not None:
        record_audit_log(
            actor=actor,
            action=_SUPPLIER_ORDER_REQUEST_STATUS_AUDIT_ACTION[changed_request.status],
            entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST,
            entity_id=changed_request.id,
            summary=changed_request.number,
        )
    _advance_order_status_if_fully_resolved(order)
    return True, None


_SUPPLIER_ORDER_REQUEST_STATUS_AUDIT_ACTION = {
    SupplierOrderRequestStatus.CONFIRMED: AuditLogAction.CONFIRMED,
    SupplierOrderRequestStatus.PARTIALLY_CONFIRMED: AuditLogAction.PARTIALLY_CONFIRMED,
    SupplierOrderRequestStatus.DECLINED: AuditLogAction.DECLINED,
}


def _recompute_supplier_order_request_status(*, order_id, supplier_id, supplier_comment=None):
    """Raw ТЗ §11/§16-17: a SupplierOrderRequest's status is derived from
    the current state of its own items, never set directly. REPLACED
    (change_order_item_supplier's remainder-reroute) counts the same as
    CANCELLED/partial-CONFIRMED here — from this supplier's own point of
    view the item is resolved-but-not-fully-satisfied either way, exactly
    like a plain decline or partial accept.

    2026-09-22: `supplier_comment`, when given (only accept_supplier_order_items
    passes one — decline has no comment concept per the user's request),
    piggybacks onto the SAME save as the status transition below. Safe to
    couple the two: this function only reaches the save when at least one
    item just moved off SUBMITTED in THIS call, so a caller-supplied comment
    and a status change always happen together — there is no call pattern
    today where one changes without the other.

    Returns the SupplierOrderRequest if its status actually changed (so
    callers can §29-audit-log the transition with the right action), None
    otherwise (still pending, or recomputed to the same status it already
    had — e.g. a second decline call on an already-fully-resolved заявка)."""
    request = SupplierOrderRequest.objects.filter(order_id=order_id, supplier_id=supplier_id).first()
    if request is None:
        return None
    items = list(request.items.all())
    if any(item.status == OrderItemStatus.SUBMITTED for item in items):
        return None
    fully_confirmed = 0
    partially_confirmed = 0
    for item in items:
        if item.status == OrderItemStatus.CONFIRMED:
            if item.confirmed_qty == item.requested_qty:
                fully_confirmed += 1
            else:
                partially_confirmed += 1
    # a single line short of its own requested_qty already makes the whole
    # заявка "Принята частично" (§11), regardless of how the other lines
    # resolved
    if partially_confirmed:
        new_status = SupplierOrderRequestStatus.PARTIALLY_CONFIRMED
    elif fully_confirmed == 0:
        new_status = SupplierOrderRequestStatus.DECLINED
    elif fully_confirmed == len(items):
        new_status = SupplierOrderRequestStatus.CONFIRMED
    else:
        new_status = SupplierOrderRequestStatus.PARTIALLY_CONFIRMED
    if request.status != new_status:
        request.status = new_status
        update_fields = ["status", "updated_at"]
        if supplier_comment is not None:
            request.supplier_comment = supplier_comment
            update_fields.append("supplier_comment")
        request.save(update_fields=update_fields)
        return request
    return None


def _enqueue_supplier_response_notification(*, order, supplier_id, request):
    from notifications.models import NotificationEvent, NotificationInboxEventType
    from notifications.services import enqueue_for_venue, record_venue_inbox_event

    request = request or SupplierOrderRequest.objects.filter(order=order, supplier_id=supplier_id).first()
    if request is None:
        return
    if request.status == SupplierOrderRequestStatus.CONFIRMED:
        label = "Подтверждено"
    elif request.status == SupplierOrderRequestStatus.PARTIALLY_CONFIRMED:
        label = "Принято частично"
    elif request.status == SupplierOrderRequestStatus.DECLINED:
        label = "Поставщик отказался"
    else:
        label = "Поставщик ответил по части позиций"
    confirmed_items = [
        {
            "name": item.requested_name,
            "quantity": str(item.confirmed_qty or 0),
            "requested_quantity": str(item.requested_qty),
            "unit": item.requested_unit.name if item.requested_unit_id else "",
        }
        for item in request.items.select_related("requested_unit").all()
        if item.status in {OrderItemStatus.CONFIRMED, OrderItemStatus.CANCELLED}
    ]
    dates = sorted(
        {
            item.confirmed_delivery_date.isoformat()
            for item in request.items.all()
            if item.confirmed_delivery_date is not None
        }
    )
    delivery_date_unknown = (
        request.status
        in {
            SupplierOrderRequestStatus.CONFIRMED,
            SupplierOrderRequestStatus.PARTIALLY_CONFIRMED,
        }
        and not dates
    )
    payload = {
        "request_number": request.number,
        "order_number": order.order_number,
        "venue": order.venue.display_name,
        "supplier": request.supplier.display_name,
        "status_label": label,
        "delivery_dates": dates,
        "delivery_date_unknown": delivery_date_unknown,
        "items": confirmed_items,
        "comment": request.supplier_comment or "",
    }
    occurred_at = timezone.now()
    inbox_event = record_venue_inbox_event(
        venue_id=order.venue_id,
        event_type=NotificationInboxEventType.SUPPLIER_RESPONSE,
        payload=payload,
        idempotency_key=f"supplier-response-inbox:{request.id}:{occurred_at.isoformat()}",
        object_type="supplier_request",
        object_id=request.id,
    )
    enqueue_for_venue(
        venue_id=order.venue_id,
        event=NotificationEvent.SUPPLIER_RESPONSE,
        payload=payload,
        idempotency_key=f"supplier-response:{request.id}:{occurred_at.isoformat()}",
        inbox_event=inbox_event,
    )


@transaction.atomic
def change_order_item_supplier(*, item, new_supplier_product):
    """2026-09-03 (Виктор/Павел board screenshot) — the other half of the
    "смена поставщика и частичного отказа" question left open by the
    2026-08-21 call (see accept_supplier_order_items). Only meaningful for
    an item this supplier won't (fully) deliver: declined outright, or
    accepted with a confirmed_qty short of requested_qty. Re-routes just the
    outstanding quantity by creating a new `submitted` OrderItem for the new
    supplier in the same Order — it goes through the normal accept/decline
    cycle independently — and flips the original item to `replaced`,
    leaving its own status/confirmed_qty as a historical record of what the
    original supplier actually confirmed or declined."""
    order = item.order
    if order.status == OrderStatus.AWAITING_DISPATCH:
        return None, "Order awaits approval and dispatch."
    if order.status == OrderStatus.COMPLETED:
        return None, "Order is completed, cannot change supplier for its items."

    if item.status == OrderItemStatus.CANCELLED:
        outstanding_qty = item.requested_qty
    elif (
        item.status == OrderItemStatus.CONFIRMED
        and item.confirmed_qty is not None
        and item.confirmed_qty < item.requested_qty
    ):
        outstanding_qty = item.requested_qty - item.confirmed_qty
    else:
        return None, f"Item is {item.status}, can only change supplier for a declined or partially-confirmed item."

    if item.supplier_product_id == new_supplier_product.id:
        return None, "New supplier product is the same as the current one."

    new_supplier_id = new_supplier_product.supplier_id
    new_request, _created = SupplierOrderRequest.objects.get_or_create(
        order=order,
        supplier_id=new_supplier_id,
        defaults={"number": _generate_supplier_order_request_number()},
    )
    if new_request.status != SupplierOrderRequestStatus.SUBMITTED:
        new_request.status = SupplierOrderRequestStatus.SUBMITTED
        new_request.save(update_fields=["status", "updated_at"])

    new_item = OrderItem.objects.create(
        order=order,
        supplier_product=new_supplier_product,
        supplier_request=new_request,
        requested_name=new_supplier_product.normalized_name,
        requested_qty=outstanding_qty,
        requested_unit=item.requested_unit,
        subdivision=item.subdivision or order.subdivision,
        status=OrderItemStatus.SUBMITTED,
        replaces=item,
    )
    original_supplier_id = item.supplier_product.supplier_id
    item.status = OrderItemStatus.REPLACED
    item.save(update_fields=["status", "updated_at"])
    _recompute_supplier_order_request_status(order_id=order.id, supplier_id=original_supplier_id)

    if order.status != OrderStatus.SUBMITTED:
        order.status = OrderStatus.SUBMITTED
        order.business_updated_at = timezone.now()
        order.save(update_fields=["status", "business_updated_at", "updated_at"])

    return new_item, None


@transaction.atomic
def decline_supplier_order_items(*, order, supplier_id, actor=None, comment=None):
    """Mirrors accept_supplier_order_items — declines only this supplier's
    still-pending items, leaving other suppliers' items in the same order
    untouched."""
    if order.status != OrderStatus.SUBMITTED:
        return False, f"Order is {order.status}, only submitted orders can be declined."

    items = order.items.select_for_update().filter(
        supplier_product__supplier_id=supplier_id, status=OrderItemStatus.SUBMITTED
    )
    if not items.exists():
        return False, "No pending items for this supplier in this order."

    now = timezone.now()
    items.update(status=OrderItemStatus.CANCELLED, supplier_responded_at=now, updated_at=now)
    changed_request = _recompute_supplier_order_request_status(
        order_id=order.id, supplier_id=supplier_id, supplier_comment=comment or None
    )
    _enqueue_supplier_response_notification(order=order, supplier_id=supplier_id, request=changed_request)
    if changed_request is not None:
        record_audit_log(
            actor=actor,
            action=_SUPPLIER_ORDER_REQUEST_STATUS_AUDIT_ACTION[changed_request.status],
            entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST,
            entity_id=changed_request.id,
            summary=changed_request.number,
        )
    _advance_order_status_if_fully_resolved(order)
    return True, None


@transaction.atomic
def cancel_order_item(*, item, reason=None, actor=None):
    """Raw ТЗ §20-21: venue cancels ONE still-pending item before its
    supplier has responded. Deliberately CANCELLED_BY_VENUE, not the plain
    CANCELLED a supplier decline produces (§23) — see the enum's docstring.
    Off-system-supplier items have no SupplierOrderRequest to recompute
    (§30 not touched here).

    Logs CANCELLED_BY_VENUE against the ITEM (§29 — "отмена отдельной
    позиции заведением" is explicitly item-scoped), not against whatever
    _recompute_supplier_order_request_status derives for the aggregate
    заявка — that derived status (declined/partially_confirmed) would be
    misleading logged as its own event here; §23 says not to conflate a
    supplier decline with a venue cancellation, so the recompute's return
    value is used only for its state-updating side effect, never logged
    from this path."""
    if item.order.status == OrderStatus.AWAITING_DISPATCH:
        return False, "Order awaits approval and dispatch."
    if item.status != OrderItemStatus.SUBMITTED:
        return False, f"Item is {item.status}, can only cancel a pending item."
    item.status = OrderItemStatus.CANCELLED_BY_VENUE
    item.cancellation_reason = reason
    item.save(update_fields=["status", "cancellation_reason", "updated_at"])
    record_audit_log(
        actor=actor,
        action=AuditLogAction.CANCELLED_BY_VENUE,
        entity_type=AuditLogEntityType.ORDER_ITEM,
        entity_id=item.id,
        summary=item.requested_name,
    )
    if item.supplier_request_id:
        _recompute_supplier_order_request_status(order_id=item.order_id, supplier_id=item.supplier_request.supplier_id)
        _enqueue_supplier_cancellation_notification(item.supplier_request)
    _advance_order_status_if_fully_resolved(item.order)
    return True, None


def dismiss_unresolved_order_item(*, item, actor=None):
    """2026-09-28 (ТЗ «Неразобранные», «Убрать из списка»): навсегда убирает
    позицию из «Неразобранных», ничего не меняя в самой позиции/заказе — ни
    статус, ни количества, ни ответ поставщика. Возврата нет (решение
    пользователя). Убрать можно только то, что сейчас в «Неразобранных»:
    отказ поставщика или частично принятую позицию."""
    if item.order.status == OrderStatus.AWAITING_DISPATCH:
        return False, "Order awaits approval and dispatch."
    is_unresolved = item.status == OrderItemStatus.CANCELLED or (
        item.status == OrderItemStatus.CONFIRMED
        and item.confirmed_qty is not None
        and item.confirmed_qty < item.requested_qty
    )
    if item.unresolved_dismissed_at is not None:
        return False, "Позиция уже убрана из «Неразобранных»."
    if not is_unresolved:
        return False, "Позиция не в «Неразобранных»: убрать можно только отказ или частично принятую позицию."
    item.unresolved_dismissed_at = timezone.now()
    item.unresolved_dismissed_by = actor if isinstance(actor, User) else None
    item.save(update_fields=["unresolved_dismissed_at", "unresolved_dismissed_by", "updated_at"])
    return True, None


@transaction.atomic
def cancel_supplier_order_request(*, supplier_request, reason=None, actor=None):
    """Raw ТЗ §20/§22: venue cancels ALL still-pending items of one заявка
    (one supplier's request within the order) in a single action —
    "отменить всю заявку", as opposed to cancel_order_item's single line.
    Same "log the actual action, not the recompute" reasoning as
    cancel_order_item above — see its docstring."""
    items = supplier_request.items.select_for_update().filter(status=OrderItemStatus.SUBMITTED)
    if not items.exists():
        return False, "No pending items in this supplier order request."
    items.update(status=OrderItemStatus.CANCELLED_BY_VENUE, cancellation_reason=reason, updated_at=timezone.now())
    record_audit_log(
        actor=actor,
        action=AuditLogAction.CANCELLED_BY_VENUE,
        entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST,
        entity_id=supplier_request.id,
        summary=supplier_request.number,
    )
    _recompute_supplier_order_request_status(
        order_id=supplier_request.order_id, supplier_id=supplier_request.supplier_id
    )
    _enqueue_supplier_cancellation_notification(supplier_request)
    _advance_order_status_if_fully_resolved(supplier_request.order)
    return True, None


def _enqueue_supplier_cancellation_notification(supplier_request):
    from notifications.models import NotificationEvent, NotificationInboxEventType
    from notifications.services import enqueue_for_supplier, record_venue_inbox_event

    order = supplier_request.order
    occurred_at = timezone.now()
    payload = {
        "request_id": str(supplier_request.id),
        "request_number": supplier_request.number,
        "order_number": order.order_number,
        "venue": order.venue.display_name,
        "cancelled_items": list(
            supplier_request.items.filter(status=OrderItemStatus.CANCELLED_BY_VENUE).values_list(
                "requested_name", flat=True
            )
        ),
    }
    record_venue_inbox_event(
        venue_id=order.venue_id,
        event_type=NotificationInboxEventType.SUPPLIER_REQUEST_CANCELLED,
        payload=payload,
        idempotency_key=f"supplier-request-cancelled-inbox:{supplier_request.id}:{occurred_at.isoformat()}",
        object_type="supplier_request",
        object_id=supplier_request.id,
    )
    enqueue_for_supplier(
        supplier_id=supplier_request.supplier_id,
        event=NotificationEvent.SUPPLIER_REQUEST_CANCELLED,
        payload=payload,
        idempotency_key=f"supplier-request-cancelled:{supplier_request.id}:{occurred_at.isoformat()}",
    )
    from notifications.tasks import kick_update_cancelled_supplier_messages

    transaction.on_commit(lambda: kick_update_cancelled_supplier_messages(str(supplier_request.id)))


def _advance_order_status_if_fully_resolved(order):
    """Once every item across all suppliers is out of `submitted` (each
    individually confirmed or cancelled), the order-level status can move
    past `submitted` too — until then it stays `submitted` even though this
    supplier's own items are already resolved, since other suppliers in the
    same order may still be pending. First-pass aggregate rule, mirrors the
    two order-page statuses discussed on the 2026-08-21 call (отправлено
    поставщику / подтверждено поставщиком) — this is `Order.status` (§3.2
    "Заказ заведения"), a coarser aggregate. 2026-09-15: the per-supplier
    status the old comment here said was missing now exists separately —
    see SupplierOrderRequest.status (§3.3 "Заявка поставщику"), computed
    by _recompute_supplier_order_request_status, not by this function."""
    if order.items.filter(status=OrderItemStatus.SUBMITTED).exists():
        return
    new_status = (
        OrderStatus.CONFIRMED
        if order.items.filter(status=OrderItemStatus.CONFIRMED).exists()
        else OrderStatus.CANCELLED
    )
    if order.status != new_status:
        order.status = new_status
        order.business_updated_at = timezone.now()
        order.save(update_fields=["status", "business_updated_at", "updated_at"])


@transaction.atomic
def complete_order(order, actor=None):
    """2026-08-26 sync call: "завершена" — 5th status seen on the order-history
    mockup, marked manually by the order's creator once the whole delivery is
    done (no automated receiving logic exists yet).

    2026-09-15 (raw ТЗ §25): "Завершена" is one of the 5 SupplierOrderRequest
    statuses too — cascades here rather than getting its own trigger, exactly
    as §25 asks ("использовать существующую реализацию перехода в
    Завершена", no new auto-rule). DECLINED requests are left alone — a
    supplier who never delivered anything has nothing to mark "завершена"."""
    if order.status != OrderStatus.CONFIRMED:
        return False, f"Order is {order.status}, only confirmed orders can be completed."
    order.status = OrderStatus.COMPLETED
    order.business_updated_at = timezone.now()
    order.save(update_fields=["status", "business_updated_at", "updated_at"])
    completed_requests = list(order.supplier_requests.exclude(status=SupplierOrderRequestStatus.DECLINED))
    order.supplier_requests.exclude(status=SupplierOrderRequestStatus.DECLINED).update(
        status=SupplierOrderRequestStatus.COMPLETED, updated_at=timezone.now()
    )
    for supplier_request in completed_requests:
        record_audit_log(
            actor=actor,
            action=AuditLogAction.COMPLETED,
            entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST,
            entity_id=supplier_request.id,
            summary=supplier_request.number,
        )
    return True, None


def mark_supplier_order_request_viewed(request, actor=None):
    """Raw ТЗ §13 "Просмотрено" — set only on the FIRST open of the заявка
    detail by the supplier; a later open must not touch it again (idempotent
    on purpose, not just an optimization: §13 explicitly says a repeat open
    doesn't change the flag). Message delivery/Telegram-MAX notification
    does NOT count as viewing (§13) — this is called only from the supplier
    detail GET view, never from notification-sending code."""
    if request.viewed_at is not None:
        return
    request.viewed_at = timezone.now()
    request.save(update_fields=["viewed_at", "updated_at"])
    record_audit_log(
        actor=actor,
        action=AuditLogAction.VIEWED,
        entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST,
        entity_id=request.id,
        summary=request.number,
    )


_OFF_SYSTEM_CLOSED_ITEM_STATUSES = {OrderItemStatus.CANCELLED_BY_VENUE, OrderItemStatus.REPLACED}


@transaction.atomic
def set_off_system_order_item_status(*, item, status, confirmed_qty=None, confirmed_delivery_date=None, comment=None):
    """2026-08-26 sync call: поставщики вне системы не имеют аккаунта — нет
    accept/decline, снабженец/юзер сам меняет статус позиции вручную.

    `comment` — 2026-09-25 (Виктор, задача #Лк1): необязательный комментарий,
    тот же UX, что у "Отказать в поставке?" реального поставщика
    (supplier_comment). Сохраняется независимо от status (не только при
    cancelled) — тот же принцип, что у supplier_comment реального
    поставщика, который тоже не завязан на конкретный статус вызова
    accept/decline."""
    if item.order.status == OrderStatus.AWAITING_DISPATCH:
        return False, "Order awaits approval and dispatch."
    if not item.off_system_supplier_id:
        return False, "This item is not assigned to an off-system supplier."
    # TZ "Поставщики «Не в системе»" §16: a cancelled or replaced item, or an item of a completed order, is closed
    # and must not be processed again. Moving confirmed/cancelled back to submitted stays allowed on purpose.
    if item.status in _OFF_SYSTEM_CLOSED_ITEM_STATUSES:
        return False, "Позиция отменена заведением или заменена, её статус больше нельзя менять."
    if item.order.status == OrderStatus.COMPLETED:
        return False, "Заказ завершён, статусы его позиций больше нельзя менять."
    # TZ §8: "Без даты подтверждение невозможно" — the same rule a real supplier's accept enforces.
    if status == OrderItemStatus.CONFIRMED and confirmed_delivery_date is None:
        raise ValidationError({"confirmed_delivery_date": "Укажите дату поставки, чтобы принять заявку."})
    item.status = status
    if confirmed_qty is not None:
        item.confirmed_qty = confirmed_qty
    if confirmed_delivery_date is not None:
        item.confirmed_delivery_date = confirmed_delivery_date
    if comment is not None:
        item.off_system_status_comment = comment or None
    # 2026-09-21: same field a real supplier's accept/decline sets — no
    # account here to auto-populate it from, so it's tied to this manual
    # status change instead. Back to SUBMITTED means still pending again,
    # not yet responded — null it back out.
    item.supplier_responded_at = None if status == OrderItemStatus.SUBMITTED else timezone.now()
    item.save(
        update_fields=[
            "status",
            "confirmed_qty",
            "confirmed_delivery_date",
            "off_system_status_comment",
            "supplier_responded_at",
            "updated_at",
        ]
    )
    _advance_order_status_if_fully_resolved(item.order)
    return True, None


def submit_venue_cart(
    cart, *, command_id, expected_settings_version, subdivision=None, delivery_address=None, contacts=None, actor=None
):
    from api.v1.catalog.approval import submit

    payload = {
        "command_id": command_id,
        "expected_settings_version": expected_settings_version,
        "subdivision_id": getattr(subdivision, "pk", None),
        "delivery_address_id": getattr(delivery_address, "pk", None),
        "contacts": contacts or [],
    }
    return submit(
        cart, actor, payload, subdivision=subdivision, delivery_address=delivery_address, contacts=contacts
    ), None
