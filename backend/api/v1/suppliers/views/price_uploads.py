"""Legacy supplier price uploads: upload, normalize, AI cleanup, publish."""

from io import BytesIO

from django.core.files.storage import default_storage
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import (
    OptionalSupplierCabinetJWTAuthentication,
)
from api.v1.permissions import (
    IsSupplierCabinetOrHasServiceToken,
)
from api.v1.suppliers.selectors import (
    supplier_price_upload_detail_queryset,
    supplier_price_upload_items_queryset,
)
from api.v1.suppliers.serializers import (
    SupplierPriceAIApplySerializer,
    SupplierPriceUploadAICleanupSerializer,
    SupplierPriceUploadColumnMappingSerializer,
    SupplierPriceUploadDetailSerializer,
    SupplierPriceUploadFileSerializer,
    SupplierPriceUploadItemCorrectionSerializer,
    SupplierPriceUploadItemListQuerySerializer,
    SupplierPriceUploadItemSerializer,
    SupplierPriceUploadListQuerySerializer,
    SupplierPriceUploadNormalizeSerializer,
    SupplierPriceUploadPreviewQuerySerializer,
    SupplierPriceUploadResponseSerializer,
    SupplierPriceUploadTextSerializer,
)
from api.v1.suppliers.services import (
    apply_supplier_price_ai_result,
    correct_supplier_price_upload_item,
    create_supplier_price_upload_from_file,
    create_supplier_price_upload_from_text,
    get_supplier_price_upload_change_plan,
    get_supplier_price_upload_preview,
    normalize_supplier_price_upload,
    rollback_supplier_price_upload,
    set_supplier_price_upload_column_mapping,
    start_supplier_price_upload_ai_cleanup,
    start_supplier_price_upload_normalization,
)
from core.models import EntityStatus
from suppliers.models import (
    Supplier,
    SupplierPriceUpload,
    SupplierUser,
    UploadItemStatus,
    UploadStatus,
)


def _require_owns_upload(request, upload_id):
    """Same scoping as _require_owns_product, for price uploads — plus one
    deliberate relaxation.

    2026-08-17: direct product decision (Витя) — a person with active
    membership in multiple companies gets a company-scoped cabinet JWT per
    company (see is_supplier_cabinet_active_member's docstring for why
    sessions are scoped this way, not per-person); switching the active
    company in the UI swaps that token, which used to 404 an upload started
    under a DIFFERENT one of the SAME person's own companies — the token
    only proves "which company," and the check only looked at that. Now
    also allows it if the acting person (JWT's `user_id` claim) has active
    membership in the upload's ACTUAL owning company, not just the one
    baked into the current token. Same-company match stays the fast path
    (no extra query); this never grants access to a company the acting
    person genuinely has no membership in, and — same as
    is_supplier_cabinet_active_member — fails closed (no cross-company
    access) for bot-issued sessions, which carry no user_id claim."""
    if not isinstance(request.user, Supplier):
        return
    upload = SupplierPriceUpload.objects.filter(pk=upload_id).only("id", "supplier_id").first()
    if upload is None:
        raise Http404
    if upload.supplier_id == request.user.id:
        return
    user_id = request.auth.get("user_id") if request.auth else None
    if (
        not user_id
        or not SupplierUser.objects.filter(
            supplier_id=upload.supplier_id, user_id=user_id, status=EntityStatus.ACTIVE
        ).exists()
    ):
        raise Http404


_APPLY_AI_RESULT_SCHEMA = inline_serializer(
    name="SupplierPriceAIApplyResult",
    fields={
        "upload_id": s.UUIDField(help_text="ID загрузки, к которой применён результат."),
        "status": s.CharField(help_text="Итоговый статус загрузки: applied / needs_review."),
        "items_total": s.IntegerField(help_text="Всего товаров в присланном результате."),
        "items_created": s.IntegerField(help_text="Сколько строк загрузки создано."),
        "products_created": s.IntegerField(help_text="Сколько новых товаров создано."),
        "products_updated": s.IntegerField(help_text="Сколько существующих товаров обновлено."),
        "prices_created": s.IntegerField(help_text="Сколько новых цен создано."),
        "items_matched": s.IntegerField(help_text="Сколько строк совпало с уже существующими товарами без изменений."),
        "needs_review": s.IntegerField(help_text="Сколько строк требуют ручной проверки."),
    },
)


