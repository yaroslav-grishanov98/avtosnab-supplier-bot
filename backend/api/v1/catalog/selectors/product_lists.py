"""Venue product list reads."""

from django.db.models import Count, Q

from catalog.models import (
    VenueProductList,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
)


def venue_product_lists_queryset(venue_id):
    return (
        VenueProductList.objects.filter(venue_id=venue_id, status=EntityStatus.ACTIVE)
        .annotate(items_count=Count("items", filter=Q(items__status=EntityStatus.ACTIVE)))
        .order_by("name", "id")
    )


def venue_product_list_items_queryset(product_list):
    # 2026-09-15: select_related("master_product") — same reasoning as
    # venue_product_matrix_queryset above, this is a list endpoint too.
    # 2026-09-17: select_related("subdivision") — same reasoning too.
    return (
        VenueProductMatrixItem.objects.filter(lists=product_list, status=EntityStatus.ACTIVE)
        .select_related(
            "supplier_product",
            "supplier_product__supplier",
            "supplier_product__unit",
            "master_product",
            "subdivision",
            "supplier_match",
        )
        .prefetch_related("lists")
        .order_by("requested_name", "id")
    )
