"""Legacy supplier price uploads."""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.suppliers.normalization import AI_MODE_AMBIGUOUS, AI_MODE_FULL, AI_MODE_OFF
from api.v1.suppliers.services import (
    AI_CLEANUP_SCOPE_ALL,
    AI_CLEANUP_SCOPE_AMBIGUOUS,
    AI_CLEANUP_SCOPE_NEEDS_REVIEW,
)
from core.models import EntityStatus, Unit
from file_imports.extraction import MAX_IMPORT_FILE_BYTES, SUPPORTED_EXTENSIONS
from suppliers.models import (
    SourceChannel,
    Supplier,
    UploadItemStatus,
    UploadStatus,
)


class SupplierPriceUploadProcessingProgressSerializer(serializers.Serializer):
    """Shape of SupplierPriceUploadDetailSerializer.processing_progress —
    was typed as a plain DictField for @extend_schema_field, which Swagger
    renders as a generic {"additionalProp1": "string", ...} object with no
    real field names (the fixed shape only lived in get_processing_progress's
    return statement, not anywhere drf-spectacular could see it)."""

    rows_total = serializers.IntegerField(help_text="Всего строк в загрузке.")
    rows_done = serializers.IntegerField(help_text="Обработано строк на текущий момент.")
    chunks_total = serializers.IntegerField(help_text="Всего AI-чанков (0, если AI не использовался).")
    chunks_done = serializers.IntegerField(help_text="Обработано AI-чанков.")
    percent = serializers.IntegerField(help_text="Процент готовности, 0-100.")


class SupplierPriceUploadFileSerializer(serializers.Serializer):
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier",
        help_text="ID поставщика, которому принадлежит прайс.",
    )
    source_channel = serializers.ChoiceField(
        choices=SourceChannel.values, help_text="Канал загрузки: telegram / max / web / api / google_sheets / manual."
    )
    source_file = serializers.FileField(
        write_only=True,
        help_text="Файл прайса: .csv, .tsv, .xlsx, .xlsm, .xls, .pdf, .docx, .doc, .txt, .jpg, .jpeg, .png.",
    )
    source_file_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Имя файла, если отличается от исходного."
    )
    telegram_message_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ID сообщения в Telegram, если загрузка из бота.",
    )
    n8n_execution_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ID запуска n8n-воркфлоу, инициировавшего загрузку.",
    )

    allowed_extensions = SUPPORTED_EXTENSIONS

    def validate_source_file(self, source_file):
        file_name = source_file.name.lower()
        if not any(file_name.endswith(extension) for extension in self.allowed_extensions):
            raise serializers.ValidationError("Unsupported file type.")
        max_bytes = min(
            int(getattr(settings, "SUPPLIER_PRICE_UPLOAD_MAX_BYTES", MAX_IMPORT_FILE_BYTES)), MAX_IMPORT_FILE_BYTES
        )
        if source_file.size > max_bytes:
            raise serializers.ValidationError(f"File is too large. Maximum size is {max_bytes} bytes.")
        if source_file.size == 0:
            raise serializers.ValidationError("File is empty.")
        return source_file


class SupplierPriceUploadTextSerializer(serializers.Serializer):
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier",
        help_text="ID поставщика, которому принадлежит прайс.",
    )
    source_channel = serializers.ChoiceField(
        choices=SourceChannel.values, help_text="Канал загрузки: telegram / max / web / api / google_sheets / manual."
    )
    text = serializers.CharField(
        trim_whitespace=True,
        allow_blank=False,
        help_text="Текст прайса — одна или несколько строк с товарами и ценами.",
    )
    source_file_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Условное имя для этой текстовой загрузки."
    )
    telegram_message_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ID сообщения в Telegram, если загрузка из бота.",
    )
    n8n_execution_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ID запуска n8n-воркфлоу, инициировавшего загрузку.",
    )

    def validate_text(self, value):
        max_chars = int(getattr(settings, "SUPPLIER_PRICE_UPLOAD_MAX_TEXT_CHARS", 2_000_000))
        if len(value) > max_chars:
            raise serializers.ValidationError(f"Text is too large. Maximum length is {max_chars} characters.")
        return value


class BotSupplierPriceUploadTextSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(
        choices=(SourceChannel.TELEGRAM, SourceChannel.MAX),
        help_text="Bot platform: telegram or max.",
    )
    chat_id = serializers.CharField(trim_whitespace=True, help_text="Chat ID.")
    telegram_user_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Telegram/Max user ID used as a fallback binding lookup.",
    )
    message_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ID of the message containing the price list.",
    )
    text = serializers.CharField(
        trim_whitespace=True,
        allow_blank=False,
        help_text="Message text containing products and prices.",
    )
    run_async = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Queue normalization in Celery. Set false for a synchronous local check.",
    )
    use_ai = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Use AI normalization. Disabled by default for deterministic processing.",
    )


def _upload_frontend_next_action(status):
    return {
        UploadStatus.RECEIVED: "preview",
        UploadStatus.PROCESSING: "poll",
        UploadStatus.PARSED: "preview",
        UploadStatus.NEEDS_REVIEW: "review_items",
        UploadStatus.APPLIED: "done",
        UploadStatus.FAILED: "retry",
        UploadStatus.ARCHIVED: "none",
    }.get(status, "poll")


def _upload_frontend_available_actions(status):
    return {
        UploadStatus.RECEIVED: ["preview", "publish", "reject"],
        UploadStatus.PROCESSING: ["poll"],
        UploadStatus.PARSED: ["preview", "publish", "reject"],
        UploadStatus.NEEDS_REVIEW: ["review_items", "publish", "reject"],
        UploadStatus.APPLIED: ["done"],
        UploadStatus.FAILED: ["retry", "reject"],
        UploadStatus.ARCHIVED: [],
    }.get(status, ["poll"])


class SupplierPriceUploadFrontendFlowMixin:
    @extend_schema_field(serializers.CharField(help_text="Следующее действие фронта."))
    def get_next_action(self, obj) -> str:
        return _upload_frontend_next_action(obj.status)

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_available_actions(self, obj) -> list[str]:
        return _upload_frontend_available_actions(obj.status)

    @extend_schema_field(serializers.CharField(help_text="URL опроса статуса этой загрузки."))
    def get_status_url(self, obj) -> str:
        return f"/api/v1/suppliers/uploads/{obj.id}"

    @extend_schema_field(serializers.CharField(help_text="URL превью строк этой загрузки."))
    def get_preview_url(self, obj) -> str:
        return f"/api/v1/suppliers/uploads/{obj.id}/preview"

    @extend_schema_field(serializers.CharField(help_text="URL подтверждения и публикации этой загрузки."))
    def get_publish_url(self, obj) -> str:
        return f"/api/v1/suppliers/uploads/{obj.id}/publish"

    @extend_schema_field(serializers.DictField(child=serializers.CharField()))
    def get_action_urls(self, obj) -> dict[str, str]:
        prefix = f"/api/v1/suppliers/uploads/{obj.id}"
        return {
            "poll": prefix,
            "preview": f"{prefix}/preview",
            "column_mapping": f"{prefix}/column-mapping",
            "change_plan": f"{prefix}/change-plan",
            "review_items": f"{prefix}/items",
            "publish": f"{prefix}/publish",
            "retry": f"{prefix}/publish",
            "reject": f"{prefix}/reject",
        }


class SupplierPriceUploadResponseSerializer(SupplierPriceUploadFrontendFlowMixin, serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID загрузки прайса.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    source_channel = serializers.ChoiceField(
        choices=SourceChannel.choices, read_only=True, help_text="Канал, из которого пришла загрузка."
    )
    source_file_name = serializers.CharField(read_only=True, allow_null=True, help_text="Имя загруженного файла.")
    source_file_uri = serializers.CharField(
        read_only=True, allow_null=True, help_text="Путь к сохранённому файлу на сервере."
    )
    status = serializers.ChoiceField(
        choices=UploadStatus.choices,
        read_only=True,
        help_text="Статус загрузки: received / processing / applied / needs_review / failed / archived.",
    )
    uploaded_at = serializers.DateTimeField(read_only=True, help_text="Дата и время загрузки.")
    raw_payload = serializers.JSONField(
        read_only=True, help_text="Служебные данные загрузки: извлечённые строки, диагностика нормализации и т.п."
    )
    next_action = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()
    status_url = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    publish_url = serializers.SerializerMethodField()
    action_urls = serializers.SerializerMethodField()


class SupplierPriceUploadListQuerySerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр по поставщику для service-token; при JWT кабинета берётся из токена.",
    )
    status = serializers.ChoiceField(
        choices=UploadStatus.values,
        required=False,
        help_text="Фильтр по статусу загрузки.",
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=100, default=20, help_text="Размер страницы, максимум 100."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")


class SupplierPriceUploadPreviewQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=100,
        default=20,
        help_text="Сколько первых извлечённых строк показать.",
    )


