"""Venue product matrix reads."""

from django.db.models import Prefetch, Q
from django.utils import timezone

from api.v1.catalog.search import apply_directory_product_search
from catalog.models import (
    SupplierProductVenuePrice,
    SupplierProductVenuePriceStatus,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
)


def venue_product_matrix_queryset(venue_id, *, filter_value="all", q=None, subdivision_id=None):
    # TASK-44 / §35 raw ТЗ: the active individual (per-venue) price must win
    # over SupplierProduct.current_price here, same rule as
    # resolve_supplier_product_price — prefetched per venue (one extra
    # query for the whole page) rather than resolved per row to avoid N+1.
    # 2026-09-15: select_related("master_product") for
    # VenueProductMatrixItemSerializer.master_product_name — was fetching
    # master_product_id only, no display name.
    # 2026-09-17: select_related("subdivision") — по прямой просьбе
    # пользователя, тем же паттерном (subdivision_id/subdivision_name на
    # ответе, фильтр по subdivision_id). venue_id уже обязателен через URL
    # (venues/{venue_id}/product-matrix) — отдельный query-фильтр по нему
    # не добавлялся, был бы избыточен.
    now = timezone.now()
    venue_prices = (
        SupplierProductVenuePrice.objects.filter(
            venue_id=venue_id,
            status=SupplierProductVenuePriceStatus.ACTIVE,
            valid_from__lte=now,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .order_by("-valid_from")
    )
    queryset = (
        VenueProductMatrixItem.objects.filter(venue_id=venue_id, status=EntityStatus.ACTIVE)
        .select_related(
            "supplier_product",
            "supplier_product__supplier",
            "supplier_product__unit",
            "master_product",
            "subdivision",
            "supplier_match",
        )
        .prefetch_related(
            "lists",
            Prefetch("supplier_product__venue_prices", queryset=venue_prices, to_attr="_matrix_venue_prices"),
        )
    )
    if filter_value == "needs_review":
        queryset = queryset.filter(needs_review=True)
    elif filter_value == "no_master_product":
        queryset = queryset.filter(master_product__isnull=True)
    if q:
        queryset = apply_directory_product_search(
            queryset,
            q,
            fields=(
                "requested_name",
                "requested_unit_label",
                "note",
                "master_product__canonical_name",
                "supplier_product__supplier_name",
                "supplier_product__normalized_name",
                "supplier_product__supplier_sku",
                "supplier_product__system_sku",
            ),
            name_field="requested_name",
        )
    if subdivision_id:
        queryset = queryset.filter(subdivision_id=subdivision_id)
    return queryset if q else queryset.order_by("requested_name", "id")
