"""Master catalog products, imports, venue links and bulk actions."""

from typing import Optional

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.measure import NetValueField
from catalog.models import (
    MasterProductImportUploadStatus,
)
from core.models import (
    EntityStatus,
    Unit,
    User,
)
from suppliers.models import SupplierProduct

from .common import ImportDecisionsErrorSerializer, UUIDListField, _validate_import_upload_size
from .product_matrix import VenueProductMatrixItemSerializer


class MasterProductVenueLinkQuerySerializer(serializers.Serializer):
    link_existing_item_id = serializers.UUIDField(
        required=False,
        help_text=(
            "ID уже существующей непривязанной строки товарной матрицы этой компании — связать именно её с "
            "мастер-товаром вместо создания новой (обычно берётся из candidate.id ответа 409 на этот же запрос)."
        ),
    )
    force_create = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Создать новую строку, даже если найден похожий непривязанный товар компании. Игнорируется, если "
            "передан link_existing_item_id."
        ),
    )


class MasterProductVenueLinkConflictSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="Пояснение конфликта.")
    candidate = VenueProductMatrixItemSerializer(
        help_text=(
            "Найденная похожая непривязанная строка товарной матрицы компании — свяжите её "
            "(link_existing_item_id) или создайте новую (force_create=true)."
        )
    )


class MasterProductSuggestionSerializer(serializers.Serializer):
    master_product_id = serializers.UUIDField(read_only=True)
    canonical_name = serializers.CharField(read_only=True)
    unit_id = serializers.UUIDField(read_only=True, allow_null=True)
    unit_code = serializers.CharField(read_only=True, allow_null=True)
    net_value = NetValueField(max_digits=14, decimal_places=4, read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True, allow_null=True)
    companies_count = serializers.IntegerField(read_only=True)
    score = serializers.FloatField(read_only=True, allow_null=True)
    already_linked_to_venue = serializers.BooleanField(read_only=True)
    can_link = serializers.BooleanField(read_only=True)


class MasterProductSuggestionResponseSerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(read_only=True)
    mode = serializers.ChoiceField(choices=["recommended", "search"], read_only=True)
    query = serializers.CharField(read_only=True)
    recommended = MasterProductSuggestionSerializer(many=True, read_only=True)
    search_results = MasterProductSuggestionSerializer(many=True, read_only=True)
    search_results_count = serializers.IntegerField(
        read_only=True,
        help_text=(
            "Всего совпадений по q, без учёта limit/offset — используйте вместе с offset для "
            "постраничного доступа к search_results. Не относится к recommended (та выдача не "
            "пагинируется)."
        ),
    )


class MasterProductListQuerySerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(
        required=True, help_text="ID владельца учётной записи, чей мастер-справочник запрашивается."
    )
    q = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Поиск по названию мастер-товара (подстрока)."
    )


class MasterProductSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID мастер-товара.")
    owner_id = serializers.UUIDField(read_only=True, help_text="ID владельца учётной записи.")
    canonical_name = serializers.CharField(read_only=True, help_text="Название мастер-товара.")
    unit_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="ID единицы измерения.")
    unit_code = serializers.SerializerMethodField()
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, allow_null=True, help_text="Вес/объём нетто."
    )
    note = serializers.CharField(read_only=True, allow_null=True, help_text="Примечание.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус мастер-товара: active / archived.",
    )
    companies_count = serializers.IntegerField(
        read_only=True, help_text="Количество компаний владельца, где привязан этот мастер-товар."
    )

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Код единицы измерения."))
    def get_unit_code(self, obj) -> Optional[str]:
        return obj.unit.code if obj.unit_id else None


class MasterProductListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True, help_text="Общее количество мастер-товаров.")
    next = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на следующую страницу.")
    previous = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на предыдущую страницу.")
    results = MasterProductSerializer(many=True, read_only=True)


class MasterProductBulkCreateItemSerializer(serializers.Serializer):
    canonical_name = serializers.CharField(trim_whitespace=True, max_length=500, help_text="Название мастер-товара.")
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        source="unit",
        required=False,
        allow_null=True,
        help_text="ID единицы измерения.",
    )
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True, help_text="Вес/объём нетто."
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Примечание.")


