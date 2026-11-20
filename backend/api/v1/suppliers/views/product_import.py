"""Unified supplier product import: analyze, review session, commit."""

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import (
    OptionalSupplierCabinetJWTAuthentication,
    SupplierCabinetJWTAuthentication,
)
from api.v1.permissions import (
    IsSupplierCabinet,
    IsSupplierCabinetOrHasServiceToken,
)
from api.v1.suppliers.bot_events import (
    notify_linked_supplier_chats,
)
from api.v1.suppliers.product_import_parsing import SupplierProductImportParseError
from api.v1.suppliers.product_import_selectors import (
    SupplierProductImportQueueUnavailable,
    analyze_supplier_product_import,
    find_reusable_supplier_product_import_session,
    list_supplier_product_import_sessions,
    supplier_product_import_file_sha256,
    supplier_product_import_session_payload,
)
from api.v1.suppliers.product_import_services import (
    apply_supplier_product_import_defaults,
    bulk_set_supplier_product_import_items_availability,
    commit_actor_ref,
    commit_supplier_product_import_session,
    delete_supplier_product_import_session,
    import_committed_notification_text,
    patch_supplier_product_import_item,
    set_supplier_product_import_item_action,
)
from api.v1.suppliers.serializers import (
    SupplierProductImportAnalyzeRequestSerializer,
    SupplierProductImportCommitAcceptedSerializer,
    SupplierProductImportCommitResultSerializer,
    SupplierProductImportConflictResponseSerializer,
    SupplierProductImportDetailResponseSerializer,
    SupplierProductImportItemActionRequestSerializer,
    SupplierProductImportItemPatchRequestSerializer,
    SupplierProductImportItemsBulkAvailabilityRequestSerializer,
    SupplierProductImportItemsDefaultsRequestSerializer,
    SupplierProductImportItemsDefaultsResponseSerializer,
    SupplierProductImportItemsDefaultsValidationErrorSerializer,
    SupplierProductImportItemSerializer,
    SupplierProductImportSessionAcceptedResponseSerializer,
    SupplierProductImportSessionListItemSerializer,
    SupplierProductImportSessionListQuerySerializer,
    SupplierProductImportSessionListResponseSerializer,
    SupplierProductImportSessionQuerySerializer,
    SupplierProductImportSessionResponseSerializer,
)
from file_imports.services import ImportFileUnavailable
from suppliers.models import (
    Supplier,
    SupplierProductImportCommitStatus,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSession,
    SupplierProductImportSessionStatus,
)

from ._shared import logger


