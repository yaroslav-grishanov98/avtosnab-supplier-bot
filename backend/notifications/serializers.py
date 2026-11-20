from rest_framework import serializers

from core.models import SupplierOrderRequestStatus

from .models import (
    NotificationAudience,
    NotificationChannel,
    NotificationDestinationType,
    NotificationEvent,
    NotificationInboxEventType,
    NotificationSupplierActionType,
)


class NotificationDestinationCreateSerializer(serializers.Serializer):
    """Проверить данные для ручного подключения группового чата."""

    audience = serializers.ChoiceField(choices=NotificationAudience.choices)
    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен для audience=supplier; для buyer не используется."
    )
    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    destination_type = serializers.ChoiceField(choices=[NotificationDestinationType.GROUP])
    chat_id = serializers.CharField(max_length=255)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    venue_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        help_text="Обязателен и должен содержать доступные заведения для audience=buyer.",
    )

    def validate(self, attrs):
        """Проверить обязательные поля и формат идентификатора Telegram-группы."""
        if attrs["audience"] == "supplier" and not attrs.get("supplier_id"):
            raise serializers.ValidationError({"supplier_id": "Укажите компанию-поставщика."})
        if attrs["audience"] == "buyer" and attrs.get("supplier_id"):
            raise serializers.ValidationError({"supplier_id": "Для покупателя это поле не используется."})
        if attrs["audience"] == "supplier" and attrs.get("venue_ids"):
            raise serializers.ValidationError({"venue_ids": "Уведомления поставщика не ограничиваются заведениями."})
        if attrs["audience"] == "buyer" and not attrs.get("venue_ids"):
            raise serializers.ValidationError({"venue_ids": "Выберите хотя бы одно заведение."})
        attrs["chat_id"] = attrs["chat_id"].strip()
        if not attrs["chat_id"]:
            raise serializers.ValidationError({"chat_id": "Укажите ID чата."})
        if (
            attrs["channel"] == NotificationChannel.TELEGRAM
            and attrs["destination_type"] == NotificationDestinationType.GROUP
            and (not attrs["chat_id"].startswith("-") or not attrs["chat_id"][1:].isdigit())
        ):
            raise serializers.ValidationError(
                {"chat_id": "Для группы Telegram укажите числовой ID, например -10028216382."}
            )
        return attrs


class NotificationRuleUpdateSerializer(serializers.Serializer):
    """Проверить список изменений правил уведомлений."""

    rules = serializers.ListField(child=serializers.DictField(), allow_empty=True)

    def validate_rules(self, rows):
        allowed_events = set(NotificationEvent.values)
        seen = set()
        for row in rows:
            event = row.get("event")
            if event not in allowed_events:
                raise serializers.ValidationError(f"Неизвестный тип уведомления: {event}.")
            if event in seen:
                raise serializers.ValidationError(f"Настройка {event} указана несколько раз.")
            seen.add(event)
            if "is_enabled" in row and not isinstance(row["is_enabled"], bool):
                raise serializers.ValidationError(f"Для {event} поле is_enabled должно быть true или false.")
            send_time = row.get("send_time")
            if send_time is not None:
                row["send_time"] = serializers.TimeField().run_validation(send_time)
        return rows


class NotificationRuleDataSerializer(serializers.Serializer):
    """Описать сохранённое правило уведомлений."""

    event = serializers.ChoiceField(choices=NotificationEvent.choices)
    is_enabled = serializers.BooleanField()
    send_time = serializers.TimeField(format="%H:%M")


class NotificationRuleUpdateRowSerializer(serializers.Serializer):
    """Описать изменение одного правила уведомлений."""

    event = serializers.ChoiceField(choices=NotificationEvent.choices)
    is_enabled = serializers.BooleanField(required=False)
    send_time = serializers.TimeField(format="%H:%M", required=False)


