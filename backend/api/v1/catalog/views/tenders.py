"""Tenders: buyer side, supplier side, items, files, offers and distribution."""

from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication, OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    SUPPLIER_PRODUCT_NOT_FOUND_MESSAGE,
    invitable_suppliers_queryset,
    supplier_can_view_tender,
    supplier_tenders_queryset,
    supplier_visible_tenders_queryset,
    tender_completion_summary,
    tender_item_current_products,
    tender_item_distribution_history,
    tender_item_offers_queryset,
    tender_item_venue_prices,
    tender_items_for_supplier_queryset,
    tender_items_with_counters,
    tender_offer_autofill_suggestions,
    tender_venue_availability,
    tenders_queryset,
    tenders_with_counters_queryset,
    user_supplier_companies,
    venue_label,
    with_offer_distribution,
    with_supplier_companies,
)
from api.v1.catalog.serializers import (
    CatalogSupplierSerializer,
    MasterProductTenderOfferRowSerializer,
    SupplierTenderListQuerySerializer,
    SupplierTenderListResponseSerializer,
    SupplierTenderSerializer,
    TenderActiveConflictSerializer,
    TenderChangeSerializer,
    TenderCompletionSummarySerializer,
    TenderCreateSerializer,
    TenderDistributionHistorySerializer,
    TenderDistributionQuerySerializer,
    TenderDistributionRequestSerializer,
    TenderFileSerializer,
    TenderFileUploadSerializer,
    TenderItemCurrentProductSerializer,
    TenderItemForSupplierSerializer,
    TenderItemsCreateSerializer,
    TenderItemSerializer,
    TenderItemUpdateSerializer,
    TenderItemVenuePricesQuerySerializer,
    TenderListQuerySerializer,
    TenderListResponseSerializer,
    TenderOfferAutofillSuggestionSerializer,
    TenderOfferSerializer,
    TenderOfferUpsertSerializer,
    TenderPublishRejectionSerializer,
    TenderSerializer,
    TenderSupplierSearchQuerySerializer,
    TenderUpdateSerializer,
    TenderVenueAvailabilityRequestSerializer,
    TenderVenueAvailabilitySerializer,
    TenderVenuePriceSerializer,
)
from api.v1.catalog.services import (
    ActiveTenderConflict,
    add_tender_items,
    apply_tender_item_distribution,
    complete_tender,
    create_tender_with_items,
    publish_tender,
    publish_tender_item,
    submit_tender_offer,
    sync_tender_completion,
    tender_item_distribution_diff,
    unpublish_tender_item,
    withdraw_tender_offer,
)
from api.v1.catalog.tender_editing import delete_tender_item, update_tender, update_tender_item
from api.v1.catalog.tender_files import add_tender_file, delete_tender_file
from api.v1.catalog.tender_history import supplier_tender_history, tender_history
from api.v1.permissions import (
    CanInitiateAccountTransfer,
    HasTenderAccess,
    HasTenderAccessToOwner,
    IsCoreUser,
)
from catalog.models import (
    Tender,
    TenderFile,
    TenderItem,
    TenderOffer,
)
from core.models import (
    EntityStatus,
    User,
)
from suppliers.models import Supplier, SupplierProduct

