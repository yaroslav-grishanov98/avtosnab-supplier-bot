"""Venue product import."""

from rest_framework import serializers

from catalog.models import (
    CatalogImportProcessingStatus,
)

from .common import ImportDecisionsErrorSerializer, _validate_import_upload_size


class VenueImportPreviewRequestSerializer(serializers.Serializer):
    raw_names = serializers.ListField(
        child=serializers.CharField(trim_whitespace=True, max_length=500),
        min_length=1,
        max_length=1000,
        help_text="Альтернативный вход без файла: названия уже должны быть извлечены вызывающей стороной. "
        "Для обычной загрузки файла используйте .../import/file/preview. Не более 1000 строк за запрос.",
    )


class VenueImportCandidateSerializer(serializers.Serializer):
    matrix_item_id = serializers.UUIDField(read_only=True, help_text="ID позиции товарной матрицы компании.")
    requested_name = serializers.CharField(read_only=True, help_text="Текущее название позиции.")
    requested_unit_label = serializers.CharField(
        read_only=True, allow_null=True, help_text="Текущая единица измерения (свободный текст). null, если не задана."
    )
    score = serializers.FloatField(
        read_only=True,
        help_text="Схожесть 0..1: 1.0 — совпадение названия без учёта регистра, иначе триграммная схожесть "
        "(не ниже VENUE_IMPORT_MATCH_MIN_SIMILARITY). Только для сортировки — пользователю не показывать (ТЗ §44).",
    )
    updated_at = serializers.DateTimeField(
        read_only=True,
        help_text="Когда эта позиция последний раз менялась. Вернуть значение как есть в decisions."
        "expected_updated_at при выборе этого кандидата — apply откажет, если карточка успела измениться "
        "(ТЗ §32, TASK-76).",
    )


class VenueImportRowSerializer(serializers.Serializer):
    raw_name = serializers.CharField(read_only=True, help_text="Сырое название строки из импорта.")
    action = serializers.ChoiceField(
        choices=["update_existing", "create_new"],
        read_only=True,
        allow_null=True,
        help_text="null — найдены похожие товары компании (см. candidates), нужно решение пользователя: "
        "replace, update_existing (товар уже есть — запомнить сопоставление), skip или create_new. "
        "create_new — похожих нет. update_existing — только при auto_resolved=true (ранее подтверждённый "
        "alias). Preview никогда не выбирает действие сам при найденном совпадении (ТЗ §46).",
    )
    candidates = VenueImportCandidateSerializer(
        many=True,
        read_only=True,
        help_text="До 5 похожих позиций товарной матрицы по убыванию схожести; точное совпадение — первым. "
        "Пусто при create_new и при auto_resolved=true.",
    )
    matrix_item_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Цель ранее подтверждённого alias (auto_resolved=true) или candidates[0].matrix_item_id. "
        "null при create_new.",
    )
    matrix_item_requested_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Название цели alias или candidates[0].requested_name. null при create_new.",
    )
    matrix_item_updated_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Когда target (alias или candidates[0]) последний раз менялся. Вернуть как есть в "
        "decisions.expected_updated_at — apply откажет, если карточка успела измениться (ТЗ §32, TASK-76). "
        "null при create_new.",
    )
    auto_resolved = serializers.BooleanField(
        read_only=True,
        help_text="true — сопоставлено автоматически по ранее подтверждённому alias (без диалога), "
        "false — предложение, требует подтверждения пользователем перед apply.",
    )