class MasterProductBulkCreateSerializer(serializers.Serializer):
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(status=EntityStatus.ACTIVE),
        source="owner",
        help_text="ID владельца учётной записи, которому будут принадлежать созданные мастер-товары.",
    )
    items = MasterProductBulkCreateItemSerializer(
        many=True,
        allow_empty=False,
        max_length=500,
        help_text="Список мастер-товаров для создания. Максимум 500 строк за один запрос.",
    )


class MasterProductBulkCreateResultItemSerializer(serializers.Serializer):
    canonical_name = serializers.CharField(read_only=True, help_text="Название из соответствующей строки запроса.")
    outcome = serializers.ChoiceField(
        choices=["created", "skipped_duplicate"],
        read_only=True,
        help_text="created — создан новый мастер-товар; skipped_duplicate — пропущен, у этого владельца уже "
        "есть активный мастер-товар с таким названием (без учёта регистра).",
    )
    master_product_id = serializers.UUIDField(
        read_only=True, help_text="ID созданного или (при пропуске) уже существующего мастер-товара."
    )


class MasterProductBulkCreateResponseSerializer(serializers.Serializer):
    results = MasterProductBulkCreateResultItemSerializer(
        many=True, read_only=True, help_text="Результат по каждой строке запроса, в том же порядке."
    )


class MasterProductBulkDeleteSerializer(serializers.Serializer):
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(status=EntityStatus.ACTIVE),
        source="owner",
        help_text="ID владельца учётной записи — все master_product_ids должны принадлежать ему.",
    )
    master_product_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=500,
        help_text="Список ID мастер-товаров для удаления. Максимум 500 строк за один запрос.",
    )


class MasterProductBulkDeleteResultItemSerializer(serializers.Serializer):
    master_product_id = serializers.UUIDField(read_only=True, help_text="ID из соответствующей строки запроса.")
    outcome = serializers.ChoiceField(
        choices=["deleted", "not_found"],
        read_only=True,
        help_text="deleted — удалён; not_found — не найден среди активных мастер-товаров этого владельца.",
    )


class MasterProductBulkDeleteResponseSerializer(serializers.Serializer):
    results = MasterProductBulkDeleteResultItemSerializer(
        many=True, read_only=True, help_text="Результат по каждому id из запроса, в том же порядке."
    )


class MasterProductCreateSerializer(serializers.Serializer):
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(status=EntityStatus.ACTIVE),
        source="owner",
        help_text="ID владельца учётной записи, которому будет принадлежать мастер-товар.",
    )
    canonical_name = serializers.CharField(trim_whitespace=True, max_length=500, help_text="Название мастер-товара.")
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        source="unit",
        required=False,
        allow_null=True,
        help_text="ID единицы измерения.",
    )
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True, help_text="Вес/объём нетто."
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Примечание.")


class MasterProductImportRequestSerializer(serializers.Serializer):
    validate_file = staticmethod(_validate_import_upload_size)

    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(status=EntityStatus.ACTIVE),
        source="owner",
        help_text="ID владельца учётной записи, в чей Мастер-справочник импортируется файл.",
    )
    file = serializers.FileField(
        help_text="Централизованный справочник (.xlsx/.xlsm/.csv), максимум 10 МБ и 5000 строк. Файлы "
        "больше 500 строк обрабатываются в фоне — см. описание ответа 202."
    )


class MasterProductImportCandidateSerializer(serializers.Serializer):
    master_product_id = serializers.UUIDField(read_only=True, help_text="ID существующего мастер-товара.")
    canonical_name = serializers.CharField(read_only=True, help_text="Текущее название мастер-товара.")
    unit_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Текущая единица измерения мастер-товара. null, если не задана."
    )
    unit_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название текущей единицы измерения. null, если не задана."
    )
    score = serializers.FloatField(
        read_only=True,
        help_text="Схожесть 0..1 (не ниже порога MASTER_PRODUCT_MATCH_MIN_SCORE). Только для сортировки — "
        "пользователю не показывать (ТЗ §44).",
    )
    updated_at = serializers.DateTimeField(
        read_only=True,
        help_text="Когда этот мастер-товар последний раз менялся. Вернуть значение как есть в "
        "decisions.expected_updated_at при выборе этого кандидата — apply откажет, если карточка успела "
        "измениться (ТЗ §32, TASK-76).",
    )