from ._shared import _MasterProductPagination


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_list",
        tags=["Тендеры"],
        summary="Тендеры владельца",
        description="Раздел «Тендеры» покупателя: тендеры владельца со счётчиками, новые сверху (§45); q — поиск "
        "по номеру и названию.",
        parameters=[
            TenderListQuerySerializer,
            OpenApiParameter(name="page", type=OpenApiTypes.INT, required=False, description="Номер страницы."),
            OpenApiParameter(
                name="page_size",
                type=OpenApiTypes.INT,
                required=False,
                description="Размер страницы (макс. 100, по умолчанию 20).",
            ),
        ],
        responses={
            200: TenderListResponseSerializer,
            400: OpenApiResponse(
                description='owner_id не передан либо не UUID: {"owner_id": ["This field is required."]} ИЛИ '
                '{"owner_id": ["Must be a valid UUID."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не "
                "owner/manager ни одной venue этого владельца."
            ),
        },
    ),
    post=extend_schema(
        operation_id="tender_create",
        tags=["Тендеры"],
        summary="Создать тендер",
        description="Raw ТЗ §6: создаёт только сам тендер (черновик, §14 — не виден поставщикам). "
        "Добавление товаров — отдельная, пока не реализованная ручка (§9).",
        request=TenderCreateSerializer,
        responses={
            201: TenderSerializer,
            400: OpenApiResponse(
                description='Не передано обязательное поле: {"name": ["This field is required."]} ИЛИ '
                "invited_suppliers пуст при access=invite_only / непуст при access=open: "
                '{"invited_suppliers": ["..."]}  ИЛИ owner_id не передан/не UUID: '
                '{"owner_id": ["This field is required."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не "
                "owner/manager ни одной venue этого владельца."
            ),
        },
    ),
)
class TenderListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccessToOwner]

    def get(self, request):
        query_ser = TenderListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        owner_id = query_ser.validated_data["owner_id"]
        queryset = tenders_queryset(owner_id, q=query_ser.validated_data.get("q"))
        paginator = _MasterProductPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        for tender in page:
            sync_tender_completion(tender)
        return paginator.get_paginated_response(TenderSerializer(page, many=True).data)

    def post(self, request):
        ser = TenderCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tender = create_tender_with_items(**ser.validated_data, actor=request.user)
        item = tenders_queryset(tender.created_by_id).get(pk=tender.pk)
        return Response(TenderSerializer(item).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_retrieve",
        tags=["Тендеры"],
        summary="Карточка тендера",
        description=(
            "Raw ТЗ §17: тот же объект, что строка списка тендеров, со счётчиками товаров и предложений. "
            "Срок приёма проверяется при чтении: тендер с истёкшим сроком приходит уже завершённым."
        ),
        responses={
            200: TenderSerializer,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Пользователь не admin, не владелец и не owner/manager заведения этого аккаунта. "
                "Такой пользователь получает 403 и на несуществующий тендер."
            ),
            404: OpenApiResponse(description='Тендера с таким id нет (ответ admin): {"detail": "Not found."}'),
        },
    ),
    patch=extend_schema(
        operation_id="tender_update",
        tags=["Тендеры"],
        summary="Изменить тендер",
        description=(
            "Raw ТЗ §14/§16, ADR-0009. Все поля необязательны; значение, равное текущему, правкой не считается — "
            "можно отправлять форму целиком. Черновик правится свободно, по правилам создания доступа и "
            "приглашённых. У опубликованного тендера: название и описание — свободно, срок — только продлить или "
            "снять, приглашённых — только добавить, доступ не меняется. Завершённый тендер не редактируется; срок "
            "приёма проверяется до правки."
        ),
        request=TenderUpdateSerializer,
        responses={
            200: TenderSerializer,
            400: OpenApiResponse(
                description='Тендер завершён: {"detail": "..."}; либо правка вне §16 — ошибка под именем поля: '
                "accepts_offers_until, access, invited_suppliers."
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Пользователь не admin, не владелец и не owner/manager заведения этого аккаунта. "
                "Такой пользователь получает 403 и на несуществующий тендер."
            ),
            404: OpenApiResponse(description='Тендера с таким id нет (ответ admin): {"detail": "Not found."}'),
        },
    ),
)
class TenderDetailView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id):
        tender = get_object_or_404(tenders_with_counters_queryset(), pk=tender_id)
        sync_tender_completion(tender)
        return Response(TenderSerializer(tender).data)

    def patch(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        ser = TenderUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        update_tender(tender=tender, data=ser.validated_data, actor=request.user)
        return Response(TenderSerializer(tenders_with_counters_queryset().get(pk=tender.pk)).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_history",
        tags=["Тендеры"],
        summary="История тендера",
        description=(
            "Raw ТЗ §16, §76, ADR-0010: создание, публикация, завершение и правки опубликованного тендера и его "
            "товаров — кто, когда, старое и новое значение. Одна строка — одно событие или одно поле; новые "
            "сверху, без пагинации. Правки черновика не пишутся. Срок приёма проверяется при чтении."
        ),
        responses={
            200: TenderChangeSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Пользователь не admin, не владелец и не owner/manager заведения этого аккаунта. "
                "Такой пользователь получает 403 и на несуществующий тендер."
            ),
            404: OpenApiResponse(description='Тендера с таким id нет (ответ admin): {"detail": "Not found."}'),
        },
    )
)
class TenderHistoryView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        return Response(TenderChangeSerializer(tender_history(tender.id), many=True).data)


_TENDER_FILE_ERRORS = {
    401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
    403: OpenApiResponse(
        description="Пользователь не admin, не владелец и не owner/manager заведения этого аккаунта. "
        "Такой пользователь получает 403 и на несуществующий тендер."
    ),
}