_UPLOAD_REJECT_SCHEMA = inline_serializer(
    name="SupplierPriceUploadRejectResult",
    fields={
        "upload_id": s.UUIDField(help_text="ID отклонённой загрузки."),
        "status": s.CharField(help_text="Статус загрузки после отката, обычно archived."),
        "already_rolled_back": s.BooleanField(
            help_text="true, если загрузка уже была откачена ранее — повторный вызов ничего не изменил."
        ),
        "products_archived": s.IntegerField(help_text="Сколько новых товаров архивировано при откате."),
        "prices_reverted": s.IntegerField(help_text="Сколько цен возвращено к предыдущему значению."),
        "skipped": s.IntegerField(help_text="Сколько строк пропущено при откате (не требовали изменений)."),
    },
)


# normalize/ai-cleanup: shape genuinely varies by run_async and whether AI
# cleanup got chained on top — this documents the union of fields that can
# appear, all optional, rather than a bare freeform object.
_NORMALIZE_RESULT_SCHEMA = inline_serializer(
    name="SupplierPriceUploadNormalizeResult",
    fields={
        "upload_id": s.UUIDField(required=False, help_text="ID обрабатываемой загрузки."),
        "supplier_id": s.UUIDField(required=False, help_text="ID поставщика."),
        "supplier_name": s.CharField(required=False, help_text="Название поставщика."),
        "status": s.CharField(
            required=False, help_text="Статус загрузки: processing / applied / needs_review / failed."
        ),
        "accepted": s.BooleanField(
            required=False, help_text="Задача поставлена в очередь Celery. Только для run_async=true."
        ),
        "run_async": s.BooleanField(required=False, help_text="Обработка запущена асинхронно."),
        "celery_task_id": s.CharField(
            required=False, allow_null=True, help_text="ID фоновой Celery-задачи, если run_async=true."
        ),
        "items_total": s.IntegerField(required=False, help_text="Всего строк в прайсе."),
        "items_created": s.IntegerField(required=False, help_text="Сколько строк загрузки создано."),
        "products_created": s.IntegerField(required=False, help_text="Сколько новых товаров создано."),
        "products_updated": s.IntegerField(required=False, help_text="Сколько существующих товаров обновлено."),
        "prices_created": s.IntegerField(required=False, help_text="Сколько новых цен создано."),
        "items_matched": s.IntegerField(
            required=False, help_text="Сколько строк совпало с уже существующими товарами без изменений."
        ),
        "needs_review": s.IntegerField(required=False, help_text="Сколько строк требуют ручной проверки."),
        "normalization": s.DictField(
            required=False,
            help_text="Полная диагностика нормализации (режим AI, счётчики по чанкам); форма зависит от режима.",
        ),
        "ai_cleanup": s.DictField(
            required=False, allow_null=True, help_text="Диагностика AI-cleanup, если он был запущен цепочкой."
        ),
        "ai_cleanup_accepted": s.BooleanField(
            required=False, help_text="AI-cleanup успешно поставлен в очередь после нормализации."
        ),
        "ai_cleanup_enqueue_error": s.CharField(
            required=False, help_text="Текст ошибки, если AI-cleanup не удалось поставить в очередь."
        ),
    },
)


