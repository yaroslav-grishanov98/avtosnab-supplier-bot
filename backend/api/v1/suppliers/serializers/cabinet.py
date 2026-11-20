"""Supplier cabinet session, settings and bot connections."""

from rest_framework import serializers

from suppliers.models import (
    SourceChannel,
    SupplierEntityType,
    SupplierFulfillmentMethod,
    UploadStatus,
)

from .common import (
    SupplierAddressSerializer,
    SupplierBankAccountSerializer,
    SupplierContactSerializer,
    SupplierScheduleRuleSerializer,
)
from .registration import SupplierDetailSerializer


class SupplierCabinetSessionRequestSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="Одноразовый токен кабинета, выданный ботом через POST cabinet/token.")


class SupplierCabinetSessionFromLoginRequestSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(
        required=False,
        help_text=(
            "Какую из своих компаний открыть в кабинете — обязательно указывать, если у пользователя "
            "несколько активных компаний (GET /suppliers/mine). Если не передан, берётся последняя "
            "созданная (для обратной совместимости, когда компания ровно одна)."
        ),
    )


class SupplierCabinetSessionSerializer(serializers.Serializer):
    access = serializers.CharField(read_only=True, help_text="JWT access-токен кабинета (заголовок Authorization).")
    refresh = serializers.CharField(read_only=True, help_text="JWT refresh-токен кабинета.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID компании-поставщика этого кабинета.")
    supplier_name = serializers.CharField(read_only=True, help_text="Название компании-поставщика.")


class SupplierCabinetUploadItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID загрузки прайса.")
    file = serializers.CharField(read_only=True, help_text="Имя загруженного файла (или «текст»).")
    date = serializers.CharField(read_only=True, help_text="Дата и время загрузки, МСК, дд.мм.гггг чч:мм.")
    status = serializers.ChoiceField(
        choices=UploadStatus.choices,
        read_only=True,
        help_text="Статус загрузки: applied / needs_review / failed и т.п.",
    )
    items_total = serializers.IntegerField(read_only=True, help_text="Всего строк в загрузке.")
    products_created = serializers.IntegerField(read_only=True, help_text="Создано новых товаров.")
    prices_created = serializers.IntegerField(read_only=True, help_text="Создано новых цен.")
    error_message = serializers.CharField(read_only=True, allow_blank=True, help_text="Текст ошибки, если была.")


class SupplierCabinetSerializer(serializers.Serializer):
    supplier = SupplierDetailSerializer(read_only=True, help_text="Карточка компании.")
    products_count = serializers.IntegerField(read_only=True, help_text="Количество активных товаров поставщика.")
    last_upload_at = serializers.CharField(
        read_only=True, allow_null=True, help_text="Дата последней успешно применённой загрузки, МСК."
    )
    uploads = SupplierCabinetUploadItemSerializer(
        many=True, read_only=True, help_text="Последние 20 загрузок прайса, от новых к старым."
    )


class SupplierCabinetUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(required=False, trim_whitespace=True, help_text="Публичное название компании.")
    legal_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Полное юридическое название компании."
    )
    short_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Сокращённое юридическое название компании."
    )
    entity_type = serializers.ChoiceField(
        choices=SupplierEntityType.choices, required=False, allow_null=True, help_text="Тип организации."
    )
    inn = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="ИНН компании.")
    kpp = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="КПП компании.")
    ogrn = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="ОГРН компании.")
    legal_address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Юридический адрес из ЕГРЮЛ/dadata."
    )
    okved = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Код ОКВЭД основного вида деятельности."
    )
    okpo = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Код ОКПО.")
    registration_date = serializers.DateField(
        required=False, allow_null=True, help_text="Дата государственной регистрации компании."
    )
    general_director = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ФИО генерального директора."
    )
    logo_url = serializers.URLField(required=False, allow_blank=True, help_text="Ссылка на логотип компании.")
    description = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Описание компании."
    )
    is_published = serializers.BooleanField(
        required=False,
        help_text=(
            "Опубликована ли компания — видна ли покупателям в каталоге. Не влияет на вход в "
            "личный кабинет (в отличие от status, который для этого и используется системой)."
        ),
    )
    delivery_terms = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Условия доставки, свободный текст."
    )
    delivery_currency = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=3,
        help_text="Код валюты для доставки курьером.",
    )
    delivery_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Минимальная сумма заказа для доставки курьером.",
    )
    delivery_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Стоимость доставки.",
    )
    delivery_zone = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Зона доставки, свободный текст."
    )
    fulfillment_method = serializers.ChoiceField(
        choices=SupplierFulfillmentMethod.choices,
        required=False,
        allow_null=True,
        help_text="Способ исполнения заказа: доставка поставщиком / самовывоз / перевозчик.",
    )
    shipping_point_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Название точки отгрузки."
    )
    shipping_point_address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Адрес отгрузки."
    )
    standard_lead_time = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Стандартный срок исполнения."
    )
    pickup_available = serializers.BooleanField(required=False, help_text="Доступен ли самовывоз у этой компании.")
    pickup_conditions = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Условия самовывоза, свободный текст."
    )
    pickup_address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Адрес пункта самовывоза."
    )
    pickup_currency = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=3,
        help_text="Код валюты для самовывоза — независим от delivery_currency.",
    )
    pickup_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Минимальная сумма заказа для самовывоза — независима от delivery_min_order_amount.",
    )
    metadata = serializers.JSONField(
        required=False, help_text="Произвольные доп. поля компании (объединяются с уже сохранёнными)."
    )
    addresses = SupplierAddressSerializer(
        many=True,
        required=False,
        help_text=(
            "Если передано — ПОЛНОСТЬЮ заменяет текущий список адресов компании (форма в макете пересохраняет "
            "весь экран целиком одной кнопкой «Сохранить»). Чтобы не трогать адреса — просто не передавайте "
            "это поле. Точечное редактирование одного адреса — через POST/PATCH/DELETE .../cabinet/addresses."
        ),
    )
    contacts = SupplierContactSerializer(
        many=True, required=False, help_text="Если передано — полностью заменяет текущий список доп. контактов."
    )
    bank_accounts = SupplierBankAccountSerializer(
        many=True, required=False, help_text="Если передано — полностью заменяет текущий список банковских счетов."
    )
    schedule_rules = SupplierScheduleRuleSerializer(
        many=True, required=False, help_text="Если передано — полностью заменяет текущие правила графика."
    )


