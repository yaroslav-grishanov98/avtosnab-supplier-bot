"""Public catalog: products, photo search and reference lists."""

from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    catalog_brands_queryset,
    catalog_categories_queryset,
    catalog_countries_queryset,
    catalog_product_detail_queryset,
    catalog_replace_order_item,
    catalog_supplier_detail_queryset,
    catalog_suppliers_queryset,
)
from api.v1.catalog.serializers import (
    CatalogBrandSerializer,
    CatalogCategorySerializer,
    CatalogCountrySerializer,
    CatalogProductDetailQueryErrorSerializer,
    CatalogProductDetailQuerySerializer,
    CatalogProductDetailSerializer,
    CatalogProductListQueryErrorSerializer,
    CatalogProductNotFoundSerializer,
    CatalogProductQuerySerializer,
    CatalogProductReplaceItemAuthSerializer,
    CatalogProductReplaceItemNotFoundSerializer,
    CatalogProductSerializer,
    CatalogSupplierDetailSerializer,
    CatalogSupplierListQuerySerializer,
    CatalogSupplierSerializer,
)
from api.v1.catalog.vision_client import PhotoSearchError, recognize_product_search_query
from catalog.models import (
    WishlistItem,
)
from core.models import (
    EntityStatus,
    User,
)

from ._shared import _PRODUCT_PAGE_SCHEMA, _catalog_products_page

_PHOTO_SEARCH_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


@extend_schema_view(
    get=extend_schema(
        tags=["Каталог"],
        summary="Список товаров каталога",
        description=(
            "Публичный endpoint — доступен без авторизации, покупателю не нужен вход, чтобы смотреть каталог. "
            "Возвращает активные товары поставщиков с актуальной ценой. JWT опционален: если передан, "
            "персонализирует `is_favorite`/`wishlists`. Если передан `venue_id`, поле `is_favorite` показывает, "
            "добавлен ли товар в избранное этого заведения. «Ищем замену» из «Неразобранных»: "
            "`replace_item_id` = id позиции → товары других поставщиков той же категории (детали — в описании "
            "параметра), выбранный товар уходит в POST .../items/{item_id}/change-supplier."
        ),
        parameters=[CatalogProductQuerySerializer],
        responses={
            200: _PRODUCT_PAGE_SCHEMA,
            400: OpenApiResponse(
                response=CatalogProductListQueryErrorSerializer,
                description="Невалидный query-параметр — ошибки по полям.",
            ),
            401: OpenApiResponse(
                response=CatalogProductReplaceItemAuthSerializer,
                description="replace_item_id передан без JWT.",
            ),
            404: OpenApiResponse(
                response=CatalogProductReplaceItemNotFoundSerializer,
                description="replace_item_id: позиции нет или пользователь не участник её заведения.",
            ),
        },
    ),
)
class CatalogProductListView(APIView):
    # Публичный просмотр каталога (покупателю не нужен вход, чтобы смотреть товары) —
    # JWT остаётся опциональным для персонализации is_favorite/wishlists, если он есть.
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params
        data = {
            k: v
            for k, v in {
                "q": params.get("q"),
                "supplier_id": params.getlist("supplier_id"),
                "category_id": params.getlist("category_id"),
                "brand_id": params.getlist("brand_id"),
                "country": params.getlist("country"),
                "availability": params.getlist("availability"),
                "offers": params.getlist("offers"),
                "venue_id": params.get("venue_id"),
                "is_favorite": params.get("is_favorite"),
                "wishlist_id": params.get("wishlist_id"),
                "price_min": params.get("price_min"),
                "price_max": params.get("price_max"),
                "sort": params.get("sort"),
                "limit": params.get("limit"),
                "offset": params.get("offset"),
                "replace_item_id": params.get("replace_item_id"),
            }.items()
            if v is not None
        }
        query_ser = CatalogProductQuerySerializer(data=data)
        query_ser.is_valid(raise_exception=True)
        filters = query_ser.validated_data
        user = request.user if isinstance(request.user, User) else None

        replace_item_id = filters.get("replace_item_id")
        if replace_item_id:
            if user is None:
                return Response(
                    {"detail": "Для replace_item_id нужна авторизация."}, status=status.HTTP_401_UNAUTHORIZED
                )
            replace_item = catalog_replace_order_item(user_id=user.id, item_id=replace_item_id)
            if replace_item is None:
                return Response({"detail": "Позиция заказа для замены не найдена."}, status=status.HTTP_404_NOT_FOUND)
            filters = {**filters, "replace_item": replace_item}

        return Response(_catalog_products_page(filters, user=user, request=request))


