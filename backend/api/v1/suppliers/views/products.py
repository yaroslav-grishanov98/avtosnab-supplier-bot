"""Supplier products: list, card, prices, images, archive and bulk actions."""

import hashlib
import time
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import (
    OptionalSupplierCabinetJWTAuthentication,
)
from api.v1.catalog.search import explain_supplier_product_match, keyboard_layout_variant, parse_search_query
from api.v1.catalog.selectors import (
    catalog_products_queryset,
    individual_price_order_history_queryset,
    supplier_product_individual_prices_queryset,
    venue_search_queryset,
)
from api.v1.catalog.serializers import (
    CatalogProductQuerySerializer,
    CatalogProductSerializer,
    SupplierProductIndividualPriceCreateSerializer,
    SupplierProductIndividualPriceQuerySerializer,
    SupplierProductVenuePriceConflictSerializer,
    SupplierProductVenuePriceOrderHistoryItemSerializer,
    SupplierProductVenuePriceOrderHistoryQuerySerializer,
    SupplierProductVenuePriceSerializer,
    SupplierProductVenuePriceUpdateSerializer,
    VenueSearchQuerySerializer,
    VenueSearchResultSerializer,
)
from api.v1.catalog.services import (
    SupplierProductVenuePriceConflict,
    archive_supplier_product_venue_price,
    delete_future_supplier_product_venue_price,
    set_supplier_product_venue_price,
    update_supplier_product_venue_price,
)
from api.v1.permissions import (
    IsSupplierCabinetActiveMember,
    IsSupplierCabinetOrHasServiceToken,
)
from api.v1.suppliers.selectors import (
    supplier_product_audit_log,
    supplier_products_queryset,
)
from api.v1.suppliers.serializers import (
    AuditLogEntrySerializer,
    SupplierCatalogSearchEventSerializer,
    SupplierProductArchiveAllResponseSerializer,
    SupplierProductArchiveAllSerializer,
    SupplierProductAutofillRequestSerializer,
    SupplierProductAutofillResponseSerializer,
    SupplierProductBulkDeleteResponseSerializer,
    SupplierProductBulkIdsSerializer,
    SupplierProductBulkUpdateAllResponseSerializer,
    SupplierProductBulkUpdateAllSerializer,
    SupplierProductBulkUpdateSerializer,
    SupplierProductCreateSerializer,
    SupplierProductDeleteAllResponseSerializer,
    SupplierProductImageReorderSerializer,
    SupplierProductImageSerializer,
    SupplierProductImageUploadSerializer,
    SupplierProductListItemSerializer,
    SupplierProductListQuerySerializer,
    SupplierProductPriceHistoryItemSerializer,
    SupplierProductRestoreAllResponseSerializer,
    SupplierProductRestoreAllSerializer,
    SupplierProductUpdateSerializer,
)
from api.v1.suppliers.services import (
    archive_all_supplier_products,
    archive_supplier_product,
    bulk_archive_supplier_products,
    bulk_delete_supplier_products,
    bulk_publish_supplier_products,
    bulk_restore_supplier_products,
    bulk_update_supplier_products,
    create_supplier_product,
    delete_all_supplier_products,
    delete_supplier_product,
    delete_supplier_product_image,
    list_supplier_product_price_history,
    publish_supplier_product,
    reorder_supplier_product_image,
    restore_all_supplier_products,
    restore_supplier_product,
    suggest_supplier_product_card,
    update_all_supplier_products,
    update_supplier_product,
    upload_supplier_product_image,
)
from catalog.models import SupplierProductVenuePrice
from core.models import AuditLog, AuditLogEntityType, EntityStatus, User, Venue
from suppliers.models import (
    Supplier,
    SupplierProduct,
    WorkflowEvent,
)


def _require_owns_product(request, product_id):
    """When authenticated as a Supplier cabinet JWT (self-service, not the
    trusted service token), the product must belong to that supplier — 404
    (not 403) so we don't confirm another supplier's product exists."""
    if isinstance(request.user, Supplier):
        if not SupplierProduct.objects.filter(pk=product_id, supplier_id=request.user.id).exists():
            raise Http404


def _require_owns_product_audit_log(request, product_id):
    """Same 404-hiding policy as _require_owns_product ("so we don't
    confirm another supplier's product exists"), extended to cover a hard
    deleted SupplierProduct: once the row is gone, ownership is checked
    against AuditLog.scope_id (the supplier_id snapshotted at delete time)
    instead of a live join. delete_supplier_product() stopped doing real
    hard deletes on 2026-09-23 (backlog#task-69) — this branch is now only
    reachable for products deleted before that date, kept for their sake."""
    if not isinstance(request.user, Supplier):
        return
    if SupplierProduct.objects.filter(pk=product_id, supplier_id=request.user.id).exists():
        return
    if SupplierProduct.objects.filter(pk=product_id).exists():
        raise Http404
    scope_id = (
        AuditLog.objects.filter(entity_type=AuditLogEntityType.SUPPLIER_PRODUCT, entity_id=product_id)
        .exclude(scope_id__isnull=True)
        .values_list("scope_id", flat=True)
        .first()
    )
    if scope_id is None or str(scope_id) != str(request.user.id):
        raise Http404


