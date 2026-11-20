from django.db.models import Count, F, Q

from core.models import AuditLog, AuditLogEntityType, EntityStatus
from suppliers.models import (
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    UploadItemStatus,
)

PRICE_CHANGE_INCREASED = "increased"
PRICE_CHANGE_DECREASED = "decreased"
PRICE_CHANGE_UNCHANGED = "unchanged"
PRICE_CHANGE_CHOICES = (PRICE_CHANGE_INCREASED, PRICE_CHANGE_DECREASED, PRICE_CHANGE_UNCHANGED)

# 2026-08-09: "у товаров компании сейчас нет сортировок — необходимо сделать по всем полям".
# Keys match SupplierProductListItemSerializer's response fields 1:1 (sort by whatever column
# you see) — values are the actual order_by paths, reusing the annotations already computed
# below (current_price/previous_price/last_upload_uploaded_at) instead of re-deriving them, so
# sorting never adds an extra query or a second Subquery. IDs (id/supplier_id/product_id/
# category_id) are excluded — a UUID has no meaningful sort order for a person reading a table.
PRODUCT_ORDERING_FIELDS = {
    "supplier_name": "supplier__display_name",
    "supplier_raw_name": "supplier_name",
    "supplier_normalized_name": "normalized_name",
    "product_name": "product__canonical_name",
    "current_price": "current_price",
    "previous_price": "previous_price",
    "unit": "unit__code",
    "country_of_origin": "country_of_origin",
    "net_value": "net_value",
    "net_unit": "net_unit__code",
    "package": "package",
    "min_order_qty": "min_order_qty",
    "delivery_multiple": "delivery_multiple",
    "brand": "brand__canonical_name",
    "category_name": "category__name",
    "category_group_name": "category__parent__parent__name",
    "category_subgroup_name": "category__parent__name",
    "availability": "availability",
    "comment": "comment",
    "status": "status",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "uploaded_at": "last_upload_uploaded_at",
}


def supplier_products_queryset(filters):
    # 2026-08-10: current_price/previous_price/price_changed_at used to be
    # correlated Subquery annotations, recomputed from SupplierProductPrice on
    # every request. Sorting by them forced Postgres to evaluate the subquery
    # for every row before it could sort (no index can back that) — measurably
    # slow (see docs/wiki/current-status.md, 2026-08-10 lag report). They are
    # now plain denormalized columns on SupplierProduct, kept in sync by
    # _sync_product_price_snapshot() (api/v1/suppliers/services.py) whenever a
    # price row's status/valid_to changes — ordinary indexed columns here.
    #
    # 2026-08-14: "last_upload" deliberately NOT in select_related — SupplierPriceUpload
    # carries raw_payload/ai_payload (JSONField, the entire parsed price list, seen up to
    # ~150KB combined for one upload in prod). select_related() duplicates the FULL related
    # row per SupplierProduct row; since one upload backs many products, a 30-row page could
    # transfer ~4.4MB of payload nobody asked for — measured as the actual root cause of
    # GET /suppliers/products taking 1-1.7s (proven via a real SELECT, not EXPLAIN ANALYZE,
    # which never transmits row data and stayed fast throughout — see
    # docs/wiki/logs/2026-08-14-suppliers-products-slow-query-root-cause.md). Only
    # last_upload_uploaded_at (a single scalar, via F()) is ever read by the serializer.
    queryset = (
        SupplierProduct.objects.select_related(
            "supplier",
            "product",
            "unit",
            "brand",
            "category__parent__parent",
            "net_unit",
            "pack_item_unit",
            "pack_type",
        )
        # 2026-08-14: trims select_related() down to the columns
        # SupplierProductListItemSerializer actually reads — select_related()
        # alone still pulls every column of every joined table (e.g. suppliers
        # has ~30 columns including metadata/delivery_terms/pickup_address),
        # duplicated per SupplierProduct row. Not the ~150KB-per-row scale of
        # the last_upload bug above, but products.enriched_text is a real
        # future risk once product-catalog matching (product_id) is populated
        # (currently 0/3839 rows — dormant, not yet a measured cost).
        .only(
            "id",
            "created_at",
            "updated_at",
            "supplier_id",
            "product_id",
            "system_sku",
            "supplier_sku",
            "supplier_name",
            "normalized_name",
            "image_url",
            "country_of_origin",
            "availability",
            "package",
            "net_value",
            "min_order_qty",
            "delivery_multiple",
            "pack_items_count",
            "pack_item_value",
            "comment",
            "short_description",
            "status",
            "current_price",
            "previous_price",
            "supplier__display_name",
            "product__canonical_name",
            "unit__code",
            "brand__canonical_name",
            "category__name",
            "category__parent_id",
            "category__parent__name",
            "category__parent__parent_id",
            "category__parent__parent__name",
            "net_unit__code",
            "pack_item_unit__code",
            "pack_type__code",
        )
        # Task 5.2, §4 ТЗ: SupplierProductListItemSerializer.get_images reads
        # obj.images.all() on every row — reverse FK (one-to-many), so a plain
        # select_related() can't cover it (that's for forward FK/O2O only);
        # without this prefetch it's an N+1 query, one extra SELECT per product
        # row on the list. See SupplierProductGalleryAPITests
        # .test_product_list_query_count_does_not_grow_with_gallery_images
        # (api/v1/suppliers/tests.py) — same bug class that hit Task 1.
        .prefetch_related("images")
        .annotate(last_upload_uploaded_at=F("last_upload__uploaded_at"))
        .order_by("-last_upload_uploaded_at", "-updated_at", "id")
    )

    supplier_id = filters.get("supplier_id")
    if supplier_id:
        queryset = queryset.filter(supplier_id=supplier_id)

    status = filters.get("status")
    if status:
        queryset = queryset.filter(status=status)

    query = filters.get("q")
    if query:
        queryset = queryset.filter(Q(supplier_name__icontains=query) | Q(normalized_name__icontains=query))

    updated_since = filters.get("updated_since")
    if updated_since:
        queryset = queryset.filter(updated_at__gte=updated_since)

    availability = filters.get("availability")
    if availability:
        queryset = queryset.filter(availability=availability)

    brand_id = filters.get("brand_id")
    if brand_id:
        queryset = queryset.filter(brand_id=brand_id)

    category_id = filters.get("category_id")
    if category_id:
        queryset = queryset.filter(category_id=category_id)

    price_min = filters.get("price_min")
    if price_min is not None:
        queryset = queryset.filter(current_price__gte=price_min)

    price_max = filters.get("price_max")
    if price_max is not None:
        queryset = queryset.filter(current_price__lte=price_max)

    price_change = filters.get("price_change")
    if price_change == PRICE_CHANGE_INCREASED:
        queryset = queryset.filter(previous_price__isnull=False, current_price__gt=F("previous_price"))
    elif price_change == PRICE_CHANGE_DECREASED:
        queryset = queryset.filter(previous_price__isnull=False, current_price__lt=F("previous_price"))
    elif price_change == PRICE_CHANGE_UNCHANGED:
        queryset = queryset.filter(previous_price__isnull=False, current_price=F("previous_price"))

    ordering = filters.get("ordering")
    if ordering:
        descending = ordering.startswith("-")
        field = ordering[1:] if descending else ordering
        order_path = PRODUCT_ORDERING_FIELDS[field]
        queryset = queryset.order_by(f"-{order_path}" if descending else order_path, "id")

    return queryset