@extend_schema_view(
    get=extend_schema(
        operation_id="catalog_product_retrieve",
        tags=["Каталог"],
        summary="Карточка товара",
        description=(
            "Публичный endpoint — тот же доступ и та же видимость, что у списка товаров каталога: "
            "активный товар с ценой у активного опубликованного поставщика, иначе 404. Поля те же, что у "
            "строки списка, плюс поля модалки карточки: `images` (галерея), `short_description`, "
            "`net_value`/`net_unit` (вес/объём нетто), `package_composition` (состав упаковки, готовая строка — "
            "`display`). JWT опционален: если передан, заполняет "
            "`wishlists`; `venue_id` нужен для `is_favorite`."
        ),
        parameters=[CatalogProductDetailQuerySerializer],
        responses={
            200: CatalogProductDetailSerializer,
            400: CatalogProductDetailQueryErrorSerializer,
            404: CatalogProductNotFoundSerializer,
        },
    ),
)
class CatalogProductDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, product_id):
        query_ser = CatalogProductDetailQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        venue_id = query_ser.validated_data.get("venue_id")
        product = get_object_or_404(catalog_product_detail_queryset(venue_id=venue_id), pk=product_id)

        wishlist_map = {}
        user = request.user if isinstance(request.user, User) else None
        if user is not None:
            wishlist_map[product.id] = [
                {"id": row["wishlist_id"], "name": row["wishlist__name"]}
                for row in WishlistItem.objects.filter(
                    supplier_product=product,
                    wishlist__user=user,
                    status=EntityStatus.ACTIVE,
                    wishlist__status=EntityStatus.ACTIVE,
                ).values("wishlist_id", "wishlist__name")
            ]

        return Response(
            CatalogProductDetailSerializer(product, context={"wishlist_map": wishlist_map, "request": request}).data
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Каталог"],
        summary="Поиск товаров по фото",
        description=(
            "Публичный endpoint — доступен без авторизации, как и остальной просмотр каталога. "
            "Принимает фото товара (multipart, поле `photo`), распознаёт товар через AI "
            "и ищет по каталогу тем же текстовым поиском, что и `q` в списке товаров."
        ),
        request=inline_serializer(
            name="CatalogPhotoSearchRequest",
            fields={"photo": s.FileField(help_text="Фото товара: JPEG, PNG или WebP.")},
        ),
        parameters=[
            OpenApiParameter("venue_id", OpenApiTypes.UUID, description="ID заведения (нужен для is_favorite)"),
            OpenApiParameter("limit", OpenApiTypes.INT, description="Размер страницы (макс. 500, по умолч. 100)"),
            OpenApiParameter("offset", OpenApiTypes.INT, description="Смещение для пагинации"),
        ],
        responses={
            200: inline_serializer(
                name="CatalogPhotoSearchPage",
                fields={
                    "count": s.IntegerField(),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "recognized_query": s.CharField(help_text="Что AI распознал на фото (пусто, если не удалось)."),
                    "results": CatalogProductSerializer(many=True),
                },
            ),
            400: OpenApiResponse(description="Файл не передан, слишком большой или недопустимого типа"),
            503: OpenApiResponse(description="Распознавание фото недоступно (AI не настроен/не отвечает)"),
        },
    ),
)
class CatalogPhotoSearchView(APIView):
    # Тот же публичный доступ, что и у списка товаров — фотопоиск не сложнее q=.
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        photo = request.FILES.get("photo")
        if not photo:
            return Response({"detail": "Файл photo обязателен."}, status=status.HTTP_400_BAD_REQUEST)

        max_bytes = settings.CATALOG_PHOTO_SEARCH_MAX_BYTES
        if photo.size > max_bytes:
            return Response(
                {"detail": f"Файл слишком большой (максимум {max_bytes // (1024 * 1024)} МБ)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        content_type = (photo.content_type or "").lower()
        if content_type not in _PHOTO_SEARCH_ALLOWED_CONTENT_TYPES:
            return Response(
                {"detail": "Поддерживаются только фото JPEG, PNG или WebP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            recognized_query = recognize_product_search_query(
                image_bytes=photo.read(),
                content_type=content_type,
            )
        except PhotoSearchError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        params = request.query_params
        data = {
            k: v
            for k, v in {
                "venue_id": params.get("venue_id"),
                "limit": params.get("limit"),
                "offset": params.get("offset"),
            }.items()
            if v is not None
        }
        query_ser = CatalogProductQuerySerializer(data=data)
        query_ser.is_valid(raise_exception=True)
        filters = query_ser.validated_data
        user = request.user if isinstance(request.user, User) else None

        if not recognized_query:
            page = {"count": 0, "limit": filters["limit"], "offset": filters["offset"], "results": []}
        else:
            filters["q"] = recognized_query
            page = _catalog_products_page(filters, user=user, request=request)

        return Response({**page, "recognized_query": recognized_query})


@extend_schema_view(
    get=extend_schema(
        tags=["Каталог"],
        summary="Список брендов для фильтра",
        description=("Публичный endpoint. Возвращает бренды, у которых есть хотя бы один активный товар с ценой."),
        responses={200: CatalogBrandSerializer(many=True)},
    ),
)
class CatalogBrandListView(APIView):
    # Справочник для фильтров каталога — тот же публичный доступ, что и products.
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        brands = catalog_brands_queryset()
        return Response(CatalogBrandSerializer(brands, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Каталог"],
        summary="Список категорий для фильтра",
        description=(
            "Публичный endpoint. Возвращает активные категории (листья дерева), у которых есть товары с ценой. "
            "Каждая категория содержит parent_id / parent_name (подгруппа) и group_id / group_name (группа)."
        ),
        responses={200: CatalogCategorySerializer(many=True)},
    ),
)
class CatalogCategoryListView(APIView):
    # Справочник для фильтров каталога — тот же публичный доступ, что и products.
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        categories = catalog_categories_queryset()
        return Response(CatalogCategorySerializer(categories, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Каталог"],
        summary="Список поставщиков для фильтра",
        description=(
            "Публичный endpoint. Возвращает активных поставщиков, у которых есть хотя бы один активный товар "
            "с ценой. Параметр `q` ищет по короткому названию, юр. лицу и ИНН."
        ),
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, description="Поиск по display_name / legal_name / ИНН"),
        ],
        responses={200: CatalogSupplierSerializer(many=True)},
    ),
)
class CatalogSupplierListView(APIView):
    # Справочник для фильтров каталога — тот же публичный доступ, что и products.
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        query_ser = CatalogSupplierListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        suppliers = catalog_suppliers_queryset(q=query_ser.validated_data.get("q"))
        return Response(CatalogSupplierSerializer(suppliers, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="catalog_supplier_retrieve",
        tags=["Каталог"],
        summary="Карточка поставщика",
        description=(
            "2026-09-15, по прямой просьбе пользователя: карточка поставщика по клику на его название в "
            "истории заказов заведения. Публичный endpoint (тот же доступ, что у списка выше) — не "
            "фильтруется по is_published/status, заказ мог быть оформлен у уже снятого с публикации/ "
            "деактивированного поставщика, карточка всё равно должна открываться. Для поставщиков вне "
            "системы (`off_system_supplier_id` на позиции заказа) используйте GET .../off-system-suppliers/"
            "{id} — это разные сущности."
        ),
        responses={
            200: CatalogSupplierDetailSerializer,
            404: OpenApiResponse(description='Поставщика с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class CatalogSupplierDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, supplier_id):
        supplier = get_object_or_404(catalog_supplier_detail_queryset(), pk=supplier_id)
        return Response(CatalogSupplierDetailSerializer(supplier).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Каталог"],
        summary="Список стран происхождения для фильтра",
        description=("Публичный endpoint. Возвращает уникальные страны происхождения из активных товаров с ценой."),
        responses={200: CatalogCountrySerializer(many=True)},
    ),
)
class CatalogCountryListView(APIView):
    # Справочник для фильтров каталога — тот же публичный доступ, что и products.
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        countries = [{"country": c} for c in catalog_countries_queryset()]
        return Response(CatalogCountrySerializer(countries, many=True).data)