class NotificationRuleUpdateRequestSerializer(serializers.Serializer):
    """Описать запрос на обновление правил чата."""

    rules = NotificationRuleUpdateRowSerializer(many=True, allow_empty=True)


class NotificationDestinationDataSerializer(serializers.Serializer):
    """Сериализовать подключённый чат и его настройки."""

    id = serializers.UUIDField()
    audience = serializers.ChoiceField(choices=NotificationAudience.choices)
    supplier_id = serializers.UUIDField(allow_null=True)
    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    destination_type = serializers.ChoiceField(choices=NotificationDestinationType.choices)
    chat_id = serializers.CharField()
    title = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()
    connected_user_id = serializers.CharField(allow_blank=True)
    connected_username = serializers.CharField(allow_blank=True)
    connected_user_name = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()
    venue_ids = serializers.ListField(child=serializers.UUIDField())
    rules = NotificationRuleDataSerializer(many=True)


class NotificationDestinationPatchRequestSerializer(serializers.Serializer):
    """Проверить изменения названия, активности и заведений чата."""

    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    is_active = serializers.BooleanField(required=False)
    venue_ids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=False)


class NotificationLinkTokenCreateRequestSerializer(serializers.Serializer):
    """Проверить параметры одноразовой ссылки подключения."""

    audience = serializers.ChoiceField(choices=NotificationAudience.choices)
    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    destination_type = serializers.ChoiceField(
        choices=NotificationDestinationType.choices, default=NotificationDestinationType.PERSONAL
    )
    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен для audience=supplier; для buyer не используется."
    )


class NotificationLinkTokenDataSerializer(serializers.Serializer):
    """Сериализовать выданную ссылку подключения."""

    token = serializers.CharField()
    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    destination_type = serializers.ChoiceField(choices=NotificationDestinationType.choices)
    expires_at = serializers.DateTimeField()


class NotificationBotConnectRequestSerializer(serializers.Serializer):
    """Проверить запрос бота на завершение подключения чата."""

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    destination_type = serializers.ChoiceField(
        choices=NotificationDestinationType.choices, default=NotificationDestinationType.PERSONAL
    )
    chat_id = serializers.CharField(max_length=255)
    token = serializers.CharField(max_length=64)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    user_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    username = serializers.CharField(required=False, allow_blank=True, max_length=255)
    display_name = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        """Проверить формат ID Telegram-группы до создания подключения."""
        if (
            attrs["channel"] == NotificationChannel.TELEGRAM
            and attrs["destination_type"] == NotificationDestinationType.GROUP
            and (not attrs["chat_id"].strip().startswith("-") or not attrs["chat_id"].strip()[1:].isdigit())
        ):
            raise serializers.ValidationError(
                {"chat_id": "Для группы Telegram укажите числовой ID, например -10028216382."}
            )
        attrs["chat_id"] = attrs["chat_id"].strip()
        return attrs


class NotificationBotStatusQuerySerializer(serializers.Serializer):
    """Проверить канал и идентификатор чата для сервисного запроса бота."""

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    chat_id = serializers.CharField(max_length=255)

    def validate_chat_id(self, value):
        """Убрать лишние пробелы и не принимать пустой идентификатор чата."""
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Укажите ID чата.")
        return value


class NotificationBotStatusResponseSerializer(serializers.Serializer):
    """Описать состояние подключения чата для бота."""

    connected = serializers.BooleanField()
    status = serializers.ChoiceField(choices=("connected", "disabled", "not_connected"))
    destination = NotificationDestinationDataSerializer(allow_null=True)
    available_events = serializers.ListField(child=serializers.CharField())


class NotificationBotTestDeliveryRequestSerializer(serializers.Serializer):
    """Проверить возможность тестовой доставки в подключённый чат."""

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    chat_id = serializers.CharField(max_length=255)

    def validate_chat_id(self, value):
        """Убрать пробелы из идентификатора чата тестовой доставки."""
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Укажите ID чата.")
        return value