_AI_CLEANUP_RESULT_SCHEMA = inline_serializer(
    name="SupplierPriceUploadAICleanupResult",
    fields={
        "accepted": s.BooleanField(required=False, help_text="Задача AI-cleanup поставлена в очередь Celery."),
        "ai_cleanup": s.DictField(
            required=False,
            help_text="Статус и диагностика cleanup-джобы; форма зависит от статуса (queued/processing/done).",
        ),
    },
)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_price_import_history",
        tags=["Импорт прайса поставщика"],
        summary="История загрузок прайсов",
        parameters=[
            OpenApiParameter("status", OpenApiTypes.STR, description="Фильтр по статусу."),
            OpenApiParameter(
                "supplier_id", OpenApiTypes.UUID, description="Для service-token; JWT берёт компанию из токена."
            ),
            OpenApiParameter("limit", OpenApiTypes.INT, description="Размер страницы, максимум 100."),
            OpenApiParameter("offset", OpenApiTypes.INT, description="Смещение для пагинации."),
        ],
        responses={
            200: inline_serializer(
                name="SupplierPriceUploadHistoryPage",
                fields={
                    "count": s.IntegerField(),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "results": SupplierPriceUploadDetailSerializer(many=True),
                },
            )
        },
    ),
    post=extend_schema(
        tags=["Импорт прайса поставщика"],
        operation_id="supplier_price_import_upload_file",
        summary="Шаг 1. Загрузить файл прайса поставщика",
        description=(
            "Основная точка входа импорта прайса. Ответ содержит status_url, preview_url, publish_url, "
            "next_action, available_actions и action_urls — фронту не нужно угадывать следующий endpoint "
            "по статусу."
        ),
        request=SupplierPriceUploadFileSerializer,
        responses={201: SupplierPriceUploadResponseSerializer},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadFileView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request):
        query_serializer = SupplierPriceUploadListQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        filters = query_serializer.validated_data

        queryset = supplier_price_upload_detail_queryset().order_by("-uploaded_at", "-id")
        if isinstance(request.user, Supplier):
            queryset = queryset.filter(supplier=request.user)
        elif filters.get("supplier_id"):
            queryset = queryset.filter(supplier_id=filters["supplier_id"])
        if filters.get("status"):
            queryset = queryset.filter(status=filters["status"])

        total_count = queryset.count()
        limit = filters["limit"]
        offset = filters["offset"]
        page = queryset[offset : offset + limit]
        return Response(
            {
                "count": total_count,
                "limit": limit,
                "offset": offset,
                "results": SupplierPriceUploadDetailSerializer(page, many=True).data,
            }
        )

    def post(self, request):
        serializer = SupplierPriceUploadFileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if isinstance(request.user, Supplier):
            serializer.validated_data["supplier"] = request.user

        upload = create_supplier_price_upload_from_file(data=serializer.validated_data)
        return Response(SupplierPriceUploadResponseSerializer(upload).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Загрузить прайс в виде текста (JSON-строки)",
        request=SupplierPriceUploadTextSerializer,
        responses={201: SupplierPriceUploadResponseSerializer},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadTextView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierPriceUploadTextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if isinstance(request.user, Supplier):
            serializer.validated_data["supplier"] = request.user

        upload = create_supplier_price_upload_from_text(data=serializer.validated_data)
        return Response(SupplierPriceUploadResponseSerializer(upload).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Применить результат AI-нормализации к загрузке",
        request=SupplierPriceAIApplySerializer,
        responses={200: _APPLY_AI_RESULT_SCHEMA},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceAIApplyView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        serializer = SupplierPriceAIApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = apply_supplier_price_ai_result(
            upload_id=upload_id,
            data=serializer.validated_data,
        )
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        operation_id="supplier_price_import_reject",
        tags=["Импорт прайса поставщика"],
        summary="Отклонить загрузку — откатить применённые товары и цены",
        request=None,
        responses={200: _UPLOAD_REJECT_SCHEMA},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadRejectView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        result = rollback_supplier_price_upload(upload_id=upload_id)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Запустить нормализацию загрузки (sync или async)",
        description=(
            "Запускает нормализацию прайс-листа. "
            "`run_async=true` ставит задачу в очередь Celery (ответ 202), "
            "`run_async=false` — синхронный запуск (ответ 200)."
        ),
        request=SupplierPriceUploadNormalizeSerializer,
        responses={200: _NORMALIZE_RESULT_SCHEMA, 202: _NORMALIZE_RESULT_SCHEMA},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadNormalizeView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        serializer = SupplierPriceUploadNormalizeSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data["run_async"]:
            result = start_supplier_price_upload_normalization(
                upload_id=upload_id,
                use_ai=serializer.validated_data["use_ai"],
                ai_mode=serializer.validated_data["ai_mode"],
                force=serializer.validated_data["force"],
                confirm_full=serializer.validated_data["confirm_full"],
                run_ai_cleanup=serializer.validated_data["run_ai_cleanup"],
                ai_cleanup_scope=serializer.validated_data["ai_cleanup_scope"],
            )
            response_status = status.HTTP_202_ACCEPTED if result.get("accepted") else status.HTTP_200_OK
            return Response(result, status=response_status)

        result = normalize_supplier_price_upload(
            upload_id=upload_id,
            use_ai=serializer.validated_data["use_ai"],
            ai_mode=serializer.validated_data["ai_mode"],
            force=serializer.validated_data["force"],
            confirm_full=serializer.validated_data["confirm_full"],
        )
        if serializer.validated_data["run_ai_cleanup"] and result.get("status") in {
            UploadStatus.APPLIED,
            UploadStatus.NEEDS_REVIEW,
        }:
            try:
                cleanup = start_supplier_price_upload_ai_cleanup(
                    upload_id=upload_id,
                    scope=serializer.validated_data["ai_cleanup_scope"],
                )
            except Exception as error:
                result = {**result, "ai_cleanup_accepted": False, "ai_cleanup_enqueue_error": str(error)}
            else:
                result = {
                    **result,
                    "ai_cleanup": cleanup.get("ai_cleanup"),
                    "ai_cleanup_accepted": cleanup.get("accepted"),
                }
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Запустить AI-cleanup (исправление ошибок нормализации)",
        request=SupplierPriceUploadAICleanupSerializer,
        responses={200: _AI_CLEANUP_RESULT_SCHEMA, 202: _AI_CLEANUP_RESULT_SCHEMA},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadAICleanupView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        serializer = SupplierPriceUploadAICleanupSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        result = start_supplier_price_upload_ai_cleanup(
            upload_id=upload_id,
            scope=serializer.validated_data["scope"],
            force=serializer.validated_data["force"],
        )
        response_status = status.HTTP_202_ACCEPTED if result.get("accepted") else status.HTTP_200_OK
        return Response(result, status=response_status)


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт прайса поставщика"],
        operation_id="supplier_price_import_status",
        summary="Шаг 2. Получить статус и следующее действие",
        description=(
            "Опрашивайте status_url, пока next_action=poll. Затем следуйте next_action и ссылкам "
            "из action_urls; available_actions перечисляет допустимые действия."
        ),
        responses={200: SupplierPriceUploadDetailSerializer},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        upload = get_object_or_404(supplier_price_upload_detail_queryset(), pk=upload_id)
        return Response(SupplierPriceUploadDetailSerializer(upload).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт прайса поставщика"],
        operation_id="supplier_price_import_preview",
        summary="Шаг 3. Проверить извлечённые строки",
        parameters=[OpenApiParameter("limit", OpenApiTypes.INT, description="Первые строки, максимум 100.")],
        responses={
            200: inline_serializer(
                name="SupplierPriceUploadPreview",
                fields={
                    "upload_id": s.UUIDField(),
                    "file_name": s.CharField(allow_null=True),
                    "rows_total": s.IntegerField(),
                    "preview_count": s.IntegerField(),
                    "columns": s.ListField(child=s.CharField()),
                    "detected_column_mapping": s.DictField(child=s.CharField()),
                    "column_mapping": s.DictField(child=s.CharField()),
                    "required_fields": s.DictField(),
                    "missing_required_fields": s.ListField(child=s.CharField()),
                    "ready_for_processing": s.BooleanField(),
                    "rows": s.ListField(child=s.DictField()),
                },
            )
        },
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadPreviewView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        upload = get_object_or_404(SupplierPriceUpload.objects.select_related("supplier"), pk=upload_id)
        query_serializer = SupplierPriceUploadPreviewQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        return Response(
            get_supplier_price_upload_preview(
                upload=upload,
                limit=query_serializer.validated_data["limit"],
            )
        )


@extend_schema_view(
    patch=extend_schema(
        operation_id="supplier_price_import_column_mapping",
        tags=["Импорт прайса поставщика"],
        summary="Шаг 3а (при необходимости). Сопоставить нестандартные колонки",
        description=(
            "Вызывайте, если preview вернул ready_for_processing=false из-за нераспознанных колонок. "
            "После сохранения mapping повторно запросите preview."
        ),
        request=SupplierPriceUploadColumnMappingSerializer,
        responses={200: SupplierPriceUploadDetailSerializer},
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadColumnMappingView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        serializer = SupplierPriceUploadColumnMappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = set_supplier_price_upload_column_mapping(
            upload_id=upload_id,
            mapping=serializer.validated_data["mapping"],
        )
        upload = supplier_price_upload_detail_queryset().get(pk=upload.id)
        return Response(SupplierPriceUploadDetailSerializer(upload).data)


_PRICE_UPLOAD_PLAN_SCHEMA = inline_serializer(
    name="SupplierPriceUploadChangePlan",
    fields={
        "upload_id": s.UUIDField(help_text="ID загрузки, для которой построен план."),
        "rows_total": s.IntegerField(help_text="Всего строк в загрузке."),
        "create": s.IntegerField(help_text="Сколько строк создаст новый товар."),
        "update": s.IntegerField(help_text="Сколько строк обновит существующий товар (цена/название/единица)."),
        "unchanged": s.IntegerField(help_text="Сколько строк совпадают с уже существующим товаром без изменений."),
        "reject": s.IntegerField(help_text="Сколько строк будет отклонено (не прошли обязательные поля)."),
        "duplicate": s.IntegerField(help_text="Сколько строк — дубли внутри самого файла."),
        "actionable": s.IntegerField(help_text="create + update — сколько строк реально изменят каталог."),
        "requires_confirmation": s.BooleanField(
            help_text="true, если actionable > 0 — нужно подтверждение публикации."
        ),
        "can_publish": s.BooleanField(
            help_text="false, если в файле есть reject или duplicate — публикация должна быть заблокирована."
        ),
        "problems": inline_serializer(
            name="SupplierPriceUploadPlanProblem",
            fields={
                "row_number": s.IntegerField(allow_null=True, help_text="Номер строки в исходном файле/тексте."),
                "reason": s.ChoiceField(
                    choices=("duplicate_in_file", "invalid_required_fields"),
                    help_text="Причина, по которой строка не попадёт в create/update.",
                ),
                "warnings": s.ListField(child=s.CharField(), help_text="Предупреждения парсера по этой строке."),
            },
            many=True,
            help_text="Проблемные строки, максимум 100.",
        ),
    },
)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_price_import_change_plan",
        tags=["Импорт прайса поставщика"],
        summary="Предварительный план изменений без записи в каталог",
        responses={200: _PRICE_UPLOAD_PLAN_SCHEMA},
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadChangePlanView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        upload = get_object_or_404(SupplierPriceUpload.objects.select_related("supplier"), pk=upload_id)
        return Response(get_supplier_price_upload_change_plan(upload=upload))


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Скачать исходный файл загрузки",
        responses={200: OpenApiTypes.BINARY},
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadSourceFileView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        upload = get_object_or_404(SupplierPriceUpload, pk=upload_id)
        if not upload.source_file_uri or not default_storage.exists(upload.source_file_uri):
            raise Http404("Original upload file is not available.")
        return FileResponse(
            default_storage.open(upload.source_file_uri, "rb"),
            as_attachment=True,
            filename=upload.source_file_name or "supplier-price-upload",
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт прайса поставщика"],
        operation_id="supplier_price_import_publish",
        summary="Шаг 4. Запустить обработку и публикацию прайса",
        description=(
            "Финальный шаг после preview/change-plan. По умолчанию запускается асинхронно; после ответа "
            "продолжайте опрашивать status_url из данных загрузки."
        ),
        request=SupplierPriceUploadNormalizeSerializer,
        responses={200: _NORMALIZE_RESULT_SCHEMA, 202: _NORMALIZE_RESULT_SCHEMA},
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadPublishView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        serializer = SupplierPriceUploadNormalizeSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        options = serializer.validated_data
        if options["run_async"]:
            result = start_supplier_price_upload_normalization(
                upload_id=upload_id,
                use_ai=options["use_ai"],
                ai_mode=options["ai_mode"],
                force=options["force"],
                confirm_full=options["confirm_full"],
                run_ai_cleanup=options["run_ai_cleanup"],
                ai_cleanup_scope=options["ai_cleanup_scope"],
            )
            response_status = status.HTTP_202_ACCEPTED if result.get("accepted") else status.HTTP_200_OK
            return Response(result, status=response_status)
        result = normalize_supplier_price_upload(
            upload_id=upload_id,
            use_ai=options["use_ai"],
            ai_mode=options["ai_mode"],
            force=options["force"],
            confirm_full=options["confirm_full"],
        )
        if options["run_ai_cleanup"] and result.get("status") in {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW}:
            cleanup = start_supplier_price_upload_ai_cleanup(
                upload_id=upload_id,
                scope=options["ai_cleanup_scope"],
            )
            result = {
                **result,
                "ai_cleanup": cleanup.get("ai_cleanup"),
                "ai_cleanup_accepted": cleanup.get("accepted"),
            }
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_price_import_review_items",
        tags=["Импорт прайса поставщика"],
        summary="Шаг 5а (если нужен review). Получить проблемные позиции",
        description="Используйте action_urls.review_items при next_action=review_items.",
        responses={
            200: inline_serializer(
                name="SupplierPriceUploadItemPage",
                fields={
                    "count": s.IntegerField(),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "results": SupplierPriceUploadItemSerializer(many=True),
                },
            )
        },
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadItemListView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        get_object_or_404(supplier_price_upload_detail_queryset(), pk=upload_id)
        query_serializer = SupplierPriceUploadItemListQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)

        filters = query_serializer.validated_data
        limit = filters["limit"]
        offset = filters["offset"]
        queryset = supplier_price_upload_items_queryset(upload_id=upload_id, filters=filters)
        total_count = queryset.count()
        page = queryset[offset : offset + limit]

        return Response(
            {
                "count": total_count,
                "limit": limit,
                "offset": offset,
                "results": SupplierPriceUploadItemSerializer(page, many=True).data,
            }
        )


@extend_schema_view(
    patch=extend_schema(
        operation_id="supplier_price_import_correct_item",
        tags=["Импорт прайса поставщика"],
        summary="Шаг 5б (если нужен review). Исправить проблемную строку",
        description=(
            "Сохраняет исправленные название, цену и/или единицу в извлечённой строке. После этого повторно "
            "вызовите action_urls.publish; cached result сбрасывается автоматически."
        ),
        request=SupplierPriceUploadItemCorrectionSerializer,
        responses={200: SupplierPriceUploadItemSerializer},
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadItemDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, upload_id, item_id):
        _require_owns_upload(request, upload_id)
        serializer = SupplierPriceUploadItemCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = correct_supplier_price_upload_item(
            upload_id=upload_id,
            item_id=item_id,
            data=serializer.validated_data,
        )
        return Response(
            {
                **SupplierPriceUploadItemSerializer(item).data,
                "reprocess_required": True,
            }
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Скачать превью загрузки в Excel",
        responses={200: OpenApiTypes.BINARY},
    ),
)
@extend_schema(exclude=True)
class SupplierPriceUploadPreviewExcelView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        upload = get_object_or_404(supplier_price_upload_detail_queryset(), pk=upload_id)
        items = upload.items.select_related("parsed_unit").order_by("row_number")

        wb = Workbook()
        ws = wb.active
        ws.title = "Превью прайса"

        headers = [
            "№",
            "Наименование",
            "Норм. название",
            "Цена",
            "Ед.изм.",
            "Упаковка",
            "Артикул",
            "Статус",
            "Комментарий",
        ]
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        for row_idx, item in enumerate(items, 2):
            payload = item.raw_payload or {}
            ws.cell(row=row_idx, column=1, value=item.row_number)
            ws.cell(row=row_idx, column=2, value=item.raw_name)
            ws.cell(row=row_idx, column=3, value=item.normalized_name or "")
            ws.cell(row=row_idx, column=4, value=item.parsed_price if item.parsed_price is not None else "")
            ws.cell(
                row=row_idx, column=5, value=item.parsed_unit.code if item.parsed_unit else payload.get("unit_code", "")
            )
            ws.cell(row=row_idx, column=6, value=payload.get("package_info", ""))
            ws.cell(row=row_idx, column=7, value=payload.get("supplier_sku", ""))
            ws.cell(row=row_idx, column=8, value=item.status)
            ws.cell(row=row_idx, column=9, value=item.review_comment or "")

        for col in range(1, len(headers) + 1):
            ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = 18
        ws.column_dimensions["B"].width = 35
        ws.column_dimensions["C"].width = 35

        buf = BytesIO()
        from file_imports.export_safety import escape_formula_cells

        escape_formula_cells(ws)
        wb.save(buf)
        buf.seek(0)

        filename = f"preview_{upload_id}.xlsx"
        return FileResponse(
            buf,
            as_attachment=True,
            filename=filename,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт прайса поставщика — дополнительные операции"],
        summary="Скачать отчёт по проблемным строкам",
        responses={200: OpenApiTypes.BINARY},
    )
)
@extend_schema(exclude=True)
class SupplierPriceUploadErrorReportView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, upload_id):
        _require_owns_upload(request, upload_id)
        upload = get_object_or_404(supplier_price_upload_detail_queryset(), pk=upload_id)
        items = (
            upload.items.select_related("parsed_unit")
            .filter(status__in=[UploadItemStatus.NEEDS_REVIEW, UploadItemStatus.REJECTED])
            .order_by("row_number")
        )

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Проблемные строки"
        headers = ["№ строки", "Название", "Цена", "Ед. изм.", "Статус", "Причина", "Предупреждения"]
        for column, header in enumerate(headers, 1):
            cell = worksheet.cell(row=1, column=column, value=header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
            cell.alignment = Alignment(horizontal="center")

        for row_index, item in enumerate(items, 2):
            payload = item.raw_payload or {}
            warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
            worksheet.cell(row=row_index, column=1, value=item.row_number)
            worksheet.cell(row=row_index, column=2, value=item.raw_name)
            worksheet.cell(row=row_index, column=3, value=item.raw_price or "")
            worksheet.cell(
                row=row_index,
                column=4,
                value=item.parsed_unit.code if item.parsed_unit else item.raw_unit or "",
            )
            worksheet.cell(row=row_index, column=5, value=item.status)
            worksheet.cell(row=row_index, column=6, value=item.review_comment or "")
            worksheet.cell(row=row_index, column=7, value="; ".join(str(value) for value in warnings))

        for column in range(1, len(headers) + 1):
            worksheet.column_dimensions[worksheet.cell(row=1, column=column).column_letter].width = 20
        worksheet.column_dimensions["B"].width = 40
        worksheet.column_dimensions["F"].width = 45
        worksheet.column_dimensions["G"].width = 35

        buffer = BytesIO()
        from file_imports.export_safety import escape_formula_cells

        escape_formula_cells(worksheet)
        workbook.save(buffer)
        buffer.seek(0)
        return FileResponse(
            buffer,
            as_attachment=True,
            filename=f"upload_errors_{upload_id}.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
