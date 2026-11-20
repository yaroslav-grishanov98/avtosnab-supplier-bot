"""Venue product matrix (company directory) and supplier matching."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication, OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    master_product_suggestions_for_venue,
    product_matrix_item_audit_log,
    venue_product_matrix_queryset,
)
from api.v1.catalog.serializers import (
    AuditLogEntrySerializer,
    CatalogProductQuerySerializer,
    MasterProductSuggestionResponseSerializer,
    MasterProductVenueLinkConflictSerializer,
    ProductSuggestionQuerySerializer,
    VenueProductMatrixItemCreateSerializer,
    VenueProductMatrixItemSerializer,
    VenueProductMatrixItemUpdateSerializer,
    VenueProductMatrixListQuerySerializer,
    VenueProductMatrixListResponseSerializer,
    VenueProductMatrixMasterProductDecisionSerializer,
    VenueProductMatrixSupplierMatchContextSerializer,
    VenueProductMatrixSupplierMatchDetailErrorSerializer,
    VenueProductMatrixSupplierMatchSelectErrorSerializer,
    VenueProductMatrixSupplierMatchSelectSerializer,
)
from api.v1.catalog.services import (
    MasterProductAlreadyLinkedConflict,
    create_product_matrix_item,
    create_product_matrix_item_with_master_decision,
    delete_product_matrix_item,
    set_product_matrix_master_product,
    update_product_matrix_item,
)
from api.v1.catalog.supplier_match import (
    SupplierMatchAlreadyLinked,
    refresh_supplier_match,
    select_supplier_match_product,
    supplier_match_search,
)
from api.v1.permissions import (
    IsVenueMember,
)
from catalog.models import (
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    User,
    Venue,
    VenueSubdivision,
)

from ._shared import _PRODUCT_PAGE_SCHEMA, _catalog_products_page


class _VenueProductMatrixPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_company_products_list",
        tags=["Справочник товаров компании"],
        summary="Справочник товаров компании",
        description=(
            "Товары конкретной компании: собственное название, единица и примечание. "
            "`master_product_id` связывает строку с общим Мастер-справочником, а `supplier_product_id` — "
            "с конкретным предложением поставщика, `subdivision_id` — с подразделением этого заведения "
            "(необязательно). Для Excel/CSV используйте двухшаговый раздел «Импорт справочника товаров "
            "компании»."
        ),
        parameters=[
            OpenApiParameter(
                name="filter",
                type=OpenApiTypes.STR,
                required=False,
                enum=["all", "needs_review", "no_master_product"],
                description="Все (по умолчанию) / Нужно проверить / Без Мастер-товара.",
            ),
            OpenApiParameter(
                name="q",
                type=OpenApiTypes.STR,
                required=False,
                description=(
                    "Поиск по справочнику компании: название позиции, связанный мастер-товар, "
                    "товар поставщика, единица, SKU и примечание. Поддерживает слова в любом порядке, "
                    "русскую раскладку и небольшие опечатки."
                ),
            ),
            OpenApiParameter(
                name="subdivision_id",
                type=OpenApiTypes.UUID,
                required=False,
                description="Фильтр по подразделению этого заведения (из GET /venues/{venue_id}/subdivisions). "
                "Без фильтра — все строки, независимо от подразделения (включая без него).",
            ),
            OpenApiParameter(name="page", type=OpenApiTypes.INT, required=False, description="Номер страницы."),
            OpenApiParameter(
                name="page_size",
                type=OpenApiTypes.INT,
                required=False,
                description="Размер страницы (макс. 100, по умолчанию 20).",
            ),
        ],
        responses={
            200: VenueProductMatrixListResponseSerializer,
            400: OpenApiResponse(
                description='Неверное значение filter: {"filter": ["\\"x\\" is not a valid choice."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
    post=extend_schema(
        operation_id="venue_company_products_create",
        tags=["Справочник товаров компании"],
        summary="Добавить товар в справочник компании",
        description=(
            "Перед сохранением вызовите master-product-suggestions. Выбранное решение можно передать в "
            "`master_product_decision`, тогда товар компании и связь с Мастер-справочником создадутся одной "
            "транзакцией. Если решение не передано, сохраняется прежнее автоматическое сопоставление."
        ),
        request=VenueProductMatrixItemCreateSerializer,
        responses={
            201: VenueProductMatrixItemSerializer,
            400: OpenApiResponse(
                description='Не передано обязательное поле: {"requested_name": ["This field is required."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
            409: MasterProductVenueLinkConflictSerializer,
        },
    ),
)
class VenueProductMatrixListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        query_ser = VenueProductMatrixListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        queryset = venue_product_matrix_queryset(
            venue_id,
            filter_value=query_ser.validated_data["filter"],
            q=query_ser.validated_data.get("q"),
            subdivision_id=query_ser.validated_data.get("subdivision_id"),
        )
        paginator = _VenueProductMatrixPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(VenueProductMatrixItemSerializer(page, many=True).data)

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueProductMatrixItemCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        if data.get("subdivision_id"):
            get_object_or_404(
                VenueSubdivision, pk=data["subdivision_id"], venue_id=venue_id, status=EntityStatus.ACTIVE
            )
        master_product_decision = data.pop("master_product_decision", None)
        if master_product_decision is None:
            item = create_product_matrix_item(venue=venue, **data)
        else:
            try:
                item = create_product_matrix_item_with_master_decision(
                    venue=venue, master_product_decision=master_product_decision, **data
                )
            except MasterProductAlreadyLinkedConflict as exc:
                return Response(
                    {
                        "detail": "Этот мастер-товар уже используется другим товаром этой компании.",
                        "candidate": VenueProductMatrixItemSerializer(exc.existing_item).data,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        return Response(VenueProductMatrixItemSerializer(item).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_company_products_master_suggestions",
        tags=["Справочник товаров компании"],
        summary="Рекомендовать или найти мастер-товар перед добавлением",
        description=(
            "Основная ручка поля «Мастер-товар» из прототипа. Передайте `name` = значение поля "
            "«Название товара в УС» (requested_name), чтобы сразу показать семантически релевантные "
            "товары при открытии формы — результат уходит в `recommended`. Передайте `q` для ручного "
            "поиска — результат уходит в `search_results`. Оба массива независимы: без `q` "
            "`search_results` всегда пуст, без `name` пуст `recommended` — это ожидаемое поведение "
            "двух разных источников выдачи, а не признак того, что товаров вообще нет. Хотя бы один "
            "из `name`/`q` обязателен; если переданы оба, в ответе будут оба массива. "
            "`recommended` — всегда top-`limit` по score и `offset` не учитывает; `offset` двигает "
            "только `search_results`, см. `search_results_count`. "
            "`can_link=false` означает, что мастер-товар уже используется другой строкой этой компании."
        ),
        parameters=[ProductSuggestionQuerySerializer],
        responses={
            200: MasterProductSuggestionResponseSerializer,
            400: OpenApiResponse(description="Не переданы name/q либо у компании нет единственного владельца."),
            401: OpenApiResponse(description="Нет JWT или он невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не управляет этой компанией."),
            404: OpenApiResponse(description="Компания не найдена."),
        },
    )
)
class VenueProductMatrixMasterSuggestionsView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id, status=EntityStatus.ACTIVE)
        ser = ProductSuggestionQuerySerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        payload = master_product_suggestions_for_venue(venue=venue, **ser.validated_data)
        return Response(MasterProductSuggestionResponseSerializer(payload).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_company_products_supplier_suggestions",
        tags=["Справочник товаров компании"],
        summary="Рекомендовать или найти товар поставщика перед назначением",
        description=(
            "Основная ручка поля «Товар поставщика». Без `q` ищет релевантные предложения по `name` "
            "(значение поля «Название товара в УС», requested_name); с `q` выполняет ручной поиск. "
            "Возвращает те же карточки SupplierProduct и ту же пагинацию (`count`/`limit`/`offset`), "
            "что общий каталог `GET /catalog/products` — используйте `offset`, чтобы дойти до "
            "совпадений за пределами первой страницы. Пустой `results` при ненулевом запросе означает "
            "не только «нет текстовых совпадений», но и то, что подходящие товары поставщика есть, но "
            "не опубликованы или без цены — тот же фильтр `status=active, is_published=true, "
            "current_price is not null`, что у общего каталога. Назначьте выбранный `id` через PATCH "
            "товара компании в поле `supplier_product_id`; другие компании и мастер-товар не изменятся."
        ),
        parameters=[ProductSuggestionQuerySerializer],
        responses={
            200: _PRODUCT_PAGE_SCHEMA,
            400: OpenApiResponse(description="Не переданы name/q."),
            401: OpenApiResponse(description="Нет JWT или он невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не управляет этой компанией."),
            404: OpenApiResponse(description="Компания не найдена."),
        },
    )
)
class VenueProductMatrixSupplierSuggestionsView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id, status=EntityStatus.ACTIVE)
        ser = ProductSuggestionQuerySerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        query = ser.validated_data.get("q") or ser.validated_data.get("name")
        catalog_query = CatalogProductQuerySerializer(
            data={
                "q": query,
                "venue_id": venue_id,
                "limit": ser.validated_data["limit"],
                "offset": ser.validated_data.get("offset", 0),
            }
        )
        catalog_query.is_valid(raise_exception=True)
        return Response(_catalog_products_page(catalog_query.validated_data, user=request.user, request=request))


@extend_schema_view(
    put=extend_schema(
        operation_id="venue_company_product_set_master_product",
        tags=["Справочник товаров компании"],
        summary="Применить выбор в поле «Мастер-товар»",
        description=(
            "Одна фронтовая операция для трёх действий из прототипа: связать существующий мастер-товар, "
            "создать новый из товара компании или снять связь. Ручное решение очищает `needs_review`. "
            "Если выбранный мастер-товар уже занят другой строкой этой компании, возвращается 409."
        ),
        request=VenueProductMatrixMasterProductDecisionSerializer,
        responses={
            200: VenueProductMatrixItemSerializer,
            400: OpenApiResponse(description="Некорректное сочетание action и полей."),
            401: OpenApiResponse(description="Нет JWT или он невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не управляет этой компанией."),
            404: OpenApiResponse(description="Компания, товар компании или мастер-товар не найдены."),
            409: MasterProductVenueLinkConflictSerializer,
        },
    )
)
class VenueProductMatrixMasterProductView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def put(self, request, venue_id, item_id):
        get_object_or_404(Venue, pk=venue_id, status=EntityStatus.ACTIVE)
        item = get_object_or_404(VenueProductMatrixItem, pk=item_id, venue_id=venue_id, status=EntityStatus.ACTIVE)
        ser = VenueProductMatrixMasterProductDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            item = set_product_matrix_master_product(item=item, **ser.validated_data)
        except MasterProductAlreadyLinkedConflict as exc:
            return Response(
                {
                    "detail": "Этот мастер-товар уже используется другим товаром этой компании.",
                    "candidate": VenueProductMatrixItemSerializer(exc.existing_item).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(VenueProductMatrixItemSerializer(item).data)


@extend_schema_view(
    patch=extend_schema(
        operation_id="venue_company_products_update",
        tags=["Справочник товаров компании"],
        summary="Изменить товар компании",
        description="Тем же PATCH привязывается товар поставщика — передайте `supplier_product_id`.",
        request=VenueProductMatrixItemUpdateSerializer,
        responses={
            200: VenueProductMatrixItemSerializer,
            400: OpenApiResponse(
                description='Ни одно поле не передано: {"non_field_errors": ["At least one field must be provided."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или строки матрицы с таким id нет: {"detail": "Not found."}'),
        },
    ),
    delete=extend_schema(
        operation_id="venue_company_products_archive",
        tags=["Справочник товаров компании"],
        summary="Архивировать товар компании",
        description="Мягкое удаление: строка помечается `archived` и перестаёт "
        "появляться в списке (`GET .../product-matrix`), но не удаляется физически "
        "— история цен, заказов и решений импорта, ссылающихся на неё, сохраняется.",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или строки матрицы с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductMatrixDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def patch(self, request, venue_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        item = get_object_or_404(VenueProductMatrixItem, pk=item_id, venue_id=venue_id)
        ser = VenueProductMatrixItemUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if ser.validated_data.get("subdivision_id"):
            get_object_or_404(
                VenueSubdivision,
                pk=ser.validated_data["subdivision_id"],
                venue_id=venue_id,
                status=EntityStatus.ACTIVE,
            )
        item = update_product_matrix_item(item, **ser.validated_data)
        return Response(VenueProductMatrixItemSerializer(item).data)

    def delete(self, request, venue_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        item = get_object_or_404(VenueProductMatrixItem, pk=item_id, venue_id=venue_id)
        delete_product_matrix_item(item, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Справочник товаров компании"],
        summary="История удаления/архивации товара компании",
        responses={
            200: AuditLogEntrySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или строки матрицы с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductMatrixItemAuditLogView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        get_object_or_404(VenueProductMatrixItem, pk=item_id, venue_id=venue_id)
        return Response(AuditLogEntrySerializer(product_matrix_item_audit_log(item_id), many=True).data)


_SUPPLIER_MATCH_AUTH_RESPONSES = {
    401: OpenApiResponse(
        response=VenueProductMatrixSupplierMatchDetailErrorSerializer,
        description='Нет JWT или он невалиден/просрочен: {"detail": "Учетные данные не были предоставлены."}.',
    ),
    403: OpenApiResponse(
        response=VenueProductMatrixSupplierMatchDetailErrorSerializer,
        description='Пользователь не участник этой компании: {"detail": "Requires a service token, staff account, '
        'or membership of this venue."}.',
    ),
    404: OpenApiResponse(
        response=VenueProductMatrixSupplierMatchDetailErrorSerializer,
        description="Компании нет/архивирована, либо строки справочника с таким id нет в этой компании: "
        '{"detail": "No Venue matches the given query."} / {"detail": "No VenueProductMatrixItem matches the given '
        'query."}.',
    ),
}


def _supplier_match_item(venue_id, item_id):
    get_object_or_404(Venue, pk=venue_id, status=EntityStatus.ACTIVE)
    return get_object_or_404(
        VenueProductMatrixItem.objects.select_related("master_product", "unit", "supplier_match"),
        pk=item_id,
        venue_id=venue_id,
        status=EntityStatus.ACTIVE,
    )


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_company_product_supplier_match_context",
        tags=["Справочник товаров компании"],
        summary="Открыть каталог для подбора товара поставщика",
        description=(
            "TASK-148, клик по «Нашли N вариантов» / «Не нашли» / «Ошибка подбора» в колонке «Товар поставщика». "
            "Возвращает блок «Подбираем товар для» (`source`) и стартовое состояние каталога (`search`: `q` + "
            "`filters` — передать как query-параметры в `GET /catalog/products`, сортировка каталога по "
            "умолчанию). Если подбор был — пересчитывает количество по актуальному каталогу и сохраняет его "
            "(`match`, обновите строку таблицы: «Нашли 12» могло стать «Нашли 10» или «Не нашли»). Без подбора "
            "или с уже выбранным товаром `match` = null, `search.q` = название товара. Товар выбирается через "
            "`POST .../supplier-match/select`."
        ),
        responses={200: VenueProductMatrixSupplierMatchContextSerializer, **_SUPPLIER_MATCH_AUTH_RESPONSES},
    )
)
class VenueProductMatrixSupplierMatchView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id, item_id):
        item = _supplier_match_item(venue_id, item_id)
        user = request.user if isinstance(request.user, User) else None
        match = refresh_supplier_match(item, user=user)
        source = {
            "item_id": item.id,
            "requested_name": item.requested_name,
            "unit_label": item.requested_unit_label or (item.unit.code if item.unit_id else None),
            "master_product_id": item.master_product_id,
            "master_product_name": item.master_product.canonical_name if item.master_product_id else None,
            "note": item.note or None,
        }
        payload = {
            "source": source,
            "search": supplier_match_search(item),
            "match": match if match is not None and not item.supplier_product_id else None,
        }
        return Response(VenueProductMatrixSupplierMatchContextSerializer(payload).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_company_product_supplier_match_select",
        tags=["Справочник товаров компании"],
        summary="Выбрать товар поставщика в режиме подбора",
        description=(
            "TASK-148, кнопка «Выбрать» в каталоге, открытом из справочника. Привязывает товар каталога к строке "
            "справочника на уровне компании (другие компании и мастер-товар не меняются), удаляет состояние "
            "подбора и возвращает обновлённую строку — замените ею строку таблицы без перезагрузки. Уже "
            "сопоставленную строку не перезаписывает (409) — её меняют существующим dropdown "
            "(`PATCH .../product-matrix/{item_id}` с `supplier_product_id`)."
        ),
        request=VenueProductMatrixSupplierMatchSelectSerializer,
        responses={
            200: VenueProductMatrixItemSerializer,
            400: OpenApiResponse(
                response=VenueProductMatrixSupplierMatchSelectErrorSerializer,
                description="supplier_product_id не передан, не UUID или товар не виден в каталоге.",
            ),
            **_SUPPLIER_MATCH_AUTH_RESPONSES,
            409: OpenApiResponse(
                response=VenueProductMatrixSupplierMatchDetailErrorSerializer,
                description='У строки уже выбран товар поставщика: {"detail": "У товара компании уже выбран товар '
                'поставщика. Изменить его можно в колонке «Товар поставщика»."}.',
            ),
        },
    )
)
class VenueProductMatrixSupplierMatchSelectView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, item_id):
        item = _supplier_match_item(venue_id, item_id)
        ser = VenueProductMatrixSupplierMatchSelectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            select_supplier_match_product(item, supplier_product=ser.validated_data["supplier_product"])
        except SupplierMatchAlreadyLinked:
            return Response(
                {
                    "detail": "У товара компании уже выбран товар поставщика. Изменить его можно в колонке "
                    "«Товар поставщика»."
                },
                status=status.HTTP_409_CONFLICT,
            )
        row = venue_product_matrix_queryset(venue_id).get(pk=item.pk)
        return Response(VenueProductMatrixItemSerializer(row, context={"request": request}).data)