class SupplierPriceUploadColumnMappingSerializer(serializers.Serializer):
    mapping = serializers.DictField(
        child=serializers.CharField(trim_whitespace=True, allow_blank=False),
        allow_empty=False,
        help_text=(
            "Сопоставление канонических полей с колонками файла. Поддерживаются: "
            "name, price, unit, sku, min_order, multiplicity, package_composition (состав упаковки, "
            "например «8 шт. × 2 кг = 16 кг/кор.»)."
        ),
    )

    allowed_fields = {"name", "price", "unit", "sku", "min_order", "multiplicity", "package_composition"}

    def validate_mapping(self, mapping):
        unknown = sorted(set(mapping) - self.allowed_fields)
        if unknown:
            raise serializers.ValidationError(f"Unsupported canonical fields: {', '.join(unknown)}.")
        duplicate_sources = sorted({value for value in mapping.values() if list(mapping.values()).count(value) > 1})
        if duplicate_sources:
            raise serializers.ValidationError(
                f"A source column can only be mapped once: {', '.join(duplicate_sources)}."
            )
        return mapping


class SupplierPriceUploadDetailSerializer(SupplierPriceUploadFrontendFlowMixin, serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID загрузки прайса.")
    upload_id = serializers.SerializerMethodField()
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    supplier_name = serializers.CharField(
        source="supplier.display_name", read_only=True, help_text="Название компании-поставщика."
    )
    source_channel = serializers.ChoiceField(
        choices=SourceChannel.choices, read_only=True, help_text="Канал, из которого пришла загрузка."
    )
    source_file_name = serializers.CharField(read_only=True, allow_null=True, help_text="Имя загруженного файла.")
    source_file_uri = serializers.CharField(
        read_only=True, allow_null=True, help_text="Путь к сохранённому файлу на сервере."
    )
    status = serializers.ChoiceField(
        choices=UploadStatus.choices,
        read_only=True,
        help_text="Статус загрузки: received / processing / applied / needs_review / failed / archived.",
    )
    uploaded_at = serializers.DateTimeField(read_only=True, help_text="Дата и время загрузки.")
    processed_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Дата и время завершения обработки."
    )
    error_message = serializers.CharField(
        read_only=True, allow_null=True, help_text="Текст ошибки, если обработка завершилась неудачно."
    )
    items_total = serializers.SerializerMethodField()
    items_new = serializers.IntegerField(read_only=True, help_text="Сколько строк оказались новыми товарами.")
    items_matched = serializers.SerializerMethodField()
    items_updated = serializers.IntegerField(read_only=True, help_text="Сколько строк обновили существующие товары.")
    items_rejected = serializers.IntegerField(read_only=True, help_text="Сколько строк было отклонено.")
    items_needs_review = serializers.IntegerField(read_only=True, help_text="Сколько строк требуют ручной проверки.")
    products_created = serializers.SerializerMethodField()
    products_updated = serializers.SerializerMethodField()
    prices_created = serializers.SerializerMethodField()
    needs_review = serializers.SerializerMethodField()
    normalization = serializers.SerializerMethodField()
    ai_cleanup = serializers.SerializerMethodField()
    processing_progress = serializers.SerializerMethodField()
    column_mapping = serializers.SerializerMethodField()
    original_file_available = serializers.SerializerMethodField()
    next_action = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()
    status_url = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    publish_url = serializers.SerializerMethodField()
    action_urls = serializers.SerializerMethodField()

    @extend_schema_field(serializers.UUIDField(help_text="ID загрузки (дублирует id)."))
    def get_upload_id(self, obj) -> str:
        return obj.id

    def _normalization(self, obj):
        raw_payload = obj.raw_payload or {}
        normalization = raw_payload.get("normalization") or {}
        return normalization if isinstance(normalization, dict) else {}

    def _normalization_result(self, obj):
        result = self._normalization(obj).get("result") or {}
        return result if isinstance(result, dict) else {}

    def _normalization_result_int(self, obj, key):
        value = self._normalization_result(obj).get(key)
        return int(value or 0)

    @extend_schema_field(SupplierPriceUploadProcessingProgressSerializer())
    def get_processing_progress(self, obj) -> dict:
        normalization = self._normalization(obj)
        progress = normalization.get("progress") or {}
        rows_total = int(normalization.get("rows_total") or self.get_items_total(obj) or 0)
        rows_done = int(progress.get("rows_done") or progress.get("items_done") or 0)
        chunks_done = int(progress.get("chunks_done") or 0)
        chunks_total = int(progress.get("chunks_total") or 0)
        if obj.status in {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW, UploadStatus.ARCHIVED}:
            rows_done = rows_total
        percent = (
            100
            if rows_total and rows_done >= rows_total
            else (round(rows_done * 100 / rows_total) if rows_total else 0)
        )
        return {
            "rows_total": rows_total,
            "rows_done": rows_done,
            "chunks_total": chunks_total,
            "chunks_done": chunks_done,
            "percent": percent,
        }

    @extend_schema_field(serializers.DictField(help_text="Сохранённое ручное сопоставление колонок."))
    def get_column_mapping(self, obj) -> dict:
        mapping = (obj.raw_payload or {}).get("column_mapping") or {}
        return mapping if isinstance(mapping, dict) else {}

    @extend_schema_field(serializers.BooleanField(help_text="Можно ли скачать исходный файл."))
    def get_original_file_available(self, obj) -> bool:
        return bool(obj.source_file_uri)

    @extend_schema_field(serializers.IntegerField(help_text="Всего строк в прайсе."))
    def get_items_total(self, obj) -> int:
        norm_total = self._normalization_result(obj).get("items_total")
        if norm_total is not None:
            return int(norm_total)
        return getattr(obj, "items_total", 0)

    @extend_schema_field(
        serializers.IntegerField(help_text="Сколько строк совпало с уже существующими товарами без изменений.")
    )
    def get_items_matched(self, obj) -> int:
        norm_matched = self._normalization_result(obj).get("items_matched")
        if norm_matched is not None:
            return int(norm_matched)
        return getattr(obj, "items_matched", 0)

    @extend_schema_field(serializers.IntegerField(help_text="Сколько новых товаров создано при нормализации."))
    def get_products_created(self, obj) -> int:
        return self._normalization_result_int(obj, "products_created")

    @extend_schema_field(serializers.IntegerField(help_text="Сколько существующих товаров обновлено при нормализации."))
    def get_products_updated(self, obj) -> int:
        return self._normalization_result_int(obj, "products_updated")

    @extend_schema_field(serializers.IntegerField(help_text="Сколько новых цен создано при нормализации."))
    def get_prices_created(self, obj) -> int:
        return self._normalization_result_int(obj, "prices_created")

    @extend_schema_field(
        serializers.IntegerField(help_text="Сколько строк по итогам нормализации требуют ручной проверки.")
    )
    def get_needs_review(self, obj) -> int:
        return self._normalization_result_int(obj, "needs_review")

    @extend_schema_field(
        serializers.DictField(help_text="Полная диагностика нормализации: режим AI, счётчики, ошибки по чанкам.")
    )
    def get_normalization(self, obj) -> Dict[str, Any]:
        return self._normalization(obj)

    @extend_schema_field(serializers.DictField(help_text="Диагностика AI-cleanup прогона, если он запускался."))
    def get_ai_cleanup(self, obj) -> Dict[str, Any]:
        cleanup = self._normalization(obj).get("ai_cleanup") or {}
        return cleanup if isinstance(cleanup, dict) else {}


class SupplierPriceUploadItemListQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=UploadItemStatus.values, required=False, help_text="Фильтр по статусу строки прайса."
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=500, default=100, help_text="Размер страницы, максимум 500."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")


class SupplierPriceUploadItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID строки загрузки.")
    row_number = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="Номер строки в исходном файле/тексте."
    )
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID созданного/обновлённого товара, если строка успешно разобрана."
    )
    raw_name = serializers.CharField(read_only=True, help_text="Исходный текст названия товара из строки.")
    raw_unit = serializers.CharField(
        read_only=True, allow_null=True, help_text="Исходный текст единицы измерения из строки."
    )
    raw_price = serializers.CharField(read_only=True, allow_null=True, help_text="Исходный текст цены из строки.")
    normalized_name = serializers.CharField(
        read_only=True, allow_blank=True, help_text="Распознанное обработанное название товара."
    )
    parsed_price = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text="Распознанная цена."
    )
    unit = serializers.CharField(
        source="parsed_unit.code", read_only=True, allow_null=True, help_text="Распознанная единица измерения."
    )
    status = serializers.ChoiceField(
        choices=UploadItemStatus.choices,
        read_only=True,
        help_text="Статус строки: new / updated / matched / rejected / needs_review.",
    )
    review_comment = serializers.CharField(
        read_only=True, allow_null=True, help_text="Комментарий о причине needs_review/rejected, если есть."
    )
    confidence = serializers.SerializerMethodField()
    warnings = serializers.SerializerMethodField()

    @extend_schema_field(
        serializers.FloatField(allow_null=True, help_text="Уверенность парсера/AI в распознавании строки (0–1).")
    )
    def get_confidence(self, obj) -> Optional[float]:
        return obj.raw_payload.get("confidence")

    @extend_schema_field(
        serializers.ListField(child=serializers.CharField(), help_text="Предупреждения парсера по этой строке.")
    )
    def get_warnings(self, obj) -> List[str]:
        warnings = obj.raw_payload.get("warnings", [])
        return warnings if isinstance(warnings, list) else []