class SupplierProductImportSessionNotReady(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Импорт ещё не готов."
    default_code = "import_session_not_ready"


class SupplierProductImportQueueUnavailableError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Не удалось поставить обработку импорта в очередь. Загрузите файл ещё раз чуть позже."
    default_code = "import_queue_unavailable"


class SupplierProductImportStorageUnavailableError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Не удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."
    default_code = "import_storage_unavailable"


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Шаг 1. Загрузить файл и создать сессию импорта (ответ сразу, обработка в фоне)",
        description=(
            "Принимает файл, разбирает его (секунды для табличных файлов), создаёт сессию импорта и сразу "
            "отвечает 202 с id сессии и статусом. Дозаполнение unit/country через LLM (единица -- когда её нет в файле, "
            "страна -- только если явно указана в названии; вес/объём нетто берётся из названия без LLM; "
            "цену/кратность/мин.количество/наличие модель не трогает) и поиск до 5 похожих товаров "
            "ТЕКУЩЕГО поставщика на строку идут в фоне для файла любого размера. Статус в поле status: "
            "pending (в очереди) → processing (в работе) → ready (готово) | failed (ошибка, текст в "
            "error_message GET сессии). Опрашивайте GET status_url (можно раз в секунду): rows_done/percent "
            "растут, а уже готовые строки появляются в items по ходу обработки. "
            "PDF, DOCX/DOC, TXT и изображения (при включённом сохранении файла) извлекаются в фоне: пока идёт "
            "извлечение, processing_status=processing, rows_total=0, percent=0, items=[]; после извлечения rows_total "
            "становится равным числу строк. Ошибка чтения такого файла (нет колонки названия, слишком много строк, "
            "нечитаемый файл) приходит как processing_status=failed с error_message, а не как 400 на analyze. "
            "Ничего не пишет в /suppliers/products — только создаёт import-сессию. "
            "Строка с артикулом (supplier_sku), который уже есть у активного товара этого поставщика, сразу "
            "получает action=replace с target_supplier_product_id и единственным кандидатом (score 1.0) -- "
            "поиск по названию для неё не нужен. "
            "Повторная загрузка того же файла (sha256) тем же поставщиком, пока сессия не завершена и не упала, "
            "не создаёт новую: в ответ приходит существующая сессия (её id и текущий статус, в том числе ready). "
            "После завершения (committed) или ошибки (failed) та же загрузка создаёт новую сессию."
        ),
        request=SupplierProductImportAnalyzeRequestSerializer,
        responses={
            202: SupplierProductImportSessionAcceptedResponseSerializer,
            400: OpenApiResponse(
                description='Файл не передан/пустой/нечитаемый/больше 12000 строк (SUPPLIER_PRODUCT_IMPORT_MAX_ROWS): {"file": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            503: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Очередь обработки недоступна, сессия помечена failed: {"detail": "Не удалось поставить '
                'обработку импорта в очередь. Загрузите файл ещё раз чуть позже."}. Либо (только при включённом '
                'сохранении исходного файла) недоступно хранилище файлов, сессия не создана: {"detail": "Не '
                'удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."}',
            ),
        },
    )
)
class SupplierProductImportAnalyzeView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request):
        serializer = SupplierProductImportAnalyzeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data["file"]

        if not isinstance(request.user, Supplier):
            raise ValidationError({"file": "Доступно только из ЛК поставщика."})

        # TASK-133: тот же файл тем же поставщиком, пока сессия не завершена, -- вернуть её же, не дублируя разбор и LLM.
        content_sha256 = supplier_product_import_file_sha256(uploaded_file)
        session = find_reusable_supplier_product_import_session(supplier=request.user, content_sha256=content_sha256)
        if session is None:
            try:
                session = analyze_supplier_product_import(
                    supplier=request.user,
                    file=uploaded_file,
                    source_file_name=uploaded_file.name or "",
                    content_sha256=content_sha256,
                )
            except SupplierProductImportParseError as error:
                raise ValidationError({"file": str(error)}) from error
            except SupplierProductImportQueueUnavailable as error:
                raise SupplierProductImportQueueUnavailableError() from error
            except ImportFileUnavailable as error:
                raise SupplierProductImportStorageUnavailableError() from error

        return Response(
            SupplierProductImportSessionAcceptedResponseSerializer(
                {
                    "id": session.id,
                    "session_id": session.id,
                    "status": session.processing_status,
                    "status_url": f"/api/v1/suppliers/products/import/{session.id}",
                }
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class _SupplierProductImportSessionListPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Мои импорты товаров",
        description=(
            "Список сессий импорта текущего поставщика, новые первыми: файл, статус (pending / processing / "
            "ready / failed), прогресс и время. Нужен, чтобы после закрытия вкладки или ухода на другую страницу "
            "найти загруженный файл и открыть его по id (GET /suppliers/products/import/{id}). По умолчанию "
            "только незавершённые (status=open, включая упавшие). Только из кабинета поставщика."
        ),
        parameters=[SupplierProductImportSessionListQuerySerializer],
        responses={
            200: SupplierProductImportSessionListResponseSerializer,
            400: OpenApiResponse(description="Некорректный параметр запроса: status, page или page_size."),
            401: OpenApiResponse(description="Нет JWT кабинета поставщика, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Нужна сессия кабинета поставщика (сервисный токен не подходит)."),
        },
    )
)
class SupplierProductImportSessionListView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get(self, request):
        query = SupplierProductImportSessionListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        status_filter = query.validated_data["status"]
        queryset = list_supplier_product_import_sessions(
            supplier=request.user, status=None if status_filter == "all" else status_filter
        )
        paginator = _SupplierProductImportSessionListPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(SupplierProductImportSessionListItemSerializer(page, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Состояние сессии импорта",
        description=(
            "Статус и прогресс обработки (processing_status, rows_done, rows_total, percent) и готовые строки. "
            "Пока сессия pending/processing, в items приходят только уже обработанные строки (дозаполненные и "
            "сопоставленные; они больше не меняются), список растёт по ходу работы; в ready приходят все. "
            "Для частого опроса: include_items=false (только статус и счётчики). Строки отдаются страницами: "
            "limit (по умолчанию 500, максимум 1000), курсор after_row_number=N (строки после N); в ответе "
            "has_more и next_after_row_number — откуда брать следующую страницу. action — только строки с этим "
            "действием (вкладки таблицы). counts считаются по всем готовым строкам, а не по возвращённой странице. "
            "field_meta (диагностика парсера) в строках по умолчанию не приходит — include_meta=true."
        ),
        parameters=[SupplierProductImportSessionQuerySerializer],
        responses={
            200: SupplierProductImportSessionResponseSerializer,
            400: OpenApiResponse(
                description="Некорректный параметр запроса: include_items, include_meta, after_row_number, limit "
                "или action."
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(description="Сессия не найдена или принадлежит другому поставщику."),
        },
    ),
    delete=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Удалить сессию импорта",
        description=(
            "Убирает незавершённую или упавшую сессию вместе с оставшимися строками. Товары, применённые из неё "
            "раньше, остаются в каталоге. Нельзя, пока идёт разбор или применение."
        ),
        responses={
            204: OpenApiResponse(description="Сессия удалена."),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(description="Сессия не найдена или принадлежит другому поставщику."),
            409: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Сессия ещё обрабатывается: {"detail": "Импорт ещё обрабатывается."} ИЛИ идёт '
                'применение: {"detail": "Идёт применение импорта."}',
            ),
        },
    ),
)
class SupplierProductImportSessionDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def get(self, request, session_id):
        session = self._get_session(request, session_id)
        query = SupplierProductImportSessionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        payload = supplier_product_import_session_payload(
            session,
            include_items=query.validated_data["include_items"],
            after_row_number=query.validated_data.get("after_row_number"),
            limit=query.validated_data.get("limit"),
            action=query.validated_data.get("action"),
            price_changed=query.validated_data.get("price_changed", False),
        )
        context = {"exclude_field_meta": not query.validated_data["include_meta"]}
        return Response(SupplierProductImportSessionResponseSerializer(payload, context=context).data)

    def delete(self, request, session_id):
        session = self._get_session(request, session_id)
        if session.processing_status in (
            SupplierProductImportProcessingStatus.PENDING,
            SupplierProductImportProcessingStatus.PROCESSING,
        ):
            raise SupplierProductImportSessionNotReady("Импорт ещё обрабатывается.")
        if session.commit_status in (
            SupplierProductImportCommitStatus.PENDING,
            SupplierProductImportCommitStatus.RUNNING,
        ):
            raise SupplierProductImportSessionNotReady("Идёт применение импорта.")
        delete_supplier_product_import_session(session=session)
        return Response(status=204)

    @staticmethod
    def _get_session(request, session_id):
        queryset = SupplierProductImportSession.objects.all()
        if isinstance(request.user, Supplier):
            queryset = queryset.filter(supplier=request.user)
        return get_object_or_404(queryset, pk=session_id)

    @staticmethod
    def _get_ready_session(request, session_id, *, allow_committing=False):
        """Правки и commit -- только у готовой сессии (TASK-132): пока идёт фоновая обработка,
        строки не дозаполнены и не сопоставлены; после failed правки бессмысленны. Пока идёт фоновое применение
        (commit_status pending/running) правки тоже отклоняются: воркер держит блокировку сессии, правка зависла бы.
        Сам commit передаёт allow_committing=True и отвечает на повтор своим текстом."""
        session = SupplierProductImportSessionDetailView._get_session(request, session_id)
        if session.processing_status in (
            SupplierProductImportProcessingStatus.PENDING,
            SupplierProductImportProcessingStatus.PROCESSING,
        ):
            raise SupplierProductImportSessionNotReady("Импорт ещё обрабатывается.")
        if session.processing_status == SupplierProductImportProcessingStatus.FAILED:
            raise SupplierProductImportSessionNotReady("Обработка импорта завершилась ошибкой.")
        if not allow_committing and session.commit_status in (
            SupplierProductImportCommitStatus.PENDING,
            SupplierProductImportCommitStatus.RUNNING,
        ):
            raise SupplierProductImportSessionNotReady("Идёт применение импорта.")
        return session


