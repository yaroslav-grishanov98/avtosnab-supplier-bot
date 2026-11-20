"""Public catalog reads: products, brands, categories, suppliers, countries."""

from django.db.models import Count, Exists, OuterRef, Q

from api.v1.catalog.search import apply_hybrid_product_search, parse_search_query
from catalog.models import (
    Brand,
    ProductCategory,
    VenueSupplierProduct,
)
from core.models import (
    EntityStatus,
    OrderItem,
)
from suppliers.models import PriceStatus, Supplier, SupplierProduct, SupplierProductPrice


def _sort_order(sort, has_offers_sort=False):
    if sort == "price_asc":
        return ("current_price", "normalized_name")
    if sort == "price_desc":
        return ("-current_price", "normalized_name")
    if sort == "name_asc":
        return ("normalized_name",)
    if sort == "name_desc":
        return ("-normalized_name",)
    if sort == "new":
        return ("-updated_at",)
    if sort == "with_offers":
        # products with any offer label come first
        return ("-has_any_offer", "supplier__display_name", "normalized_name", "id")
    if sort == "in_stock_first":
        return ("availability_order", "supplier__display_name", "normalized_name", "id")
    # default
    return ("supplier__display_name", "normalized_name", "id")


def catalog_products_queryset(filters, venue_id=None, user=None):
    # 2026-08-10: current_price is now a denormalized column on SupplierProduct,
    # no correlated Subquery needed — see docs/wiki/current-status.md.
    from django.db.models import Case, IntegerField, Value, When

    sort = filters.get("sort") or "default"

    queryset = SupplierProduct.objects.select_related(
        "supplier", "unit", "brand", "category", "category__parent", "category__parent__parent"
    ).filter(
        status=EntityStatus.ACTIVE,
        current_price__isnull=False,
        # 2026-07-29: раньше отсутствовало — удалённый (status=archived) или снятый с
        # публикации (is_published=False) поставщик всё равно оставался виден в каталоге,
        # его товары никуда не пропадали. Найдено при реализации self-service удаления
        # компании — без этого фильтра "удаление" не значило бы ничего для покупателя.
        supplier__status=EntityStatus.ACTIVE,
        supplier__is_published=True,
    )

    if sort == "with_offers":
        queryset = queryset.annotate(
            has_any_offer=Case(
                When(Q(is_promo=True) | Q(is_discount=True) | Q(is_new=True), then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
    if sort == "in_stock_first":
        # 2026-09-18: добавлен ProductAvailability.ON_ORDER ("Под заказ") —
        # ставим между "в наличии" и "нет в наличии", он всё же покупаем,
        # просто не сразу.
        queryset = queryset.annotate(
            availability_order=Case(
                When(availability="in_stock", then=Value(0)),
                When(availability="on_order", then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )

    if venue_id:
        fav_subq = VenueSupplierProduct.objects.filter(
            venue_id=venue_id,
            supplier_product=OuterRef("pk"),
            is_favorite=True,
            status=EntityStatus.ACTIVE,
        )
        queryset = queryset.annotate(is_favorite=Exists(fav_subq))

    q = filters.get("q")

    # 2026-10-02: «Ищем замену» из «Неразобранных» — replace_item (OrderItem, доступ проверен во view).
    replace_item = filters.get("replace_item")
    if replace_item is not None:
        source_product = replace_item.supplier_product
        if source_product is not None:
            queryset = queryset.exclude(supplier_id=source_product.supplier_id)
        replace_category_id = (source_product.category_id if source_product else None) or (
            replace_item.product.category_id if replace_item.product_id else None
        )
        if replace_category_id:
            queryset = queryset.filter(
                Q(category_id=replace_category_id)
                | Q(category__parent_id=replace_category_id)
                | Q(category__parent__parent_id=replace_category_id)
            )
        elif not q:
            q = replace_item.requested_name

    parsed_search_query = parse_search_query(q)

    # multi-select filters — within each: OR, between them: AND
    supplier_ids = filters.get("supplier_id") or []
    if supplier_ids:
        queryset = queryset.filter(supplier_id__in=supplier_ids)

    category_ids = filters.get("category_id") or []
    if category_ids:
        queryset = queryset.filter(
            Q(category_id__in=category_ids)
            | Q(category__parent_id__in=category_ids)
            | Q(category__parent__parent_id__in=category_ids)
        )

    brand_ids = filters.get("brand_id") or []
    if brand_ids:
        queryset = queryset.filter(brand_id__in=brand_ids)

    countries = filters.get("country") or []
    if countries:
        queryset = queryset.filter(country_of_origin__in=countries)

    availability = filters.get("availability") or []
    if availability:
        queryset = queryset.filter(availability__in=availability)

    offers = filters.get("offers") or []
    if offers:
        offer_q = Q()
        if "promo" in offers:
            offer_q |= Q(is_promo=True)
        if "discount" in offers:
            offer_q |= Q(is_discount=True)
        if "new" in offers:
            offer_q |= Q(is_new=True)
        queryset = queryset.filter(offer_q)

    price_min = filters.get("price_min")
    if price_min is not None:
        queryset = queryset.filter(current_price__gte=price_min)

    price_max = filters.get("price_max")
    if price_max is not None:
        queryset = queryset.filter(current_price__lte=price_max)
    elif parsed_search_query and parsed_search_query.price_max is not None:
        queryset = queryset.filter(current_price__lte=parsed_search_query.price_max)

    is_favorite = filters.get("is_favorite")
    if is_favorite and venue_id:
        queryset = queryset.filter(
            venue_supplier_products__venue_id=venue_id,
            venue_supplier_products__is_favorite=True,
            venue_supplier_products__status=EntityStatus.ACTIVE,
        ).distinct()

    wishlist_id = filters.get("wishlist_id")
    if wishlist_id and user:
        queryset = queryset.filter(
            wishlist_items__wishlist_id=wishlist_id,
            wishlist_items__wishlist__user=user,
            wishlist_items__status=EntityStatus.ACTIVE,
            wishlist_items__wishlist__status=EntityStatus.ACTIVE,
        ).distinct()

    if q:
        searched_queryset = apply_hybrid_product_search(queryset, q)
        if sort == "default":
            queryset = searched_queryset
        else:
            matching_ids = searched_queryset.order_by().values("pk")
            queryset = queryset.filter(pk__in=matching_ids).order_by(*_sort_order(sort))
    else:
        queryset = queryset.order_by(*_sort_order(sort))

    return queryset


def catalog_product_detail_queryset(*, venue_id=None):
    """Карточка товара каталога (GET /catalog/products/{id}) — та же видимость, что у
    списка (catalog_products_queryset без фильтров): активный товар с ценой у активного
    опубликованного поставщика. Плюс то, что читает только карточка: галерея, нетто, состав упаковки."""
    return (
        catalog_products_queryset({}, venue_id=venue_id)
        .select_related("net_unit", "pack_item_unit", "pack_type")
        .prefetch_related("images")
    )


def find_candidate_catalog_products(*, name, limit=None):
    """TASK-87: candidate catalog.Product matches for the supplier product-card
    autofill recommendation (api.v1.catalog.product_card_llm.suggest_product_card).
    Reuses the same hybrid lexical+vector search already serving
    SupplierCatalogSearchView (catalog_products_queryset + apply_hybrid_product_search)
    instead of a new search pipeline — the results are per-supplier offers, so this
    dedupes down to distinct catalog.Product rows (the shared entity autofill should
    avoid duplicating via SupplierProduct.product, see api/v1/catalog/embeddings.py
    ensure_product_for_supplier_product), ranked by first appearance (already ranked
    by the hybrid search)."""
    limit = limit or 10
    if not str(name or "").strip():
        return []
    queryset = catalog_products_queryset({"q": name}).select_related("product").filter(product__isnull=False)
    seen_ids = set()
    candidates = []
    for supplier_product in queryset:
        product = supplier_product.product
        if product.id in seen_ids:
            continue
        seen_ids.add(product.id)
        candidates.append(product)
        if len(candidates) >= limit:
            break
    return candidates


def catalog_brands_queryset():
    """Active brands that have at least one active supplier product with a price."""
    current_price = SupplierProductPrice.objects.filter(
        supplier_product=OuterRef("pk"),
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    )
    active_products = (
        SupplierProduct.objects.filter(
            brand=OuterRef("pk"),
            status=EntityStatus.ACTIVE,
        )
        .annotate(has_price=Exists(current_price))
        .filter(has_price=True)
    )

    return (
        Brand.objects.filter(status=EntityStatus.ACTIVE)
        .annotate(
            product_count=Count(
                "supplier_products",
                filter=Q(supplier_products__status=EntityStatus.ACTIVE),
            )
        )
        .filter(Exists(active_products))
        .order_by("canonical_name")
    )


def catalog_categories_queryset():
    """Active leaf categories (and their parents) that have active products."""
    current_price = SupplierProductPrice.objects.filter(
        supplier_product=OuterRef("pk"),
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    )
    active_product_ids = (
        SupplierProduct.objects.filter(
            category=OuterRef("pk"),
            status=EntityStatus.ACTIVE,
        )
        .annotate(has_price=Exists(current_price))
        .filter(has_price=True)
    )

    return (
        ProductCategory.objects.select_related("parent", "parent__parent")
        .filter(status=EntityStatus.ACTIVE)
        .filter(Exists(active_product_ids))
        .order_by("parent__parent__name", "parent__name", "name")
    )


def catalog_suppliers_queryset(q=None):
    """Active suppliers that have at least one active product with a price.
    Optional q searches display_name, legal_name, and inn."""
    current_price = SupplierProductPrice.objects.filter(
        supplier_product=OuterRef("pk"),
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    )
    active_products = (
        SupplierProduct.objects.filter(
            supplier=OuterRef("pk"),
            status=EntityStatus.ACTIVE,
        )
        .annotate(has_price=Exists(current_price))
        .filter(has_price=True)
    )

    queryset = (
        Supplier.objects.filter(status=EntityStatus.ACTIVE, is_published=True)
        .filter(Exists(active_products))
        .order_by("display_name")
    )

    if q:
        queryset = queryset.filter(Q(display_name__icontains=q) | Q(legal_name__icontains=q) | Q(inn__icontains=q))

    return queryset


def catalog_supplier_detail_queryset():
    """2026-09-15: карточка поставщика по клику на название в истории
    заказов заведения — не фильтруется по status/is_published (в отличие
    от catalog_suppliers_queryset выше), заказ мог быть оформлен у уже
    снятого с публикации/деактивированного поставщика."""
    return Supplier.objects.prefetch_related("addresses", "contacts", "schedule_rules")


def catalog_countries_queryset():
    """Distinct non-null countries from active products with active prices."""
    current_price = SupplierProductPrice.objects.filter(
        supplier_product=OuterRef("pk"),
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    )
    return (
        SupplierProduct.objects.filter(
            status=EntityStatus.ACTIVE,
            country_of_origin__isnull=False,
        )
        .annotate(has_price=Exists(current_price))
        .filter(has_price=True)
        .exclude(country_of_origin="")
        .values_list("country_of_origin", flat=True)
        .distinct()
        .order_by("country_of_origin")
    )


def catalog_replace_order_item(*, user_id, item_id):
    """2026-10-02: позиция заказа для «Ищем замену» в каталоге (GET /catalog/products?replace_item_id=…) —
    только из заведений, где пользователь активный участник; иначе None (view отдаёт 404)."""
    return (
        OrderItem.objects.filter(
            pk=item_id,
            order__venue__venue_users__user_id=user_id,
            order__venue__venue_users__status=EntityStatus.ACTIVE,
        )
        .select_related("supplier_product", "product")
        .first()
    )


def catalog_visible_supplier_products():
    """Тот же фильтр видимости, что у каталога: выбрать можно только то, что пользователь видел."""
    return SupplierProduct.objects.filter(
        status=EntityStatus.ACTIVE,
        current_price__isnull=False,
        supplier__status=EntityStatus.ACTIVE,
        supplier__is_published=True,
    )