class SupplierPriceUploadItemCorrectionSerializer(serializers.Serializer):
    name = serializers.CharField(
        required=False,
        trim_whitespace=True,
        allow_blank=False,
        help_text="Исправленное название товара.",
    )
    price = serializers.DecimalField(
        required=False,
        max_digits=14,
        decimal_places=2,
        min_value=0,
        help_text="Исправленная цена.",
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        required=False,
        queryset=Unit.objects.all(),
        source="unit",
        help_text="Исправленная единица измерения.",
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one correction must be provided.")
        return attrs


class SupplierPriceAIProductSerializer(serializers.Serializer):
    row_number = serializers.IntegerField(
        required=False, min_value=1, help_text="Номер строки в исходном прайсе, к которой относится товар."
    )
    raw_name = serializers.CharField(trim_whitespace=True, help_text="Исходное название товара из прайса.")
    normalized_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Обработанное AI название товара."
    )
    price = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True, help_text="Цена товара."
    )
    currency = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, max_length=3, help_text="Код валюты, например RUB."
    )
    unit_id = serializers.UUIDField(required=False, help_text="ID уже известной единицы измерения, если сопоставлена.")
    unit_code = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Код единицы измерения текстом, если unit_id неизвестен.",
    )
    package = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Расфасовка/упаковка товара."
    )
    package_info = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Дополнительная информация об упаковке.",
    )
    brand = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Распознанный бренд товара."
    )
    volume = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Объём товара, если применимо.",
    )
    volume_unit = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Единица измерения объёма."
    )
    weight = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Вес товара, если применимо.",
    )
    weight_unit = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Единица измерения веса."
    )
    min_order_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Минимальное количество для заказа.",
    )
    multiplicity = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True, help_text="Кратность поставки."
    )
    pack_items_count = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, help_text="Состав упаковки: штук в упаковке (8)."
    )
    pack_item_value = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        allow_null=True,
        help_text="Состав упаковки: вес/объём одной штуки (2).",
    )
    pack_item_unit_code = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Состав упаковки: код единицы одной штуки (кг). Без распознанной единицы состав не пишется.",
    )
    pack_type_code = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Состав упаковки: код тары (кор), необязателен.",
    )
    supplier_sku = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Артикул товара у поставщика.",
    )
    category_path = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Путь категории от группы к виду товара, определённый AI.",
    )
    image_url = serializers.URLField(
        required=False, allow_blank=True, allow_null=True, help_text="Ссылка на изображение товара."
    )
    availability_status = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Наличие товара, если указано в прайсе.",
    )
    confidence = serializers.DecimalField(
        max_digits=5,
        decimal_places=4,
        min_value=0,
        max_value=1,
        required=False,
        help_text="Уверенность AI в распознавании строки (0–1).",
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Комментарий к товару."
    )
    warnings = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Предупреждения AI по этой строке."
    )