class MasterProductImportPreviewRowSerializer(serializers.Serializer):
    row_number = serializers.IntegerField(read_only=True, allow_null=True, help_text="Номер строки в исходном файле.")
    raw_name = serializers.CharField(read_only=True, help_text="Название товара из файла.")
    unit_label = serializers.CharField(
        read_only=True, allow_null=True, help_text="Единица измерения из файла (сырой текст)."
    )
    unit_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="Распознанная единица измерения.")
    unit_unrecognized = serializers.BooleanField(
        read_only=True,
        help_text="true — единица unit_label из файла не найдена в справочнике единиц измерения и "
        "LLM тоже не смогла её уверенно определить (см. unit_source); unit_id при этом null, нужен "
        "ручной выбор перед apply (ТЗ §52). false, если единица распознана из файла напрямую ИЛИ "
        'успешно подсказана LLM (в этом случае unit_source = "llm"). Незнакомая единица одной '
        "строки не блокирует preview остальных строк файла (TASK-77).",
    )
    unit_source = serializers.ChoiceField(
        choices=["llm"],
        read_only=True,
        allow_null=True,
        help_text="llm — единица измерения не была распознана из файла напрямую, но подсказана "
        "моделью и не требует отдельного подтверждения пользователя (ТЗ §38, TASK-75). null — "
        "единица пришла из файла как есть, отсутствует, или LLM не смогла её определить.",
    )
    net_value = NetValueField(max_digits=14, decimal_places=4, read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True, allow_null=True)
    action = serializers.ChoiceField(
        choices=["create_new"],
        read_only=True,
        allow_null=True,
        help_text="null — найдены похожие мастер-товары (см. candidates), нужно решение пользователя: replace, "
        "skip или create_new. create_new — похожих нет, по умолчанию добавить как новый. Preview никогда не "
        "выбирает replace/update_existing сам (ТЗ §46).",
    )
    candidates = MasterProductImportCandidateSerializer(
        many=True,
        read_only=True,
        help_text="До 5 похожих мастер-товаров владельца по убыванию схожести. Пусто, если похожих нет.",
    )
    candidate_master_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Устарело: то же, что candidates[0].master_product_id."
    )
    candidate_canonical_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Устарело: то же, что candidates[0].canonical_name."
    )
    score = serializers.FloatField(
        read_only=True, allow_null=True, help_text="Устарело: то же, что candidates[0].score. Не показывать."
    )
    needs_attention = serializers.BooleanField(
        read_only=True,
        help_text="true — нужно решение пользователя: либо есть candidates (action = null), либо единица "
        "измерения из файла не распознана (unit_unrecognized = true, ТЗ §52) — в этом случае action всё ещё "
        "create_new, но перед apply нужен ручной выбор единицы. false — похожих нет и единица распознана "
        "(или отсутствует в файле).",
    )


class MasterProductImportPreviewResponseSerializer(serializers.Serializer):
    sheet_name = serializers.CharField(read_only=True, allow_null=True)
    header_row_number = serializers.IntegerField(read_only=True, allow_null=True)
    detected_columns = serializers.DictField(child=serializers.CharField(), read_only=True)
    total_rows = serializers.IntegerField(read_only=True, help_text="Всего строк в файле.")
    rows_without_name = serializers.IntegerField(
        read_only=True, help_text="Строк, пропущенных из-за отсутствия названия товара — не входят в rows."
    )
    auto_resolved_count = serializers.IntegerField(
        read_only=True, help_text="Строк без похожих мастер-товаров (action = create_new)."
    )
    needs_attention_count = serializers.IntegerField(
        read_only=True, help_text="Строк, требующих решения пользователя (action = null)."
    )
    session_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Id сессии ревью: PATCH import-sessions/{session_id}/items/{item_id}, commit - POST "
        "import-sessions/{session_id}. Для больших файлов приходит в result после обработки.",
    )
    rows = MasterProductImportPreviewRowSerializer(many=True, read_only=True)


class MasterProductImportUploadAcceptedResponseSerializer(serializers.Serializer):
    upload_id = serializers.UUIDField(read_only=True, help_text="Id фоновой обработки импорта.")
    status = serializers.ChoiceField(
        choices=MasterProductImportUploadStatus.choices,
        read_only=True,
        help_text="Всегда processing сразу после создания.",
    )
    status_url = serializers.CharField(
        read_only=True, help_text="GET сюда — статус, прогресс и, когда готово, результат."
    )


class MasterProductImportUploadStatusQuerySerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(required=True, help_text="Тот же owner_id, что был передан в .../import/preview.")


class MasterProductImportUploadStatusResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=MasterProductImportUploadStatus.choices,
        read_only=True,
        help_text="processing — обрабатывается, result всегда null. ready — готово, result заполнен. "
        "failed — ошибка, error_message заполнен.",
    )
    rows_total = serializers.IntegerField(
        read_only=True, help_text="Всего строк файла, включая пропущенные без названия."
    )
    rows_done = serializers.IntegerField(read_only=True, help_text="Обработано строк на текущий момент.")
    percent = serializers.SerializerMethodField()
    result = MasterProductImportPreviewResponseSerializer(
        read_only=True, allow_null=True, help_text="Заполнено только при status=ready."
    )
    error_message = serializers.CharField(
        read_only=True, allow_null=True, help_text="Заполнено только при status=failed."
    )

    @extend_schema_field(serializers.IntegerField(help_text="0-100, по rows_done/rows_total."))
    def get_percent(self, obj) -> int:
        if not obj.rows_total:
            return 100 if obj.status == MasterProductImportUploadStatus.READY else 0
        return min(100, round(obj.rows_done * 100 / obj.rows_total))


class MasterProductImportApplyErrorSerializer(ImportDecisionsErrorSerializer):
    owner_id = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Ошибка owner_id: не передан, невалидный UUID или владелец не найден/неактивен.",
    )


class MasterProductImportApplyDecisionSerializer(serializers.Serializer):
    raw_name = serializers.CharField(trim_whitespace=True, max_length=500, help_text="Название строки файла.")
    action = serializers.ChoiceField(
        choices=["update_existing", "create_new", "skip", "replace"],
        help_text="create_new — добавить как новый мастер-товар; skip — не импортировать строку; replace — "
        "заменить название и единицу измерения мастер-товара master_product_id данными строки (ID и связи "
        "сохраняются, net_value/note не меняются); update_existing — сопоставить с master_product_id без "
        "изменения его полей.",
    )
    master_product_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Обязателен при action=update_existing и action=replace."
    )
    unit_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Единица измерения. Обязательна при action=replace (станет единицей заменяемого "
        "мастер-товара); при create_new — необязательна. Должна ссылаться на активную единицу измерения.",
    )
    net_value = NetValueField(
        max_digits=14,
        decimal_places=4,
        required=False,
        allow_null=True,
        help_text="Вес/объём нетто нового мастер-товара. Используется только при action=create_new.",
    )
    note = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="Примечание нового мастер-товара. Используется только при action=create_new.",
    )
    expected_updated_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Необязательно. Значение candidates[].updated_at из preview для выбранного master_product_id "
        "(action=update_existing/replace). Если передано и мастер-товар успел измениться с момента preview — "
        "apply откажет всей пачкой (ТЗ §32, TASK-76). Не передано — проверка не выполняется.",
    )

    def validate(self, attrs):
        if attrs["action"] == "update_existing" and not attrs.get("master_product_id"):
            raise serializers.ValidationError({"master_product_id": "Обязателен при action=update_existing."})
        if attrs["action"] == "replace":
            errors = {
                field: "Обязателен при action=replace."
                for field in ("master_product_id", "unit_id")
                if not attrs.get(field)
            }
            if errors:
                raise serializers.ValidationError(errors)
        return attrs


class MasterProductImportApplyRequestSerializer(serializers.Serializer):
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(status=EntityStatus.ACTIVE),
        source="owner",
        help_text="ID владельца учётной записи, в чей Мастер-справочник применяются решения.",
    )
    decisions = MasterProductImportApplyDecisionSerializer(
        many=True, help_text="Решения по каждой строке импорта (из .../import/preview). Не более 5000 решений."
    )

    MAX_DECISIONS = 5000

    def validate_decisions(self, value):
        # Limit checked here, not via max_length= — see VenueImportApplyRequestSerializer.
        if not value:
            raise serializers.ValidationError("Нужно передать хотя бы одно решение.")
        if len(value) > self.MAX_DECISIONS:
            raise serializers.ValidationError(f"Не более {self.MAX_DECISIONS} решений за запрос.")
        return value


