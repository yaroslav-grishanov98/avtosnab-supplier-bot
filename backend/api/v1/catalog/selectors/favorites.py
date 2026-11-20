"""Venue favorites and wishlists reads."""

from django.db.models import Count, Q

from catalog.models import (
    VenueSupplierProduct,
    Wishlist,
    WishlistItem,
)
from core.models import (
    EntityStatus,
)


def venue_favorites_queryset(venue_id):
    return (
        VenueSupplierProduct.objects.select_related(
            "supplier_product",
            "supplier_product__supplier",
            "supplier_product__unit",
            "supplier_product__brand",
            "supplier_product__category",
        )
        .filter(venue_id=venue_id, status=EntityStatus.ACTIVE)
        .order_by("role", "supplier_product__normalized_name", "id")
    )


def user_wishlists_queryset(user):
    return (
        Wishlist.objects.filter(user=user, status=EntityStatus.ACTIVE)
        .annotate(items_count=Count("items", filter=Q(items__status=EntityStatus.ACTIVE)))
        .order_by("name", "id")
    )


def wishlist_items_queryset(wishlist):
    return (
        WishlistItem.objects.select_related(
            "supplier_product",
            "supplier_product__supplier",
            "supplier_product__unit",
        )
        .filter(wishlist=wishlist, status=EntityStatus.ACTIVE)
        .order_by("supplier_product__normalized_name", "id")
    )
