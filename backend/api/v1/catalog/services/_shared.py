"""Helpers shared by several catalog service modules."""

from rest_framework.exceptions import ValidationError

from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    OrderItem,
    SupplierOrderRequest,
)

from .orders import _generate_supplier_order_request_number


def _enqueue_order_notifications(*, order, order_items, frozen=None):
    from decimal import Decimal

    from notifications.models import NotificationEvent, NotificationInboxEventType
    from notifications.services import enqueue_for_supplier, enqueue_for_venue, record_venue_inbox_event

    frozen_items = {x["id"]: x for x in (frozen or {}).get("items", [])}
    requests = list(order.supplier_requests.select_related("supplier").prefetch_related("items__requested_unit"))
    requests_by_id = {supplier_request.id: supplier_request for supplier_request in requests}
    suppliers = {}
    for item in order_items:
        if item.supplier_request_id:
            supplier_name = (
                frozen_items.get(str(item.pk), {}).get("supplier_name")
                or requests_by_id[item.supplier_request_id].supplier.display_name
            )
        elif item.off_system_supplier_id:
            supplier_name = (
                frozen_items.get(str(item.pk), {}).get("supplier_name") or item.off_system_supplier.display_name
            )
        else:
            supplier_name = "Без поставщика"
        suppliers.setdefault(supplier_name, []).append(
            {
                "name": item.requested_name,
                "quantity": str(item.requested_qty),
                "unit": (frozen or {})
                .get("_notification_unit_names", {})
                .get(str(item.pk), item.requested_unit.name if item.requested_unit_id else ""),
            }
        )
    payload = {
        "order_number": order.order_number,
        "venue": order.venue.display_name,
        "requested_delivery_date": order.requested_delivery_date.isoformat() if order.requested_delivery_date else None,
        "suppliers": suppliers,
    }
    inbox_event = record_venue_inbox_event(
        venue_id=order.venue_id,
        event_type=NotificationInboxEventType.ORDER_SUBMITTED,
        payload=payload,
        idempotency_key=f"order-submitted-inbox:{order.id}",
        object_type="order",
        object_id=order.id,
    )
    enqueue_for_venue(
        venue_id=order.venue_id,
        event=NotificationEvent.ORDER_SUBMITTED,
        payload=payload,
        idempotency_key=f"order-submitted:{order.id}",
        inbox_event=inbox_event,
    )
    for supplier_request in requests:
        request_items = list(
            supplier_request.items.select_related("requested_unit", "subdivision").order_by("created_at", "id")
        )
        address = order.delivery_address
        address_parts = []
        if address:
            address_parts = [
                address.locality,
                address.street,
                address.apartment_office,
                address.address,
            ]
        if frozen is not None:
            saved_address = frozen.get("delivery_address") or {}
            address_parts = [saved_address.get(key) for key in ("locality", "street", "apartment_office", "address")]
        delivery_address = ", ".join(dict.fromkeys(part.strip() for part in address_parts if part and part.strip()))
        request_total = sum(
            ((item.applied_price or Decimal("0")) * item.requested_qty for item in request_items), Decimal("0")
        )
        has_complete_prices = all(item.applied_price is not None for item in request_items)
        enqueue_for_supplier(
            supplier_id=supplier_request.supplier_id,
            event=NotificationEvent.SUPPLIER_NEW_REQUEST,
            payload={
                "request_id": str(supplier_request.id),
                "order_id": str(order.id),
                "request_number": supplier_request.number,
                "order_number": order.order_number,
                "venue": order.venue.display_name,
                "delivery_address": delivery_address,
                "total_amount": str(request_total) if has_complete_prices else "",
                "minimum_order_amount": (
                    str(supplier_request.supplier.delivery_min_order_amount)
                    if supplier_request.supplier.delivery_min_order_amount is not None
                    else ""
                ),
                "currency": supplier_request.supplier.delivery_currency or "RUB",
                "buyer_comment": order.comment or "",
                "supplier_id": str(supplier_request.supplier_id),
                "items": [
                    {
                        "name": item.requested_name,
                        "quantity": str(item.requested_qty),
                        "unit": (frozen or {})
                        .get("_notification_unit_names", {})
                        .get(str(item.pk), item.requested_unit.name if item.requested_unit_id else ""),
                        "subdivision": frozen_items.get(str(item.pk), {}).get("subdivision_name")
                        or (frozen or {}).get("subdivision_name")
                        or (
                            item.subdivision.name
                            if item.subdivision_id
                            else order.subdivision.name
                            if order.subdivision_id
                            else "Без подразделения"
                        ),
                        "item_total": str((item.applied_price or Decimal("0")) * item.requested_qty),
                    }
                    for item in request_items
                ],
            },
            idempotency_key=f"supplier-new-request:{supplier_request.id}",
        )