class SupplierPriceAIApplySerializer(serializers.Serializer):
    schema_version = serializers.CharField(
        help_text="Версия схемы результата AI. Поддерживается только supplier_price_ai.v1."
    )
    processing_version = serializers.CharField(
        trim_whitespace=True, help_text="Версия пайплайна обработки, сгенерировавшего результат."
    )
    supplier_id = serializers.UUIDField(
        required=False, help_text="ID поставщика (можно не указывать, если есть upload_id)."
    )
    upload_id = serializers.UUIDField(required=False, help_text="ID загрузки, к которой применяется результат.")
    source_channel = serializers.ChoiceField(
        choices=SourceChannel.values, required=False, help_text="Канал исходной загрузки."
    )
    supplier_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Название поставщика для справки."
    )
    products = SupplierPriceAIProductSerializer(many=True, help_text="Список распознанных товаров.")

    def validate_schema_version(self, value):
        if value != "supplier_price_ai.v1":
            raise serializers.ValidationError("Unsupported schema_version.")
        return value


class SupplierPriceUploadNormalizeSerializer(serializers.Serializer):
    use_ai = serializers.BooleanField(
        required=False, default=True, help_text="Использовать AI для неуверенно распознанных строк."
    )
    ai_mode = serializers.ChoiceField(
        choices=(AI_MODE_FULL, AI_MODE_AMBIGUOUS, AI_MODE_OFF),
        required=False,
        help_text="Режим AI: full (все строки через AI) / ambiguous (только неуверенные) / off (без AI).",
    )
    force = serializers.BooleanField(
        required=False, default=False, help_text="Перезапустить нормализацию, даже если загрузка уже обработана."
    )
    confirm_full = serializers.BooleanField(
        required=False, default=False, help_text="Явное подтверждение полного AI-прогона (ai_mode=full)."
    )
    # 2026-08-15: default was False. Synchronous processing runs every row of the upload inside
    # one HTTP request; at the ~47ms/query DB round-trip floor measured live this session, a
    # few-hundred-row price list already exceeds gunicorn's --timeout 120 (entrypoint.sh), the
    # worker gets SIGABRT/SIGKILLed mid-transaction, and the upload is left permanently stuck in
    # status=processing (see docs/wiki/logs/2026-08-15-cabinet-latency-floor-and-stuck-uploads.md
    # — this exact failure hit a live 834-row upload). Celery is healthy in prod (confirmed via
    # VictoriaLogs — worker connected, task registered) with a 30-minute CELERY_TASK_TIME_LIMIT,
    # comfortably covering realistic upload sizes.
    run_async = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "Запустить в фоне через Celery (ответ 202, опрашивать GET /uploads/{id} на статус) "
            "вместо синхронной обработки (200 сразу с результатом). По умолчанию true — "
            "синхронный путь на больших прайсах упирается в таймаут воркера. Явный false "
            "остаётся доступен для мелких ручных проверок, где нужен немедленный результат."
        ),
    )
    run_ai_cleanup = serializers.BooleanField(
        required=False, default=False, help_text="Сразу запустить AI-cleanup после нормализации."
    )
    ai_cleanup_scope = serializers.ChoiceField(
        choices=(AI_CLEANUP_SCOPE_ALL, AI_CLEANUP_SCOPE_AMBIGUOUS, AI_CLEANUP_SCOPE_NEEDS_REVIEW),
        required=False,
        default=AI_CLEANUP_SCOPE_ALL,
        help_text="Какие строки чистить: all / ambiguous / needs_review.",
    )

    def validate(self, attrs):
        if isinstance(self.initial_data, dict) and "async" in self.initial_data:
            try:
                attrs["run_async"] = serializers.BooleanField().run_validation(self.initial_data.get("async"))
            except serializers.ValidationError as error:
                raise serializers.ValidationError({"async": error.detail}) from error
        if not attrs.get("use_ai", True):
            attrs["ai_mode"] = AI_MODE_OFF
            return attrs
        attrs["ai_mode"] = attrs.get("ai_mode")
        return attrs


class SupplierPriceUploadAICleanupSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(
        choices=(AI_CLEANUP_SCOPE_ALL, AI_CLEANUP_SCOPE_AMBIGUOUS, AI_CLEANUP_SCOPE_NEEDS_REVIEW),
        required=False,
        default=AI_CLEANUP_SCOPE_ALL,
        help_text="Какие строки чистить: all / ambiguous / needs_review.",
    )
    force = serializers.BooleanField(
        required=False, default=False, help_text="Перезапустить cleanup, даже если он уже выполнялся."
    )
