"""Master catalog: products, imports, venue links and bulk actions."""

from django.conf import settings
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
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.catalog.import_sessions import (
    catalog_import_async_extraction,
    persist_catalog_import_session,
)
from api.v1.catalog.importers import parse_catalog_file
from api.v1.catalog.selectors import (
    _parse_import_decimal,
    master_product_audit_log,
    master_product_bulk_supplier_product_diff,
    master_product_detail_queryset,
    master_product_tender_offer_diff,
    master_product_venue_breakdown,
    master_products_queryset,
    resolve_master_product_import_rows,
)
from api.v1.catalog.serializers import (
    AuditLogEntrySerializer,
    MasterProductBulkCreateResponseSerializer,
    MasterProductBulkCreateSerializer,
    MasterProductBulkDeleteResponseSerializer,
    MasterProductBulkDeleteSerializer,
    MasterProductBulkSupplierProductAssignSerializer,
    MasterProductBulkSupplierProductRowSerializer,
    MasterProductCreateSerializer,
    MasterProductImportApplyErrorSerializer,
    MasterProductImportApplyRequestSerializer,
    MasterProductImportApplyResponseSerializer,
    MasterProductImportPreviewResponseSerializer,
    MasterProductImportRequestSerializer,
    MasterProductImportUploadAcceptedResponseSerializer,
    MasterProductImportUploadStatusQuerySerializer,
    MasterProductImportUploadStatusResponseSerializer,
    MasterProductListQuerySerializer,
    MasterProductListResponseSerializer,
    MasterProductSerializer,
    MasterProductTenderOfferRowSerializer,
    MasterProductTenderOfferSerializer,
    MasterProductUpdateSerializer,
    MasterProductVenueBatchRequestSerializer,
    MasterProductVenueBatchResponseSerializer,
    MasterProductVenueBreakdownSerializer,
    MasterProductVenueLinkConflictSerializer,
    MasterProductVenueLinkQuerySerializer,
    VenueProductMatrixItemSerializer,
)
from api.v1.catalog.services import (
    MasterImportQueueUnavailable,
    MasterProductVenueLinkConflict,
    apply_master_product_import,
    apply_master_product_venue_batch,
    apply_tender_offer,
    bulk_assign_supplier_product_to_master_product,
    create_master_product,
    create_master_products_bulk,
    delete_master_product,
    delete_master_products_bulk,
    link_master_product_to_venue,
    master_product_bulk_supplier_product_targets,
    master_product_venue_batch_preview,
    start_master_product_import,
    unlink_master_product_from_venue,
    update_master_product,
)
from api.v1.permissions import (
    HasMasterCatalogAccess,
    HasMasterCatalogAccessToOwner,
    HasMasterProductAuditLogAccess,
    IsCoreUser,
)
from api.v1.suppliers.serializers import SupplierUnitListQuerySerializer, SupplierUnitSerializer
from catalog.models import (
    CatalogImportDomain,
    MasterProduct,
    MasterProductImportUpload,
)
from core.models import (
    EntityStatus,
    Unit,
    Venue,
)
from file_imports.models import ImportFileScope
from file_imports.services import import_file_storage_enabled

from ._shared import _MasterProductPagination, _store_import_file_if_enabled
from .import_common import ImportQueueUnavailableError