def _create_supplier_order_requests(*, order, order_items, actor=None, frozen=None):
    """Raw ТЗ §3.3/§7: one SupplierOrderRequest per distinct supplier
    represented in this Order's items — off-system-supplier/free-text items
    (no supplier_product) are deliberately excluded, see SupplierOrderRequest
    docstring (§30 not yet defined by the doc, existing OffSystemSupplier
    flow untouched)."""
    frozen_items = {x["id"]: x for x in (frozen or {}).get("items", [])}
    items_by_supplier = {}
    for item in order_items:
        saved = frozen_items.get(str(item.pk), {})
        supplier_id = (
            saved.get("supplier_id")
            if frozen is not None
            else (item.supplier_product.supplier_id if item.supplier_product_id else None)
        )
        if supplier_id is None or saved.get("off_system_supplier_id"):
            continue
        items_by_supplier.setdefault(supplier_id, []).append(item)

    for supplier_id, items in items_by_supplier.items():
        request = SupplierOrderRequest.objects.create(
            order=order,
            supplier_id=supplier_id,
            number=_generate_supplier_order_request_number(),
        )
        for item in items:
            item.supplier_request = request
        OrderItem.objects.bulk_update(items, ["supplier_request"])
        record_audit_log(
            actor=actor,
            action=AuditLogAction.SUBMITTED,
            entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST,
            entity_id=request.id,
            summary=request.number,
        )


def _name_taken_error(field="name"):
    return ValidationError({field: "Запись с таким названием уже есть."})


def _replace_audit_summary(*, old_name, old_unit, new_name, new_unit):
    return f"«{old_name}» ({old_unit or '—'}) → «{new_name}» ({new_unit or '—'})"


def _decisions_error(message):
    """Pass-1 error of an import apply: `decisions` is always a list in the
    400 body (ImportDecisionsErrorSerializer), never a bare string."""
    return ValidationError({"decisions": [message]})


def _target_conflict_reason(decision, existing, *, raw_name):
    """2026-09-29 (unified-import-architecture spec §9.1/§9.2): optimistic lock
    как per-row конфликт вместо whole-batch-abort (было TASK-76's
    _raise_if_target_changed). expected_updated_at по-прежнему опционален --
    декларация без него (сегодняшний фронт, все существующие тесты) ведёт
    себя как раньше. При наличии и несовпадении -- не raise, а возвращает
    причину конфликта; вызывающий помечает decision конфликтующей и не
    применяет её, остальная пачка продолжает обрабатываться."""
    expected = decision.get("expected_updated_at")
    if expected is not None and existing.updated_at != expected:
        return f'"{raw_name}": товар изменился после проверки. Проверьте его ещё раз.'
    return None


# Sentinel distinguishing "the caller never mentioned supplier_product_id"
# (leave the current value alone) from "the caller explicitly named one".
# A plain None default can't tell those apart, and clearing supplier_product
# back to null is deliberately out of scope for the batch endpoint (TASK-52).
_UNSET = object()