def _require_own_products(request, product_ids):
    """Bulk version of _require_owns_product — fails closed: if even one id
    doesn't belong to the calling supplier, the whole batch is rejected
    (404), not partially applied to the ones that do."""
    if isinstance(request.user, Supplier):
        requested_ids = {str(pid) for pid in product_ids}
        owned_ids = set(
            str(pk)
            for pk in SupplierProduct.objects.filter(pk__in=product_ids, supplier_id=request.user.id).values_list(
                "pk", flat=True
            )
        )
        if not requested_ids.issubset(owned_ids):
            raise Http404


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_products_list",
        tags=["Товары поставщика"],
        summary="Список товаров поставщика",
        parameters=[
            OpenApiParameter("supplier_id", OpenApiTypes.UUID, description="Фильтр по конкретному поставщику."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Фильтр по статусу товара: active / archived."),
            OpenApiParameter("q", OpenApiTypes.STR, description="Поиск по названию товара."),
            OpenApiParameter(
                "updated_since", OpenApiTypes.DATETIME, description="Только товары, изменённые после этой даты/времени."
            ),
            OpenApiParameter(
                "availability",
                OpenApiTypes.STR,
                description="Фильтр по доступности: in_stock / on_order / out_of_stock.",
            ),
            OpenApiParameter("brand_id", OpenApiTypes.UUID, description="Фильтр по бренду."),
            OpenApiParameter("category_id", OpenApiTypes.UUID, description="Фильтр по категории."),
            OpenApiParameter("price_min", OpenApiTypes.FLOAT, description="Минимальная актуальная цена."),
            OpenApiParameter("price_max", OpenApiTypes.FLOAT, description="Максимальная актуальная цена."),
            OpenApiParameter(
                "ordering",
                OpenApiTypes.STR,
                description=(
                    "Сортировка по любому полю ответа: имя поля — по возрастанию, с минусом впереди "
                    "('-current_price') — по убыванию. Допустимые поля — см. SupplierProductListQuerySerializer."
                ),
            ),
            OpenApiParameter("limit", OpenApiTypes.INT, description="Размер страницы (макс. 500, по умолч. 100)."),
            OpenApiParameter("offset", OpenApiTypes.INT, description="Смещение для пагинации."),
        ],
        responses={
            200: inline_serializer(
                name="SupplierProductListPage",
                fields={
                    "count": s.IntegerField(),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "results": SupplierProductListItemSerializer(many=True),
                },
            )
        },
    ),
    post=extend_schema(
        operation_id="supplier_products_create",
        tags=["Товары поставщика"],
        summary="Добавить товар вручную",
        description=(
            "Раньше товары появлялись только через загрузку прайса — эта ручка создаёт один товар "
            "напрямую, с теми же полями, что в таблице товаров. supplier_id обязателен только при "
            "вызове с сервис-токеном/staff — при cabinet JWT берётся из сессии. brand/category_path "
            "резолвятся так же, как при парсинге прайса (get_or_create по названию, без дублей)."
        ),
        request=SupplierProductCreateSerializer,
        responses={201: SupplierProductListItemSerializer},
    ),
    delete=extend_schema(
        operation_id="supplier_products_delete_all",
        tags=["Товары поставщика"],
        summary="Архивировать все товары компании (DELETE = archive)",
        description=(
            "2026-09-23: полностью совпадает с POST /products/archive-all (backlog#task-69, ТЗ «Карточка "
            "товара поставщика» §32 — удаление должно сохранять историю, отмена решения от 2026-08-11). "
            "Товары физически не удаляются — переводятся в archived, вместе с ценой/избранным/списками "
            "покупок покупателей (SupplierProductPrice/SupplierProductVenuePrice/VenueSupplierProduct/"
            "WishlistItem), обратимо через POST /products/restore-all. Трогает только ещё активные товары "
            "(уже архивированные — no-op). supplier_id обязателен только при вызове с сервис-токеном/staff; "
            "при cabinet JWT берётся из сессии, тело запроса может быть пустым."
        ),
        request=SupplierProductArchiveAllSerializer,
        responses={200: SupplierProductDeleteAllResponseSerializer},
    ),
)
class SupplierProductListView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        query_serializer = SupplierProductListQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)

        filters = query_serializer.validated_data
        if isinstance(request.user, Supplier):
            # Self-service supplier JWT — always scoped to their own
            # company, regardless of any supplier_id query param.
            filters["supplier_id"] = request.user.id
        limit = filters["limit"]
        offset = filters["offset"]

        queryset = supplier_products_queryset(filters)
        total_count = queryset.count()
        page = list(queryset[offset : offset + limit])
        results = SupplierProductListItemSerializer(page, many=True).data

        return Response(
            {
                "count": total_count,
                "limit": limit,
                "offset": offset,
                "results": results,
            }
        )

    def post(self, request):
        serializer = SupplierProductCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        supplier_id = data.get("supplier_id")

        if isinstance(request.user, Supplier):
            supplier_id = request.user.id
        elif not supplier_id:
            raise ValidationError({"supplier_id": "Обязательное поле при вызове без cabinet-сессии."})

        supplier_product = create_supplier_product(supplier_id=supplier_id, data=data)
        # Reload through the same queryset as GET so annotations (current_price
        # etc.) and select_related depth match the list response exactly.
        supplier_product = supplier_products_queryset({}).get(pk=supplier_product.pk)
        return Response(SupplierProductListItemSerializer(supplier_product).data, status=201)

    def delete(self, request):
        serializer = SupplierProductArchiveAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplier_id = serializer.validated_data.get("supplier_id")

        if isinstance(request.user, Supplier):
            supplier_id = request.user.id
        elif not supplier_id:
            raise ValidationError({"supplier_id": "Обязательное поле при вызове без cabinet-сессии."})

        deleted_count = delete_all_supplier_products(supplier_id=supplier_id)
        return Response({"supplier_id": supplier_id, "deleted_count": deleted_count})


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Рекомендация для карточки товара (автозаполнение через ИИ)",
        description=(
            "TASK-69 (§6, задача 6 реализации — реанимирована 2026-09-25 на общем LLM-ядре, TASK-87): "
            "по названию товара предлагает нормализованное имя для поиска, единицу измерения и похожие "
            "товары из общего каталога, чтобы не создавать дубликат. Ничего не сохраняет и не создаёт — "
            "чистая рекомендация, применяет её пользователь/фронт. Может вызываться и до создания товара "
            "(при заполнении формы), и при редактировании уже существующего — товар не идентифицируется "
            "по id, только по переданным name/unit_id."
        ),
        request=SupplierProductAutofillRequestSerializer,
        responses={
            200: SupplierProductAutofillResponseSerializer,
            400: OpenApiResponse(
                response=inline_serializer(
                    name="SupplierProductAutofillBadRequest",
                    fields={"name": s.ListField(child=s.CharField(), required=False)},
                ),
                description="Пустое название товара, либо supplier_id не передан при вызове с сервис-токеном.",
            ),
            404: OpenApiResponse(
                response=inline_serializer(name="SupplierProductAutofillNotFound", fields={"detail": s.CharField()}),
                description="supplier_id не найден.",
            ),
        },
    )
)
class SupplierProductAutofillView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductAutofillRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        supplier_id = data.get("supplier_id")

        if isinstance(request.user, Supplier):
            supplier_id = request.user.id
        elif not supplier_id:
            raise ValidationError({"supplier_id": "Обязательное поле при вызове без cabinet-сессии."})
        get_object_or_404(Supplier, pk=supplier_id)

        suggestion = suggest_supplier_product_card(name=data["name"], unit=data.get("unit"))
        return Response(SupplierProductAutofillResponseSerializer(suggestion).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Гибридный поиск по общему каталогу для поставщика",
        description=(
            "Ищет среди активных товаров всех поставщиков тем же lexical/semantic + Qdrant + weighted RRF "
            "pipeline, который используется в пользовательском каталоге. Результаты не ограничиваются "
            "товарами компании из JWT."
        ),
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, required=True, description="Название искомого товара."),
            OpenApiParameter("supplier_id", OpenApiTypes.UUID, many=True, description="Фильтр по поставщику."),
            OpenApiParameter("category_id", OpenApiTypes.UUID, many=True, description="Фильтр по категории."),
            OpenApiParameter("brand_id", OpenApiTypes.UUID, many=True, description="Фильтр по бренду."),
            OpenApiParameter(
                "availability", OpenApiTypes.STR, many=True, description="in_stock / on_order / out_of_stock."
            ),
            OpenApiParameter("price_min", OpenApiTypes.FLOAT, description="Минимальная цена."),
            OpenApiParameter("price_max", OpenApiTypes.FLOAT, description="Максимальная цена."),
            OpenApiParameter(
                "group_offers",
                OpenApiTypes.BOOL,
                description="Сгруппировать одинаковый товар от разных поставщиков.",
            ),
            OpenApiParameter("limit", OpenApiTypes.INT, description="Размер страницы, максимум 500."),
            OpenApiParameter("offset", OpenApiTypes.INT, description="Смещение для пагинации."),
        ],
        responses={
            200: inline_serializer(
                name="SupplierCatalogSearchPage",
                fields={
                    "count": s.IntegerField(),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "search_id": s.UUIDField(),
                    "duration_ms": s.FloatField(),
                    "cache_hit": s.BooleanField(),
                    "results": CatalogProductSerializer(many=True),
                    "group_count": s.IntegerField(required=False),
                    "groups": s.ListField(child=s.DictField(), required=False),
                },
            )
        },
    )
)
class SupplierCatalogSearchView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        started_at = time.monotonic()
        params = request.query_params
        data = {
            key: value
            for key, value in {
                "q": params.get("q"),
                "supplier_id": params.getlist("supplier_id"),
                "category_id": params.getlist("category_id"),
                "brand_id": params.getlist("brand_id"),
                "country": params.getlist("country"),
                "availability": params.getlist("availability"),
                "offers": params.getlist("offers"),
                "price_min": params.get("price_min"),
                "price_max": params.get("price_max"),
                "limit": params.get("limit"),
                "offset": params.get("offset"),
            }.items()
            if value is not None
        }
        query_serializer = CatalogProductQuerySerializer(data=data)
        query_serializer.is_valid(raise_exception=True)
        filters = query_serializer.validated_data
        if not filters.get("q"):
            raise ValidationError({"q": "Укажите название товара для поиска."})

        limit = filters["limit"]
        offset = filters["offset"]
        group_offers = str(params.get("group_offers", "")).lower() in {"1", "true", "yes"}
        cache_timeout = int(getattr(settings, "SUPPLIER_CATALOG_SEARCH_CACHE_TIMEOUT", 60) or 0)
        cache_signature = hashlib.sha256(params.urlencode().encode("utf-8")).hexdigest()
        cache_key = f"supplier-catalog-search:v2:{cache_signature}"
        payload = cache.get(cache_key) if cache_timeout else None
        cache_hit = payload is not None

        if payload is None:
            queryset = catalog_products_queryset(filters)
            total_count = queryset.count()
            page = list(queryset[offset : offset + limit])
            serialized = list(CatalogProductSerializer(page, many=True, context={"request": request}).data)
            for item, product in zip(serialized, page):
                item["supplier_sku"] = product.supplier_sku
                item["system_sku"] = product.system_sku
                item["match"] = explain_supplier_product_match(product, filters["q"])
            payload = {
                "count": total_count,
                "limit": limit,
                "offset": offset,
                "results": serialized,
            }
            if group_offers:
                groups = {}
                for item in serialized:
                    group_key = "|".join(
                        str(value or "").casefold()
                        for value in (item["normalized_name"], item.get("brand_id"), item.get("category_id"))
                    )
                    group = groups.setdefault(
                        group_key,
                        {
                            "group_key": hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:20],
                            "normalized_name": item["normalized_name"],
                            "brand": item.get("brand"),
                            "category": item.get("category"),
                            "best_price": item["current_price"],
                            "offers": [],
                        },
                    )
                    group["offers"].append(item)
                    if item["current_price"] is not None and (
                        group["best_price"] is None
                        or Decimal(str(item["current_price"])) < Decimal(str(group["best_price"]))
                    ):
                        group["best_price"] = item["current_price"]
                payload["groups"] = list(groups.values())
                payload["group_count"] = len(groups)
            if cache_timeout:
                cache.set(cache_key, payload, cache_timeout)

        search_id = uuid4()
        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        parsed_query = parse_search_query(filters["q"])
        supplier = request.user if isinstance(request.user, Supplier) else None
        WorkflowEvent.objects.create(
            event_type="supplier_catalog_search",
            chat_id="",
            supplier=supplier,
            payload_json={
                "search_id": str(search_id),
                "query": filters["q"],
                "normalized_query": parsed_query.normalized if parsed_query else "",
                "filters": {
                    key: [str(v) for v in value] if isinstance(value, list) else str(value)
                    for key, value in filters.items()
                    if key not in {"q", "limit", "offset"} and value not in (None, [], "")
                },
                "result_count": payload["count"],
                "result_ids": [str(item["id"]) for item in payload["results"][:100]],
                "outcome": "results_found" if payload["count"] else "zero_results",
                "duration_ms": duration_ms,
                "cache_hit": cache_hit,
                "group_offers": group_offers,
            },
        )
        return Response({**payload, "search_id": search_id, "duration_ms": duration_ms, "cache_hit": cache_hit})


