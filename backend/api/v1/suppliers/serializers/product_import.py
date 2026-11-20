"""Unified supplier product import."""

from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.measure import NetValueField
from api.v1.suppliers.package_composition import package_composition
from core.models import EntityStatus, Unit, UnitKind
from suppliers.models import (
    ProductAvailability,
    SupplierProductImportAction,
    SupplierProductImportCommitStatus,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSessionStatus,
)

from .common import PackageCompositionSerializer, _validate_import_upload_size


class SupplierProductImportAnalyzeRequestSerializer(serializers.Serializer):
    validate_file = staticmethod(_validate_import_upload_size)

    file = serializers.FileField(
        help_text="Файл со списком товаров поставщика: .xlsx/.xlsm/.xls/.csv/.tsv (разбор сразу) или "
        ".pdf/.docx/.doc/.txt/.jpg/.jpeg/.png (извлекается в фоне, когда включено сохранение файла)."
    )


class SupplierProductImportMatchCandidateSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    unit_id = serializers.UUIDField(read_only=True, allow_null=True)
    unit_name = serializers.CharField(read_only=True, allow_null=True)
    score = serializers.FloatField(
        read_only=True, help_text="0..1, только для сортировки — пользователю не показывать."
    )


def _target_price(item):
    """Текущая цена товара, который заменяет строка (action=replace), иначе None."""
    if item.action != "replace" or item.target_supplier_product_id is None:
        return None
    return item.target_supplier_product.current_price


# Every source the import code writes into SupplierProductImportItem.field_sources (no key = from the file).
SUPPLIER_PRODUCT_IMPORT_FIELD_SOURCES = [
    ("name", "Страна найдена в названии строки"),
    ("section", "Страна из заголовка раздела"),
    ("gs1_prefix", "Страна по префиксу штрихкода"),
    ("llm", "Заполнено LLM"),
    ("user", "Введено пользователем"),
    ("rule", "Выведено правилом"),
    ("target", "Скопировано из заменяемой карточки"),
]


