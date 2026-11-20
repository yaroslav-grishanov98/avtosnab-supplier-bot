"""Helpers shared by several catalog view modules."""

from drf_spectacular.utils import (
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework.pagination import PageNumberPagination

from api.v1.catalog.selectors import (
    catalog_products_queryset,
)
from api.v1.catalog.serializers import (
    CatalogProductSerializer,
)
from catalog.models import (
    WishlistItem,
)
from core.models import (
    EntityStatus,
)
from file_imports.services import ImportFileUnavailable, import_file_storage_enabled, store_import_file

from .import_common import ImportFileStorageUnavailableError

_PRODUCT_PAGE_SCHEMA = inline_serializer(
    name="CatalogProductPage",
    fields={
        "count": s.IntegerField(help_text="Всего товаров по фильтру"),
        "limit": s.IntegerField(),
        "offset": s.IntegerField(),
        "results": CatalogProductSerializer(many=True),
    },
)


def _catalog_products_page(filters, *, user, request=None):
    """Shared by CatalogProductListView and CatalogPhotoSearchView so both
    apply the exact same filtering/pagination/wishlist-hydration logic."""
    venue_id = filters.get("venue_id")
    limit = filters["limit"]
    offset = filters["offset"]

    queryset = catalog_products_queryset(filters, venue_id=venue_id, user=user)
    total = queryset.count()
    page = list(queryset[offset : offset + limit])

    wishlist_map = {}
    if user is not None:
        rows = WishlistItem.objects.filter(
            supplier_product_id__in=[p.id for p in page],
            wishlist__user=user,
            status=EntityStatus.ACTIVE,
            wishlist__status=EntityStatus.ACTIVE,
        ).values("supplier_product_id", "wishlist_id", "wishlist__name")
        for row in rows:
            wishlist_map.setdefault(row["supplier_product_id"], []).append(
                {"id": row["wishlist_id"], "name": row["wishlist__name"]}
            )

    return {
        "count": total,
        "limit": limit,
        "offset": offset,
        "results": CatalogProductSerializer(
            page, many=True, context={"wishlist_map": wishlist_map, "request": request}
        ).data,
    }


def _store_import_file_if_enabled(*, uploaded_file, scope_type, scope_id):
    if not import_file_storage_enabled():
        return None
    try:
        return store_import_file(uploaded_file=uploaded_file, scope_type=scope_type, scope_id=scope_id)
    except ImportFileUnavailable as error:
        raise ImportFileStorageUnavailableError() from error


class _MasterProductPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