class VenueImportDecisionSerializer(serializers.Serializer):
    row_number = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Номер строки исходного файла. Не влияет на запись, но помогает сопоставить ошибки с preview.",
    )
    raw_name = serializers.CharField(trim_whitespace=True, max_length=500, help_text="Сырое название строки.")
    requested_unit_label = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Единица измерения из file/preview. Сохраняется при action=create_new; обязательна при "
        "action=replace (станет единицей заменяемого товара).",
    )
    unit_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Необязательная подсказанная/подтверждённая единица измерения (core.Unit) — из "
        "preview.unit_id или ручного выбора пользователя. Не заменяет requested_unit_label и не "
        "обязательна ни для одного действия (TASK-81, тот же принцип, что для Мастер-справочника). "
        "Сохраняется при action=create_new/replace; игнорируется при update_existing.",
    )
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Примечание из file/preview. Сохраняется только при action=create_new.",
    )
    action = serializers.ChoiceField(
        choices=["update_existing", "create_new", "skip", "replace"],
        help_text="create_new — добавить как новый товар компании; skip — не импортировать строку; replace — "
        "заменить название и единицу измерения товара matrix_item_id данными строки (ID, связи, примечание "
        "сохраняются; сопоставление запоминается); update_existing — «товар уже есть»: запомнить "
        "сопоставление с matrix_item_id без изменения его полей.",
    )
    matrix_item_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Обязателен при action=update_existing и action=replace."
    )
    expected_updated_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Необязательно. Значение matrix_item_updated_at/candidates[].updated_at из preview для "
        "выбранного matrix_item_id (action=update_existing/replace). Если передано и товар компании успел "
        "измениться с момента preview — apply откажет всей пачкой (ТЗ §32, TASK-76). Не передано — проверка "
        "не выполняется.",
    )

    def validate(self, attrs):
        if attrs["action"] == "update_existing" and not attrs.get("matrix_item_id"):
            raise serializers.ValidationError({"matrix_item_id": "Обязателен при action=update_existing."})
        if attrs["action"] == "replace":
            errors = {}
            if not attrs.get("matrix_item_id"):
                errors["matrix_item_id"] = "Обязателен при action=replace."
            if not (attrs.get("requested_unit_label") or "").strip():
                errors["requested_unit_label"] = "Обязателен при action=replace."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs


class VenueImportApplyRequestSerializer(serializers.Serializer):
    decisions = VenueImportDecisionSerializer(
        many=True,
        help_text="Подтверждённые решения из ответа file/preview или JSON preview. Для create_new переносите "
        "также requested_unit_label/note; для update_existing и replace нужен matrix_item_id, для replace — "
        "ещё requested_unit_label; передавайте только строки с принятым решением. Не более 1000 решений; "
        "весь запрос применяется транзакционно.",
    )
    present_matrix_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
        max_length=1000,
        help_text="TASK-74. Для частичного apply («импортировать проверенные, оставить спорные»): ID "
        "товаров компании, сопоставленных с какой-либо строкой ЭТОГО ЖЕ файла импорта, по которым в этом "
        "запросе решения нет. Источник id — не только исходный preview: если apply вызывается "
        "ПОСЛЕДОВАТЕЛЬНО несколько раз для одного и того же файла (частичные apply один за другим), сюда "
        "нужно передавать также matrix_item_id из ответа КАЖДОГО предыдущего apply-вызова этой же "
        "последовательности — например, товар, созданный через create_new в предыдущем вызове, не мог "
        "иметь id в исходном preview (его тогда ещё не существовало), поэтому защитить его от "
        "missing_from_latest_import может только id из ответа того вызова, который его создал. Без этого "
        "товары, ещё не получившие решение в текущем запросе, ошибочно получат "
        "missing_from_latest_import=true, хотя они всё ещё есть в файле — просто ещё не решены. Лишние id "
        "не создают, не изменяют и не удаляют ничего постороннего — единственный эффект передачи id здесь: "
        "если у этого товара сейчас missing_from_latest_import=true, он будет снят (сброшен в false); "
        "поэтому передавайте только id, которые действительно нужно защитить/снять с пометки.",
    )

    MAX_DECISIONS = 1000

    def validate_decisions(self, value):
        # The limit is checked here, not via max_length= on the field: DRF puts a
        # ListSerializer max_length error under non_field_errors (an object), while
        # the 400 contract (ImportDecisionsErrorSerializer) says decisions is a list.
        if not value:
            raise serializers.ValidationError("Нужно передать хотя бы одно решение.")
        if len(value) > self.MAX_DECISIONS:
            raise serializers.ValidationError(f"Не более {self.MAX_DECISIONS} решений за запрос.")
        return value


class VenueImportApplyRowSerializer(serializers.Serializer):
    raw_name = serializers.CharField(read_only=True, help_text="Сырое название строки из импорта.")
    action = serializers.ChoiceField(
        choices=["update_existing", "create_new", "skip", "replace"],
        read_only=True,
        help_text="Применённое решение (то же значение, что было передано в decisions). Для replace "
        "matrix_item_requested_name — уже новое название.",
    )
    matrix_item_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID итогового товара компании (существующего или созданного). null только для skip.",
    )
    matrix_item_requested_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название итогового товара компании. null только для skip."
    )
    alias_created = serializers.BooleanField(
        read_only=True, help_text="true, если для этой строки записан/обновлён VenueProductImportAlias."
    )
    conflict = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="2026-09-29: если не null -- строка НЕ применена (дублирующийся target replace в этой пачке, "
        "либо target изменился после preview) и требует повторного review; остальные строки пачки применены как "
        "обычно (per-row, не whole-batch-abort).",
    )