class SupplierProductImportItemSerializer(serializers.Serializer):
    parser_version = serializers.SerializerMethodField()
    field_meta = serializers.SerializerMethodField()
    parse_issues = serializers.SerializerMethodField()
    business_issues = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField())
    def get_parser_version(self, obj):
        return (obj.parsed_snapshot or {}).get("parser_version", "")

    def get_fields(self):
        fields = super().get_fields()
        # GET сессии по умолчанию не отдаёт field_meta (~3 КБ на строку, две трети ответа): страницы по 500 строк
        # весили 1-2 МБ. Строка PATCH и прочие вызовы без этого флага в контексте получают поле как раньше.
        if self.context.get("exclude_field_meta"):
            fields.pop("field_meta", None)
        return fields

    @extend_schema_field(
        serializers.JSONField(
            help_text="Источник, статус и надёжность каждого поля строки (диагностика парсера). В GET сессии "
            "импорта приходит только при include_meta=true."
        )
    )
    def get_field_meta(self, obj):
        from file_imports.adapters import field_metadata

        return field_metadata(obj.parsed_snapshot)

    @extend_schema_field(serializers.JSONField())
    def get_parse_issues(self, obj):
        from file_imports.canonical import effective_issues

        return effective_issues(obj.parsed_snapshot)

    @extend_schema_field(serializers.JSONField())
    def get_business_issues(self, obj):
        from api.v1.suppliers.product_import_services import _item_errors

        parse_messages = {issue["message"] for issue in self.get_parse_issues(obj)}
        return [
            {"code": "business_requirement", "message": message}
            for message in _item_errors(obj, validate_units=False)
            if message not in parse_messages
        ]

    ready = serializers.SerializerMethodField()
    validation_errors = serializers.SerializerMethodField()

    @extend_schema_field(serializers.BooleanField())
    def get_ready(self, obj):
        from api.v1.suppliers.product_import_services import _item_errors

        return (
            obj.action in ("new", "replace")
            and not _item_errors(obj, validate_units=False)
            and not obj.validation_errors
        )

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_validation_errors(self, obj):
        from api.v1.suppliers.product_import_services import _item_errors

        return obj.validation_errors or (
            _item_errors(obj, validate_units=False) if obj.action in ("new", "replace") else []
        )

    id = serializers.UUIDField(read_only=True)
    row_number = serializers.IntegerField(read_only=True)
    raw_name = serializers.CharField(read_only=True)
    name = serializers.CharField()
    country_of_origin = serializers.CharField(allow_null=True, required=False)
    unit_id = serializers.UUIDField(allow_null=True, required=False)
    price = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True, required=False)
    delivery_multiple = serializers.DecimalField(max_digits=14, decimal_places=4, allow_null=True, required=False)
    net_value = NetValueField(max_digits=14, decimal_places=4, allow_null=True, required=False)
    net_unit_id = serializers.UUIDField(allow_null=True, required=False)
    min_order_qty = serializers.DecimalField(max_digits=14, decimal_places=4, allow_null=True, required=False)
    availability = serializers.ChoiceField(choices=ProductAvailability.choices, allow_null=True, required=False)
    supplier_sku = serializers.CharField(allow_null=True, required=False)
    pack_items_count = serializers.IntegerField(
        allow_null=True, required=False, help_text="Состав упаковки: сколько штук в упаковке (8 в «8 шт. × 2 кг»)."
    )
    pack_item_value = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        allow_null=True,
        required=False,
        help_text="Состав упаковки: вес/объём/количество одной штуки (2 в «8 шт. × 2 кг»).",
    )
    pack_item_unit_id = serializers.UUIDField(
        allow_null=True, required=False, help_text="Состав упаковки: единица одной штуки (кг), не тара."
    )
    pack_type_id = serializers.UUIDField(
        allow_null=True, required=False, help_text="Состав упаковки: тара (кор), единица вида package. Необязательна."
    )
    package_composition = serializers.SerializerMethodField()
    action = serializers.ChoiceField(choices=SupplierProductImportAction.choices, read_only=True)
    target_supplier_product_id = serializers.UUIDField(read_only=True, allow_null=True)
    target_expected_updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
    target_current_price = serializers.SerializerMethodField()
    price_change = serializers.SerializerMethodField()
    price_change_percent = serializers.SerializerMethodField()
    imported_at = serializers.DateTimeField(read_only=True, allow_null=True)
    field_sources = serializers.DictField(
        child=serializers.ChoiceField(choices=SUPPLIER_PRODUCT_IMPORT_FIELD_SOURCES),
        read_only=True,
        help_text=(
            'Откуда взято значение поля строки: ключ -- имя поля, значение -- источник, например {"unit": "llm", '
            '"country_of_origin": "gs1_prefix", "net_value": "rule"}. Источники: "name" (страна найдена в названии '
            'строки), "section" (страна из заголовка раздела), "gs1_prefix" (страна по префиксу штрихкода), "llm" '
            '(заполнено LLM), "user" (введено пользователем), "rule" (выведено правилом: нетто 1 единицы продажи), '
            '"target" (скопировано из карточки, которую заменяет строка; при применении не перезаписывается). '
            'Отсутствие ключа = source "file".'
        ),
    )
    match_candidates = SupplierProductImportMatchCandidateSerializer(many=True, read_only=True)

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=2,
            allow_null=True,
            read_only=True,
            help_text="Текущая цена товара, который заменяет строка (action=replace); иначе null.",
        )
    )
    def get_target_current_price(self, obj):
        return _target_price(obj)

    @extend_schema_field(
        serializers.ChoiceField(
            choices=[("same", "same"), ("up", "up"), ("down", "down")],
            allow_null=True,
            read_only=True,
            help_text="Как цена строки соотносится с текущей ценой заменяемого товара: same — без изменений, up — "
            "выросла, down — снизилась. null: строка не заменяет товар или у неё нет цены.",
        )
    )
    def get_price_change(self, obj):
        target, price = _target_price(obj), obj.price
        if target is None or price is None:
            return None
        return "same" if price == target else "up" if price > target else "down"

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=9,
            decimal_places=1,
            allow_null=True,
            read_only=True,
            help_text="Изменение цены в процентах относительно текущей цены заменяемого товара (+8.3 / -12.5); "
            "null, если сравнивать не с чем или текущая цена равна нулю.",
        )
    )
    def get_price_change_percent(self, obj):
        target, price = _target_price(obj), obj.price
        if target is None or price is None or target == 0:
            return None
        return ((price - target) / target * 100).quantize(Decimal("0.1"))

    @extend_schema_field(
        PackageCompositionSerializer(
            allow_null=True,
            help_text="Состав упаковки строкой для показа и итогом; null, если не заполнен. Правится через "
            "pack_items_count / pack_item_value / pack_item_unit_id / pack_type_id.",
        )
    )
    def get_package_composition(self, obj):
        composition = package_composition(obj)
        return PackageCompositionSerializer(composition).data if composition else None


