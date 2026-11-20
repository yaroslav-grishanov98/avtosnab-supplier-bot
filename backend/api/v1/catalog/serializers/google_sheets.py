"""Google Sheets export and sync log."""

from typing import Optional

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalog.models import (
    GoogleSheetSyncLog,
)
from core.models import (
    EntityStatus,
)
from suppliers.models import SourceChannel, Supplier, SupplierPriceUpload


class GoogleSheetExportQuerySerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(required=False, help_text="Фильтр экспорта по конкретному поставщику.")
    updated_since = serializers.DateTimeField(
        required=False, help_text="Экспортировать только строки, изменённые после этой даты/времени."
    )


class GoogleSheetExportRowSerializer(serializers.Serializer):
    supplier_product_id = serializers.UUIDField(source="id", read_only=True, help_text="ID товара поставщика.")
    product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID эталонного товара каталога (Product), если сопоставлен."
    )
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    supplier_name = serializers.CharField(
        source="supplier.display_name", read_only=True, help_text="Название компании-поставщика."
    )
    supplier_raw_name = serializers.CharField(
        source="supplier_name", read_only=True, help_text="Исходное название товара из прайса поставщика."
    )
    supplier_normalized_name = serializers.CharField(
        source="normalized_name", read_only=True, help_text="Обработанное название товара."
    )
    master_product_name = serializers.CharField(
        source="product.canonical_name",
        read_only=True,
        allow_null=True,
        help_text="Название эталонного товара каталога, если сопоставлен.",
    )
    category = serializers.CharField(
        source="product.category.name", read_only=True, allow_null=True, help_text="Категория эталонного товара."
    )
    brand = serializers.CharField(
        source="product.brand.canonical_name", read_only=True, allow_null=True, help_text="Бренд эталонного товара."
    )
    price = serializers.DecimalField(
        source="current_price", max_digits=14, decimal_places=2, read_only=True, help_text="Актуальная цена."
    )
    currency = serializers.CharField(
        source="current_currency", read_only=True, allow_null=True, help_text="Валюта цены, обычно RUB."
    )
    unit = serializers.CharField(
        source="unit.code", read_only=True, allow_null=True, help_text="Код единицы измерения."
    )
    package_info = serializers.CharField(
        source="package", read_only=True, allow_null=True, help_text="Расфасовка/упаковка товара."
    )
    min_order_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, allow_null=True, help_text="Минимальное количество для заказа."
    )
    multiplicity = serializers.DecimalField(
        source="delivery_multiple",
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Кратность поставки.",
    )
    availability_status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        source="status",
        read_only=True,
        help_text="Статус записи товара: active / archived.",
    )
    price_updated_at = serializers.DateTimeField(
        source="current_price_updated_at",
        read_only=True,
        allow_null=True,
        help_text="Когда цена была обновлена в последний раз.",
    )
    upload_id = serializers.UUIDField(
        source="last_upload_id",
        read_only=True,
        allow_null=True,
        help_text="ID последней загрузки прайса, из которой пришли эти данные.",
    )
    source_channel = serializers.ChoiceField(
        choices=SourceChannel.choices,
        source="last_upload.source_channel",
        read_only=True,
        allow_null=True,
        help_text="Канал последней загрузки: telegram / max / web / api / google_sheets / manual.",
    )
    confidence = serializers.SerializerMethodField()
    needs_review = serializers.SerializerMethodField()
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий поставщика к товару.")

    @extend_schema_field(
        serializers.FloatField(
            allow_null=True,
            help_text="Уверенность AI/парсера в распознавании строки прайса (0–1), null если не считалась.",
        )
    )
    def get_confidence(self, obj) -> Optional[float]:
        payload = getattr(obj, "latest_upload_item_payload", None) or {}
        if not isinstance(payload, dict):
            return None
        return payload.get("confidence")

    @extend_schema_field(
        serializers.BooleanField(help_text="Требует ли строка прайса ручной проверки после нормализации.")
    )
    def get_needs_review(self, obj) -> bool:
        return getattr(obj, "latest_upload_item_status", None) == "needs_review"


class GoogleSheetSyncLogCreateSerializer(serializers.ModelSerializer):
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier",
        required=False,
        allow_null=True,
        help_text="Поставщик, к которому относится синхронизация (если применимо).",
    )
    upload_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierPriceUpload.objects.all(),
        source="upload",
        required=False,
        allow_null=True,
        help_text="Загрузка прайса, вызвавшая синхронизацию (если применимо).",
    )

    class Meta:
        model = GoogleSheetSyncLog
        fields = (
            "id",
            "spreadsheet_id",
            "sheet_name",
            "status",
            "trigger",
            "supplier_id",
            "upload_id",
            "n8n_execution_id",
            "rows_read",
            "rows_written",
            "started_at",
            "finished_at",
            "error_message",
            "raw_payload",
        )
        read_only_fields = ("id",)
        extra_kwargs = {
            "spreadsheet_id": {"help_text": "ID Google-таблицы, с которой синхронизировались."},
            "sheet_name": {"required": False, "help_text": "Название листа внутри таблицы."},
            "status": {"help_text": "Итог синхронизации: started / success / failed."},
            "trigger": {
                "required": False,
                "help_text": "Что запустило синхронизацию: upload / manual / schedule и т.п.",
            },
            "n8n_execution_id": {
                "required": False,
                "allow_null": True,
                "allow_blank": True,
                "help_text": "ID запуска n8n-воркфлоу, выполнившего синхронизацию.",
            },
            "rows_read": {"required": False, "help_text": "Сколько строк было прочитано из источника."},
            "rows_written": {"required": False, "help_text": "Сколько строк записано в Google-таблицу."},
            "started_at": {"required": False, "help_text": "Когда синхронизация началась."},
            "finished_at": {"required": False, "allow_null": True, "help_text": "Когда синхронизация завершилась."},
            "error_message": {
                "required": False,
                "allow_null": True,
                "allow_blank": True,
                "help_text": "Текст ошибки, если синхронизация завершилась неудачно.",
            },
            "raw_payload": {"required": False, "help_text": "Произвольные диагностические данные о прогоне."},
        }


class GoogleSheetSyncLogResponseSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="ID поставщика синхронизации.")
    upload_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID загрузки, вызвавшей синхронизацию."
    )

    class Meta:
        model = GoogleSheetSyncLog
        fields = (
            "id",
            "spreadsheet_id",
            "sheet_name",
            "status",
            "trigger",
            "supplier_id",
            "upload_id",
            "n8n_execution_id",
            "rows_read",
            "rows_written",
            "started_at",
            "finished_at",
            "error_message",
            "raw_payload",
            "created_at",
            "updated_at",
        )
