"""Venue product import (file preview, apply, master match)."""

from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.catalog.import_sessions import (
    build_venue_preview_rows,
    catalog_import_async_extraction,
    persist_catalog_import_session,
)
from api.v1.catalog.importers import parse_catalog_file
from api.v1.catalog.selectors import (
    resolve_master_product_matches_for_import,
    resolve_venue_import_rows,
)
from api.v1.catalog.serializers import (
    VenueImportApplyErrorSerializer,
    VenueImportApplyRequestSerializer,
    VenueImportApplyRowSerializer,
    VenueImportFileAcceptedResponseSerializer,
    VenueImportFilePreviewRequestSerializer,
    VenueImportFilePreviewResponseSerializer,
    VenueImportMasterMatchApplyRequestSerializer,
    VenueImportMasterMatchApplyRowSerializer,
    VenueImportMasterMatchPreviewRequestSerializer,
    VenueImportMasterMatchRowSerializer,
    VenueImportPreviewRequestSerializer,
    VenueImportRowSerializer,
)
from api.v1.catalog.services import (
    apply_master_product_import_matches,
    apply_venue_import,
)
from api.v1.catalog.tasks import process_venue_import_session_task
from api.v1.permissions import (
    IsVenueMember,
)
from catalog.models import (
    CatalogImportDomain,
    CatalogImportProcessingStatus,
    CatalogImportSession,
)
from core.models import (
    User,
    Venue,
)
from file_imports.models import ImportFileScope
from file_imports.services import import_file_storage_enabled
from file_imports.versions import parser_version_for

from ._shared import _store_import_file_if_enabled
from .import_common import ImportQueueUnavailableError


