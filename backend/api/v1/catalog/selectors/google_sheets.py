"""Google Sheets export reads."""

from django.db.models import OuterRef, Subquery
from django.db.models.functions import Greatest

from core.models import (
    EntityStatus,
)
from suppliers.models import PriceStatus, SupplierPriceUploadItem, SupplierProduct, SupplierProductPrice


def google_sheet_export_queryset(filters):
    # 2026-08-10: current_price is now a denormalized column on SupplierProduct
    # (see docs/wiki/current-status.md), so it's no longer annotated here — only
    # current_currency/current_price_updated_at/current_price_valid_from still
    # need the correlated subquery, since those weren't denormalized.
    current_price_row = SupplierProductPrice.objects.filter(
        supplier_product=OuterRef("pk"),
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    ).order_by("-valid_from", "-created_at")
    latest_upload_item = SupplierPriceUploadItem.objects.filter(
        supplier_product=OuterRef("pk"),
    ).order_by("-created_at", "-id")

    queryset = (
        SupplierProduct.objects.select_related(
            "supplier",
            "product",
            "product__brand",
            "product__category",
            "unit",
            "last_upload",
        )
        .annotate(
            current_currency=Subquery(current_price_row.values("currency")[:1]),
            current_price_updated_at=Subquery(current_price_row.values("updated_at")[:1]),
            current_price_valid_from=Subquery(current_price_row.values("valid_from")[:1]),
            latest_upload_item_payload=Subquery(latest_upload_item.values("raw_payload")[:1]),
            latest_upload_item_status=Subquery(latest_upload_item.values("status")[:1]),
            export_updated_at=Greatest("updated_at", "current_price_updated_at"),
        )
        .filter(status=EntityStatus.ACTIVE, current_price__isnull=False)
        .order_by("supplier__display_name", "normalized_name", "id")
    )

    supplier_id = filters.get("supplier_id")
    if supplier_id:
        queryset = queryset.filter(supplier_id=supplier_id)

    updated_since = filters.get("updated_since")
    if updated_since:
        queryset = queryset.filter(export_updated_at__gte=updated_since)

    return queryset
