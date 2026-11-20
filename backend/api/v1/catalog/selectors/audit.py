"""Audit log read models."""

from django.db.models import Q

from core.models import (
    AuditLog,
    AuditLogEntityType,
    OrderItem,
)


def product_matrix_item_audit_log(item_id):
    return list(
        AuditLog.objects.filter(entity_type=AuditLogEntityType.VENUE_PRODUCT_MATRIX_ITEM, entity_id=item_id).order_by(
            "-created_at"
        )
    )


def supplier_product_venue_price_audit_log(price_id):
    return list(
        AuditLog.objects.filter(
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT_VENUE_PRICE, entity_id=price_id
        ).order_by("-created_at")
    )


def master_product_audit_log(master_product_id):
    return list(
        AuditLog.objects.filter(entity_type=AuditLogEntityType.MASTER_PRODUCT, entity_id=master_product_id).order_by(
            "-created_at"
        )
    )


def supplier_order_request_audit_log(supplier_request_id):
    """Raw ТЗ §29 "История" — merges events logged against the заявка
    itself (submitted/viewed/confirmed/partially_confirmed/declined/
    completed/cancelled_by_venue-for-the-whole-request) with events logged
    against its individual items (cancelled_by_venue for one line, §21) —
    a single combined timeline, as §29 implies ("для заявки И позиции")."""
    item_ids = list(OrderItem.objects.filter(supplier_request_id=supplier_request_id).values_list("id", flat=True))
    return list(
        AuditLog.objects.filter(
            Q(entity_type=AuditLogEntityType.SUPPLIER_ORDER_REQUEST, entity_id=supplier_request_id)
            | Q(entity_type=AuditLogEntityType.ORDER_ITEM, entity_id__in=item_ids)
        ).order_by("-created_at")
    )