@extend_schema_view(
    post=extend_schema(
        tags=["Мастер-справочник"],
        summary="Массовое создание мастер-товаров",
        description="§11.3 raw ТЗ: прямой импорт списка мастер-товаров без автоматического добавления "
        "ни в одну компанию. Строка с названием, совпадающим (без учёта регистра) с уже существующим "
        "активным мастер-товаром этого владельца, пропускается, а не дублируется.",
        request=MasterProductBulkCreateSerializer,
        responses={
            201: MasterProductBulkCreateResponseSerializer,
            400: OpenApiResponse(
                description='Пустой список items: {"items": ["This list may not be empty."]} ИЛИ '
                'не передано обязательное поле строки: {"items": [{"canonical_name": ["This field is required."]}]} '
                'ИЛИ больше 500 строк: {"items": ["Ensure this field has no more than 500 elements."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не "
                "owner/manager ни одной venue этого владельца."
            ),
        },
    ),
)
class MasterProductBulkCreateView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccessToOwner]

    def post(self, request):
        ser = MasterProductBulkCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        results = create_master_products_bulk(owner=ser.validated_data["owner"], items=ser.validated_data["items"])
        return Response(
            MasterProductBulkCreateResponseSerializer({"results": results}).data, status=status.HTTP_201_CREATED
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Мастер-справочник"],
        summary="Массовое удаление мастер-товаров",
        description="§49 raw ТЗ: «Удалить» для нескольких выбранных строк Мастер-справочника разом. "
        "Настоящий hard delete на каждый id (VenueProductMatrixItem.master_product сбрасывается в null "
        "автоматически), как у одиночного DELETE. id, не найденный среди активных мастер-товаров этого "
        "владельца, помечается not_found, остальной батч всё равно применяется.",
        request=MasterProductBulkDeleteSerializer,
        responses={
            200: MasterProductBulkDeleteResponseSerializer,
            400: OpenApiResponse(
                description='Пустой список master_product_ids: {"master_product_ids": ["This list may not be empty."]} '
                'ИЛИ больше 500 id: {"master_product_ids": ["Ensure this field has no more than 500 elements."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не "
                "owner/manager ни одной venue этого владельца."
            ),
        },
    ),
)
class MasterProductBulkDeleteView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccessToOwner]

    def post(self, request):
        ser = MasterProductBulkDeleteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        results = delete_master_products_bulk(
            owner=ser.validated_data["owner"],
            master_product_ids=ser.validated_data["master_product_ids"],
            actor=request.user,
        )
        return Response(MasterProductBulkDeleteResponseSerializer({"results": results}).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Мастер-справочник"],
        summary="Список единиц измерения",
        description=(
            "TASK-49: справочник для дропдауна «Ед.Изм. в УС» в Мастер-справочнике. "
            "Тот же справочник Unit, что и `GET /suppliers/units`, но на CoreUserJWTAuthentication — "
            "ручка поставщика недоступна venue-стороне (другой круг авторизации). "
            "?kind сужает список: weight/volume/piece/package."
        ),
        parameters=[
            OpenApiParameter("kind", OpenApiTypes.STR, description="Фильтр по типу: weight/volume/piece/package."),
        ],
        responses={200: SupplierUnitSerializer(many=True)},
    ),
)
class MasterCatalogUnitListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

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
        operation_id="master_catalog_list",
        tags=["Мастер-справочник"],
        summary="Мастер-товары владельца",
        parameters=[
            OpenApiParameter(
                name="owner_id", type=OpenApiTypes.UUID, required=True, description="ID владельца учётной записи."
            ),
            OpenApiParameter(
                name="q",
                type=OpenApiTypes.STR,
                required=False,
                description=(
                    "Поиск по мастер-справочнику: название, единица и примечание. Поддерживает слова "
                    "в любом порядке, русскую раскладку и небольшие опечатки."
                ),
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
            200: MasterProductListResponseSerializer,
            400: OpenApiResponse(
                description=(
                    'owner_id не передан либо не UUID: {"owner_id": ["This field is required."]} ИЛИ '
                    '{"owner_id": ["Must be a valid UUID."]}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description=(
                    "owner_id — валидный UUID, но пользователь не admin, не сам владелец и не owner/manager "
                    "ни одной venue этого владельца."
                )
            ),
        },
    ),
    post=extend_schema(
        operation_id="master_catalog_create",
        tags=["Мастер-справочник"],
        summary="Создать мастер-товар",
        request=MasterProductCreateSerializer,
        responses={
            201: MasterProductSerializer,
            400: OpenApiResponse(
                description=(
                    'Не передано обязательное поле: {"canonical_name": ["This field is required."]} ИЛИ '
                    'owner_id не передан/не UUID: {"owner_id": ["This field is required."]}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description=(
                    "owner_id — валидный UUID, но пользователь не admin, не сам владелец и не owner/manager "
                    "ни одной venue этого владельца."
                )
            ),
        },
    ),
)
class MasterProductListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccessToOwner]

    def get(self, request):
        query_ser = MasterProductListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        owner_id = query_ser.validated_data["owner_id"]
        q = query_ser.validated_data.get("q")
        queryset = master_products_queryset(owner_id, q=q)
        paginator = _MasterProductPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(MasterProductSerializer(page, many=True).data)

    def post(self, request):
        ser = MasterProductCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        mp = create_master_product(**ser.validated_data)
        item = master_products_queryset(mp.owner_id).get(pk=mp.pk)
        return Response(MasterProductSerializer(item).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        operation_id="master_catalog_import_apply",
        tags=["Импорт Мастер-справочника"],
        summary="Шаг 2. Применить подтверждённый импорт Мастер-справочника",
        description="Применяет решения пользователя по строкам .../import/preview. Передавайте только строки, "
        "по которым решение принято. replace заменяет название и единицу измерения выбранного мастер-товара "
        "(ID, связи с компаниями, net_value и note сохраняются). update_existing не перезаписывает поля "
        "существующего мастер-товара. create_new дедуплицируется внутри одного запроса по названию "
        "(case-insensitive). Весь запрос применяется транзакционно: при любой ошибке ничего не записывается.",
        request=MasterProductImportApplyRequestSerializer,
        responses={
            200: MasterProductImportApplyResponseSerializer,
            400: OpenApiResponse(
                response=MasterProductImportApplyErrorSerializer,
                description="Ничего не записано; decisions — всегда список. Причины: decisions пуст "
                "(«Нужно передать хотя бы одно решение.») или больше 500 («Не более 500 решений за "
                "запрос.»); у решения нет "
                "master_product_id (update_existing/replace) или unit_id (replace) — полевая ошибка "
                "`Обязателен при action=...`; мастер-товар не найден у этого владельца или архивен; единица "
                "измерения не найдена или архивна; owner_id невалиден; несколько решений replace указывают на "
                "одну и ту же карточку (сообщение перечисляет все такие строки).",
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не "
                "owner/manager ни одной venue этого владельца."
            ),
        },
    )
)
class MasterProductImportApplyView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccessToOwner]

    def post(self, request):
        ser = MasterProductImportApplyRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = apply_master_product_import(
            owner=ser.validated_data["owner"], decisions=ser.validated_data["decisions"], actor=request.user
        )
        return Response(MasterProductImportApplyResponseSerializer({"rows": rows}).data)