@extend_schema_view(
    post=extend_schema(
        operation_id="tender_file_create",
        tags=["Тендеры"],
        summary="Добавить файл к тендеру",
        description=(
            "Raw ТЗ §6, §16, ADR-0011: документ или спецификация покупателя. Один файл за запрос, до 20 МБ, "
            "PDF, DOC, DOCX, XLS, XLSX, JPG, PNG, до 20 файлов на тендер. Черновик и опубликованный тендер."
        ),
        request={"multipart/form-data": TenderFileUploadSerializer},
        responses={
            201: TenderFileSerializer,
            400: OpenApiResponse(
                description='Формат или размер не подходят, файлов уже 20, тендер завершён: {"detail": "..."} — '
                'текст для показа; файл не передан: {"file": ["..."]}'
            ),
            **_TENDER_FILE_ERRORS,
            404: OpenApiResponse(description='Тендера с таким id нет (ответ admin): {"detail": "Not found."}'),
        },
    )
)
class TenderFileCreateView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        ser = TenderFileUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tender_file = add_tender_file(tender=tender, uploaded_file=ser.validated_data["file"], actor=request.user)
        return Response(TenderFileSerializer(tender_file).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        operation_id="tender_file_delete",
        tags=["Тендеры"],
        summary="Удалить файл тендера",
        description="Raw ТЗ §16, ADR-0011: запись и объект в хранилище удаляются, ссылка перестаёт открываться.",
        request=None,
        responses={
            204: OpenApiResponse(description="Файл удалён."),
            400: OpenApiResponse(description='Тендер завершён: {"detail": "..."}'),
            **_TENDER_FILE_ERRORS,
            404: OpenApiResponse(
                description='Файла с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    )
)
class TenderFileDetailView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def delete(self, request, tender_id, file_id):
        tender_file = get_object_or_404(TenderFile.objects.select_related("tender"), pk=file_id, tender_id=tender_id)
        sync_tender_completion(tender_file.tender)
        delete_tender_file(tender_file=tender_file, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_supplier_search",
        tags=["Тендеры"],
        summary="Поставщики для приглашения в тендер",
        description=(
            "Raw ТЗ §8, ADR-0014: поиск поставщика по названию или ИНН для закрытого тендера — все активные "
            "опубликованные поставщики, в том числе без прайса (в отличие от фильтра каталога). По названию, "
            "без пагинации."
        ),
        parameters=[TenderSupplierSearchQuerySerializer],
        responses={
            200: CatalogSupplierSerializer(many=True),
            400: OpenApiResponse(description='q длиннее 200 символов: {"q": ["..."]}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
        },
    )
)
class TenderSupplierSearchView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query = TenderSupplierSearchQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        suppliers = invitable_suppliers_queryset(q=query.validated_data.get("q"))
        return Response(CatalogSupplierSerializer(suppliers, many=True).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="tender_venue_availability",
        tags=["Тендеры"],
        summary="Доступность заведений для позиций будущего тендера",
        description=(
            "ТЗ «Мастер-справочник — создание тендера» §6–§11, ADR-0007. Только чтение, ничего не пишет; POST — "
            "из-за длины списка мастер-товаров. По каждому мастер-товару — все действующие заведения аккаунта: "
            "linked (товар используется в заведении) и активный тендер, который уже держит связку. Автоподбор = "
            "linked и не занято; «N недоступны» = linked и занято; в ручном выборе занятые недоступны."
        ),
        request=TenderVenueAvailabilityRequestSerializer,
        responses={
            200: TenderVenueAvailabilitySerializer(many=True),
            400: OpenApiResponse(
                description="Пустой список, больше 200 мастер-товаров, либо мастер-товар не этого владельца: "
                '{"master_product_ids": ["..."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не owner/manager "
                "ни одного заведения этого владельца."
            ),
        },
    )
)
class TenderVenueAvailabilityView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccessToOwner]

    def post(self, request):
        ser = TenderVenueAvailabilityRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = tender_venue_availability(ser.validated_data["owner_id"], ser.validated_data["master_product_ids"])
        return Response(TenderVenueAvailabilitySerializer(rows, many=True).data)


def _core_user(request):
    """Supplier tender views also accept the service token, whose request.user is not a core user."""
    return request.user if isinstance(request.user, User) else None