class MasterProductImportApplyRowSerializer(serializers.Serializer):
    raw_name = serializers.CharField(read_only=True, help_text="Название товара из файла.")
    action = serializers.ChoiceField(
        choices=["update_existing", "create_new", "skip", "replace"],
        read_only=True,
        help_text="Применённое действие: update_existing — сопоставлена с master_product_id, create_new — "
        "добавлена как новая, skip — строка не импортировалась, replace — название и единица мастер-товара "
        "master_product_id заменены (canonical_name/unit_id в ответе — уже новые).",
    )
    master_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID мастер-товара, с которым сопоставлена или который создан."
    )
    canonical_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название мастер-товара после применения."
    )
    unit_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="Единица измерения мастер-товара.")
    net_value = NetValueField(
        max_digits=14, decimal_places=4, read_only=True, allow_null=True, help_text="Вес/объём нетто мастер-товара."
    )
    note = serializers.CharField(read_only=True, allow_null=True, help_text="Примечание мастер-товара.")
    conflict = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="2026-09-29: если не null -- строка НЕ применена (дублирующийся target replace в этой пачке, "
        "либо target изменился после preview) и требует повторного review; остальные строки пачки применены как "
        "обычно (per-row, не whole-batch-abort).",
    )


class MasterProductImportApplyResponseSerializer(serializers.Serializer):
    rows = MasterProductImportApplyRowSerializer(
        many=True, read_only=True, help_text="Результат применения решений, по одной строке на решение."
    )


class MasterProductUpdateSerializer(serializers.Serializer):
    canonical_name = serializers.CharField(
        required=False, trim_whitespace=True, max_length=500, help_text="Новое название мастер-товара."
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        source="unit",
        required=False,
        allow_null=True,
        help_text="Новая единица измерения.",
    )
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True, help_text="Новый вес/объём нетто."
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Новое примечание.")

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class MasterProductVenueBreakdownSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID компании (venue).")
    venue_name = serializers.CharField(read_only=True, help_text="Название компании.")
    linked = serializers.BooleanField(read_only=True, help_text="Привязан ли мастер-товар к этой компании.")
    matrix_item_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID строки товарной матрицы компании, если привязан."
    )
    requested_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название товара в справочнике компании."
    )
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID привязанного товара поставщика."
    )
    supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название товара поставщика."
    )
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text=(
            "Итоговая цена товара для этой компании: индивидуальная цена (SupplierProductVenuePrice), если для "
            "компании действует активная запись с покрывающим текущий момент периодом действия, иначе базовая "
            "цена поставщика (SupplierProduct.current_price)."
        ),
    )


class MasterProductVenueBatchChangeSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(help_text="ID компании (venue).")
    link_action = serializers.ChoiceField(
        choices=["keep", "link", "unlink"],
        default="keep",
        help_text="keep — не менять привязку (можно всё равно передать supplier_product_id, если строка уже "
        "привязана); link — связать (см. link_existing_item_id/force_create, та же семантика, что у "
        "PUT .../venues/{venue_id}); unlink — отвязать.",
    )
    link_existing_item_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="При link_action=link — связать эту существующую строку."
    )
    force_create = serializers.BooleanField(
        required=False,
        default=False,
        help_text="При link_action=link — создать новую строку, не спрашивая про похожую.",
    )
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        required=False,
        help_text="Назначить/сменить товар поставщика для этой компании. Не передавайте поле вовсе, чтобы "
        "оставить как есть — снять привязку через этот эндпоинт нельзя (см. PATCH .../product-matrix/{item_id}).",
    )


class MasterProductVenueBatchRequestSerializer(serializers.Serializer):
    changes = MasterProductVenueBatchChangeSerializer(
        many=True, min_length=1, max_length=200, help_text="Изменения по компаниям, сохраняются одним вызовом."
    )


class MasterProductVenueBatchRowSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID компании (venue).")
    venue_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название компании.")
    current_linked = serializers.BooleanField(
        read_only=True, help_text="Был ли мастер-товар привязан к этой компании до применения изменения."
    )
    current_matrix_item_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID строки товарной матрицы компании до изменения, если был."
    )
    current_requested_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название товара в справочнике компании до изменения."
    )
    current_supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID привязанного товара поставщика до изменения."
    )
    current_supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название товара поставщика до изменения."
    )
    current_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Итоговая цена товара для этой компании до изменения (см. price в венью-разбивке).",
    )
    link_action = serializers.ChoiceField(
        choices=["keep", "link", "unlink"], read_only=True, help_text="Применённое действие для этой компании."
    )
    new_matrix_item_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID строки товарной матрицы компании после изменения, если привязана. В ответе PREVIEW это "
        "значение иллюстративно (строка была создана и откатана в рамках транзакции превью) — не кешируйте и не "
        "используйте его в других вызовах; реальный, устойчивый id возвращается только ответом APPLY.",
    )
    new_supplier_product_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID товара поставщика после изменения. В ответе PREVIEW относится к строке, которая была "
        "откатана — не кешируйте и не переиспользуйте, полагайтесь только на ответ APPLY.",
    )
    new_supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название товара поставщика после изменения."
    )
    changed = serializers.BooleanField(read_only=True, help_text="Отличается ли состояние после изменения от текущего.")