def _require_import_user(request):
    """IsVenueMember also accepts the shared service token — but it reads it from
    `X-Service-Token`, a header CoreUserJWTAuthentication never looks at, so a
    request carrying only that header passes authentication (request.user stays
    AnonymousUser) *and* the permission check. The import endpoints have no valid
    anonymous/service mode at all (Plan 5 Global Constraint: every alias row needs
    a real core.User in confirmed_by), so deny outright instead of silently
    substituting None the way the optional-user catalog views do."""
    if not isinstance(request.user, User):
        raise PermissionDenied("Импорт требует авторизации реальным пользователем, сервис-токен не подходит.")


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_import_file_preview",
        tags=["Импорт справочника товаров компании"],
        summary="Шаг 1. Загрузить файл и получить предварительный результат",
        description=(
            "**Основная точка входа для импорта файла во фронтенде.**\n\n"
            "Передайте файл в поле `file` с `Content-Type: multipart/form-data`. Поддерживаются "
            "`.xlsx`, `.xlsm` и `.csv`, максимум 10 МБ и 1000 строк. Ручка сама находит заголовки и "
            "колонки названия, единицы измерения и примечания, сравнивает строки с товарной матрицей и "
            "возвращает предлагаемые действия. База данных на этом шаге не изменяется.\n\n"
            "Покажите `rows` пользователю. Строка с `action = null` требует решения: пользователь выбирает "
            "одного из `candidates` (до 5, точное совпадение первым) для `replace` — заменить название и "
            "единицу выбранной позиции, ID сохраняется — или для `update_existing` («Не добавлять», товар уже "
            "есть — запомнить сопоставление), либо `skip`/`create_new`. Строки без решения в `decisions` не "
            "передавайте. Затем передайте `decisions` в "
            "`POST /api/v1/catalog/venues/{venue_id}/product-matrix/import/apply`: `row_number`, `raw_name`, "
            "`requested_unit_label`, `note`, `action` и `matrix_item_id` для `replace`/`update_existing`. "
            "`update_existing` существующий товар не перезаписывает.\n\n"
            "**Режим «всегда в воркере».** При включённом флаге `CATALOG_IMPORT_ALWAYS_ASYNC` (по умолчанию "
            "выключен; также нужны `IMPORT_FILE_STORAGE_ENABLED` и `VENUE_IMPORT_ASYNC_ENABLED`) файл любого "
            "размера обрабатывается в воркере: ответ 202 с `session_id` и `status_url` "
            "(`/api/v1/catalog/import-sessions/{session_id}`), строки появляются после "
            "`processing_status=ready`. При выключенном флаге поведение прежнее."
        ),
        request=VenueImportFilePreviewRequestSerializer,
        examples=[
            OpenApiExample(
                "Результат разбора файла",
                value={
                    "sheet_name": "Товары",
                    "header_row_number": 2,
                    "detected_columns": {"name": "Наименование", "unit": "Ед. изм.", "note": "Комментарий"},
                    "rows": [
                        {
                            "row_number": 3,
                            "raw_name": "Мука пшеничная в/с",
                            "requested_unit_label": "кг",
                            "note": "мешок 25 кг",
                            "action": None,
                            "candidates": [
                                {
                                    "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                                    "requested_name": "Мука пшеничная",
                                    "requested_unit_label": "кг",
                                    "score": 0.79,
                                }
                            ],
                            "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "matrix_item_requested_name": "Мука пшеничная",
                            "auto_resolved": False,
                        },
                        {
                            "row_number": 4,
                            "raw_name": "Сахар тростниковый",
                            "requested_unit_label": "кг",
                            "note": None,
                            "action": "create_new",
                            "candidates": [],
                            "matrix_item_id": None,
                            "matrix_item_requested_name": None,
                            "auto_resolved": False,
                        },
                    ],
                },
                response_only=True,
                status_codes=["200"],
            )
        ],
        responses={
            200: VenueImportFilePreviewResponseSerializer,
            202: VenueImportFileAcceptedResponseSerializer,
            400: OpenApiResponse(
                description='Файл не передан/пустой/слишком большой/неподдерживаемый формат: {"file": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Не участник (owner/manager) этого заведения, либо запрос авторизован только "
                'сервис-токеном без реального пользователя: {"detail": "..."}'
            ),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
            503: OpenApiResponse(
                description="Только при включённом сохранении исходного файла: хранилище файлов недоступно, "
                'сессия не создана: {"detail": "Не удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."}; '
                'либо очередь обработки недоступна, сессия помечена failed: {"detail": "Не удалось поставить обработку '
                'импорта в очередь. Загрузите файл ещё раз чуть позже."}'
            ),
        },
    )
)
class VenueProductImportFilePreviewView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, venue_id):
        _require_import_user(request)
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueImportFilePreviewRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        async_enabled = bool(getattr(settings, "VENUE_IMPORT_ASYNC_ENABLED", False)) and import_file_storage_enabled()
        max_rows = int(getattr(settings, "VENUE_IMPORT_MAX_ROWS", 12000)) if async_enabled else 1000
        extract_async = async_enabled and catalog_import_async_extraction(ser.validated_data["file"])
        parsed = (
            None
            if extract_async
            else parse_catalog_file(
                ser.validated_data["file"],
                max_rows=max_rows,
                canonical_enabled=getattr(settings, "CANONICAL_VENUE_PARSER_ENABLED", False),
            )
        )
        import_file = _store_import_file_if_enabled(
            uploaded_file=ser.validated_data["file"], scope_type=ImportFileScope.VENUE, scope_id=venue.id
        )
        threshold = int(getattr(settings, "VENUE_IMPORT_ASYNC_THRESHOLD_ROWS", 500) or 500)
        if async_enabled and (extract_async or len(parsed.rows) > threshold):
            session = CatalogImportSession.objects.create(
                owner=request.user,
                venue=venue,
                domain=CatalogImportDomain.VENUE,
                parser_version=parser_version_for(getattr(settings, "CANONICAL_VENUE_PARSER_ENABLED", False)),
                source_file_name=getattr(ser.validated_data["file"], "name", "") or "",
                import_file=import_file,
                processing_status=CatalogImportProcessingStatus.PENDING,
                rows_total=len(parsed.rows) if parsed else 0,
                meta={
                    "sheet_name": parsed.sheet_name,
                    "header_row_number": parsed.header_row_number,
                    "detected_columns": parsed.detected_columns,
                    "rows_without_name": parsed.rows_without_name,
                }
                if parsed
                else {},
            )
            try:
                process_venue_import_session_task.delay(session_id=str(session.id))
            except Exception as error:
                CatalogImportSession.objects.filter(pk=session.pk).update(
                    processing_status=CatalogImportProcessingStatus.FAILED,
                    error_message="Не удалось поставить обработку в очередь.",
                )
                raise ImportQueueUnavailableError() from error
            return Response(
                VenueImportFileAcceptedResponseSerializer(
                    {
                        "session_id": session.id,
                        "status": session.processing_status,
                        "status_url": f"/api/v1/catalog/import-sessions/{session.id}",
                    }
                ).data,
                status=status.HTTP_202_ACCEPTED,
            )
        matches = resolve_venue_import_rows(
            venue=venue,
            raw_names=[row["name"] for row in parsed.rows],
            unit_labels=[row.get("unit") for row in parsed.rows],
            **({"allow_unit_llm": False} if any(row.get("_parsed") for row in parsed.rows) else {}),
        )
        rows = build_venue_preview_rows(parsed.rows, matches)
        payload = {
            "sheet_name": parsed.sheet_name,
            "header_row_number": parsed.header_row_number,
            "detected_columns": parsed.detected_columns,
            "total_rows": len(rows) + parsed.rows_without_name,
            "rows_without_name": parsed.rows_without_name,
            "rows": rows,
        }
        response_payload = VenueImportFilePreviewResponseSerializer(payload).data
        session = persist_catalog_import_session(
            owner=request.user,
            venue=venue,
            domain=CatalogImportDomain.VENUE,
            source_file_name=getattr(ser.validated_data["file"], "name", ""),
            rows=response_payload["rows"],
            parsed_rows=parsed.rows,
            import_file=import_file,
        )
        response_payload["session_id"] = str(session.id)
        return Response(response_payload)


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_import_raw_names_preview",
        tags=["Импорт справочника компании — дополнительные операции"],
        summary="Дополнительно. Построить превью из уже распарсенных названий",
        description=(
            "**Альтернативный вход без загрузки файла. Обычному фронтенду для импорта файла эта ручка "
            "не нужна.**\n\n"
            "Используйте её, только если вызывающая сторона уже самостоятельно разобрала файл или получила "
            "названия из другой системы. Передайте JSON-массив `raw_names`. Ручка не меняет базу данных и "
            "для каждого названия возвращает до 5 похожих позиций (`candidates`); при найденных похожих "
            "`action = null` (решение пользователя), без них — `create_new`. Ранее подтверждённое "
            "сопоставление возвращается с `auto_resolved=true`.\n\n"
            "После проверки результата сформируйте `decisions` и передайте их в "
            "`POST /api/v1/catalog/venues/{venue_id}/product-matrix/import/apply`."
        ),
        request=VenueImportPreviewRequestSerializer,
        examples=[
            OpenApiExample(
                "Названия уже извлечены вызывающей стороной",
                value={"raw_names": ["Мука пшеничная в/с", "Сахар тростниковый"]},
                request_only=True,
            ),
            OpenApiExample(
                "Предлагаемые решения",
                value=[
                    {
                        "raw_name": "Мука пшеничная в/с",
                        "action": None,
                        "candidates": [
                            {
                                "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                                "requested_name": "Мука пшеничная",
                                "requested_unit_label": "кг",
                                "score": 0.79,
                            }
                        ],
                        "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "matrix_item_requested_name": "Мука пшеничная",
                        "auto_resolved": False,
                    },
                    {
                        "raw_name": "Сахар тростниковый",
                        "action": "create_new",
                        "candidates": [],
                        "matrix_item_id": None,
                        "matrix_item_requested_name": None,
                        "auto_resolved": False,
                    },
                ],
                response_only=True,
                status_codes=["200"],
            ),
        ],
        responses={
            200: VenueImportRowSerializer(many=True),
            400: OpenApiResponse(description='raw_names не передан или пуст: {"raw_names": [...]}'),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Не участник (owner/manager) этого заведения, либо запрос авторизован "
                "только сервис-токеном (`X-Service-Token`) без реального пользователя — импорт "
                'требует настоящего core.User: {"detail": "..."}'
            ),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductImportPreviewView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        _require_import_user(request)
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueImportPreviewRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = resolve_venue_import_rows(venue=venue, raw_names=ser.validated_data["raw_names"])
        return Response(VenueImportRowSerializer(rows, many=True).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_import_apply",
        tags=["Импорт справочника товаров компании"],
        summary="Шаг 2. Применить подтверждённые решения",
        description=(
            "**Вызывайте после `.../import/file/preview` — это второй и последний обязательный шаг "
            "фронтового импорта файла.** Ручка также принимает решения, сформированные дополнительной "
            "JSON-ручкой `.../import/preview`.\n\n"
            "Передайте по каждой строке поля из preview и выбранный `action`. Для `update_existing` также "
            "обязателен `matrix_item_id`. `update_existing` сохраняет соответствие с существующим товаром, "
            "не изменяя сам товар; `create_new` создаёт товар компании с названием, единицей, примечанием "
            "и `source=accounting_system`; "
            "`skip` пропускает строку. `replace` заменяет название и единицу измерения выбранного товара "
            "компании (ID, связи и примечание сохраняются) и запоминает сопоставление. Передавайте только "
            "строки, по которым решение принято. Весь запрос применяется одной транзакцией.\n\n"
            "После успешного ответа основной импорт завершён. `master-match/*` нужен только для отдельной "
            "ручной проверки привязок к мастер-каталогу."
            "\n\nЧастичный apply (TASK-74): decisions может покрывать не все строки preview — «оставленные на "
            "потом» просто не попадают в decisions. Чтобы такие строки не получили ложный "
            "missing_from_latest_import=true, передайте их matrix_item_id (из candidates/matrix_item_id "
            "соответствующей строки preview) в present_matrix_item_ids. При ПОСЛЕДОВАТЕЛЬНОСТИ частичных "
            "apply-вызовов для одного и того же файла источник id для present_matrix_item_ids — не только "
            "исходный preview: включайте также matrix_item_id из ответа каждого ПРЕДЫДУЩЕГО apply-вызова "
            "этой же последовательности (например, товар, только что созданный через create_new, не имеет "
            "id в исходном preview вообще — его защитит только id из того ответа). Лишние id ничего не "
            "создают и не меняют — их единственный эффект: если у товара уже стоит "
            "missing_from_latest_import=true, он будет снят."
        ),
        request=VenueImportApplyRequestSerializer,
        examples=[
            OpenApiExample(
                "Решения из результата preview",
                value={
                    "decisions": [
                        {
                            "row_number": 3,
                            "raw_name": "Мука пшеничная в/с",
                            "requested_unit_label": "кг",
                            "note": "мешок 25 кг",
                            "action": "update_existing",
                            "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        },
                        {
                            "row_number": 4,
                            "raw_name": "Сахар тростниковый",
                            "requested_unit_label": "кг",
                            "note": None,
                            "action": "create_new",
                        },
                        {"row_number": 5, "raw_name": "Служебная строка", "action": "skip"},
                    ],
                    "present_matrix_item_ids": [],
                },
                request_only=True,
            ),
            OpenApiExample(
                "Результат применения",
                value=[
                    {
                        "raw_name": "Мука пшеничная в/с",
                        "action": "update_existing",
                        "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "matrix_item_requested_name": "Мука пшеничная",
                        "alias_created": True,
                    },
                    {
                        "raw_name": "Сахар тростниковый",
                        "action": "create_new",
                        "matrix_item_id": "4fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "matrix_item_requested_name": "Сахар тростниковый",
                        "alias_created": True,
                    },
                    {
                        "raw_name": "Служебная строка",
                        "action": "skip",
                        "matrix_item_id": None,
                        "matrix_item_requested_name": None,
                        "alias_created": False,
                    },
                ],
                response_only=True,
                status_codes=["200"],
            ),
        ],
        responses={
            200: VenueImportApplyRowSerializer(many=True),
            400: OpenApiResponse(
                response=VenueImportApplyErrorSerializer,
                description="Ничего не записано; decisions — всегда список. Причины: decisions пуст "
                "(«Нужно передать хотя бы одно решение.») или больше 1000 («Не более 1000 решений за "
                "запрос.»); у решения нет "
                "matrix_item_id (update_existing/replace) или requested_unit_label (replace) — полевая ошибка "
                "`Обязателен при action=...`; товара нет в матрице этой компании; товар архивный; несколько "
                "решений replace указывают на одну и ту же карточку (сообщение перечисляет все такие строки); "
                "present_matrix_item_ids содержит невалидный UUID или больше 1000 элементов.",
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Не участник (owner/manager) этого заведения, либо запрос авторизован "
                "только сервис-токеном (`X-Service-Token`) без реального пользователя — каждой "
                'alias-записи нужен confirmed_by-пользователь: {"detail": "..."}'
            ),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductImportApplyView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        _require_import_user(request)
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueImportApplyRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = apply_venue_import(
            venue=venue,
            user=request.user,
            decisions=ser.validated_data["decisions"],
            present_matrix_item_ids=ser.validated_data["present_matrix_item_ids"],
        )
        return Response(VenueImportApplyRowSerializer(rows, many=True).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_import_master_match_preview",
        tags=["Импорт справочника компании — дополнительные операции"],
        summary="Дополнительно. Предложить привязку к мастер-каталогу",
        description=(
            "**Не загружает файл и не является обязательной частью импорта.** Используйте после "
            "`.../import/apply`, только если пользователю нужно вручную проверить или изменить привязки "
            "товаров компании к мастер-каталогу выбранного владельца (`owner_id`).\n\n"
            "Ручка не меняет базу данных. Для каждого `matrix_item_id` возвращает предложение: "
            "`keep_existing` — привязка уже есть, `link_existing` — найден похожий мастер-товар, "
            "`create_new` — подходящий товар не найден. Затем пользовательские решения можно передать в "
            "`.../import/master-match/apply`."
        ),
        request=VenueImportMasterMatchPreviewRequestSerializer,
        examples=[
            OpenApiExample(
                "Проверить привязки после импорта",
                value={
                    "owner_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "matrix_item_ids": [
                        "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "4fa85f64-5717-4562-b3fc-2c963f66afa6",
                    ],
                },
                request_only=True,
            )
        ],
        responses={
            200: VenueImportMasterMatchRowSerializer(many=True),
            400: OpenApiResponse(
                description="matrix_item_ids не передан/пуст, owner_id не активный владелец этой компании, "
                'либо matrix_item_ids содержит id не из этой компании: {"...": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Не участник (owner/manager) этого заведения, либо запрос авторизован "
                'только сервис-токеном без реального пользователя: {"detail": "..."}'
            ),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductImportMasterMatchPreviewView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        _require_import_user(request)
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueImportMasterMatchPreviewRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = resolve_master_product_matches_for_import(
            venue=venue,
            owner_id=ser.validated_data["owner_id"],
            matrix_item_ids=ser.validated_data["matrix_item_ids"],
        )
        return Response(VenueImportMasterMatchRowSerializer(rows, many=True).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_import_master_match_apply",
        tags=["Импорт справочника компании — дополнительные операции"],
        summary="Дополнительно. Применить привязку к мастер-каталогу",
        description=(
            "**Не является обязательной частью импорта файла.** Вызывайте только после "
            "`.../import/master-match/preview`, если пользователь подтвердил ручные решения. "
            "`link_existing` привязывает существующий мастер-товар, `create_new` создаёт мастер-товар "
            "владельца и привязывает его, `skip` не изменяет строку товарной матрицы."
        ),
        request=VenueImportMasterMatchApplyRequestSerializer,
        examples=[
            OpenApiExample(
                "Подтверждённые привязки",
                value={
                    "owner_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "decisions": [
                        {
                            "matrix_item_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "action": "link_existing",
                            "master_product_id": "5fa85f64-5717-4562-b3fc-2c963f66afa6",
                        },
                        {
                            "matrix_item_id": "4fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "action": "skip",
                        },
                    ],
                },
                request_only=True,
            )
        ],
        responses={
            200: VenueImportMasterMatchApplyRowSerializer(many=True),
            400: OpenApiResponse(
                description="decisions пуст/невалиден, link_existing без валидного master_product_id этого "
                'владельца, либо мастер-товар уже привязан к другой строке этой компании: {"...": "..."}'
            ),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="Не участник (owner/manager) этого заведения, либо запрос авторизован "
                'только сервис-токеном без реального пользователя: {"detail": "..."}'
            ),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueProductImportMasterMatchApplyView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        _require_import_user(request)
        venue = get_object_or_404(Venue, pk=venue_id)
        ser = VenueImportMasterMatchApplyRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = apply_master_product_import_matches(
            venue=venue, owner_id=ser.validated_data["owner_id"], decisions=ser.validated_data["decisions"]
        )
        return Response(VenueImportMasterMatchApplyRowSerializer(rows, many=True).data)