class SupplierProductImportSessionCountsSerializer(serializers.Serializer):
    ready = serializers.IntegerField(
        read_only=True, help_text="Строки new/replace с заполненными обязательными полями."
    )
    all = serializers.IntegerField(read_only=True)
    review = serializers.IntegerField(read_only=True)
    new = serializers.IntegerField(read_only=True)
    replace = serializers.IntegerField(read_only=True)
    skip = serializers.IntegerField(read_only=True)
    price_changed = serializers.IntegerField(
        read_only=True,
        help_text="Сколько замен (action=replace) меняют цену: цена строки отличается от текущей цены заменяемого "
        "товара. Строки с той же ценой и без цены не считаются.",
    )


class SupplierProductImportRowErrorSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(read_only=True)
    row_number = serializers.IntegerField(read_only=True)
    messages = serializers.ListField(child=serializers.CharField(), read_only=True)


class SupplierProductImportCommitAcceptedSerializer(serializers.Serializer):
    commit_status = serializers.ChoiceField(
        choices=SupplierProductImportCommitStatus.choices,
        read_only=True,
        help_text="pending -- применение поставлено в очередь. Дальше опрашивайте GET status_url: running -- "
        "применяется, none + commit_result -- готово, failed + commit_error -- ошибка.",
    )
    status_url = serializers.CharField(
        read_only=True, help_text="Путь для опроса: GET /api/v1/suppliers/products/import/{session_id}."
    )


class SupplierProductImportCommitResultSerializer(serializers.Serializer):
    errors = SupplierProductImportRowErrorSerializer(many=True, read_only=True)
    imported_count = serializers.IntegerField(read_only=True)
    skipped_review_count = serializers.IntegerField(read_only=True)
    skipped_incomplete_count = serializers.IntegerField(read_only=True)
    conflicted_count = serializers.IntegerField(read_only=True)
    not_ready_row_numbers = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    session_status = serializers.ChoiceField(
        choices=SupplierProductImportSessionStatus.choices,
        read_only=True,
        help_text="open -- в сессии остались строки для разбора; committed -- строк не осталось (в синхронном "
        "режиме сессия при этом удалена, в фоновом хранится до очистки).",
    )
    session_deleted = serializers.BooleanField(
        read_only=True,
        help_text="true -- все строки обработаны и сессия удалена: GET/PATCH/commit по этому session_id дальше "
        "отвечают 404 (только синхронный режим; в фоновом всегда false). Заимпортированные и пропущенные (skip) "
        "строки из сессии удаляются при каждом commit.",
    )
    draft_supplier_product_ids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
        help_text="ID товаров, созданных этим commit'ом (action=new) со статусом draft -- "
        "не видны покупателям, пока не будут опубликованы через POST products/<id>/publish "
        "или POST products/bulk-publish.",
    )


class SupplierProductImportSessionWarningSerializer(serializers.Serializer):
    code = serializers.ChoiceField(
        choices=[
            ("low_price_share", "Мало строк с ценой"),
            ("sheet_skipped", "Лист книги пропущен"),
        ],
        read_only=True,
        help_text="low_price_share — цена найдена менее чем в половине строк (порог настраивается), возможно выбран "
        "не тот лист или сдвинуты колонки. sheet_skipped — лист Excel не использован, потому что на нём не найдена "
        "таблица товаров.",
    )
    message = serializers.CharField(read_only=True, help_text="Текст предупреждения для показа пользователю.")
    rows_total = serializers.IntegerField(
        read_only=True, required=False, help_text="Только low_price_share: всего строк с названием товара."
    )
    rows_with_price = serializers.IntegerField(
        read_only=True, required=False, help_text="Только low_price_share: сколько из них с распознанной ценой."
    )


class SupplierProductImportSessionResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(source="session.id", read_only=True)
    status = serializers.ChoiceField(
        source="session.status", choices=SupplierProductImportSessionStatus.choices, read_only=True
    )
    processing_status = serializers.ChoiceField(
        source="session.processing_status",
        choices=SupplierProductImportProcessingStatus.choices,
        read_only=True,
        help_text="pending — сессия в очереди, воркер ещё не начал. processing — идёт дозаполнение и поиск дублей "
        "(в items уже есть готовые строки, опрашивать этот же GET). ready — готово, items содержит все строки. "
        "failed — ошибка обработки, error_message заполнен.",
    )
    rows_total = serializers.IntegerField(
        source="session.rows_total", read_only=True, help_text="Всего строк с названием товара в файле."
    )
    rows_done = serializers.IntegerField(
        source="session.rows_done", read_only=True, help_text="Обработано строк на текущий момент."
    )
    percent = serializers.SerializerMethodField()
    error_message = serializers.CharField(
        source="session.error_message",
        read_only=True,
        allow_null=True,
        help_text="Заполнено только при processing_status=failed.",
    )
    commit_status = serializers.ChoiceField(
        source="session.commit_status",
        choices=SupplierProductImportCommitStatus.choices,
        read_only=True,
        help_text="Фоновое применение (POST .../commit, режим 202). none -- не выполняется (не запускалось либо "
        "завершилось: итог в commit_result). pending -- в очереди. running -- применяется. failed -- ошибка, "
        "текст в commit_error; commit можно повторить. Пока pending/running, правки сессии отвечают 409.",
    )
    commit_error = serializers.CharField(
        source="session.commit_error",
        read_only=True,
        allow_null=True,
        help_text="Заполнено только при commit_status=failed.",
    )
    commit_result = SupplierProductImportCommitResultSerializer(
        source="session.commit_result",
        read_only=True,
        allow_null=True,
        help_text="Итог последнего фонового применения (тот же объект, что в ответе 200 синхронного commit); null, "
        "пока применение не завершалось успешно.",
    )
    counts = SupplierProductImportSessionCountsSerializer(read_only=True)
    warnings = SupplierProductImportSessionWarningSerializer(
        source="session.warnings",
        many=True,
        read_only=True,
        help_text="Предупреждения о качестве таблицы. Только информируют: импорт не блокируют и значения не "
        "меняют. Пустой список, если замечаний нет.",
    )
    has_more = serializers.BooleanField(
        read_only=True,
        help_text="true — в items не все подходящие строки, есть продолжение: запросите его с "
        "after_row_number=next_after_row_number.",
    )
    next_after_row_number = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="row_number последней строки в items, если has_more=true; иначе null. Передайте как "
        "after_row_number для следующей страницы.",
    )
    items = SupplierProductImportItemSerializer(
        many=True,
        read_only=True,
        help_text="Готовые (дозаполненные и сопоставленные) строки по порядку row_number. Пока pending/processing "
        "их становится всё больше, в ready — все строки. Пусто при include_items=false и при failed. С "
        "after_row_number — только строки после него.",
    )

    @extend_schema_field(serializers.IntegerField(help_text="0-100, по rows_done/rows_total."))
    def get_percent(self, obj) -> int:
        session = obj["session"]
        if not session.rows_total:
            return 100 if session.processing_status == SupplierProductImportProcessingStatus.READY else 0
        return min(100, session.rows_done * 100 // session.rows_total)


class SupplierProductImportSessionQuerySerializer(serializers.Serializer):
    include_items = serializers.BooleanField(
        required=False,
        default=True,
        help_text="false — не возвращать items (лёгкий опрос статуса и прогресса). По умолчанию true.",
    )
    include_meta = serializers.BooleanField(
        required=False,
        default=False,
        help_text="true — добавить в каждую строку field_meta (диагностика парсера: источник и статус полей). "
        "По умолчанию false: без него ответ в разы меньше и быстрее.",
    )
    after_row_number = serializers.IntegerField(
        required=False,
        min_value=0,
        help_text="Вернуть только строки с row_number больше этого значения (номер последней уже полученной "
        "строки) — курсор: для следующей страницы передайте next_after_row_number из прошлого ответа.",
    )
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Сколько строк вернуть за раз. По умолчанию 500, максимум 1000 (больше — будет 1000). "
        "Есть ли продолжение, говорит has_more.",
    )
    action = serializers.ChoiceField(
        required=False,
        choices=SupplierProductImportAction.choices,
        help_text="Вернуть только строки с этим действием (вкладки таблицы): review — требует проверки, "
        "new — добавить как новый, replace — заменить товар, skip — не добавлять. counts при этом не меняются.",
    )
    price_changed = serializers.BooleanField(
        required=False,
        default=False,
        help_text="true — только замены, у которых цена отличается от текущей цены заменяемого товара. "
        "Совмещается с action. counts при этом не меняются.",
    )


class SupplierProductImportSessionAcceptedResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(
        read_only=True,
        help_text="Id сессии импорта — то же поле, что `id` в GET сессии.",
    )
    session_id = serializers.UUIDField(
        read_only=True, help_text="То же значение, что `id` (оставлено для совместимости)."
    )
    status = serializers.ChoiceField(
        choices=SupplierProductImportProcessingStatus.choices,
        read_only=True,
        help_text="pending сразу после создания новой сессии; при повторной загрузке того же файла — текущий "
        "статус существующей сессии (pending / processing / ready).",
    )
    status_url = serializers.CharField(
        read_only=True, help_text="GET сюда — статус, прогресс и, когда готово, строки сессии."
    )


class SupplierProductImportConflictResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True, help_text="Причина отказа.")


class SupplierProductImportDetailResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True, help_text="Текст ошибки.")


class SupplierProductImportItemPatchRequestSerializer(serializers.Serializer):
    name = serializers.CharField(required=False)
    country_of_origin = serializers.CharField(required=False, allow_null=True)
    unit_id = serializers.UUIDField(required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True)
    delivery_multiple = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True
    )
    net_value = NetValueField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True
    )
    net_unit_id = serializers.UUIDField(required=False, allow_null=True)
    min_order_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True
    )
    availability = serializers.ChoiceField(choices=ProductAvailability.choices, required=False, allow_null=True)
    supplier_sku = serializers.CharField(required=False, allow_null=True)
    pack_items_count = serializers.IntegerField(
        min_value=1,
        required=False,
        allow_null=True,
        help_text="Состав упаковки: сколько штук в упаковке (8 в «8 шт. × 2 кг»). Только вместе с pack_item_value "
        "и pack_item_unit_id; null во всех трёх очищает состав.",
    )
    pack_item_value = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        allow_null=True,
        help_text="Состав упаковки: вес/объём/количество одной штуки (2 в «8 шт. × 2 кг»).",
    )
    pack_item_unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE).exclude(kind=UnitKind.PACKAGE),
        required=False,
        allow_null=True,
        source="pack_item_unit",
        help_text="Состав упаковки: единица одной штуки (кг) — вес, объём или штуки, не тара.",
    )
    pack_type_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE, kind=UnitKind.PACKAGE),
        required=False,
        allow_null=True,
        source="pack_type",
        help_text="Состав упаковки: тара (кор) — единица вида package. Необязательна.",
    )

    def validate(self, attrs):
        for field in ("unit_id", "net_unit_id"):
            if (
                attrs.get(field) is not None
                and not Unit.objects.filter(pk=attrs[field], status=EntityStatus.ACTIVE).exists()
            ):
                raise serializers.ValidationError({field: "Единица не найдена или архивна."})
        return attrs


class SupplierProductImportItemActionRequestSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=SupplierProductImportAction.choices)
    target_supplier_product_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Для replace — активный товар или черновик текущего поставщика из кандидатов или поиска.",
    )


class SupplierProductImportItemsBulkAvailabilityRequestSerializer(serializers.Serializer):
    item_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=1000)
    availability = serializers.ChoiceField(choices=ProductAvailability.choices)


class SupplierProductImportItemsDefaultsRequestSerializer(serializers.Serializer):
    country_of_origin = serializers.CharField(
        required=False,
        help_text="Страна происхождения для строк, где она не заполнена. Заполненные значения не меняются.",
    )
    min_order_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        help_text="Минимальный заказ для строк, где он не заполнен. Заполненные значения не меняются.",
    )
    delivery_multiple = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        help_text="Кратность поставки для строк, где она не заполнена. Заполненные значения не меняются.",
    )
    availability = serializers.ChoiceField(
        choices=ProductAvailability.choices,
        required=False,
        help_text="Наличие для строк, где оно не заполнено: in_stock — в наличии, on_order — под заказ, "
        "out_of_stock — нет в наличии. Заполненные значения не меняются.",
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        source="unit",
        help_text="ID единицы продажи для строк, где она не указана ни в файле, ни в названии (парсер её не "
        "подставляет, строка ждёт ручного выбора). Для кг, л и шт в строке без нетто нетто становится 1 такой "
        "единицы. Строки с уже заполненной единицей не меняются.",
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Укажите хотя бы одно поле: country_of_origin, min_order_qty, delivery_multiple, availability "
                "или unit_id."
            )
        return attrs