def _master_import_max_rows():
    # 12000 строк возможны только при сохранении файла: воркер читает файл сам (строки не идут в Celery аргументом).
    if import_file_storage_enabled():
        return int(getattr(settings, "MASTER_PRODUCT_IMPORT_MAX_ROWS", 12000))
    return 5000


def _master_product_import_async_threshold_rows():
    try:
        value = int(getattr(settings, "MASTER_PRODUCT_IMPORT_ASYNC_THRESHOLD_ROWS", 500) or 500)
    except (TypeError, ValueError):
        return 500
    return value if value > 0 else 500


@extend_schema_view(
    post=extend_schema(
        operation_id="master_catalog_import_preview",
        tags=["Импорт Мастер-справочника"],
        summary="Шаг 1. Загрузить файл и получить превью Мастер-справочника",
        description=(
            "Не создаёт и не меняет мастер-товары. Принимает файл (.xlsx/.xlsm/.csv, максимум 10 МБ и "
            "5000 строк) и для каждой строки ищет до 5 похожих мастер-товаров владельца (`candidates`). "
            "Если похожие есть — `action = null` («требует проверки»): пользователь выбирает `replace` "
            "(заменить название и единицу выбранной карточки, ID сохраняется), `skip` или `create_new`. "
            "Если похожих нет — `action = create_new`. Незнакомая единица измерения одной строки файла "
            "помечает эту строку (unit_unrecognized=true, unit_id=null) и не блокирует остальной preview "
            "(ТЗ §52). Иногда бэкенд сам уверенно подбирает единицу через LLM "
            '(unit_source="llm", unit_unrecognized=false) — такая строка тоже не требует подтверждения от '
            "пользователя, как и изначально распознанная единица. Решения передаются в POST "
            ".../import/apply. Может лениво досчитать отсутствующие embedding-векторы мастер-товаров "
            "владельца (self-healing кэш, см. _ensure_master_product_embeddings) — это не бизнес-данные. "
            "**Файлы больше 500 строк (ТЗ §48) обрабатываются в фоне**: ответ — 202 с upload_id, "
            "прогресс и результат — через GET .../import/uploads/{upload_id}. "
            "**Режим «всегда в воркере»:** при включённом флаге `CATALOG_IMPORT_ALWAYS_ASYNC` (по умолчанию "
            "выключен; также нужен `IMPORT_FILE_STORAGE_ENABLED`) файл любого размера обрабатывается в "
            "воркере: ответ 202 с `upload_id` и `status_url` "
            "(`/api/v1/catalog/master-products/import/uploads/{upload_id}`); опрашивайте его до "
            "`status=ready`, затем `result.session_id` открывает сессию ревью. Строки в ответе preview в "
            "этом режиме не приходят."
        ),
        request=MasterProductImportRequestSerializer,
        responses={
            200: MasterProductImportPreviewResponseSerializer,
            202: MasterProductImportUploadAcceptedResponseSerializer,
            400: OpenApiResponse(
                description="Файл не передан/пустой/слишком большой/неподдерживаемый формат/больше 5000 "
                'строк: {"file": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description=(
                    "owner_id — валидный UUID, но пользователь не admin, не сам владелец и не owner/manager "
                    "ни одной venue этого владельца."
                )
            ),
            503: OpenApiResponse(
                description=(
                    "Сервис временно недоступен. (1) Только при включённом сохранении исходного файла: хранилище "
                    "файлов недоступно, сессия не создана: "
                    '{"detail": "Не удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."}. '
                    "(2) Фоновая обработка (202-ветка) не смогла встать в очередь: загрузка помечена failed "
                    "(error_message «Не удалось поставить обработку в очередь.»), ответ: "
                    '{"detail": "Не удалось поставить обработку импорта в очередь. Загрузите файл ещё раз чуть позже."}'
                )
            ),
        },
    )
)
class MasterProductImportPreviewView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccessToOwner]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        ser = MasterProductImportRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        owner = ser.validated_data["owner"]
        extract_async = catalog_import_async_extraction(ser.validated_data["file"])
        parsed = (
            None
            if extract_async
            else parse_catalog_file(ser.validated_data["file"], max_rows=_master_import_max_rows())
        )
        import_file = _store_import_file_if_enabled(
            uploaded_file=ser.validated_data["file"], scope_type=ImportFileScope.OWNER, scope_id=owner.id
        )

        if not extract_async and len(parsed.rows) <= _master_product_import_async_threshold_rows():
            rows = resolve_master_product_import_rows(owner=owner, rows=parsed.rows)
            needs_attention_count = sum(1 for row in rows if row.needs_attention)
            payload = {
                "sheet_name": parsed.sheet_name,
                "header_row_number": parsed.header_row_number,
                "detected_columns": parsed.detected_columns,
                "total_rows": len(rows) + parsed.rows_without_name,
                "rows_without_name": parsed.rows_without_name,
                "auto_resolved_count": len(rows) - needs_attention_count,
                "needs_attention_count": needs_attention_count,
                "rows": rows,
            }
            response_payload = MasterProductImportPreviewResponseSerializer(payload).data
            session = persist_catalog_import_session(
                owner=owner,
                domain=CatalogImportDomain.MASTER,
                source_file_name=getattr(ser.validated_data["file"], "name", ""),
                rows=response_payload["rows"],
                parsed_rows=parsed.rows,
                import_file=import_file,
            )
            response_payload["session_id"] = str(session.id)
            return Response(response_payload)

        for row in parsed.rows if parsed else []:
            _parse_import_decimal(row.get("net"), row_number=row.get("row_number"))

        try:
            upload = start_master_product_import(
                owner=owner,
                source_file_name=getattr(ser.validated_data["file"], "name", "") or "",
                rows=parsed.rows if parsed else None,
                parsed_meta={
                    "sheet_name": parsed.sheet_name,
                    "header_row_number": parsed.header_row_number,
                    "detected_columns": parsed.detected_columns,
                    "rows_without_name": parsed.rows_without_name,
                }
                if parsed
                else {},
                import_file=import_file,
            )
        except MasterImportQueueUnavailable as error:
            raise ImportQueueUnavailableError() from error
        return Response(
            MasterProductImportUploadAcceptedResponseSerializer(
                {
                    "upload_id": upload.id,
                    "status": upload.status,
                    "status_url": f"/api/v1/catalog/master-products/import/uploads/{upload.id}",
                }
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema_view(
    get=extend_schema(
        operation_id="master_catalog_import_upload_status",
        tags=["Импорт Мастер-справочника"],
        summary="Статус/прогресс/результат фоновой обработки большого импорта",
        description=(
            "Опрашивать после 202 от .../import/preview. status: processing (result всегда null), "
            "ready (result заполнен — та же форма, что синхронный 200 у .../import/preview), failed "
            "(error_message заполнен). percent — по rows_done/rows_total."
        ),
        parameters=[
            OpenApiParameter(
                name="owner_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Тот же owner_id, что был передан в .../import/preview.",
            )
        ],
        responses={
            200: MasterProductImportUploadStatusResponseSerializer,
            400: OpenApiResponse(
                description=(
                    'owner_id не передан либо не UUID: {"owner_id": ["This field is required."]} ИЛИ '
                    '{"owner_id": ["Must be a valid UUID."]}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="owner_id — валидный UUID, но пользователь не admin, не сам владелец и не "
                "owner/manager ни одной venue этого владельца."
            ),
            404: OpenApiResponse(
                description="upload_id не найден, либо owner_id не совпадает с владельцем этой "
                'записи: {"detail": "Not found."}'
            ),
        },
    )
)
class MasterProductImportUploadStatusView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccessToOwner]

    def get(self, request, upload_id):
        query_ser = MasterProductImportUploadStatusQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        owner_id = query_ser.validated_data["owner_id"]
        upload = get_object_or_404(MasterProductImportUpload, pk=upload_id)
        if upload.owner_id != owner_id:
            raise Http404
        return Response(MasterProductImportUploadStatusResponseSerializer(upload).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="master_catalog_retrieve",
        tags=["Мастер-справочник"],
        summary="Мастер-товар по id",
        responses={
            200: MasterProductSerializer,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description=(
                    "Мастер-товара с таким id нет, либо пользователь не admin, не владелец и не owner/manager "
                    "ни одной venue владельца."
                )
            ),
        },
    ),
    patch=extend_schema(
        operation_id="master_catalog_update",
        tags=["Мастер-справочник"],
        summary="Изменить мастер-товар",
        request=MasterProductUpdateSerializer,
        responses={
            200: MasterProductSerializer,
            400: OpenApiResponse(
                description='Ни одно поле не передано: {"non_field_errors": ["At least one field must be provided."]}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
    delete=extend_schema(
        operation_id="master_catalog_delete",
        tags=["Мастер-справочник"],
        summary="Удалить мастер-товар (настоящий DELETE)",
        description=(
            "Hard delete — без архивации. VenueProductMatrixItem.master_product сбрасывается в null "
            "автоматически (SET_NULL); сами строки товарной матрицы, назначения supplier_product и история "
            "не затрагиваются."
        ),
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
)
class MasterProductDetailView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def get(self, request, master_product_id):
        mp = get_object_or_404(master_product_detail_queryset(), pk=master_product_id)
        return Response(MasterProductSerializer(mp).data)

    def patch(self, request, master_product_id):
        # Fix 5 (2026-09-01 fix wave): fetch/re-fetch through the same
        # ACTIVE-filtered queryset get() uses, so all three methods on this
        # view agree on what "exists" means — the old bare .get() re-fetch
        # would raise an uncaught MasterProduct.DoesNotExist (500) if the
        # object somehow weren't ACTIVE (unreachable today: hard-delete-only
        # means no non-ACTIVE MasterProduct rows ever exist).
        mp = get_object_or_404(master_product_detail_queryset(), pk=master_product_id)
        ser = MasterProductUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        update_master_product(mp, **ser.validated_data)
        item = get_object_or_404(master_product_detail_queryset(), pk=mp.pk)
        return Response(MasterProductSerializer(item).data)

    def delete(self, request, master_product_id):
        mp = get_object_or_404(master_product_detail_queryset(), pk=master_product_id)
        delete_master_product(mp, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Мастер-справочник"],
        summary="История удаления мастер-товара",
        description="Работает и после удаления самого мастер-товара (hard "
        "delete) — показывает, кто и когда его удалил.",
        responses={
            200: AuditLogEntrySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
)
class MasterProductAuditLogView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterProductAuditLogAccess]

    def get(self, request, master_product_id):
        return Response(AuditLogEntrySerializer(master_product_audit_log(master_product_id), many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="master_catalog_venues_breakdown",
        tags=["Мастер-справочник"],
        summary='Модалка "Компаний: N" — разбивка по компаниям владельца',
        responses={
            200: MasterProductVenueBreakdownSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
)
class MasterProductVenueBreakdownView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def get(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        rows = master_product_venue_breakdown(mp)
        return Response(MasterProductVenueBreakdownSerializer(rows, many=True).data)


@extend_schema_view(
    put=extend_schema(
        tags=["Мастер-справочник"],
        summary="Привязать мастер-товар к компании",
        description=(
            "Спека §16: перед созданием ищет в справочнике этой компании непривязанный товар с точно таким же "
            "названием (case-insensitive) — при совпадении ничего не создаёт и отдаёт 409 с кандидатом. Чтобы "
            "продолжить, повторите запрос с link_existing_item_id (связать найденную строку) или "
            "force_create=true (создать новую несмотря на совпадение)."
        ),
        parameters=[
            OpenApiParameter(
                "link_existing_item_id",
                OpenApiTypes.UUID,
                required=False,
                description="Связать эту уже существующую непривязанную строку вместо создания новой.",
            ),
            OpenApiParameter(
                "force_create",
                OpenApiTypes.BOOL,
                required=False,
                description="Создать новую строку, даже если найден похожий непривязанный товар. "
                "Игнорируется вместе с link_existing_item_id.",
            ),
        ],
        request=None,
        responses={
            200: VenueProductMatrixItemSerializer,
            201: VenueProductMatrixItemSerializer,
            400: OpenApiResponse(
                description=(
                    'Уже привязан: {"detail": "Этот мастер-товар уже привязан к этой компании."} ИЛИ '
                    'компания не принадлежит владельцу: {"detail": "Эта компания не принадлежит владельцу '
                    'мастер-справочника."} ИЛИ строка не найдена/чужая/уже привязана: '
                    '{"link_existing_item_id": "Строка не найдена, принадлежит другой компании или уже '
                    'привязана к мастер-товару."} ИЛИ link_existing_item_id/force_create не проходят '
                    "валидацию формата (например, невалидный UUID): стандартная ошибка валидации DRF, "
                    '{"<имя_поля>": ["..."]}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
            409: MasterProductVenueLinkConflictSerializer,
        },
    ),
    delete=extend_schema(
        tags=["Мастер-справочник"],
        summary="Отвязать мастер-товар от компании",
        description="Снимает связь master_product у строки товарной матрицы — сама строка не удаляется.",
        responses={
            204: None,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
            404: OpenApiResponse(description="Эта компания не имеет привязанной строки для этого мастер-товара."),
        },
    ),
)
class MasterProductVenueLinkView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def put(self, request, master_product_id, venue_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        venue = get_object_or_404(Venue, pk=venue_id)
        query_ser = MasterProductVenueLinkQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        link_existing_item_id = query_ser.validated_data.get("link_existing_item_id")
        force_create = query_ser.validated_data["force_create"]
        try:
            item = link_master_product_to_venue(
                master_product=mp,
                venue=venue,
                link_existing_item_id=link_existing_item_id,
                force_create=force_create,
            )
        except MasterProductVenueLinkConflict as exc:
            return Response(
                {
                    "detail": "Найден похожий непривязанный товар компании — свяжите его или создайте новый.",
                    "candidate": VenueProductMatrixItemSerializer(exc.candidate).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        response_status = status.HTTP_200_OK if link_existing_item_id else status.HTTP_201_CREATED
        return Response(VenueProductMatrixItemSerializer(item).data, status=response_status)

    def delete(self, request, master_product_id, venue_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        unlink_master_product_from_venue(master_product=mp, venue_id=venue_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _master_product_batch_changes(ser_data):
    return [
        {
            "venue_id": change["venue_id"],
            "link_action": change.get("link_action", "keep"),
            "link_existing_item_id": change.get("link_existing_item_id"),
            "force_create": change.get("force_create", False),
            **({"supplier_product_id": change["supplier_product"].id} if "supplier_product" in change else {}),
        }
        for change in ser_data["changes"]
    ]


@extend_schema_view(
    post=extend_schema(
        operation_id="master_catalog_venues_batch_preview",
        tags=["Мастер-справочник"],
        summary='Модалка "Компаний: N" — сохранить изменения, превью (Сейчас → Будет)',
        description="Без побочных эффектов — не пишет в БД (выполняется и откатывается в одной транзакции, "
        "поэтому валидируется и падает ровно как apply, включая §16 конфликт похожего товара компании). "
        "Меняются только компании из массива changes; отсутствующие в массиве связи остаются как есть. "
        "new_matrix_item_id/new_supplier_product_id в этом ответе иллюстративны (строка откатывается) — не "
        "кешируйте и не переиспользуйте их, реальные и стабильные id вернёт только POST .../venues/batch/apply.",
        request=MasterProductVenueBatchRequestSerializer,
        responses={
            200: MasterProductVenueBatchResponseSerializer,
            400: OpenApiResponse(
                description='Компания не принадлежит владельцу, либо несовместимая комбинация полей: {"changes": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
            409: MasterProductVenueLinkConflictSerializer,
        },
    )
)
class MasterProductVenueBatchPreviewView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def post(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        ser = MasterProductVenueBatchRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            rows = master_product_venue_batch_preview(
                master_product=mp, changes=_master_product_batch_changes(ser.validated_data)
            )
        except MasterProductVenueLinkConflict as exc:
            return Response(
                {
                    "detail": "Найден похожий непривязанный товар компании — свяжите его или создайте новый.",
                    "candidate": VenueProductMatrixItemSerializer(exc.candidate).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(MasterProductVenueBatchResponseSerializer({"rows": rows}).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="master_catalog_venues_batch_apply",
        tags=["Мастер-справочник"],
        summary='Модалка "Компаний: N" — сохранить изменения, применить',
        description="Применяет тот же diff, что показывает POST .../venues/batch/preview, одной транзакцией "
        "(все изменения по всем компаниям применяются вместе или ни одно). Меняются только компании "
        "из массива changes; отсутствующие в массиве связи остаются как есть.",
        request=MasterProductVenueBatchRequestSerializer,
        responses={
            200: MasterProductVenueBatchResponseSerializer,
            400: OpenApiResponse(
                description='Компания не принадлежит владельцу, либо несовместимая комбинация полей: {"changes": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
            409: MasterProductVenueLinkConflictSerializer,
        },
    )
)
class MasterProductVenueBatchApplyView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def post(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        ser = MasterProductVenueBatchRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            rows = apply_master_product_venue_batch(
                master_product=mp, changes=_master_product_batch_changes(ser.validated_data)
            )
        except MasterProductVenueLinkConflict as exc:
            return Response(
                {
                    "detail": "Найден похожий непривязанный товар компании — свяжите его или создайте новый.",
                    "candidate": VenueProductMatrixItemSerializer(exc.candidate).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(MasterProductVenueBatchResponseSerializer({"rows": rows}).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Мастер-справочник — дополнительные операции"],
        summary="Массовое назначение товара поставщика — превью (Сейчас → Будет)",
        description=(
            "Без побочных эффектов — не меняет БД. Показывает diff перед POST на этом же адресе "
            "(спека §6, правило 9: без молчаливой перезаписи). venue_id можно передать несколько раз; "
            "если не передан — берутся все компании, уже привязанные к этому мастер-товару."
        ),
        parameters=[
            OpenApiParameter(
                "supplier_product_id", OpenApiTypes.UUID, required=True, description="ID товара поставщика."
            ),
            OpenApiParameter(
                "venue_id",
                OpenApiTypes.UUID,
                many=True,
                required=False,
                description="ID компании — можно повторить несколько раз. Не передан — все привязанные компании.",
            ),
        ],
        responses={
            200: MasterProductBulkSupplierProductRowSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "supplier_product_id не передан/невалиден/архивен, либо среди venue_id есть компания, не "
                    'привязанная к этому мастер-товару: {"venue_ids": "Мастер-товар не привязан к этим '
                    'компаниям: ..."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
    post=extend_schema(
        tags=["Мастер-справочник — дополнительные операции"],
        summary="Массовое назначение товара поставщика — применить",
        description="Применяет тот же diff, что показывает GET на этом же адресе.",
        request=MasterProductBulkSupplierProductAssignSerializer,
        responses={
            200: MasterProductBulkSupplierProductRowSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "supplier_product_id не передан/невалиден/архивен, либо среди venue_ids есть компания, не "
                    'привязанная к этому мастер-товару: {"venue_ids": "Мастер-товар не привязан к этим '
                    'компаниям: ..."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
)
class MasterProductBulkSupplierProductAssignmentView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def get(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        data = {
            k: v
            for k, v in {
                "supplier_product_id": request.query_params.get("supplier_product_id"),
                "venue_ids": request.query_params.getlist("venue_id") or None,
            }.items()
            if v is not None
        }
        ser = MasterProductBulkSupplierProductAssignSerializer(data=data)
        ser.is_valid(raise_exception=True)
        items = master_product_bulk_supplier_product_targets(
            master_product=mp, venue_ids=ser.validated_data.get("venue_ids")
        )
        rows = master_product_bulk_supplier_product_diff(
            items=items, new_supplier_product=ser.validated_data["supplier_product"]
        )
        return Response(MasterProductBulkSupplierProductRowSerializer(rows, many=True).data)

    def post(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        ser = MasterProductBulkSupplierProductAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = bulk_assign_supplier_product_to_master_product(
            master_product=mp,
            supplier_product=ser.validated_data["supplier_product"],
            venue_ids=ser.validated_data.get("venue_ids"),
        )
        return Response(MasterProductBulkSupplierProductRowSerializer(rows, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Мастер-справочник"],
        summary="Применить предложение тендера — превью",
        description=(
            "Показывает diff (товар поставщика + цена) без изменений — то же, что применит POST на этом же "
            "адресе. Сырой ТЗ §45-47: тендер меняет назначение товара поставщика и индивидуальную цену "
            "одновременно, для явно выбранных компаний."
        ),
        parameters=[
            OpenApiParameter(
                name="supplier_product_id",
                type=OpenApiTypes.UUID,
                required=True,
                description="ID товара поставщика из выбранного предложения тендера.",
            ),
            OpenApiParameter(
                name="price", type=OpenApiTypes.NUMBER, required=True, description="Цена выбранного предложения."
            ),
            OpenApiParameter(
                name="currency",
                type=OpenApiTypes.STR,
                required=False,
                description="Валюта (ISO 4217). Сейчас поддерживается только RUB. По умолчанию RUB.",
            ),
            OpenApiParameter(
                name="venue_id",
                type=OpenApiTypes.UUID,
                required=True,
                many=True,
                description=(
                    "Компании (venue), для которых выбрано предложение этого поставщика — только уже привязанные к "
                    "этому мастер-товару. Обязателен, минимум одна."
                ),
            ),
            OpenApiParameter(
                name="source_reference",
                type=OpenApiTypes.STR,
                required=True,
                description="Ссылка на тендер, например «Тендер №42».",
            ),
        ],
        responses={
            200: MasterProductTenderOfferRowSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "Не передано обязательное поле, venue_id пуст, price отрицательный, currency отличается от "
                    "RUB, supplier_product_id не найден/архивен, либо среди venue_id есть компания, не "
                    'привязанная к этому мастер-товару: {"venue_ids": "Мастер-товар не привязан к этим '
                    'компаниям: ..."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
    post=extend_schema(
        tags=["Мастер-справочник"],
        summary="Применить предложение тендера",
        description=(
            "Применяет тот же diff, что показывает GET на этом же адресе: одной атомарной операцией назначает "
            "товар поставщика выбранным компаниям (как массовое назначение, §19) и фиксирует для них "
            "индивидуальную цену с source_type=tender (как ручное назначение цены, §35), с явной ссылкой на "
            "тендер в source_reference."
        ),
        request=MasterProductTenderOfferSerializer,
        responses={
            200: MasterProductTenderOfferRowSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "Не передано обязательное поле (в т.ч. source_reference), venue_ids пуст, price "
                    "отрицательный, currency отличается от RUB, supplier_product_id не найден/архивен, либо "
                    "среди venue_ids есть компания, не привязанная к этому мастер-товару: "
                    '{"venue_ids": "Мастер-товар не привязан к этим компаниям: ..."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Мастер-товара с таким id нет, либо нет доступа."),
        },
    ),
)
class MasterProductTenderOfferView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [HasMasterCatalogAccess]

    def get(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        data = {
            k: v
            for k, v in {
                "supplier_product_id": request.query_params.get("supplier_product_id"),
                "price": request.query_params.get("price"),
                "currency": request.query_params.get("currency"),
                "venue_ids": request.query_params.getlist("venue_id") or None,
                "source_reference": request.query_params.get("source_reference"),
            }.items()
            if v is not None
        }
        ser = MasterProductTenderOfferSerializer(data=data)
        ser.is_valid(raise_exception=True)
        items = master_product_bulk_supplier_product_targets(
            master_product=mp, venue_ids=ser.validated_data["venue_ids"]
        )
        rows = master_product_tender_offer_diff(
            items=items,
            new_supplier_product=ser.validated_data["supplier_product"],
            new_price=ser.validated_data["price"],
            new_currency=ser.validated_data["currency"],
        )
        return Response(MasterProductTenderOfferRowSerializer(rows, many=True).data)

    def post(self, request, master_product_id):
        mp = get_object_or_404(MasterProduct, pk=master_product_id)
        ser = MasterProductTenderOfferSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        rows = apply_tender_offer(
            master_product=mp,
            supplier_product=data["supplier_product"],
            price=data["price"],
            currency=data["currency"],
            venue_ids=data["venue_ids"],
            source_reference=data["source_reference"],
            created_by=request.user,
        )
        return Response(MasterProductTenderOfferRowSerializer(rows, many=True).data)
