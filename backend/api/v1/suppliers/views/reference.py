"""Reference lists for the supplier cabinet."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import (
    OptionalSupplierCabinetJWTAuthentication,
)
from api.v1.permissions import (
    IsSupplierCabinetOrHasServiceToken,
)
from api.v1.suppliers.selectors import (
    annotate_category_depth,
    supplier_brands_queryset,
    supplier_categories_queryset,
)
from api.v1.suppliers.serializers import (
    SupplierBrandListQuerySerializer,
    SupplierBrandSerializer,
    SupplierCategorySerializer,
    SupplierCountrySerializer,
    SupplierUnitListQuerySerializer,
    SupplierUnitSerializer,
)
from core.countries import COUNTRY_NAMES
from core.models import EntityStatus, Unit


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Список единиц измерения",
        description=(
            "Справочник для unit_id/net_unit_id в POST/PATCH товара — 2026-08-09: "
            "раньше этих ID неоткуда было взять (справочник существовал только как management-команда, "
            "которую забыли прогнать на dev-стенде). ?kind сужает список под конкретное поле формы "
            "(net_unit_id — kind=weight для товаров, где нетто измеряется весом, kind=volume — объёмом)."
        ),
        parameters=[
            OpenApiParameter("kind", OpenApiTypes.STR, description="Фильтр по типу: weight/volume/piece/package."),
        ],
        responses={200: SupplierUnitSerializer(many=True)},
    ),
)
class SupplierUnitListView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        query_ser = SupplierUnitListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)

        units = Unit.objects.filter(status=EntityStatus.ACTIVE).order_by("kind", "code")
        kind = query_ser.validated_data.get("kind")
        if kind:
            units = units.filter(kind=kind)

        return Response(SupplierUnitSerializer(units, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Список стран производства",
        description=(
            "2026-08-11: созвон 2026-08-10 — country_of_origin в форме товара было свободным "
            "текстом без источника вариантов, риск опечаток/дублей. Статический справочник "
            "(не таблица в БД — country_of_origin не FK), полный список стран по алфавиту."
        ),
        responses={200: SupplierCountrySerializer(many=True)},
    ),
)
class SupplierCountryListView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        countries = [{"name": name} for name in COUNTRY_NAMES]
        return Response(SupplierCountrySerializer(countries, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Подсказка по брендам",
        description=(
            "2026-08-11: созвон 2026-08-10 — автокомплит по названию бренда в форме товара, "
            "чтобы снизить дубли с опечатками (один бренд с разным написанием). Не векторный "
            "поиск — обычный icontains-поиск по уже введённым/утверждённым на платформе брендам "
            "(Brand наполняется через get_or_create при каждом создании/импорте товара)."
        ),
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, description="Подстрока для поиска по названию бренда."),
        ],
        responses={200: SupplierBrandSerializer(many=True)},
    ),
)
class SupplierBrandListView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        query_ser = SupplierBrandListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)

        brands = supplier_brands_queryset(q=query_ser.validated_data.get("q"))
        return Response(SupplierBrandSerializer(brands, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Товары поставщика"],
        summary="Список категорий (группа/подгруппа/вид)",
        description=(
            "2026-08-11: созвон 2026-08-10 — Витя, категория в форме товара была зависимым "
            'списком без источника данных ("мне список неоткуда взять"). Не то же самое, что '
            "каталожный GET /catalog/categories — тот отдаёт только листья, у которых уже есть "
            "товары с ценой (фильтр покупательского каталога), тут — весь справочник, включая "
            "категории без единого товара пока.\n\n"
            "2026-08-12: по просьбе Вити ответ сгруппирован по уровню вложенности — "
            '`{"0": [{id, name, parent_id}, ...], "1": [...], "2": [...]}` '
            "(0=группа, 1=подгруппа, 2=вид), а не плоский список с полем depth на каждой записи."
        ),
        responses={
            200: inline_serializer(
                name="SupplierCategoryGroupedResponse",
                fields={
                    "0": SupplierCategorySerializer(many=True),
                    "1": SupplierCategorySerializer(many=True),
                    "2": SupplierCategorySerializer(many=True),
                },
            )
        },
    ),
)
class SupplierCategoryListView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        # 2026-08-12: Витя — удобнее сразу сгруппированным по уровню
        # {"0": [...], "1": [...], "2": [...]}, а не плоским списком с полем
        # depth на каждой записи (фронту всё равно приходится делать эту
        # группировку самому, раз он строит 3 каскадных select'а).
        categories = annotate_category_depth(supplier_categories_queryset())
        grouped = {"0": [], "1": [], "2": []}
        for item in SupplierCategorySerializer(categories, many=True).data:
            depth_key = str(item.pop("depth"))
            grouped.setdefault(depth_key, []).append(item)
        return Response(grouped)