@extend_schema_view(
    patch=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Правка полей строки импорта",
        description=(
            "Правка name/unit_id сбрасывает найденное совпадение и запускает поиск дублей заново только "
            "для этой строки (ТЗ §14)."
        ),
        request=SupplierProductImportItemPatchRequestSerializer,
        responses={
            200: SupplierProductImportItemSerializer,
            400: OpenApiResponse(description="Некорректное значение поля."),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(description="Сессия или строка не найдена / принадлежит другому поставщику."),
            409: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Сессия ещё в фоновой обработке: {"detail": "Импорт ещё обрабатывается."} ИЛИ обработка '
                'завершилась ошибкой: {"detail": "Обработка импорта завершилась ошибкой."}',
            ),
        },
    )
)
class SupplierProductImportItemDetailView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, session_id, item_id):
        session = SupplierProductImportSessionDetailView._get_ready_session(request, session_id)
        item = get_object_or_404(session.items, pk=item_id)

        serializer = SupplierProductImportItemPatchRequestSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        try:
            item = patch_supplier_product_import_item(
                item=item, data=serializer.validated_data, actor_id=getattr(request.user, "pk", None)
            )
        except DjangoValidationError as error:
            raise ValidationError({"detail": error.messages}) from error
        return Response(SupplierProductImportItemSerializer(item).data)