class NotificationBotTestDeliveryResponseSerializer(serializers.Serializer):
    """Описать результат проверки тестовой доставки."""

    ok = serializers.BooleanField()
    message = serializers.CharField()


class NotificationHistorySerializer(serializers.Serializer):
    """Формат строки истории действий для кабинета."""

    id = serializers.UUIDField()
    created_at = serializers.DateTimeField()
    action = serializers.CharField()
    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    destination_type = serializers.ChoiceField(choices=NotificationDestinationType.choices)
    chat_id = serializers.CharField()
    actor_id = serializers.CharField(allow_blank=True)
    actor_name = serializers.CharField(allow_blank=True)
    details = serializers.DictField()
    destination_id = serializers.UUIDField(allow_null=True)


class NotificationInboxQuerySerializer(serializers.Serializer):
    """Проверить параметры фильтрации и постраничной загрузки ленты."""

    venue_id = serializers.UUIDField(required=False)
    channel = serializers.ChoiceField(choices=NotificationChannel.choices, required=False)
    unread = serializers.BooleanField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=100, default=50)
    offset = serializers.IntegerField(min_value=0, default=0)


class NotificationInboxDeliveryDataSerializer(serializers.Serializer):
    """Показать состояние доставки события в одном канале."""

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    status = serializers.ChoiceField(choices=("pending", "processing", "sent", "failed"))
    attempts = serializers.IntegerField()
    sent_at = serializers.DateTimeField(allow_null=True)
    status_message = serializers.CharField(allow_blank=True)


class NotificationInboxEventDataSerializer(serializers.Serializer):
    """Вернуть бизнес-событие и его персональное состояние прочтения."""

    id = serializers.UUIDField()
    event_type = serializers.ChoiceField(choices=NotificationInboxEventType.choices)
    title = serializers.CharField()
    created_at = serializers.DateTimeField()
    venue_id = serializers.UUIDField()
    venue_name = serializers.CharField()
    payload = serializers.JSONField()
    object_type = serializers.CharField(allow_blank=True)
    object_id = serializers.CharField(allow_blank=True)
    is_read = serializers.BooleanField()
    read_at = serializers.DateTimeField(allow_null=True)
    deliveries = NotificationInboxDeliveryDataSerializer(many=True)


class NotificationInboxListResponseSerializer(serializers.Serializer):
    """Описать страницу ленты и счётчик непрочитанных событий."""

    results = NotificationInboxEventDataSerializer(many=True)
    count = serializers.IntegerField()
    unread_count = serializers.IntegerField()


class NotificationInboxReadResponseSerializer(serializers.Serializer):
    """Подтвердить отметку события как прочитанного."""

    id = serializers.UUIDField()
    is_read = serializers.BooleanField()
    read_at = serializers.DateTimeField()


class NotificationInboxReadAllRequestSerializer(serializers.Serializer):
    """Проверить необязательное ограничение отметки по заведению."""

    venue_id = serializers.UUIDField(required=False)


class NotificationInboxReadAllResponseSerializer(serializers.Serializer):
    """Вернуть количество отмеченных событий и остаток непрочитанного."""

    marked_read = serializers.IntegerField()
    unread_count = serializers.IntegerField()


class NotificationSupplierActionRequestSerializer(serializers.Serializer):
    """Проверить ответ поставщика по заявке из бота."""

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    chat_id = serializers.CharField(max_length=255)
    action = serializers.ChoiceField(choices=NotificationSupplierActionType.choices)
    confirmed_delivery_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text=(
            "Для action=accept или action=partial укажите дату в формате ГГГГ-ММ-ДД "
            "либо передайте delivery_date_unknown=true."
        ),
    )
    delivery_date_unknown = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Если true, поставщик принимает заявку без обещания точной даты поставки.",
    )
    item_quantities = serializers.DictField(
        child=serializers.DecimalField(max_digits=14, decimal_places=4),
        required=False,
        help_text="Для action=partial: объект, где ключ — UUID позиции заявки, значение — количество.",
    )
    comment = serializers.CharField(required=False, allow_blank=True)
    responded_by = serializers.CharField(required=False, allow_blank=True, max_length=255)