class SupplierProductImportItemsDefaultsResponseSerializer(serializers.Serializer):
    updated_count = serializers.IntegerField(
        read_only=True,
        help_text="Сколько строк реально изменилось (хотя бы одно поле было пустым и заполнено). "
        "Строки skip и уже импортированные не учитываются.",
    )


class SupplierProductImportItemsDefaultsValidationErrorSerializer(serializers.Serializer):
    non_field_errors = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        required=False,
        help_text="Ошибка запроса целиком: ни одного поля не передано.",
    )
    country_of_origin = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False, help_text="Ошибки поля country_of_origin."
    )
    min_order_qty = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False, help_text="Ошибки поля min_order_qty."
    )
    delivery_multiple = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False, help_text="Ошибки поля delivery_multiple."
    )
    availability = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False, help_text="Ошибки поля availability."
    )
    unit_id = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False, help_text="Ошибки поля unit_id."
    )


class SupplierProductImportSessionListQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        default="open",
        choices=[("open", "open"), ("committed", "committed"), ("all", "all")],
        help_text="open (по умолчанию) — незавершённые импорты, включая упавшие (failed); committed — уже "
        "применённые к каталогу; all — все.",
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1, help_text="Номер страницы, с 1.")
    page_size = serializers.IntegerField(
        required=False, min_value=1, default=20, help_text="Размер страницы, по умолчанию 20, максимум 100."
    )


class SupplierProductImportSessionListItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="Id сессии: GET /suppliers/products/import/{id}.")
    source_file_name = serializers.CharField(read_only=True, help_text="Имя загруженного файла.")
    status = serializers.ChoiceField(
        choices=SupplierProductImportSessionStatus.choices,
        read_only=True,
        help_text="open — импорт не завершён, committed — применён к каталогу.",
    )
    processing_status = serializers.ChoiceField(
        choices=SupplierProductImportProcessingStatus.choices,
        read_only=True,
        help_text="pending — в очереди, processing — идёт обработка, ready — готово к проверке, failed — ошибка.",
    )
    rows_total = serializers.IntegerField(read_only=True, help_text="Всего строк с названием товара в файле.")
    rows_done = serializers.IntegerField(read_only=True, help_text="Обработано строк на текущий момент.")
    percent = serializers.SerializerMethodField(help_text="0-100, по rows_done/rows_total.")
    error_message = serializers.CharField(
        read_only=True, allow_null=True, help_text="Заполнено только при processing_status=failed."
    )
    commit_status = serializers.ChoiceField(
        choices=SupplierProductImportCommitStatus.choices,
        read_only=True,
        help_text="Фоновое применение: none -- не выполняется, pending -- в очереди, running -- применяется, "
        "failed -- ошибка (подробности в GET сессии).",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Когда файл был загружен.")
    updated_at = serializers.DateTimeField(read_only=True, help_text="Последнее изменение сессии.")
    status_url = serializers.SerializerMethodField(help_text="GET сюда — статус, прогресс и строки сессии.")

    @extend_schema_field(serializers.IntegerField(help_text="0-100, по rows_done/rows_total."))
    def get_percent(self, obj) -> int:
        if not obj.rows_total:
            return 100 if obj.processing_status == SupplierProductImportProcessingStatus.READY else 0
        return min(100, obj.rows_done * 100 // obj.rows_total)

    @extend_schema_field(serializers.CharField())
    def get_status_url(self, obj) -> str:
        return f"/api/v1/suppliers/products/import/{obj.id}"


class SupplierProductImportSessionListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True, help_text="Всего сессий по фильтру.")
    next = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на следующую страницу или null.")
    previous = serializers.URLField(
        read_only=True, allow_null=True, help_text="Ссылка на предыдущую страницу или null."
    )
    results = SupplierProductImportSessionListItemSerializer(many=True, read_only=True)