@extend_schema_view(
    put=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Изменить решение по строке (new/replace/skip)",
        request=SupplierProductImportItemActionRequestSerializer,
        responses={
            200: SupplierProductImportItemSerializer,
            400: OpenApiResponse(
                description="action=replace без активного target_supplier_product_id текущего поставщика."
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(description="Сессия или строка не найдена / принадлежит другому поставщику."),
            409: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Сессия ещё в фоновой обработке: {"detail": "Импорт ещё обрабатывается."} ИЛИ обработка '
                'завершилась ошибкой: {"detail": "Обработка импорта завершилась ошибкой."}',
            ),
        },
    )
)
class SupplierProductImportItemActionView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def put(self, request, session_id, item_id):
        session = SupplierProductImportSessionDetailView._get_ready_session(request, session_id)
        item = get_object_or_404(session.items, pk=item_id)

        serializer = SupplierProductImportItemActionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            item = set_supplier_product_import_item_action(
                item=item,
                action=serializer.validated_data["action"],
                target_supplier_product_id=serializer.validated_data.get("target_supplier_product_id"),
                actor_id=getattr(request.user, "pk", None),
            )
        except DjangoValidationError as error:
            raise ValidationError({"target_supplier_product_id": error.messages}) from error

        return Response(SupplierProductImportItemSerializer(item).data)


@extend_schema_view(
    patch=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Массово выставить статус наличия для выбранных строк (ТЗ §18, MVP)",
        description="Строки со status=skip молча исключаются — их поля read-only (ТЗ §15).",
        request=SupplierProductImportItemsBulkAvailabilityRequestSerializer,
        responses={
            200: OpenApiResponse(description='{"updated_count": 3}'),
            400: OpenApiResponse(description="Пустой item_ids или больше 1000 элементов."),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(description="Сессия не найдена / принадлежит другому поставщику."),
            409: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Сессия ещё в фоновой обработке: {"detail": "Импорт ещё обрабатывается."} ИЛИ обработка '
                'завершилась ошибкой: {"detail": "Обработка импорта завершилась ошибкой."}',
            ),
        },
    )
)
class SupplierProductImportItemsBulkView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, session_id):
        session = SupplierProductImportSessionDetailView._get_ready_session(request, session_id)
        serializer = SupplierProductImportItemsBulkAvailabilityRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated_count = bulk_set_supplier_product_import_items_availability(
            session=session,
            item_ids=serializer.validated_data["item_ids"],
            availability=serializer.validated_data["availability"],
        )
        return Response({"updated_count": updated_count})