class NotificationSupplierActionResponseSerializer(serializers.Serializer):
    """Сериализовать результат ответа поставщика."""

    ok = serializers.BooleanField()
    request_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=SupplierOrderRequestStatus.choices, required=False)


class NotificationDeliveryReceiptRequestSerializer(serializers.Serializer):
    """Проверить подтверждение получения поставки из бота."""

    RESULT_CHOICES = ("complete", "discrepancy")

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    chat_id = serializers.CharField(max_length=255)
    delivery_date = serializers.DateField(required=False)
    supplier_request_id = serializers.UUIDField(required=False, allow_null=True)
    result = serializers.ChoiceField(choices=RESULT_CHOICES, default="complete")
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    telegram_user_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    telegram_user_name = serializers.CharField(required=False, allow_blank=True, max_length=255)


class NotificationDeliveryReceiptResponseSerializer(serializers.Serializer):
    """Сериализовать результат подтверждения поставки."""

    ok = serializers.BooleanField()
    confirmed_count = serializers.IntegerField()
    already_confirmed = serializers.IntegerField()


class NotificationDeliveryReviewRequestSerializer(serializers.Serializer):
    """Проверить параметры открытия карточек ежедневного чек-листа."""

    channel = serializers.ChoiceField(choices=NotificationChannel.choices)
    chat_id = serializers.CharField(max_length=255)
    delivery_date = serializers.DateField(required=False)
    scope = serializers.CharField(required=False, default="all", max_length=32)


class NotificationDeliveryReviewResponseSerializer(serializers.Serializer):
    """Сериализовать заявки для последовательной проверки поставки."""

    date = serializers.DateField()
    scope = serializers.CharField()
    scope_name = serializers.CharField()
    deliveries = serializers.ListField(child=serializers.DictField())


class NotificationSupplierRequestItemSerializer(serializers.Serializer):
    """Сериализовать товар в заявке поставщика."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    quantity = serializers.CharField()
    unit = serializers.CharField(allow_blank=True)


class NotificationSupplierRequestBotDataSerializer(serializers.Serializer):
    """Сериализовать заявку для сообщения поставщику."""

    id = serializers.UUIDField()
    order_number = serializers.CharField()
    venue = serializers.CharField()
    items = NotificationSupplierRequestItemSerializer(many=True)


class NotificationTelegramMiniAppItemSerializer(serializers.Serializer):
    """Сериализовать товар для Telegram Mini App."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    quantity = serializers.CharField()
    unit = serializers.CharField(allow_blank=True)
    subdivision = serializers.CharField(allow_blank=True)
    unit_price = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)


class NotificationTelegramMiniAppRequestSerializer(serializers.Serializer):
    """Сериализовать заявку для Telegram Mini App."""

    id = serializers.UUIDField()
    request_number = serializers.CharField()
    order_number = serializers.CharField()
    venue = serializers.CharField()
    delivery_address = serializers.CharField(allow_blank=True)
    buyer_comment = serializers.CharField(allow_blank=True)
    total_amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    currency = serializers.CharField()
    minimum_order_amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    minimum_shortfall = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    items = NotificationTelegramMiniAppItemSerializer(many=True)


class NotificationTelegramMiniAppPartialRequestSerializer(serializers.Serializer):
    """Проверить частичный ответ поставщика из Telegram Mini App."""

    init_data = serializers.CharField()
    launch_token = serializers.CharField()
    action = serializers.ChoiceField(choices=[("partial", "Частичное подтверждение")])
    confirmed_delivery_date = serializers.DateField()
    item_quantities = serializers.DictField(child=serializers.DecimalField(max_digits=14, decimal_places=4))
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000)