class VenueImportFilePreviewRequestSerializer(serializers.Serializer):
    validate_file = staticmethod(_validate_import_upload_size)

    file = serializers.FileField(
        help_text="Основной вход импорта для фронтенда. Файл справочника компании (.xlsx/.xlsm/.csv), "
        "максимум 10 МБ и 1000 строк. Отправляется как multipart/form-data."
    )


class VenueImportFilePreviewRowSerializer(serializers.Serializer):
    row_number = serializers.IntegerField(read_only=True, help_text="Номер строки в исходном файле.")
    raw_name = serializers.CharField(read_only=True, help_text="Название товара из файла.")
    requested_unit_label = serializers.CharField(
        read_only=True, allow_null=True, help_text="Единица измерения из файла. null, если не указана."
    )
    unit_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Распознанная единица измерения (core.Unit) — из requested_unit_label напрямую или "
        "подсказана LLM (см. unit_source). null, если не распознана или requested_unit_label пуст.",
    )
    unit_unrecognized = serializers.BooleanField(
        read_only=True,
        help_text="true — requested_unit_label не найден в справочнике единиц измерения и LLM тоже не "
        "смогла его уверенно определить (см. unit_source); unit_id при этом null. Не блокирует apply — "
        "unit_id необязателен ни для одного действия (TASK-81, тот же принцип, что для Мастер-справочника).",
    )
    unit_source = serializers.ChoiceField(
        choices=["llm"],
        read_only=True,
        allow_null=True,
        help_text="llm — unit_id не распознан из requested_unit_label напрямую, но подсказан моделью и "
        "не требует отдельного подтверждения (ТЗ §38). null — единица распознана напрямую, отсутствует, "
        "или LLM не смогла её определить.",
    )
    note = serializers.CharField(
        read_only=True, allow_null=True, help_text="Примечание из файла. null, если не указано."
    )
    action = serializers.ChoiceField(
        choices=["update_existing", "create_new"],
        read_only=True,
        allow_null=True,
        help_text="null — найдены похожие товары компании (см. candidates), нужно решение пользователя: "
        "replace, update_existing (товар уже есть — запомнить сопоставление), skip или create_new. "
        "create_new — похожих нет. update_existing — только при auto_resolved=true (ранее подтверждённый "
        "alias). Preview никогда не выбирает действие сам при найденном совпадении (ТЗ §46).",
    )
    candidates = VenueImportCandidateSerializer(
        many=True,
        read_only=True,
        help_text="До 5 похожих позиций товарной матрицы по убыванию схожести; точное совпадение — первым. "
        "Пусто при create_new и при auto_resolved=true.",
    )
    matrix_item_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Цель ранее подтверждённого alias (auto_resolved=true) или candidates[0].matrix_item_id. "
        "null при create_new.",
    )
    matrix_item_requested_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Название цели alias или candidates[0].requested_name. null при create_new.",
    )
    matrix_item_updated_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Когда target (alias или candidates[0]) последний раз менялся. Вернуть как есть в "
        "decisions.expected_updated_at — apply откажет, если карточка успела измениться (ТЗ §32, TASK-76). "
        "null при create_new.",
    )
    auto_resolved = serializers.BooleanField(
        read_only=True,
        help_text="true только для строки с ранее подтверждённым alias — свежее точное совпадение "
        "всё равно требует подтверждения перед apply (то же правило, что у JSON-варианта превью).",
    )


class VenueImportFilePreviewResponseSerializer(serializers.Serializer):
    sheet_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Имя листа Excel, откуда взяты данные. null для CSV."
    )
    header_row_number = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Номер строки с заголовками в исходном файле. null если заголовок не найден.",
    )
    detected_columns = serializers.DictField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Сопоставление найденных колонок файла: name/unit/note → заголовок колонки.",
    )
    total_rows = serializers.IntegerField(read_only=True, help_text="Всего строк в файле.")
    rows_without_name = serializers.IntegerField(
        read_only=True, help_text="Строк, пропущенных из-за отсутствия названия товара — не входят в rows."
    )
    session_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Id сессии ревью: PATCH import-sessions/{session_id}/items/{item_id}, commit - POST "
        "import-sessions/{session_id}. Для больших файлов приходит в result после обработки.",
    )
    rows = VenueImportFilePreviewRowSerializer(
        many=True,
        read_only=True,
        help_text="Строки файла с предлагаемым действием. После подтверждения преобразуйте их в decisions "
        "для POST .../import/apply: перенесите row_number, raw_name, requested_unit_label, note, action и "
        "matrix_item_id для update_existing.",
    )


class VenueImportMasterMatchPreviewRequestSerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(help_text="ID владельца мастер-справочника, с которым сопоставляем.")
    matrix_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=1000,
        help_text="ID строк товарной матрицы этой компании для сопоставления с мастер-справочником. "
        "Не более 1000 за запрос.",
    )


class VenueImportMasterMatchRowSerializer(serializers.Serializer):
    matrix_item_id = serializers.UUIDField(read_only=True, help_text="ID строки товарной матрицы.")
    requested_name = serializers.CharField(read_only=True, help_text="Название товара компании.")
    current_master_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Текущий привязанный мастер-товар, если есть. null, если нет."
    )
    current_master_product_canonical_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название текущего привязанного мастер-товара. null, если нет."
    )
    proposed_master_product_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Предлагаемый мастер-товар для привязки. null для create_new/keep_existing.",
    )
    proposed_master_product_canonical_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Название предлагаемого мастер-товара. null для create_new/keep_existing.",
    )
    proposed_action = serializers.ChoiceField(
        choices=["keep_existing", "link_existing", "create_new"],
        read_only=True,
        help_text="keep_existing — уже привязан, менять не нужно; link_existing — сопоставить с "
        "proposed_master_product_id; create_new — добавить новый мастер-товар.",
    )
    score = serializers.FloatField(
        read_only=True,
        allow_null=True,
        help_text="Оценка схожести (0-1) для link_existing/create_new. null для keep_existing.",
    )


class VenueImportMasterMatchDecisionSerializer(serializers.Serializer):
    matrix_item_id = serializers.UUIDField(help_text="ID строки товарной матрицы.")
    action = serializers.ChoiceField(
        choices=["link_existing", "create_new", "skip"],
        help_text="link_existing — привязать master_product_id, create_new — создать новый мастер-товар, "
        "skip — не менять эту строку.",
    )
    master_product_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Обязателен при action=link_existing."
    )

    def validate(self, attrs):
        if attrs["action"] == "link_existing" and not attrs.get("master_product_id"):
            raise serializers.ValidationError({"master_product_id": "Обязателен при action=link_existing."})
        return attrs


class VenueImportMasterMatchApplyRequestSerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(help_text="ID владельца мастер-справочника, с которым сопоставляем.")
    decisions = VenueImportMasterMatchDecisionSerializer(
        many=True,
        max_length=1000,
        help_text="Решения по каждой строке. Не более 1000 за запрос.",
    )

    def validate_decisions(self, value):
        if not value:
            raise serializers.ValidationError("Нужно передать хотя бы одно решение.")
        return value


class VenueImportMasterMatchApplyRowSerializer(serializers.Serializer):
    matrix_item_id = serializers.UUIDField(read_only=True, help_text="ID строки товарной матрицы.")
    action = serializers.ChoiceField(
        choices=["link_existing", "create_new", "skip"],
        read_only=True,
        help_text="Применённое решение (то же значение, что было передано в decisions).",
    )
    master_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID мастер-товара после применения. null, если строка не привязана."
    )
    master_product_canonical_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Название мастер-товара после применения. null, если строка не привязана.",
    )


class VenueImportApplyErrorSerializer(ImportDecisionsErrorSerializer):
    present_matrix_item_ids = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Ошибка present_matrix_item_ids: элемент списка не является валидным UUID, либо список "
        "длиннее 1000 элементов.",
    )


class VenueImportFileAcceptedResponseSerializer(serializers.Serializer):
    session_id = serializers.UUIDField(read_only=True, help_text="Id сессии ревью.")
    status = serializers.ChoiceField(
        choices=CatalogImportProcessingStatus.choices, read_only=True, help_text="Сразу после создания - pending."
    )
    status_url = serializers.CharField(
        read_only=True, help_text="GET сюда - статус, прогресс и готовые строки (processing_status, rows_done, items)."
    )