@extend_schema_view(
    patch=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Значения по умолчанию для всего файла",
        description=(
            "Заполняет переданные поля (country_of_origin, min_order_qty, delivery_multiple, availability, "
            "unit_id) только в тех строках сессии, где они пусты. Уже заполненные значения не перезаписываются; "
            "строки со status=skip и уже импортированные пропускаются. Заполненные поля помечаются "
            '`field_sources[поле] = "user"`, а сообщение «Не заполнено поле …» снимается. '
            "unit_id: парсер не подставляет единицу по умолчанию, строки без единицы ждут выбора; для кг, л и шт "
            "в строке без нетто нетто становится 1 такой единицы (`field_sources` нетто = `rule`). "
            "Нужно передать хотя бы одно поле."
        ),
        request=SupplierProductImportItemsDefaultsRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=SupplierProductImportItemsDefaultsResponseSerializer,
                description="Сколько строк изменилось.",
            ),
            400: OpenApiResponse(
                response=SupplierProductImportItemsDefaultsValidationErrorSerializer,
                description='Не передано ни одного поля: {"non_field_errors": ["Укажите хотя бы одно поле: '
                'country_of_origin, min_order_qty, delivery_multiple, availability или unit_id."]}. Некорректное значение '
                'поля: {"<поле>": ["<причина>"]}.',
            ),
            401: OpenApiResponse(
                response=SupplierProductImportDetailResponseSerializer,
                description='Нет JWT, либо JWT невалиден/просрочен. Без JWT: {"detail": "Учетные данные не были '
                'предоставлены."}',
            ),
            404: OpenApiResponse(
                response=SupplierProductImportDetailResponseSerializer,
                description='Сессия не найдена / принадлежит другому поставщику: {"detail": "No SupplierProductImportSession matches the given query."}',
            ),
            409: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Сессия ещё в фоновой обработке: {"detail": "Импорт ещё обрабатывается."} ИЛИ обработка '
                'завершилась ошибкой: {"detail": "Обработка импорта завершилась ошибкой."} ИЛИ идёт применение '
                'импорта: {"detail": "Идёт применение импорта."}',
            ),
        },
    )
)
class SupplierProductImportItemsDefaultsView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def patch(self, request, session_id):
        session = SupplierProductImportSessionDetailView._get_ready_session(request, session_id)
        serializer = SupplierProductImportItemsDefaultsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated_count = apply_supplier_product_import_defaults(
            session=session, values=serializer.validated_data, actor_id=getattr(request.user, "pk", None)
        )
        return Response({"updated_count": updated_count})