_SUPPLIER_CATALOG_SUGGESTIONS_SCHEMA = inline_serializer(
    name="SupplierCatalogSuggestions",
    fields={
        "query": s.CharField(help_text="Запрос, по которому построены подсказки (из параметра q)."),
        "suggestions": inline_serializer(
            name="SupplierCatalogSuggestionItem",
            fields={
                "label": s.CharField(help_text="Название товара, показываемое в подсказке."),
                "supplier_sku": s.CharField(allow_null=True, help_text="Артикул товара у поставщика, если есть."),
                "system_sku": s.CharField(allow_null=True, help_text="Внутренний SKU каталога, если присвоен."),
                "brand": s.CharField(allow_null=True, help_text="Название бренда, если распознан."),
            },
            many=True,
            help_text="До limit подсказок.",
        ),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Подсказки общего каталога",
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, required=True, description="Префикс, минимум 2 символа."),
            OpenApiParameter("limit", OpenApiTypes.INT, description="До 20 подсказок."),
        ],
        responses={200: _SUPPLIER_CATALOG_SUGGESTIONS_SCHEMA},
    )
)
class SupplierCatalogSearchSuggestionsView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        query = str(request.query_params.get("q") or "").strip()
        if len(query) < 2:
            raise ValidationError({"q": "Введите минимум два символа."})
        try:
            limit = min(max(int(request.query_params.get("limit", 10)), 1), 20)
        except (TypeError, ValueError) as error:
            raise ValidationError({"limit": "Укажите целое число от 1 до 20."}) from error
        parsed = parse_search_query(query)
        normalized = parsed.normalized if parsed else query
        layout_variant = keyboard_layout_variant(normalized)
        cache_key = f"supplier-catalog-suggestions:v1:{hashlib.sha256(f'{normalized}:{limit}'.encode()).hexdigest()}"
        suggestions = cache.get(cache_key)
        if suggestions is None:
            queryset = catalog_products_queryset({"sort": "name_asc"}).filter(
                Q(normalized_name__icontains=normalized)
                | Q(supplier_name__icontains=normalized)
                | Q(normalized_name__icontains=layout_variant)
                | Q(supplier_name__icontains=layout_variant)
                | Q(supplier_sku__icontains=query)
                | Q(system_sku__icontains=query)
            )
            suggestions = []
            seen = set()
            for product in queryset[: limit * 5]:
                label = product.normalized_name or product.supplier_name
                key = label.casefold()
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(
                    {
                        "label": label,
                        "supplier_sku": product.supplier_sku,
                        "system_sku": product.system_sku,
                        "brand": getattr(product.brand, "canonical_name", None),
                    }
                )
                if len(suggestions) >= limit:
                    break
            cache.set(cache_key, suggestions, 60)
        return Response({"query": query, "suggestions": suggestions})