class MasterProductVenueBatchResponseSerializer(serializers.Serializer):
    rows = MasterProductVenueBatchRowSerializer(
        many=True, read_only=True, help_text="Строки по каждой компании из запроса, в том же порядке."
    )


class MasterProductBulkSupplierProductAssignSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        help_text="ID товара поставщика, который назначается всем выбранным компаниям.",
    )
    venue_ids = UUIDListField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Компании (venue), к которым применить — только уже привязанные к этому мастер-товару. "
            "Не передано или пусто — все привязанные компании."
        ),
    )


class MasterProductBulkSupplierProductRowSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID компании (venue).")
    venue_name = serializers.CharField(read_only=True, help_text="Название компании.")
    matrix_item_id = serializers.UUIDField(read_only=True, help_text="ID строки товарной матрицы компании.")
    requested_name = serializers.CharField(read_only=True, help_text="Название товара в справочнике компании.")
    current_supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Товар поставщика, привязанный сейчас. null — ещё не привязан."
    )
    current_supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название текущего товара поставщика."
    )
    new_supplier_product_id = serializers.UUIDField(
        read_only=True, help_text="Товар поставщика, который будет назначен."
    )
    new_supplier_product_name = serializers.CharField(
        read_only=True, help_text="Название назначаемого товара поставщика."
    )
    changed = serializers.BooleanField(
        read_only=True, help_text="true, если назначаемый товар отличается от текущего для этой компании."
    )


class MasterProductTenderOfferSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        help_text="ID товара поставщика из выбранного предложения тендера.",
    )
    price = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, help_text="Цена выбранного предложения."
    )
    currency = serializers.ChoiceField(
        choices=[("RUB", "RUB")],
        required=False,
        default="RUB",
        help_text="Валюта (ISO 4217). Сейчас поддерживается только RUB. По умолчанию RUB.",
    )
    venue_ids = UUIDListField(
        help_text=(
            "Компании (venue), для которых выбрано предложение этого поставщика — только уже привязанные к этому "
            "мастер-товару. Обязателен, минимум одна (в отличие от массового назначения товара поставщика, "
            "здесь нет варианта «все привязанные компании по умолчанию» — п. 45 исходного документа)."
        ),
    )
    source_reference = serializers.CharField(
        help_text=(
            "Ссылка на тендер, например «Тендер №42». Обязателен — источник цены на этом эндпоинте всегда "
            "тендер (source_type=tender), в отличие от ручного назначения цены, где источник опционален."
        ),
    )

    def validate_venue_ids(self, value):
        if not value:
            raise serializers.ValidationError("Укажите хотя бы одну компанию.")
        return value


class MasterProductTenderOfferRowSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID компании (venue).")
    venue_name = serializers.CharField(read_only=True, help_text="Название компании.")
    matrix_item_id = serializers.UUIDField(read_only=True, help_text="ID строки товарной матрицы компании.")
    requested_name = serializers.CharField(read_only=True, help_text="Название товара в справочнике компании.")
    current_supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Товар поставщика, привязанный сейчас. null — ещё не привязан."
    )
    current_supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название текущего товара поставщика."
    )
    new_supplier_product_id = serializers.UUIDField(
        read_only=True, help_text="Товар поставщика из выбранного предложения."
    )
    new_supplier_product_name = serializers.CharField(
        read_only=True, help_text="Название товара поставщика из выбранного предложения."
    )
    changed = serializers.BooleanField(
        read_only=True, help_text="true, если назначаемый товар отличается от текущего для этой компании."
    )
    current_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Текущая цена (индивидуальная либо базовая). null — товар поставщика ещё не назначен.",
    )
    current_price_currency = serializers.CharField(read_only=True, allow_null=True, help_text="Валюта текущей цены.")
    new_price = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, help_text="Цена выбранного предложения тендера."
    )
    new_price_currency = serializers.CharField(read_only=True, help_text="Валюта цены выбранного предложения.")