@extend_schema_view(
    post=extend_schema(
        tags=["Импорт товаров поставщика"],
        summary="Импортировать готовые строки сессии",
        description=(
            "Пишет только new/replace строки с заполненными обязательными полями. Заимпортированные и "
            "пропущенные (skip) строки удаляются из сессии; review и недоделанные остаются (ТЗ §16-17).\n\n"
            "**Режим по умолчанию (синхронный, `SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC=false`)**: ответ 200 с итогом. "
            "Когда строк не осталось, сессия удаляется (`session_deleted=true`), и по её session_id дальше 404.\n\n"
            "**Фоновый режим (`SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC=true`)**: ответ 202 `{commit_status, status_url}`, "
            "применение выполняет воркер. Опрашивайте GET `status_url` (лучше `include_items=false`): "
            "`commit_status` pending -> running -> none (итог в `commit_result`, тот же объект, что в ответе 200) "
            "либо failed (текст в `commit_error`; после failed можно повторить commit). Пока `commit_status` "
            "pending/running, любые правки сессии отвечают 409 «Идёт применение импорта.». Когда строк не осталось, "
            "сессия не удаляется сразу, а получает `status=committed` (`session_deleted=false`) и хранится для "
            "чтения итога."
        ),
        request=None,
        responses={
            200: SupplierProductImportCommitResultSerializer,
            202: SupplierProductImportCommitAcceptedSerializer,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            404: OpenApiResponse(
                description="Сессия не найдена / принадлежит другому поставщику / уже удалена после полного импорта."
            ),
            409: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description='Сессия ещё в фоновой обработке: {"detail": "Импорт ещё обрабатывается."} ИЛИ обработка '
                'завершилась ошибкой: {"detail": "Обработка импорта завершилась ошибкой."} ИЛИ '
                '{"detail": "Сессия уже импортирована."} (commit повторно) ИЛИ, в фоновом режиме, '
                '{"detail": "Применение уже выполняется."} (commit уже в очереди или выполняется).',
            ),
            503: OpenApiResponse(
                response=SupplierProductImportConflictResponseSerializer,
                description="Только в фоновом режиме: брокер очереди недоступен, применение не поставлено: "
                '{"detail": "Не удалось поставить применение в очередь. Повторите чуть позже."} '
                "(commit_status=failed, commit можно повторить).",
            ),
        },
    )
)
class SupplierProductImportCommitView(APIView):
    authentication_classes = [OptionalSupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetOrHasServiceToken]

    def post(self, request, session_id):
        session = SupplierProductImportSessionDetailView._get_ready_session(request, session_id, allow_committing=True)
        if session.status == SupplierProductImportSessionStatus.COMMITTED:
            return Response({"detail": "Сессия уже импортирована."}, status=status.HTTP_409_CONFLICT)
        if getattr(settings, "SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC", False):
            return self._enqueue(request, session)
        if session.commit_status in (
            SupplierProductImportCommitStatus.PENDING,
            SupplierProductImportCommitStatus.RUNNING,
        ):
            # Флаг выключили при уже идущем фоновом применении: синхронный commit параллельно с воркером не пускаем.
            return Response({"detail": "Применение уже выполняется."}, status=status.HTTP_409_CONFLICT)

        result = commit_supplier_product_import_session(session=session, actor=request.user)
        if result["imported_count"]:
            notify_linked_supplier_chats(
                supplier=session.supplier,
                text=import_committed_notification_text(
                    supplier=session.supplier, imported_count=result["imported_count"]
                ),
            )
        return Response(SupplierProductImportCommitResultSerializer(result).data)

    @staticmethod
    def _enqueue(request, session):
        from api.v1.suppliers.tasks import commit_supplier_product_import_session_task

        with transaction.atomic():
            # Под блокировкой строки сессии: два одновременных POST не поставят две задачи.
            locked = SupplierProductImportSession.objects.select_for_update().filter(pk=session.pk).first()
            if locked is None:
                raise NotFound("Сессия импорта уже удалена.")
            if locked.commit_status in (
                SupplierProductImportCommitStatus.PENDING,
                SupplierProductImportCommitStatus.RUNNING,
            ):
                return Response({"detail": "Применение уже выполняется."}, status=status.HTTP_409_CONFLICT)
            if locked.status == SupplierProductImportSessionStatus.COMMITTED:
                return Response({"detail": "Сессия уже импортирована."}, status=status.HTTP_409_CONFLICT)
            locked.commit_status = SupplierProductImportCommitStatus.PENDING
            locked.commit_error = None
            locked.commit_result = None
            locked.save(update_fields=["commit_status", "commit_error", "commit_result", "updated_at"])
        try:
            commit_supplier_product_import_session_task.delay(
                session_id=str(session.id), actor=commit_actor_ref(request.user)
            )
        except Exception:
            logger.exception("supplier_product_import.commit.enqueue_failed session_id=%s", session.id)
            SupplierProductImportSession.objects.filter(pk=session.pk).update(
                commit_status=SupplierProductImportCommitStatus.FAILED,
                commit_error="Не удалось поставить применение в очередь.",
                updated_at=timezone.now(),
            )
            raise SupplierProductImportQueueUnavailableError(
                "Не удалось поставить применение в очередь. Повторите чуть позже."
            )
        return Response(
            {
                "commit_status": SupplierProductImportCommitStatus.PENDING,
                "status_url": f"/api/v1/suppliers/products/import/{session.id}",
            },
            status=status.HTTP_202_ACCEPTED,
        )