class SupplierBotConnectionSerializer(serializers.Serializer):
    """
    Одна строка таблицы «Подключенные агенты».
    Используется в GET /suppliers/cabinet/bot-connections — возвращает список таких объектов.
    """

    id = serializers.UUIDField(
        read_only=True, help_text="ID привязки (BotUserBinding.id). Нужен для DELETE-запроса при удалении агента."
    )
    channel = serializers.ChoiceField(
        choices=(SourceChannel.TELEGRAM, SourceChannel.MAX),
        read_only=True,
        help_text="Канал подключения: telegram или max.",
    )
    chat_id = serializers.CharField(read_only=True, help_text="Chat ID агента в мессенджере.")
    external_user_id = serializers.CharField(read_only=True, allow_null=True, help_text="User ID агента в мессенджере.")
    username = serializers.CharField(
        read_only=True, allow_null=True, help_text="Username агента (если указан в профиле)."
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время, когда агент подключился к боту.")
    last_activity_at = serializers.DateTimeField(
        read_only=True, help_text="Дата и время последнего действия агента в боте."
    )
    is_owner = serializers.BooleanField(
        read_only=True,
        help_text=(
            "True — это подключение владельца компании (нельзя удалить). False — подключение агента (можно удалить)."
        ),
    )


class SupplierBotLinkGenerateSerializer(serializers.Serializer):
    """
    Входные данные для генерации ссылки-приглашения.
    Принимает канал: telegram или max.
    """

    channel = serializers.ChoiceField(
        choices=(SourceChannel.TELEGRAM, SourceChannel.MAX), help_text="Выберите канал: telegram или max."
    )


class SupplierBotLinkResponseSerializer(serializers.Serializer):
    """
    Ответ после генерации ссылки-приглашения.
    Содержит токен, готовую ссылку и срок действия.
    """

    token = serializers.CharField(help_text="Одноразовый токен (часть deep_link после ?start=).")
    deep_link = serializers.CharField(
        help_text="Готовая ссылка для отправки агенту (например, https://t.me/bot?start=TOKEN)."
    )
    expires_at = serializers.DateTimeField(help_text="Дата и время, когда ссылка перестанет работать.")
    channel = serializers.ChoiceField(
        choices=(SourceChannel.TELEGRAM, SourceChannel.MAX),
        help_text="Канал, для которого сгенерирована ссылка: telegram или max.",
    )


class SupplierBotLoginConfirmSerializer(serializers.Serializer):
    """
    2026-08-18: сайт, после того как пользователь вошёл в кабинет и выбрал
    компанию (внутри Telegram Mini App), сообщает бэкенду telegram_user_id/
    chat_id этого чата — supplier берётся из cabinet JWT, не из тела запроса
    (см. SupplierCabinetBotLoginConfirmView)."""

    platform = serializers.ChoiceField(
        choices=(SourceChannel.TELEGRAM, SourceChannel.MAX),
        default=SourceChannel.TELEGRAM,
        help_text="Канал: telegram или max.",
    )
    telegram_user_id = serializers.CharField(trim_whitespace=True, help_text="user_id пользователя в Telegram/Max.")
    chat_id = serializers.CharField(trim_whitespace=True, help_text="chat_id чата с ботом.")


class SupplierBotLoginConfirmResponseSerializer(serializers.Serializer):
    binding_id = serializers.UUIDField(help_text="ID привязки (BotUserBinding.id).")
    created = serializers.BooleanField(help_text="true — привязка создана впервые, false — уже существовала.")
    notified = serializers.BooleanField(
        help_text="Пытались ли отправить сообщение в чат (false — подавлено дедупликацией повторного вызова)."
    )
    message_sent = serializers.BooleanField(help_text="Сообщение реально доставлено через Telegram Bot API.")


class SupplierBotAccountConfirmSerializer(serializers.Serializer):
    """Подтвердить Telegram Mini App после входа в core-аккаунт."""

    init_data = serializers.CharField(
        trim_whitespace=False,
        help_text="Подписанная строка Telegram WebApp.initData; user/chat не принимаются на доверии от frontend.",
    )


class SupplierBotConnectionsListSerializer(serializers.Serializer):
    """
    Ответ для GET-запроса списка подключений.
    Содержит общую статистику и массив подключений для таблицы.
    """

    total_count = serializers.IntegerField(help_text="Общее количество подключенных агентов (включая владельца).")
    telegram_count = serializers.IntegerField(help_text="Сколько агентов подключено через Telegram.")
    max_count = serializers.IntegerField(help_text="Сколько агентов подключено через Max.")
    connections = SupplierBotConnectionSerializer(many=True, help_text="Список подключений для отображения в таблице.")
