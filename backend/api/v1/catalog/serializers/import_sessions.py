"""Catalog import review sessions (Master/Venue)."""

from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers

from catalog.models import (
    CatalogImportDomain,
    CatalogImportItemAction,
    CatalogImportProcessingStatus,
    CatalogImportSessionStatus,
)

from .master_catalog import MasterProductImportApplyRowSerializer, MasterProductImportCandidateSerializer
from .venue_import import VenueImportApplyRowSerializer, VenueImportCandidateSerializer

_CATALOG_IMPORT_CANDIDATES = PolymorphicProxySerializer(
    component_name="CatalogImportCandidate",
    serializers=[MasterProductImportCandidateSerializer, VenueImportCandidateSerializer],
    resource_type_field_name=None,
    many=True,
)


class CatalogImportSessionItemSerializer(serializers.Serializer):
    parser_version = serializers.SerializerMethodField()
    field_meta = serializers.SerializerMethodField()
    parse_issues = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField())
    def get_parser_version(self, obj):
        return (obj.parsed_snapshot or {}).get("parser_version", "")

    @extend_schema_field(serializers.JSONField())
    def get_field_meta(self, obj):
        from file_imports.adapters import field_metadata

        return field_metadata(obj.parsed_snapshot)

    @extend_schema_field(serializers.JSONField())
    def get_parse_issues(self, obj):
        from file_imports.canonical import effective_issues

        return effective_issues(obj.parsed_snapshot)

    id = serializers.UUIDField(read_only=True)
    applied_at = serializers.DateTimeField(read_only=True, allow_null=True)
    apply_result = serializers.JSONField(read_only=True)
    row_number = serializers.IntegerField(read_only=True, help_text="Номер строки в исходном файле.")
    raw_values = serializers.JSONField(
        read_only=True, help_text="Строка preview как её вернул файловый preview (поля зависят от домена)."
    )
    normalized_values = serializers.JSONField(
        read_only=True, help_text="Рабочие значения строки, используются при commit. PATCH обновляет переданные ключи."
    )
    field_sources = serializers.JSONField(
        read_only=True, help_text='Источник значений полей, например {"unit": "llm"}. Нет ключа = значение из файла.'
    )
    candidates = serializers.SerializerMethodField(
        help_text="До 5 похожих карточек: для master - MasterProductImportCandidate, для venue - VenueImportCandidate."
    )
    validation_errors = serializers.ListField(
        child=serializers.CharField(), read_only=True, help_text="Ошибки строки, например конфликт при commit."
    )
    action = serializers.ChoiceField(
        choices=CatalogImportItemAction.choices,
        read_only=True,
        help_text="review - нужно решение пользователя; create_new - создать; update_existing - сопоставить с "
        "target_id; replace - заменить карточку; skip - не импортировать.",
    )
    target_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Master: master_product_id; venue: matrix_item_id."
    )
    target_expected_updated_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Когда target менялся в последний раз на момент выбора; commit вернёт строку в review, если "
        "карточка изменилась после этого момента.",
    )

    @extend_schema_field(_CATALOG_IMPORT_CANDIDATES)
    def get_candidates(self, obj):
        return obj.candidates


class CatalogImportSessionResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    domain = serializers.ChoiceField(
        choices=CatalogImportDomain.choices, read_only=True, help_text="master - Мастер-справочник, venue - компания."
    )
    venue_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="Только для domain=venue.")
    source_file_name = serializers.CharField(read_only=True, help_text="Имя загруженного файла.")
    status = serializers.ChoiceField(
        choices=CatalogImportSessionStatus.choices,
        read_only=True,
        help_text="open - идёт ревью, committed - результат уже применён.",
    )
    processing_status = serializers.ChoiceField(
        choices=CatalogImportProcessingStatus.choices,
        read_only=True,
        help_text="Фоновая обработка файла. До ready строки могут быть неполными, commit и PATCH недоступны (409).",
    )
    rows_total = serializers.IntegerField(read_only=True, help_text="Строк в файле (для фоновой обработки).")
    rows_done = serializers.IntegerField(read_only=True, help_text="Обработано строк на текущий момент.")
    percent = serializers.SerializerMethodField(help_text="0-100, по rows_done/rows_total.")
    error_message = serializers.CharField(
        read_only=True, allow_null=True, help_text="Заполнено только при processing_status=failed."
    )
    meta = serializers.JSONField(
        read_only=True,
        help_text="Данные разбора файла для фоновых сессий: sheet_name, header_row_number, detected_columns, "
        "rows_without_name. Пустой объект у синхронных сессий.",
    )
    items = CatalogImportSessionItemSerializer(many=True, read_only=True, help_text="Строки ревью по порядку файла.")

    @extend_schema_field(serializers.IntegerField(help_text="0-100, по rows_done/rows_total."))
    def get_percent(self, obj) -> int:
        if obj.processing_status == CatalogImportProcessingStatus.READY:
            return 100
        if not obj.rows_total:
            return 0
        return min(100, obj.rows_done * 100 // obj.rows_total)


class CatalogImportSessionErrorSerializer(serializers.Serializer):
    session = serializers.CharField(required=False, help_text="«Сессия уже завершена.» (commit/PATCH после commit).")
    items = serializers.CharField(required=False, help_text="«Сначала примите решение по всем строкам.»")
    venue = serializers.CharField(required=False, help_text="«Для venue-сессии не задано заведение.»")
    action = serializers.CharField(required=False, help_text="«Недопустимое действие строки.»")
    target_id = serializers.CharField(
        required=False,
        help_text="Цель не найдена, чужая или архивная; для update_existing нужна цель; невалидный UUID.",
    )


class CatalogImportSessionBusyResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(
        read_only=True,
        help_text="«Импорт ещё обрабатывается.» (pending/processing) или «Обработка импорта завершилась ошибкой.» "
        "(failed).",
    )


class CatalogImportItemResultSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(read_only=True)
    row_number = serializers.IntegerField(read_only=True)
    status = serializers.ChoiceField(choices=["applied", "skipped", "review", "conflict", "error"], read_only=True)
    errors = serializers.ListField(child=serializers.CharField(), read_only=True)
    result = serializers.JSONField(read_only=True, required=False)


class CatalogImportSessionConflictResponseSerializer(serializers.Serializer):
    remaining_count = serializers.IntegerField(read_only=True)
    item_results = CatalogImportItemResultSerializer(many=True, read_only=True)
    session_id = serializers.UUIDField(read_only=True)
    status = serializers.ChoiceField(choices=CatalogImportSessionStatus.choices, read_only=True)
    conflicted_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
        help_text="Строки, чья цель изменилась после preview; они возвращены в review. Применять было нечего.",
    )
    rows = serializers.ListField(child=serializers.JSONField(), read_only=True, help_text="Всегда пустой список.")


class CatalogImportSessionCommitResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField(read_only=True)
    status = serializers.ChoiceField(
        choices=CatalogImportSessionStatus.choices,
        read_only=True,
        help_text="open, если есть неприменённые строки; иначе committed.",
    )
    remaining_count = serializers.IntegerField(read_only=True)
    conflicted_item_ids = serializers.ListField(child=serializers.UUIDField(), read_only=True)
    item_results = CatalogImportItemResultSerializer(many=True, read_only=True)
    rows = serializers.SerializerMethodField(
        help_text="Результат применения по строкам: master - MasterProductImportApplyRow, venue - VenueImportApplyRow."
    )

    @extend_schema_field(
        PolymorphicProxySerializer(
            component_name="CatalogImportCommitRow",
            serializers=[MasterProductImportApplyRowSerializer, VenueImportApplyRowSerializer],
            resource_type_field_name=None,
            many=True,
        )
    )
    def get_rows(self, obj):
        row_serializer = (
            MasterProductImportApplyRowSerializer if obj["domain"] == "master" else VenueImportApplyRowSerializer
        )
        return row_serializer(obj["rows"], many=True).data


class CatalogImportSessionItemPatchRequestSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=CatalogImportItemAction.choices, required=False, help_text="Решение по строке."
    )
    target_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Master: master_product_id; venue: matrix_item_id. null - убрать цель.",
    )
    raw_values = serializers.DictField(required=False, help_text="Исправленные значения строки.")
    normalized_values = serializers.DictField(required=False)
    field_sources = serializers.DictField(required=False)
    candidates = serializers.JSONField(required=False)
    validation_errors = serializers.ListField(child=serializers.CharField(), required=False)

    def validate(self, attrs):
        for field in ("raw_values", "normalized_values", "field_sources"):
            if field in attrs and not isinstance(attrs[field], dict):
                raise serializers.ValidationError({field: "Ожидается объект."})
        raw, normalized = attrs.get("raw_values", {}), attrs.get("normalized_values", {})
        if any(raw[key] != normalized[key] for key in raw.keys() & normalized.keys()):
            raise serializers.ValidationError({"normalized_values": "Значения противоречат raw_values."})
        return attrs


class CatalogImportSessionItemPatchResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    action = serializers.ChoiceField(choices=CatalogImportItemAction.choices, read_only=True)
    target_id = serializers.UUIDField(read_only=True, allow_null=True)