def supplier_price_upload_detail_queryset():
    return SupplierPriceUpload.objects.select_related("supplier").annotate(
        items_total=Count("items"),
        items_new=Count("items", filter=Q(items__status=UploadItemStatus.NEW)),
        items_matched=Count("items", filter=Q(items__status=UploadItemStatus.MATCHED)),
        items_updated=Count("items", filter=Q(items__status=UploadItemStatus.UPDATED)),
        items_rejected=Count("items", filter=Q(items__status=UploadItemStatus.REJECTED)),
        items_needs_review=Count("items", filter=Q(items__status=UploadItemStatus.NEEDS_REVIEW)),
    )


def supplier_price_upload_items_queryset(*, upload_id, filters):
    queryset = (
        SupplierPriceUploadItem.objects.select_related("parsed_unit", "supplier_product")
        .filter(upload_id=upload_id)
        .order_by("row_number", "created_at", "id")
    )

    status = filters.get("status")
    if status:
        queryset = queryset.filter(status=status)

    return queryset


def supplier_brands_queryset(*, q=None):
    """2026-08-11: созвон 2026-08-10 — Витя, поле brand при добавлении товара было свободным
    текстом, нужна подсказка "по уже введённым/утверждённым на платформе вариантам" (explicit —
    не векторный поиск, обычный автокомплит), чтобы снизить дубли с опечатками. Brand уже
    существует и наполняется через _resolve_brand()'s get_or_create при каждом создании/импорте
    товара — тут только поиск по уже накопленным записям, ничего нового не создаёт.

    2026-08-29: без q список сортировался по алфавиту и обрезался до 50 — при
    130+ брендах в системе только что созданный бренд (get_or_create при
    сохранении товара) физически не попадал в эти первые 50, если его
    название не начиналось с одной из первых букв алфавита. Выглядело как
    "бренд не добавляется", хотя он реально создавался и находился поиском
    по q. Без q теперь сортировка по created_at (новые сверху) — свежесозданный
    бренд гарантированно виден сразу, даже пока список ещё не отфильтрован
    поиском."""
    from catalog.models import Brand

    queryset = Brand.objects.all()
    if q:
        queryset = queryset.filter(canonical_name__icontains=q.strip()).order_by("canonical_name")
    else:
        queryset = queryset.order_by("-created_at")
    return queryset[:50]


def supplier_categories_queryset():
    """2026-08-11: созвон 2026-08-10 — Витя, категория (группа/подгруппа/вид) была зависимым
    списком без источника данных: "мне этот список неоткуда взять". ProductCategory уже
    существует как self-referential дерево (наполняется через _resolve_category_path()'s
    get_or_create), просто ничего не отдавало его наружу для формы поставщика — CatalogCategory*
    отдаёт только листья, у которых уже есть товары с ценой (фильтр каталога покупателя), это не
    подходит для формы добавления товара (нужно видеть категорию до первого товара в ней)."""
    from catalog.models import ProductCategory

    return ProductCategory.objects.filter(status=EntityStatus.ACTIVE).select_related("parent__parent").order_by("name")


def annotate_category_depth(categories):
    """Depth: 0 for a group (no parent), 1 for a subgroup, 2 for a view — computed in Python from
    the select_related('parent__parent') chain already fetched by supplier_categories_queryset(),
    no extra queries for the (expected 3-level) domain depth this project uses."""
    result = []
    for category in categories:
        depth = 0
        node = category.parent
        while node is not None:
            depth += 1
            node = node.parent
        category.depth = depth
        result.append(category)
    return result


def supplier_product_audit_log(supplier_product_id):
    return list(
        AuditLog.objects.filter(
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT, entity_id=supplier_product_id
        ).order_by("-created_at")
    )