def _active_tender_conflict_response(conflicts):
    """Raw ТЗ §12 — shared by TenderItemListCreateView.post and
    TenderPublishView.post, the two points a TenderItem can become active."""
    lines = [
        f"{c['master_product'].canonical_name} + {venue_label(c['venue'])} уже участвует в Тендере "
        f"№{c['tender'].number} «{c['tender'].name}»."
        for c in conflicts
    ]
    return Response(
        {
            "detail": " ".join(lines),
            "conflicts": [
                {
                    "master_product_id": c["master_product"].id,
                    "master_product_name": c["master_product"].canonical_name,
                    "venue_id": c["venue"].id,
                    "venue_name": venue_label(c["venue"]),
                    "tender_id": c["tender"].id,
                    "tender_number": c["tender"].number,
                    "tender_name": c["tender"].name,
                }
                for c in conflicts
            ],
        },
        status=status.HTTP_409_CONFLICT,
    )


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_item_list",
        tags=["Тендеры"],
        summary="Список позиций тендера",
        description="Все товарные позиции тендера на одной странице (raw ТЗ §71) — без пагинации.",
        responses={
            200: TenderItemSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Тендера с таким id нет: {"detail": "Not found."} — только для admin; обычный '
                "пользователь на несуществующий тендер получает 403."
            ),
        },
    ),
    post=extend_schema(
        operation_id="tender_item_create",
        tags=["Тендеры"],
        summary="Добавить позиции в тендер",
        description="Можно добавить сразу несколько мастер-товаров за один запрос (raw ТЗ §9).",
        request=TenderItemsCreateSerializer,
        responses={
            201: TenderItemSerializer(many=True),
            400: OpenApiResponse(
                description="Мастер-товар не принадлежит владельцу тендера, ИЛИ уже добавлен в этот тендер, ИЛИ "
                'заведение не принадлежит владельцу тендера: {"venue_ids": ["..."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Тендера с таким id нет: {"detail": "Not found."} — только для admin; обычный '
                "пользователь на несуществующий тендер получает 403."
            ),
            409: OpenApiResponse(
                response=TenderActiveConflictSerializer,
                description="Raw ТЗ §12: тендер уже активен, и хотя бы одна связка мастер-товар+заведение уже "
                "участвует в другом активном тендере.",
            ),
        },
    ),
)
class TenderItemListCreateView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id):
        get_object_or_404(Tender, pk=tender_id)
        items = tender_items_with_counters(tender_id)
        return Response(TenderItemSerializer(items, many=True).data)

    def post(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        ser = TenderItemsCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        resolved = [
            {
                "master_product": entry["master_product"],
                "product_kind_label": entry.get("product_kind_label"),
                "comment": entry.get("comment"),
                "venue_ids": entry.get("venue_ids"),
            }
            for entry in ser.validated_data["items"]
        ]
        try:
            created = add_tender_items(tender=tender, items=resolved, actor=request.user)
        except ActiveTenderConflict as exc:
            return _active_tender_conflict_response(exc.conflicts)
        items = tender_items_with_counters(tender.id, item_ids=[item.id for item in created])
        return Response(TenderItemSerializer(items, many=True).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        operation_id="tender_item_update",
        tags=["Тендеры"],
        summary="Изменить позицию тендера",
        description=(
            "Raw ТЗ §14/§16, ADR-0009. Все поля необязательны; значение, равное текущему, правкой не считается. "
            "Комментарий и вид товара — свободно. Заведения: в черновике — любой список, включая пустой; у "
            "опубликованного тендера — только добавить, новые проверяются на конфликт с другими активными "
            "тендерами (§12). Завершённый тендер не редактируется."
        ),
        request=TenderItemUpdateSerializer,
        responses={
            200: TenderItemSerializer,
            400: OpenApiResponse(
                description='Тендер завершён: {"detail": "..."}; либо заведение убрано или не принадлежит '
                'владельцу тендера: {"venue_ids": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
            409: OpenApiResponse(
                response=TenderActiveConflictSerializer,
                description="Raw ТЗ §12: добавленное заведение уже участвует с этим мастер-товаром в другом "
                "активном тендере. Позиция не меняется.",
            ),
        },
    ),
    delete=extend_schema(
        operation_id="tender_item_delete",
        tags=["Тендеры"],
        summary="Удалить позицию из черновика",
        description=(
            "Raw ТЗ §14, ADR-0009: позиция удаляется только из черновика — у него не бывает предложений. "
            "В опубликованном тендере позицию снимают с публикации (…/unpublish)."
        ),
        request=None,
        responses={
            204: OpenApiResponse(description="Позиция удалена."),
            400: OpenApiResponse(description='Тендер опубликован или завершён: {"detail": "..."}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    ),
)
class TenderItemDetailView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def patch(self, request, tender_id, item_id):
        item = get_object_or_404(TenderItem.objects.select_related("tender"), pk=item_id, tender_id=tender_id)
        sync_tender_completion(item.tender)
        ser = TenderItemUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            update_tender_item(item=item, data=ser.validated_data, actor=request.user)
        except ActiveTenderConflict as exc:
            return _active_tender_conflict_response(exc.conflicts)
        items = tender_items_with_counters(tender_id, item_ids=[item.id])
        return Response(TenderItemSerializer(items[0]).data)

    def delete(self, request, tender_id, item_id):
        item = get_object_or_404(TenderItem.objects.select_related("tender"), pk=item_id, tender_id=tender_id)
        sync_tender_completion(item.tender)
        delete_tender_item(item)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_item_distribution_history",
        tags=["Тендеры"],
        summary="История распределений товара тендера",
        description=(
            "Raw ТЗ §76 «Распределение»: какой поставщик, товар, цена, заведение, когда и кем — по каждому "
            "распределению, новые сверху, включая заменённые. Строится по индивидуальным ценам из тендера, поэтому "
            "показывает и распределения, сделанные до появления этой ручки."
        ),
        responses={
            200: TenderDistributionHistorySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    )
)
class TenderItemDistributionHistoryView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id, item_id):
        item = get_object_or_404(TenderItem, pk=item_id, tender_id=tender_id)
        return Response(TenderDistributionHistorySerializer(tender_item_distribution_history(item), many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_item_current_products",
        tags=["Тендеры"],
        summary="Текущий товар по заведениям позиции",
        description=(
            "Для окна «Предложения по товару»: что сейчас стоит в справочнике каждого заведения позиции — чей товар "
            "и почём (индивидуальная цена, иначе базовая), по названию заведения. Нет товара — поля поставщика и "
            "цены null (ADR-0016)."
        ),
        responses={
            200: TenderItemCurrentProductSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    )
)
class TenderItemCurrentProductsView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id, item_id):
        item = get_object_or_404(TenderItem.objects.prefetch_related("venues"), pk=item_id, tender_id=tender_id)
        return Response(TenderItemCurrentProductSerializer(tender_item_current_products(item), many=True).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="tender_item_publish",
        tags=["Тендеры"],
        summary="Опубликовать позицию тендера",
        description="Raw ТЗ §18/§75 (TASK-59): возвращает позицию в состояние «Опубликован». "
        "Идемпотентно — повторный вызов на уже опубликованной позиции не ошибка.",
        request=None,
        responses={
            200: TenderItemSerializer,
            400: OpenApiResponse(description='Тендер завершён: {"detail": "..."}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    )
)
class TenderItemPublishView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def post(self, request, tender_id, item_id):
        item = get_object_or_404(TenderItem.objects.select_related("tender"), pk=item_id, tender_id=tender_id)
        sync_tender_completion(item.tender)
        publish_tender_item(item, actor=request.user)
        return Response(TenderItemSerializer(tender_items_with_counters(tender_id, item_ids=[item.id])[0]).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="tender_item_unpublish",
        tags=["Тендеры"],
        summary="Снять позицию тендера с публикации",
        description="Raw ТЗ §18/§75 (TASK-59): скрывает позицию от поставщиков без физического "
        "удаления — история предложений сохраняется. Идемпотентно.",
        request=None,
        responses={
            200: TenderItemSerializer,
            400: OpenApiResponse(description='Тендер завершён: {"detail": "..."}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    )
)
class TenderItemUnpublishView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def post(self, request, tender_id, item_id):
        item = get_object_or_404(TenderItem.objects.select_related("tender"), pk=item_id, tender_id=tender_id)
        sync_tender_completion(item.tender)
        unpublish_tender_item(item, actor=request.user)
        return Response(TenderItemSerializer(tender_items_with_counters(tender_id, item_ids=[item.id])[0]).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="tender_publish",
        tags=["Тендеры"],
        summary="Опубликовать тендер",
        description="Черновик (draft) → активен (active) — raw ТЗ §15. Необратимо в рамках этой ручки.",
        request=None,
        responses={
            200: TenderSerializer,
            400: OpenApiResponse(
                response=TenderPublishRejectionSerializer,
                description="Тендер уже не в статусе draft, либо у него нет ни одной позиции, либо у опубликованной "
                "позиции нет заведений (§16) — тогда tender_item_ids перечисляет такие позиции.",
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Тендера с таким id нет: {"detail": "Not found."} — только для admin; обычный '
                "пользователь на несуществующий тендер получает 403."
            ),
            409: OpenApiResponse(
                response=TenderActiveConflictSerializer,
                description="Raw ТЗ §12: хотя бы одна связка мастер-товар+заведение этого тендера уже участвует "
                "в другом активном тендере — публикация отклонена.",
            ),
        },
    )
)
class TenderPublishView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def post(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        try:
            publish_tender(tender, actor=request.user)
        except ActiveTenderConflict as exc:
            return _active_tender_conflict_response(exc.conflicts)
        return Response(TenderSerializer(tenders_with_counters_queryset().get(pk=tender.pk)).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_complete_summary",
        tags=["Тендеры"],
        summary="Сводка для модалки завершения тендера",
        description=(
            "Raw ТЗ §41: «Товаров / Предложений / Распределено / Не распределено» перед подтверждением "
            "завершения. Только чтение, ничего не пишет. Распределено/Не распределено считается по текущему "
            "состоянию Справочника компании (отдельного журнала распределений в первой версии нет)."
        ),
        responses={
            200: TenderCompletionSummarySerializer,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Тендера с таким id нет: {"detail": "Not found."} — только для admin; обычный '
                "пользователь на несуществующий тендер получает 403."
            ),
        },
    ),
    post=extend_schema(
        operation_id="tender_complete",
        tags=["Тендеры"],
        summary="Завершить тендер",
        description=(
            "Raw ТЗ §40-42: активен → завершён, вручную. После этого поставщики не могут отправлять/менять "
            "предложения, но сохраняют доступ на чтение (историю); распределение больше не производится."
        ),
        request=None,
        responses={
            200: TenderSerializer,
            400: OpenApiResponse(description='Тендер не активен (черновик или уже завершён): {"detail": "..."}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Тендера с таким id нет: {"detail": "Not found."} — только для admin; обычный '
                "пользователь на несуществующий тендер получает 403."
            ),
        },
    ),
)
class TenderCompleteView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        return Response(TenderCompletionSummarySerializer(tender_completion_summary(tender)).data)

    def post(self, request, tender_id):
        tender = get_object_or_404(Tender, pk=tender_id)
        complete_tender(tender, actor=request.user)
        return Response(TenderSerializer(tenders_with_counters_queryset().get(pk=tender.pk)).data)


@extend_schema_view(
    put=extend_schema(
        operation_id="supplier_tender_offer_submit",
        tags=["Тендеры"],
        summary="Подать/изменить предложение поставщика по позиции тендера",
        description="Raw ТЗ §35: повторный вызов обновляет текущее предложение, новое не создаётся.",
        request=TenderOfferUpsertSerializer,
        responses={
            200: TenderOfferSerializer,
            400: OpenApiResponse(
                description='Тендер недоступен поставщику (не активен / не приглашён): {"detail": "..."}; товар чужой '
                'компании, архивный или удалён: {"supplier_product_id": ["Товар не найден в каталоге компании."]}; '
                'заведение не входит в позицию или уже назначено покупателем: {"venue_ids": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description='Поставщика, тендера или позиции с таким id нет: {"detail": "Not found."}'
            ),
        },
    ),
    delete=extend_schema(
        operation_id="supplier_tender_offer_withdraw",
        tags=["Тендеры"],
        summary="Снять предложение поставщика по позиции тендера",
        description=(
            "Raw ТЗ §67 «Не предлагаем» для уже отправленного предложения (ADR-0006): запись не удаляется, "
            "получает withdrawn_at. Покупатель её больше не видит, в счётчиках она не участвует; поставщик видит "
            "своё снятое в my_offer. «Вернуть в предложения» — обычная подача PUT, та же запись. Повторное снятие "
            "не ошибка, момент первого снятия сохраняется."
        ),
        request=None,
        responses={
            200: TenderOfferSerializer,
            400: OpenApiResponse(
                description='Тендер недоступен поставщику (не активен / не приглашён): {"detail": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description="Поставщика, тендера или позиции с таким id нет, либо этот поставщик ничего не "
                'отправлял по позиции: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierTenderOfferView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def put(self, request, supplier_id, tender_id, item_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        item = get_object_or_404(TenderItem, pk=item_id, tender_id=tender_id, published=True)
        sync_tender_completion(item.tender)
        ser = TenderOfferUpsertSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        offer = submit_tender_offer(
            tender_item=item,
            supplier=supplier,
            supplier_product=data["supplier_product"],
            price=data["price"],
            currency=data["currency"],
            venue_ids=data["venue_ids"],
            comment=data.get("comment"),
            actor=_core_user(request),
        )
        return Response(TenderOfferSerializer(with_offer_distribution([offer])[0]).data)

    def delete(self, request, supplier_id, tender_id, item_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        item = get_object_or_404(TenderItem, pk=item_id, tender_id=tender_id, published=True)
        sync_tender_completion(item.tender)
        offer = get_object_or_404(TenderOffer, tender_item=item, supplier=supplier)
        withdrawn = withdraw_tender_offer(offer, actor=_core_user(request))
        return Response(TenderOfferSerializer(with_offer_distribution([withdrawn])[0]).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_tender_list",
        tags=["Тендеры"],
        summary="Тендеры, доступные поставщику",
        description=(
            "Raw ТЗ §44-51: открытые тендеры и закрытые, куда приглашён этот поставщик; черновиков нет. "
            "Новые сверху (§45). Вкладка status — по фактическому статусу: тендер с истёкшим сроком уже в "
            "completed. Строка — тот же SupplierTender, что заголовок отклика; сведений о других поставщиках "
            "нет (§51)."
        ),
        parameters=[
            SupplierTenderListQuerySerializer,
            OpenApiParameter(name="page", type=OpenApiTypes.INT, required=False, description="Номер страницы."),
            OpenApiParameter(
                name="page_size",
                type=OpenApiTypes.INT,
                required=False,
                description="Размер страницы (макс. 100, по умолчанию 20).",
            ),
        ],
        responses={
            200: SupplierTenderListResponseSerializer,
            400: OpenApiResponse(description='status не active/completed: {"status": ["..."]}'),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
        },
    )
)
class SupplierTenderListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        query = SupplierTenderListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = supplier_visible_tenders_queryset(
            [supplier.id],
            status=query.validated_data["status"],
            q=query.validated_data.get("q"),
            my_work=query.validated_data.get("my_work"),
        )
        paginator = _MasterProductPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        for tender in page:
            sync_tender_completion(tender)
        return paginator.get_paginated_response(SupplierTenderSerializer(page, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_mine_tender_list",
        tags=["Тендеры"],
        summary="Тендеры всех компаний поставщика пользователя",
        description=(
            "Raw ТЗ §44-51 без выбора компании: открытые тендеры и закрытые, куда приглашена любая из активных "
            "компаний пользователя, без дублей. Вкладки, поиск, пагинация и порядок — как у supplier_tender_list. "
            "В строке — какие из его компаний видят тендер и сколько позиций каждая ответила; отклик — через "
            "ручки с id выбранной компании. Нет компаний — пустой список."
        ),
        parameters=[
            SupplierTenderListQuerySerializer,
            OpenApiParameter(name="page", type=OpenApiTypes.INT, required=False, description="Номер страницы."),
            OpenApiParameter(
                name="page_size",
                type=OpenApiTypes.INT,
                required=False,
                description="Размер страницы (макс. 100, по умолчанию 20).",
            ),
        ],
        responses={
            200: SupplierTenderListResponseSerializer,
            400: OpenApiResponse(
                description='status не active/completed или my_work не из списка: {"status": ["..."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
        },
    )
)
class SupplierMineTenderListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query = SupplierTenderListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        companies = user_supplier_companies(request.user)
        queryset = supplier_visible_tenders_queryset(
            [company.id for company in companies],
            status=query.validated_data["status"],
            q=query.validated_data.get("q"),
            my_work=query.validated_data.get("my_work"),
        )
        if not companies:  # an open tender is visible to every supplier, not to a user with no supplier company
            queryset = queryset.none()
        paginator = _MasterProductPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        for tender in page:
            sync_tender_completion(tender)
        with_supplier_companies(page, companies)
        return paginator.get_paginated_response(SupplierTenderSerializer(page, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_tender_retrieve",
        tags=["Тендеры"],
        summary="Заголовок тендера для поставщика",
        description=(
            "Raw ТЗ §53: шапка страницы отклика — открывается и по прямой ссылке, без списка тендеров. "
            "Заказчик — заведения покупателя по опубликованным товарам (ADR-0003). Сведений о других "
            "поставщиках нет (§51). Срок приёма проверяется при чтении: истёкший тендер приходит завершённым."
        ),
        responses={
            200: SupplierTenderSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description="Поставщика/тендера нет, либо тендер ещё черновик, либо это invite_only-тендер, "
                'в который поставщик не приглашён: {"detail": "Not found."}'
            ),
        },
    )
)
class SupplierTenderDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id, tender_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        tender = get_object_or_404(supplier_tenders_queryset([supplier.id]), pk=tender_id)
        sync_tender_completion(tender)
        if not supplier_can_view_tender(supplier=supplier, tender=tender):
            raise Http404
        return Response(SupplierTenderSerializer(tender).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_tender_item_list",
        tags=["Тендеры"],
        summary="Позиции тендера, доступные поставщику",
        description=(
            "Raw ТЗ §52: все позиции на одной странице, с текущим предложением этого поставщика (если есть). "
            "Виден активный ИЛИ завершённый тендер (§42 — поставщик хранит доступ к истории после завершения), "
            "черновик — никогда."
        ),
        responses={
            200: TenderItemForSupplierSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description="Поставщика/тендера нет, либо тендер ещё черновик, либо это invite_only-тендер, "
                'в который поставщик не приглашён: {"detail": "Not found."}'
            ),
        },
    )
)
class SupplierTenderItemListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id, tender_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        if not supplier_can_view_tender(supplier=supplier, tender=tender):
            raise Http404
        items = list(tender_items_for_supplier_queryset(tender_id, supplier_id))
        with_offer_distribution([offer for item in items for offer in item.my_offers])
        return Response(TenderItemForSupplierSerializer(items, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_tender_history",
        tags=["Тендеры"],
        summary="История тендера для поставщика",
        description=(
            "Та же история, что у покупателя (§16, §76, ADR-0010), новые сверху. Без правок списка приглашённых "
            "(§51: ничего о других поставщиках), без событий товаров, снятых сейчас с публикации (§75), и без "
            "автора: actor_id и actor_name всегда null — показывать «Покупатель»."
        ),
        responses={
            200: TenderChangeSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description="Поставщика/тендера нет, либо тендер ещё черновик, либо это invite_only-тендер, "
                'в который поставщик не приглашён: {"detail": "Not found."}'
            ),
        },
    )
)
class SupplierTenderHistoryView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id, tender_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        if not supplier_can_view_tender(supplier=supplier, tender=tender):
            raise Http404
        rows = TenderChangeSerializer(supplier_tender_history(tender.id, supplier.id), many=True).data
        # docs/adr/0010: the buyer's staff stay anonymous to suppliers.
        return Response([{**row, "actor_id": None, "actor_name": None} for row in rows])


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_tender_offer_autofill",
        tags=["Тендеры"],
        summary="Подсказка для кнопки «Заполнить автоматически» (все позиции тендера)",
        description=(
            "Raw ТЗ §62-64, минимальный v1-срез: подсказка строится ТОЛЬКО по уже существующим связям "
            "мастер-товара с товарами этого поставщика (VenueProductMatrixItem) — без fuzzy-поиска по "
            "названию/характеристикам (отдельный будущий гэп). Заведения в подсказке — всегда полный "
            "список заведений позиции (автовыбор подтверждён Павлом, backlog#task-67), не подмножество. "
            "Ничего не пишет — TenderOffer создаётся только через уже существующий PUT .../offer."
        ),
        responses={
            200: TenderOfferAutofillSuggestionSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description="Поставщика/тендера нет, либо тендер ещё черновик, либо это invite_only-тендер, "
                'в который поставщик не приглашён: {"detail": "Not found."}'
            ),
        },
    )
)
class SupplierTenderOfferAutofillView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id, tender_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        if not supplier_can_view_tender(supplier=supplier, tender=tender):
            raise Http404
        suggestions = tender_offer_autofill_suggestions(tender_id=tender_id, supplier=supplier)
        return Response(TenderOfferAutofillSuggestionSerializer(suggestions, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_tender_item_venue_prices",
        tags=["Тендеры"],
        summary="Цены выбранного товара по заведениям позиции",
        description=(
            "Raw ТЗ §58-60: для товара, который поставщик выбрал в строке отклика сам, — текущая цена по каждому "
            "заведению позиции, индивидуальная или базовая, как venue_prices в подсказке автозаполнения. "
            "Минимум из них — цена для подстановки (§59). Ничего не пишет."
        ),
        parameters=[TenderItemVenuePricesQuerySerializer],
        responses={
            200: TenderVenuePriceSerializer(many=True),
            400: OpenApiResponse(
                description="supplier_product_id не передан, либо это не действующий товар этого поставщика: "
                '{"supplier_product_id": ["..."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description="Поставщика, тендера или позиции нет, тендер ещё черновик, позиция снята с публикации, "
                'либо это invite_only-тендер, в который поставщик не приглашён: {"detail": "Not found."}'
            ),
        },
    )
)
class SupplierTenderItemVenuePricesView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id, tender_id, item_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        tender = get_object_or_404(Tender, pk=tender_id)
        sync_tender_completion(tender)
        if not supplier_can_view_tender(supplier=supplier, tender=tender):
            raise Http404
        item = get_object_or_404(
            TenderItem.objects.prefetch_related("venues"), pk=item_id, tender=tender, published=True
        )
        query = TenderItemVenuePricesQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        product = SupplierProduct.objects.filter(
            pk=query.validated_data["supplier_product_id"], supplier=supplier, status=EntityStatus.ACTIVE
        ).first()
        if product is None:
            raise ValidationError({"supplier_product_id": [SUPPLIER_PRODUCT_NOT_FOUND_MESSAGE]})
        prices = tender_item_venue_prices(item=item, supplier_product=product)
        return Response(TenderVenuePriceSerializer(prices, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_distribution_preview",
        tags=["Тендеры"],
        summary="Превью распределения предложения по заведениям",
        description="Только чтение — raw ТЗ §24-26 «Сейчас / Будет», ничего не пишет.",
        parameters=[TenderDistributionQuerySerializer],
        responses={
            200: MasterProductTenderOfferRowSerializer(many=True),
            400: OpenApiResponse(
                description="Заведение не входит в пересечение позиции и предложения, ИЛИ мастер-товар там ещё "
                'не привязан к собственному справочнику: {"venue_ids": ["..."]}, ИЛИ товар поставщика в '
                'этом предложении удалён: {"detail": "Товар поставщика в этом предложении удалён — '
                'распределение невозможно."}, ИЛИ поставщик снял предложение (§67): {"detail": "Поставщик снял '
                'это предложение — распределение невозможно."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции или предложения с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    ),
    post=extend_schema(
        operation_id="tender_distribution_apply",
        tags=["Тендеры"],
        summary="Применить распределение предложения по заведениям",
        description="Назначает товар поставщика и фиксирует индивидуальную цену (raw ТЗ §30-31), атомарно.",
        request=TenderDistributionRequestSerializer,
        responses={
            200: MasterProductTenderOfferRowSerializer(many=True),
            400: OpenApiResponse(
                description="Заведение не входит в пересечение позиции и предложения, ИЛИ мастер-товар там ещё "
                'не привязан к собственному справочнику: {"venue_ids": ["..."]}, ИЛИ товар поставщика в '
                'этом предложении удалён: {"detail": "Товар поставщика в этом предложении удалён — '
                'распределение невозможно."}, ИЛИ поставщик снял предложение (§67): {"detail": "Поставщик снял '
                'это предложение — распределение невозможно."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции или предложения с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    ),
)
class TenderDistributionView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def _get_offer(self, tender_id, item_id, offer_id):
        return get_object_or_404(TenderOffer, pk=offer_id, tender_item_id=item_id, tender_item__tender_id=tender_id)

    def get(self, request, tender_id, item_id, offer_id):
        offer = self._get_offer(tender_id, item_id, offer_id)
        sync_tender_completion(offer.tender_item.tender)
        query_ser = TenderDistributionQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        rows = tender_item_distribution_diff(offer=offer, venue_ids=query_ser.validated_data["venue_ids"])
        return Response(MasterProductTenderOfferRowSerializer(rows, many=True).data)

    def post(self, request, tender_id, item_id, offer_id):
        offer = self._get_offer(tender_id, item_id, offer_id)
        sync_tender_completion(offer.tender_item.tender)
        ser = TenderDistributionRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = apply_tender_item_distribution(
            offer=offer, venue_ids=ser.validated_data["venue_ids"], created_by=request.user
        )
        return Response(MasterProductTenderOfferRowSerializer(rows, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_item_offers_list",
        tags=["Тендеры"],
        summary="Предложения поставщиков по одной позиции тендера",
        description="Raw ТЗ §20-21 — по одной строке на поставщика, самое свежее изменение первым.",
        responses={
            200: TenderOfferSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Пользователь не admin, не владелец и не owner/manager этого тендера."),
            404: OpenApiResponse(
                description='Позиции с таким id нет в тендере: {"detail": "Not found."}; '
                "несуществующий тендер admin получает как 404, обычный пользователь — как 403."
            ),
        },
    )
)
class TenderItemOffersView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasTenderAccess]

    def get(self, request, tender_id, item_id):
        get_object_or_404(TenderItem, pk=item_id, tender_id=tender_id)
        offers = with_offer_distribution(tender_item_offers_queryset(item_id))
        return Response(TenderOfferSerializer(offers, many=True).data)