_SUPPLIER_CATALOG_SEARCH_EVENT_SCHEMA = inline_serializer(
    name="SupplierCatalogSearchEventAccepted",
    fields={
        "event_id": s.UUIDField(help_text="ID записанного события."),
        "accepted": s.BooleanField(default=True, help_text="Всегда true для этого ответа."),
    },
)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Записать действие или feedback после поиска",
        request=SupplierCatalogSearchEventSerializer,
        responses={201: _SUPPLIER_CATALOG_SEARCH_EVENT_SCHEMA},
    )
)
class SupplierCatalogSearchEventView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierCatalogSearchEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        search_events = WorkflowEvent.objects.filter(
            event_type="supplier_catalog_search",
            payload_json__search_id=str(data["search_id"]),
        )
        if isinstance(request.user, Supplier):
            search_events = search_events.filter(supplier=request.user)
        search_event = search_events.order_by("-created_at").first()
        if search_event is None:
            raise ValidationError({"search_id": "Search event was not found for the current supplier."})
        product_id = data.get("product_id")
        result_ids = (search_event.payload_json or {}).get("result_ids") or []
        if product_id and str(product_id) not in result_ids:
            raise ValidationError({"product_id": "Product was not present in this search result page."})
        event = WorkflowEvent.objects.create(
            event_type=f"supplier_catalog_search_{data['event_type']}",
            chat_id="",
            supplier=request.user if isinstance(request.user, Supplier) else None,
            payload_json={
                key: str(value) if key in {"search_id", "product_id"} and value else value
                for key, value in data.items()
            },
        )
        return Response({"event_id": event.id, "accepted": True}, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        operation_id="supplier_products_update",
        tags=["Товары поставщика"],
        summary="Обновить товар поставщика",
        request=SupplierProductUpdateSerializer,
        responses={200: SupplierProductListItemSerializer},
    ),
    delete=extend_schema(
        operation_id="supplier_products_delete",
        tags=["Товары поставщика"],
        summary="Архивировать товар (DELETE = archive)",
        description=(
            "2026-09-23: снова полностью совпадает с POST .../archive (backlog#task-69, ТЗ «Карточка "
            "товара поставщика» §32 — удаление должно сохранять историю; отменяет решение от 2026-08-11, "
            "когда этот DELETE на время стал реальным физическим удалением). Товар не удаляется — "
            "переводится в archived, история цен и избранное/списки покупок покупателей сохраняются "
            "(SupplierProductPrice/SupplierProductVenuePrice/VenueSupplierProduct/WishlistItem — обратно "
            "RESTRICT, не CASCADE). Обратимо через POST .../restore."
        ),
        request=None,
        responses={204: None},
    ),
)
class SupplierProductDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, id):
        _require_owns_product(request, id)
        serializer = SupplierProductUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        update_supplier_product(
            supplier_product_id=id,
            data=serializer.validated_data,
        )

        supplier_product = supplier_products_queryset({}).get(pk=id)
        return Response(SupplierProductListItemSerializer(supplier_product).data)

    def delete(self, request, id):
        _require_owns_product(request, id)
        delete_supplier_product(supplier_product_id=id, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="История изменения цены товара",
        description=(
            "Каждое изменение цены (вручную или из загрузки прайса) — отдельная запись, от новой к "
            "старой. status=active — единственная действующая сейчас запись (если товар без цены — "
            "список может быть пустым), superseded — сменённые, cancelled — отменённые архивацией "
            "товара. source показывает, откуда взялась запись: upload / manual."
        ),
        responses={200: SupplierProductPriceHistoryItemSerializer(many=True)},
    ),
)
class SupplierProductPriceHistoryView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, id):
        _require_owns_product(request, id)
        history = list_supplier_product_price_history(supplier_product_id=id)
        return Response(SupplierProductPriceHistoryItemSerializer(history, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="История удаления/архивации товара поставщика",
        description="Работает и для уже архивированного товара (DELETE с 2026-09-23 не удаляет "
        "физически, см. описание там), и для товаров, безвозвратно удалённых до этой даты — "
        "показывает, кто и когда его удалил или архивировал.",
        responses={
            200: AuditLogEntrySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(
                description='Товар не найден, либо принадлежит другому поставщику: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierProductAuditLogView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, id):
        _require_owns_product_audit_log(request, id)
        return Response(AuditLogEntrySerializer(supplier_product_audit_log(id), many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Найти заведения по ИНН/названию (для назначения индивидуальной цены)",
        description=(
            "2026-09-23, по просьбе пользователя: чтобы назначить индивидуальную цену "
            "(`POST /suppliers/products/{id}/individual-prices`), нужно сначала найти venue_ids "
            "искомых компаний — раньше в кабинете поставщика для этого не было способа (единственный "
            "существующий `GET /venues/search-by-inn` — для другого сценария: заведение ищет само "
            "себя, чтобы присоединиться, только точный ИНН, максимум одно совпадение, права другие). "
            "`q` матчится подстрокой сразу по названию (публичному и юридическому) и ИНН — одно поле "
            "поиска для обоих случаев."
        ),
        parameters=[VenueSearchQuerySerializer],
        responses={
            200: inline_serializer(
                name="VenueSearchResponse",
                fields={
                    "count": s.IntegerField(help_text="Всего найдено заведений."),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "results": VenueSearchResultSerializer(many=True),
                },
            ),
            400: OpenApiResponse(
                description='q короче 2 символов: {"q": ["Ensure this field has at least 2 characters."]}'
            ),
            401: OpenApiResponse(
                description="Нет JWT кабинета поставщика/сервис-токена, либо JWT невалиден/просрочен."
            ),
        },
    ),
)
class SupplierVenueSearchView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        query_ser = VenueSearchQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        limit = query_ser.validated_data["limit"]
        offset = query_ser.validated_data["offset"]

        queryset = venue_search_queryset(query_ser.validated_data["q"])
        total = queryset.count()
        page = queryset[offset : offset + limit]

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": VenueSearchResultSerializer(page, many=True).data,
            }
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Индивидуальные цены товара по компаниям",
        description="ТЗ «Поставщик Изменения цен» (2026-09-07): все индивидуальные цены этого товара, "
        "по всем компаниям, кому они назначены. Строки с одинаковым group_id созданы одной операцией "
        "«назначить нескольким компаниям сразу» — фронт может показать их одной строкой в таблице.",
        parameters=[
            OpenApiParameter(
                name="venue_id", type=OpenApiTypes.UUID, required=False, description="Ограничить одной компанией."
            ),
        ],
        responses={
            200: SupplierProductVenuePriceSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(
                description='Товар не найден, либо принадлежит другому поставщику: {"detail": "Not found."}'
            ),
        },
    ),
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Назначить индивидуальную цену товара одной или нескольким компаниям",
        description="ТЗ «Поставщик Изменения цен», §12: одна операция может назначить одну цену сразу "
        "нескольким компаниям — на уровне данных это отдельная запись на каждую компанию (общий "
        "group_id), поэтому позже цену можно изменить для одной компании, не трогая остальные.",
        request=SupplierProductIndividualPriceCreateSerializer,
        responses={
            201: SupplierProductVenuePriceSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "Не передано обязательное поле, либо: "
                    'venue_ids пуст — {"venue_ids": ["Укажите хотя бы одну компанию."]}; '
                    "venue_ids содержит не-UUID; "
                    'одна или несколько компаний не найдены/неактивны — {"venue_ids": '
                    '"Одна или несколько компаний не найдены/неактивны."}; '
                    "price отрицательный; currency отличается от RUB; "
                    'valid_to не позже valid_from — {"valid_to": "Должно быть позже valid_from."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT кабинета поставщика, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Требуется активное членство в компании-поставщике."),
            404: OpenApiResponse(
                description='Товар не найден, либо принадлежит другому поставщику: {"detail": "Not found."}'
            ),
            409: OpenApiResponse(
                response=SupplierProductVenuePriceConflictSerializer(many=True),
                description="Хотя бы у одной из компаний уже есть активная пересекающаяся по периоду цена — "
                "ничего не записано. Повторить запрос с confirm_replace=true, чтобы заменить.",
            ),
        },
    ),
)
class SupplierProductIndividualPriceListCreateView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]

    def get_permissions(self):
        # Read is available to the same service-token-or-cabinet audience as
        # every other product sub-resource; writing a price requires an
        # identified, active cabinet member (IsSupplierCabinetActiveMember)
        # so created_by can always be resolved to a real core.User.
        if self.request.method == "POST":
            return [IsSupplierCabinetActiveMember()]
        return [IsSupplierCabinetOrHasServiceToken()]

    def get(self, request, id):
        _require_owns_product(request, id)
        ser = SupplierProductIndividualPriceQuerySerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        qs = supplier_product_individual_prices_queryset(supplier_product_id=id, **ser.validated_data)
        return Response(SupplierProductVenuePriceSerializer(qs, many=True).data)

    def post(self, request, id):
        _require_owns_product(request, id)
        supplier_product = get_object_or_404(SupplierProduct, pk=id)
        ser = SupplierProductIndividualPriceCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        venue_ids = data.pop("venue_ids")
        confirm_replace = data.pop("confirm_replace")
        venues = list(Venue.objects.filter(id__in=venue_ids, status=EntityStatus.ACTIVE))
        if len(venues) != len(set(str(v) for v in venue_ids)):
            raise s.ValidationError({"venue_ids": "Одна или несколько компаний не найдены/неактивны."})
        created_by = User.objects.filter(id=request.auth.get("user_id")).first() if request.auth else None
        try:
            rows = set_supplier_product_venue_price(
                supplier_product=supplier_product,
                venues=venues,
                created_by=created_by,
                confirm_replace=confirm_replace,
                **data,
            )
        except SupplierProductVenuePriceConflict as exc:
            return Response(
                SupplierProductVenuePriceConflictSerializer(exc.conflicts, many=True).data,
                status=status.HTTP_409_CONFLICT,
            )
        return Response(SupplierProductVenuePriceSerializer(rows, many=True).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Архивировать индивидуальную цену",
        request=None,
        responses={
            200: SupplierProductVenuePriceSerializer,
            400: OpenApiResponse(description='Цена уже не активна: {"detail": "Эта цена уже не активна."}'),
            401: OpenApiResponse(description="Нет JWT кабинета поставщика, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Требуется активное членство в компании-поставщике."),
            404: OpenApiResponse(
                description='Цена не найдена, либо товар принадлежит другому поставщику: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierProductIndividualPriceArchiveView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetActiveMember]

    def post(self, request, id, price_id):
        _require_owns_product(request, id)
        price = get_object_or_404(SupplierProductVenuePrice, pk=price_id, supplier_product_id=id)
        price = archive_supplier_product_venue_price(price, actor=request.user)
        return Response(SupplierProductVenuePriceSerializer(price).data)


@extend_schema_view(
    patch=extend_schema(
        tags=["Товары поставщика"],
        summary="Изменить ещё не наступившую ручную цену",
        description=(
            "§29 ТЗ, «Будущая ручная цена»: редактирование доступно только для цены, у которой "
            "valid_from ещё не наступил, status=active и source_type=manual. Действующую (уже "
            "стартовавшую) или цену из тендера редактировать нельзя — используйте archive/новую цену."
        ),
        request=SupplierProductVenuePriceUpdateSerializer,
        responses={
            200: SupplierProductVenuePriceSerializer,
            400: OpenApiResponse(
                description=(
                    'Один из: не передано ни одного поля {"non_field_errors": ["At least one field must '
                    'be provided."]}; цена уже не активна {"detail": "Эта цена уже не активна."}; цена из '
                    'тендера {"detail": "Цену, назначенную из тендера, нельзя редактировать вручную."}; цена '
                    'уже действует {"detail": "Действие доступно только для ещё не наступившей цены."}; '
                    "новая дата начала не в будущем "
                    '{"valid_from": ["Новая дата начала должна остаться в будущем."]}; valid_to раньше или '
                    'равен valid_from {"valid_to": ["Должно быть позже valid_from."]}; то же нарушение '
                    "модельным CheckConstraint sp_venue_prices_period "
                    '{"__all__": ["Нарушено ограничение \\"sp_venue_prices_period\\"."]}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT кабинета поставщика, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Требуется активное членство в компании-поставщике."),
            404: OpenApiResponse(
                description='Цена не найдена, либо товар принадлежит другому поставщику: {"detail": "Not found."}'
            ),
            409: OpenApiResponse(
                response=SupplierProductVenuePriceConflictSerializer(many=True),
                description="Новый период пересекается с чужой активной ценой на то же venue — ничего не "
                "записано. У этого эндпоинта нет confirm_replace, конфликт нужно развести вручную "
                "(например, сначала заархивировать/изменить конфликтующую цену).",
            ),
        },
    ),
    delete=extend_schema(
        tags=["Товары поставщика"],
        summary="Удалить ещё не наступившую ручную цену",
        description=(
            "§29 ТЗ, «Будущая ручная цена»: удаление доступно только для цены, у которой valid_from "
            "ещё не наступил, status=active и source_type=manual. Действующую (уже стартовавшую) или "
            "цену из тендера удалить нельзя — доступен только archive."
        ),
        request=None,
        responses={
            204: None,
            400: OpenApiResponse(
                description=(
                    'Один из: цена уже не активна {"detail": "Эта цена уже не активна."}; цена из тендера '
                    '{"detail": "Цену, назначенную из тендера, нельзя редактировать вручную."}; цена уже '
                    'действует {"detail": "Действие доступно только для ещё не наступившей цены."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT кабинета поставщика, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Требуется активное членство в компании-поставщике."),
            404: OpenApiResponse(
                description='Цена не найдена, либо товар принадлежит другому поставщику: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierProductIndividualPriceDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetActiveMember]

    def patch(self, request, id, price_id):
        _require_owns_product(request, id)
        price = get_object_or_404(SupplierProductVenuePrice, pk=price_id, supplier_product_id=id)
        ser = SupplierProductVenuePriceUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        price_value = data.pop("price", None)
        try:
            price = update_supplier_product_venue_price(price, actor=request.user, price_value=price_value, **data)
        except (ValidationError, DjangoValidationError) as exc:
            # full_clean() inside update_supplier_product_venue_price validates model-level
            # CheckConstraints (e.g. sp_venue_prices_period: valid_to > valid_from) and raises
            # django.core.exceptions.ValidationError on violation — a different, unrelated
            # class from rest_framework.exceptions.ValidationError (the service's own guard
            # clauses raise that one). Neither is a subclass of the other and DRF's default
            # exception handler doesn't translate the Django one, so both must be caught here
            # or a constraint violation surfaces as an unhandled 500 instead of a 400.
            detail = exc.detail if hasattr(exc, "detail") else exc.message_dict
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        except SupplierProductVenuePriceConflict as exc:
            # 2026-09-21 final-review fix wave (Fix 1): moving this price's
            # window can now collide with a sibling ACTIVE price for the
            # same venue, same as the create path — but this endpoint has no
            # confirm_replace escape hatch, so a genuine conflict is just
            # reported, never silently resolved.
            return Response(
                SupplierProductVenuePriceConflictSerializer(exc.conflicts, many=True).data,
                status=status.HTTP_409_CONFLICT,
            )
        return Response(SupplierProductVenuePriceSerializer(price).data)

    def delete(self, request, id, price_id):
        _require_owns_product(request, id)
        price = get_object_or_404(SupplierProductVenuePrice, pk=price_id, supplier_product_id=id)
        try:
            delete_future_supplier_product_venue_price(price, actor=request.user)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


_INDIVIDUAL_PRICE_ORDER_HISTORY_PAGE_SCHEMA = inline_serializer(
    name="SupplierProductVenuePriceOrderHistoryPage",
    fields={
        "count": s.IntegerField(help_text="Всего строк (одна строка = одна позиция заявки, снапшотнутая на эту цену)."),
        "limit": s.IntegerField(),
        "offset": s.IntegerField(),
        "results": SupplierProductVenuePriceOrderHistoryItemSerializer(many=True),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="История заказов по конкретной индивидуальной цене",
        description="§26-27 ТЗ «Карточка товара поставщика»: постраничная построчная история заявок, "
        "у которых applied_individual_price указывает на эту конкретную запись цены (строго по прямой "
        "ссылке — не восстанавливается по датам/суммам, тот же принцип, что и агрегат confirmed_qty на "
        "SupplierProductVenuePriceSerializer). §27: фильтры по заведению, периоду и поиск по номеру "
        "заявки (подстрока).",
        parameters=[SupplierProductVenuePriceOrderHistoryQuerySerializer],
        responses={
            200: _INDIVIDUAL_PRICE_ORDER_HISTORY_PAGE_SCHEMA,
            400: OpenApiResponse(
                description=(
                    "Невалидные параметры фильтра/пагинации, например: "
                    'venue_id не UUID {"venue_id": ["Must be a valid UUID."]}; '
                    'date_from/date_to не распознаны как дата-время {"date_from": ["Datetime has wrong '
                    'format. Use one of these formats instead: YYYY-MM-DDThh:mm[:ss[.uuuuuu]]..."]}; '
                    'limit вне диапазона 1-200 {"limit": ["Ensure this value is less than or equal to '
                    '200."]}; offset отрицательный {"offset": ["Ensure this value is greater than or equal '
                    'to 0."]}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(
                description='Цена не найдена, либо товар принадлежит другому поставщику: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierProductIndividualPriceOrderHistoryView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, id, price_id):
        _require_owns_product(request, id)
        get_object_or_404(SupplierProductVenuePrice, pk=price_id, supplier_product_id=id)
        query_ser = SupplierProductVenuePriceOrderHistoryQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        filters = dict(query_ser.validated_data)
        limit = filters.pop("limit")
        offset = filters.pop("offset")
        qs = individual_price_order_history_queryset(price_id=price_id, **filters)
        total = qs.count()
        page = qs[offset : offset + limit]
        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": SupplierProductVenuePriceOrderHistoryItemSerializer(page, many=True).data,
            }
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Архивировать товар поставщика",
        request=None,
        responses={200: SupplierProductListItemSerializer},
    ),
)
class SupplierProductArchiveView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, id):
        _require_owns_product(request, id)
        archive_supplier_product(supplier_product_id=id, actor=request.user)

        supplier_product = supplier_products_queryset({}).get(pk=id)
        return Response(SupplierProductListItemSerializer(supplier_product).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Восстановить товар поставщика из архива",
        description=(
            "Обратная операция к archive: возвращает товар в active и, если у него нет уже активной "
            "цены, восстанавливает ту, что archive отменил (status=active, valid_to=null)."
        ),
        request=None,
        responses={200: SupplierProductListItemSerializer},
    ),
)
class SupplierProductRestoreView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, id):
        _require_owns_product(request, id)
        restore_supplier_product(supplier_product_id=id)

        supplier_product = supplier_products_queryset({}).get(pk=id)
        return Response(SupplierProductListItemSerializer(supplier_product).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Опубликовать товар (draft -> active)",
        description=(
            "Товары, созданные импортом (POST products/import/{session_id}/commit), создаются как "
            "draft и не видны покупателям, пока не будут опубликованы этим эндпоинтом. Идемпотентно: "
            "повторный вызов на уже active товаре просто возвращает его текущее состояние."
        ),
        request=None,
        responses={200: SupplierProductListItemSerializer},
    ),
)
class SupplierProductPublishView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, id):
        _require_owns_product(request, id)
        publish_supplier_product(supplier_product_id=id, actor=request.user)

        supplier_product = supplier_products_queryset({}).get(pk=id)
        return Response(SupplierProductListItemSerializer(supplier_product).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Массово опубликовать товары (draft -> active)",
        description="Аналог одиночного publish, но сразу для списка id. Всё-или-ничего по ownership, как и bulk-archive.",
        request=SupplierProductBulkIdsSerializer,
        responses={200: SupplierProductListItemSerializer(many=True)},
    ),
)
class SupplierProductBulkPublishView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductBulkIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        _require_own_products(request, ids)

        bulk_publish_supplier_products(supplier_product_ids=ids)
        products = supplier_products_queryset({}).filter(pk__in=ids)
        return Response(SupplierProductListItemSerializer(products, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Список фото товара",
        description="2026-09-13: фото товара — галерея из нескольких изображений, не одно. "
        "SupplierProduct.image_url (в остальном API) синхронизирован с самым ранним (первым "
        "загруженным) фото из этой галереи.",
        responses={200: SupplierProductImageSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Добавить фото товара",
        description=(
            "Multipart-загрузка файла — сохраняет через default_storage (S3, как и логотип компании) и "
            "добавляет в галерею фото товара (2026-08-03: единственная загрузка картинок раньше жила в "
            "легаси-прокси под shadowed-страницей, писала файл напрямую в ФС контейнера мимо S3). "
            "2026-09-13: каждый вызов ДОБАВЛЯЕТ фото, не заменяет предыдущее (раньше — заменял). Первое "
            "загруженное фото становится значением SupplierProduct.image_url."
        ),
        request=SupplierProductImageUploadSerializer,
        responses={201: SupplierProductImageSerializer},
    ),
)
class SupplierProductImageUploadView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, id):
        _require_owns_product(request, id)
        supplier_product = get_object_or_404(SupplierProduct, pk=id)
        return Response(SupplierProductImageSerializer(supplier_product.images.all(), many=True).data)

    def post(self, request, id):
        _require_owns_product(request, id)
        serializer = SupplierProductImageUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = upload_supplier_product_image(supplier_product_id=id, image_file=serializer.validated_data["image"])
        return Response(SupplierProductImageSerializer(image).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Товары поставщика"],
        summary="Удалить фото товара",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class SupplierProductImageDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def delete(self, request, id, image_id):
        _require_owns_product(request, id)
        delete_supplier_product_image(supplier_product_id=id, image_id=image_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    patch=extend_schema(
        tags=["Товары поставщика"],
        summary="Переупорядочить галерею товара",
        description=(
            "2026-09-21, §4 ТЗ. Принимает ordered_image_ids — id ВСЕХ фото товара в новом порядке "
            "(без пропусков и без чужих id). Пересчитывает position по позиции в списке; первый "
            "элемент становится главным фото — SupplierProduct.image_url синхронизируется с ним."
        ),
        request=SupplierProductImageReorderSerializer,
        responses={
            200: SupplierProductListItemSerializer,
            400: OpenApiResponse(
                description=(
                    'Пустой список (allow_empty=False на ordered_image_ids) {"ordered_image_ids": '
                    '["This list may not be empty."]}; список не совпадает ровно с текущим набором id '
                    "фото товара — не хватает id, есть лишний, или id принадлежит другому товару "
                    '{"ordered_image_ids": "Список должен содержать ровно все фото товара, без пропусков."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT кабинета поставщика или сервис-токена."),
            404: OpenApiResponse(
                description='Товар не найден, либо принадлежит другому поставщику: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierProductImageReorderView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, id):
        _require_owns_product(request, id)
        serializer = SupplierProductImageReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reorder_supplier_product_image(
            supplier_product_id=id, ordered_image_ids=serializer.validated_data["ordered_image_ids"]
        )
        supplier_product = supplier_products_queryset({}).get(pk=id)
        return Response(SupplierProductListItemSerializer(supplier_product).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Массово архивировать товары поставщика",
        description=(
            "Аналог одиночного archive, но сразу для списка id — для мультивыбора в таблице "
            "товаров. Всё-или-ничего: если хотя бы один id не существует или (self-service JWT) "
            "принадлежит другому поставщику, весь запрос отклоняется 404, ни один товар не меняется."
        ),
        request=SupplierProductBulkIdsSerializer,
        responses={200: SupplierProductListItemSerializer(many=True)},
    ),
)
class SupplierProductBulkArchiveView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductBulkIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        _require_own_products(request, ids)

        bulk_archive_supplier_products(supplier_product_ids=ids)
        products = supplier_products_queryset({}).filter(pk__in=ids)
        return Response(SupplierProductListItemSerializer(products, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Массово восстановить товары поставщика из архива",
        description=("Аналог одиночного restore, но сразу для списка id. Всё-или-ничего, как и bulk-archive."),
        request=SupplierProductBulkIdsSerializer,
        responses={200: SupplierProductListItemSerializer(many=True)},
    ),
)
class SupplierProductBulkRestoreView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductBulkIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        _require_own_products(request, ids)

        bulk_restore_supplier_products(supplier_product_ids=ids)
        products = supplier_products_queryset({}).filter(pk__in=ids)
        return Response(SupplierProductListItemSerializer(products, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Архивировать все товары компании",
        description=(
            "Для кнопки на экране «Мои компании» — архивирует все ещё активные товары поставщика "
            "одним запросом, без захода в таблицу товаров и мультивыбора. supplier_id обязателен "
            "только при вызове с сервис-токеном/staff; при cabinet JWT берётся из сессии, тело "
            "запроса может быть пустым."
        ),
        request=SupplierProductArchiveAllSerializer,
        responses={200: SupplierProductArchiveAllResponseSerializer},
    ),
)
class SupplierProductArchiveAllView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductArchiveAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplier_id = serializer.validated_data.get("supplier_id")

        if isinstance(request.user, Supplier):
            supplier_id = request.user.id
        elif not supplier_id:
            raise ValidationError({"supplier_id": "Обязательное поле при вызове без cabinet-сессии."})

        archived_count = archive_all_supplier_products(supplier_id=supplier_id)
        return Response({"supplier_id": supplier_id, "archived_count": archived_count})


@extend_schema_view(
    post=extend_schema(
        tags=["Товары поставщика"],
        summary="Восстановить все товары компании из архива",
        description=(
            "Обратная операция к archive-all — возвращает все ещё архивные товары поставщика в active "
            "одним запросом, включая отменённые при архивации цены. Тот же supplier_id-контракт, что "
            'у archive-all (cabinet JWT: тело пустое; сервис-токен/staff: {"supplier_id": "..."}).'
        ),
        request=SupplierProductRestoreAllSerializer,
        responses={200: SupplierProductRestoreAllResponseSerializer},
    ),
)
class SupplierProductRestoreAllView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductRestoreAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplier_id = serializer.validated_data.get("supplier_id")

        if isinstance(request.user, Supplier):
            supplier_id = request.user.id
        elif not supplier_id:
            raise ValidationError({"supplier_id": "Обязательное поле при вызове без cabinet-сессии."})

        restored_count = restore_all_supplier_products(supplier_id=supplier_id)
        return Response({"supplier_id": supplier_id, "restored_count": restored_count})


@extend_schema_view(
    patch=extend_schema(
        tags=["Товары поставщика"],
        summary="Массово изменить поля у нескольких товаров",
        description=(
            "Те же поля, что у PATCH /suppliers/products/{id} (доступность, статус, цена, бренд, "
            "категория и т.п.), применяются одинаково ко всем id в списке одним запросом — для "
            "мультивыбора в таблице товаров. Всё-или-ничего, как и у bulk-archive."
        ),
        request=SupplierProductBulkUpdateSerializer,
        responses={200: SupplierProductListItemSerializer(many=True)},
    ),
    delete=extend_schema(
        tags=["Товары поставщика"],
        summary="Массово архивировать товары (DELETE = archive)",
        description=(
            "2026-09-23: тот же смысл, что у DELETE /suppliers/products/{id} — архивирует, не удаляет "
            'физически (backlog#task-69, см. описание там). Тело — {"ids": [...]}, всё-или-ничего.'
        ),
        request=SupplierProductBulkIdsSerializer,
        responses={200: SupplierProductBulkDeleteResponseSerializer},
    ),
)
class SupplierProductBulkUpdateView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request):
        serializer = SupplierProductBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        ids = data.pop("ids")
        _require_own_products(request, ids)

        bulk_update_supplier_products(supplier_product_ids=ids, data=data)
        products = supplier_products_queryset({}).filter(pk__in=ids)
        return Response(SupplierProductListItemSerializer(products, many=True).data)

    def delete(self, request):
        serializer = SupplierProductBulkIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        _require_own_products(request, ids)

        bulk_delete_supplier_products(supplier_product_ids=ids)
        return Response({"deleted_count": len(ids), "ids": ids})


@extend_schema_view(
    patch=extend_schema(
        tags=["Товары поставщика"],
        summary="Массово изменить поля у ВСЕХ товаров компании",
        description=(
            "2026-08-27: та же логика, что у PATCH /suppliers/products/bulk, но без списка id — "
            "для галочки «выбрать все» в bulk-actions таблицы товаров. Применяется ко всем ещё "
            "активным товарам поставщика (архивные не трогает — тот же scope, что у archive-all). "
            "supplier_id обязателен только при вызове с сервис-токеном/staff; при cabinet JWT "
            "берётся из сессии."
        ),
        request=SupplierProductBulkUpdateAllSerializer,
        responses={200: SupplierProductBulkUpdateAllResponseSerializer},
    ),
)
class SupplierProductBulkUpdateAllView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request):
        serializer = SupplierProductBulkUpdateAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        supplier_id = data.pop("supplier_id", None)

        if isinstance(request.user, Supplier):
            supplier_id = request.user.id
        elif not supplier_id:
            raise ValidationError({"supplier_id": "Обязательное поле при вызове без cabinet-сессии."})

        updated_count = update_all_supplier_products(supplier_id=supplier_id, data=data)
        return Response({"supplier_id": supplier_id, "updated_count": updated_count})
