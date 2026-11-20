"""Supplier registration, INN/address lookup and supplier search."""

from rest_framework import serializers

from core.models import EntityStatus
from suppliers.models import (
    SupplierEntityType,
    SupplierFulfillmentMethod,
)

from .common import (
    SupplierAddressSerializer,
    SupplierBankAccountSerializer,
    SupplierContactSerializer,
    SupplierImageSerializer,
    SupplierScheduleRuleSerializer,
)


class SupplierInnLookupQuerySerializer(serializers.Serializer):
    inn = serializers.CharField(trim_whitespace=True, help_text="ИНН для проверки, 10 или 12 цифр.")


class SupplierInnLookupResponseSerializer(serializers.Serializer):
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН компании (из запроса).")
    kpp = serializers.CharField(read_only=True, allow_null=True, help_text="КПП компании, если применим.")
    ogrn = serializers.CharField(read_only=True, allow_null=True, help_text="ОГРН компании.")
    legal_name = serializers.CharField(read_only=True, allow_null=True, help_text="Полное юридическое название.")
    short_name = serializers.CharField(read_only=True, allow_null=True, help_text="Сокращённое юридическое название.")
    entity_type = serializers.ChoiceField(
        choices=SupplierEntityType.choices,
        read_only=True,
        allow_null=True,
        help_text=(
            "Тип организации (Supplier.entity_type) — organization для ЮЛ, individual_entrepreneur для ИП, "
            "по данным реестра dadata. dadata не отличает физ.лицо без статуса ИП (нет записи в ЕГРЮЛ/ЕГРИП, "
            "которую можно найти по ИНН) — для этого случая всегда null, поле выбирается вручную."
        ),
    )
    management_name = serializers.CharField(read_only=True, allow_null=True, help_text="ФИО руководителя.")
    management_post = serializers.CharField(read_only=True, allow_null=True, help_text="Должность руководителя.")
    address = serializers.CharField(read_only=True, allow_null=True, help_text="Юридический адрес одной строкой.")
    address_type = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Тип адреса (SupplierAddressType) — всегда 'legal', т.к. dadata отдаёт адрес из реестра.",
    )
    region = serializers.CharField(
        read_only=True, allow_null=True, help_text="Регион с типом, например 'Красноярский край'."
    )
    district = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Район с типом (муниципальный либо городской — что из двух есть у dadata для этого адреса).",
    )
    city_type = serializers.CharField(
        read_only=True, allow_null=True, help_text="Тип населённого пункта: город / посёлок городского типа / и т.п."
    )
    city = serializers.CharField(read_only=True, allow_null=True, help_text="Город/населённый пункт без типа.")
    street = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Улица, дом, корпус одной строкой (без региона/города/квартиры-офиса/индекса).",
    )
    apartment_office = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text=(
            "Квартира/офис/помещение/комната одной строкой — dadata не различает эти варианты отдельными "
            "полями. Этаж dadata не отдаёт вообще (нет в ФИАС/КЛАДР)."
        ),
    )
    postal_code = serializers.CharField(read_only=True, allow_null=True, help_text="Почтовый индекс.")
    status = serializers.CharField(
        read_only=True, allow_null=True, help_text="Статус в реестре: ACTIVE / LIQUIDATED / и т.п."
    )
    okved = serializers.CharField(read_only=True, allow_null=True, help_text="Код ОКВЭД основного вида деятельности.")
    okpo = serializers.CharField(read_only=True, allow_null=True, help_text="Код ОКПО.")
    registration_date = serializers.DateField(
        read_only=True, allow_null=True, help_text="Дата государственной регистрации компании."
    )


class SupplierAddressLookupQuerySerializer(serializers.Serializer):
    query = serializers.CharField(trim_whitespace=True, help_text="Часть адреса (как в поле ввода, по мере набора).")


class SupplierAddressSuggestionSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True, allow_null=True, help_text="Полный адрес одной строкой.")
    country = serializers.CharField(read_only=True, allow_null=True, help_text="Страна.")
    region = serializers.CharField(
        read_only=True, allow_null=True, help_text="Регион с типом, например 'Красноярский край'."
    )
    district = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Район с типом (муниципальный либо городской — что из двух есть у dadata для этого адреса).",
    )
    locality = serializers.CharField(
        read_only=True, allow_null=True, help_text="Населённый пункт с типом, например 'г. Красноярск'."
    )
    street = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Улица, дом, корпус одной строкой (без региона/города/квартиры-офиса/индекса).",
    )
    apartment_office = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text=(
            "Квартира/офис/помещение/комната одной строкой — dadata не различает эти варианты отдельными "
            "полями. Этаж dadata не отдаёт вообще (нет в ФИАС/КЛАДР)."
        ),
    )
    postal_code = serializers.CharField(read_only=True, allow_null=True, help_text="Почтовый индекс.")


class SupplierSearchByInnQuerySerializer(serializers.Serializer):
    inn = serializers.CharField(trim_whitespace=True, help_text="ИНН компании для поиска, 10 или 12 цифр.")


class SupplierSearchByInnResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID найденной компании.")
    display_name = serializers.CharField(read_only=True, help_text="Публичное название компании.")
    legal_name = serializers.CharField(read_only=True, help_text="Юридическое название компании.")
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН компании.")


class SupplierListQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=500, default=100, help_text="Размер страницы, максимум 500."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")


class SupplierCreateSerializer(serializers.Serializer):
    display_name = serializers.CharField(trim_whitespace=True, help_text="Публичное название компании.")
    legal_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Полное юридическое название. По умолчанию равно display_name.",
    )
    short_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text=(
            "Сокращённое юридическое название (например, «ООО «Ромашка»» вместо «Общество с "
            "ограниченной ответственностью «Ромашка»»). Можно подтянуть по ИНН или ввести вручную."
        ),
    )
    entity_type = serializers.ChoiceField(
        choices=SupplierEntityType.choices,
        required=False,
        allow_null=True,
        help_text=(
            "Тип организации: организация (юрлицо) / ИП / физ. лицо. Справочное поле, backend не сверяет его "
            "с длиной ИНН — validate_inn() сама определяет формат (10 цифр / 12 цифр) по количеству цифр."
        ),
    )
    inn = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ИНН компании, если известен (10 или 12 цифр).",
    )
    kpp = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="КПП компании, если известен."
    )
    ogrn = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ОГРН компании, если известен."
    )
    legal_address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Юридический адрес из ЕГРЮЛ/dadata."
    )
    okved = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Код ОКВЭД основного вида деятельности. Можно подтянуть по ИНН (GET /suppliers/inn-lookup) или ввести вручную.",
    )
    okpo = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Код ОКПО. Можно подтянуть по ИНН или ввести вручную.",
    )
    registration_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Дата государственной регистрации компании. Можно подтянуть по ИНН или ввести вручную.",
    )
    general_director = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text=(
            "ФИО генерального директора. dadata по ИНН отдаёт это как management_name в ответе "
            "inn-lookup — фронт может предзаполнить это поле, но сохраняется оно только отсюда."
        ),
    )
    logo_url = serializers.URLField(required=False, allow_blank=True, help_text="Ссылка на логотип компании.")
    description = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Описание компании."
    )
    is_published = serializers.BooleanField(
        required=False,
        help_text=(
            "Опубликована ли компания — видна ли покупателям в каталоге. Не влияет на вход в "
            "личный кабинет (в отличие от status, который для этого и используется системой). "
            "По умолчанию true."
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
        help_text='Код валюты для доставки курьером, например "RUB". По умолчанию RUB.',
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
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Адрес отгрузки — откуда реально уезжает товар, не обязательно совпадает с юр. адресом.",
    )
    standard_lead_time = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text='Стандартный срок исполнения, свободный текст (например, "1-2 рабочих дня").',
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
        help_text='Код валюты для самовывоза, например "RUB". По умолчанию RUB — независим от delivery_currency.',
    )
    pickup_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Минимальная сумма заказа для самовывоза — независима от delivery_min_order_amount.",
    )
    confirm_not_duplicate = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Подтвердить создание, даже если найдено похожее по названию/адресу совпадение "
            "(possible_duplicate). Не помогает при точном совпадении ИНН/КПП/ОГРН — это блокируется всегда."
        ),
    )
    addresses = SupplierAddressSerializer(
        many=True,
        required=False,
        help_text=(
            "Адреса компании — можно передать сразу здесь одним запросом (форма в макете — один экран, "
            "одна кнопка «Сохранить»), не обязательно отдельными вызовами POST /cabinet/addresses."
        ),
    )
    contacts = SupplierContactSerializer(
        many=True, required=False, help_text="Доп. контакты компании — можно передать сразу при создании."
    )
    bank_accounts = SupplierBankAccountSerializer(
        many=True, required=False, help_text="Банковские реквизиты — можно передать сразу при создании."
    )
    schedule_rules = SupplierScheduleRuleSerializer(
        many=True, required=False, help_text="Правила графика приёма/доставки — можно передать сразу при создании."
    )


class SupplierDetailSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID компании-поставщика.")
    display_name = serializers.CharField(read_only=True, help_text="Публичное название компании.")
    legal_name = serializers.CharField(read_only=True, help_text="Полное юридическое название компании.")
    short_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Сокращённое юридическое название компании."
    )
    entity_type = serializers.CharField(read_only=True, allow_null=True, help_text="Тип организации.")
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН компании.")
    kpp = serializers.CharField(read_only=True, allow_null=True, help_text="КПП компании.")
    ogrn = serializers.CharField(read_only=True, allow_null=True, help_text="ОГРН компании.")
    legal_address = serializers.CharField(
        read_only=True, allow_null=True, help_text="Юридический адрес из ЕГРЮЛ/dadata."
    )
    okved = serializers.CharField(read_only=True, allow_null=True, help_text="Код ОКВЭД основного вида деятельности.")
    okpo = serializers.CharField(read_only=True, allow_null=True, help_text="Код ОКПО.")
    registration_date = serializers.DateField(
        read_only=True, allow_null=True, help_text="Дата государственной регистрации компании."
    )
    general_director = serializers.CharField(read_only=True, allow_null=True, help_text="ФИО генерального директора.")
    logo_url = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Ссылка на логотип компании — самое раннее (первое загруженное) фото из галереи "
        "`images` ниже, null если галерея пуста.",
    )
    description = serializers.CharField(read_only=True, allow_null=True, help_text="Описание компании.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text=(
            "Системный статус компании: active / inactive / archived. Управляется платформой "
            "(модерация, регистрация, архивация) — самим поставщиком не редактируется; для "
            "публикации/снятия с публикации в каталоге используйте is_published."
        ),
    )
    is_published = serializers.BooleanField(
        read_only=True,
        help_text=(
            "Опубликована ли компания — видна ли покупателям в каталоге. Редактируется через "
            "is_published в POST /suppliers/mine или PATCH /suppliers/cabinet/me."
        ),
    )
    delivery_terms = serializers.CharField(read_only=True, allow_null=True, help_text="Условия доставки.")
    delivery_currency = serializers.CharField(read_only=True, help_text="Код валюты для доставки курьером.")
    delivery_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Минимальная сумма заказа для доставки курьером.",
    )
    delivery_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text="Стоимость доставки."
    )
    delivery_zone = serializers.CharField(read_only=True, allow_null=True, help_text="Зона доставки.")
    fulfillment_method = serializers.CharField(read_only=True, allow_null=True, help_text="Способ исполнения заказа.")
    shipping_point_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название точки отгрузки.")
    shipping_point_address = serializers.CharField(read_only=True, allow_null=True, help_text="Адрес отгрузки.")
    standard_lead_time = serializers.CharField(
        read_only=True, allow_null=True, help_text="Стандартный срок исполнения."
    )
    pickup_available = serializers.BooleanField(read_only=True, help_text="Доступен ли самовывоз.")
    pickup_conditions = serializers.CharField(read_only=True, allow_null=True, help_text="Условия самовывоза.")
    pickup_address = serializers.CharField(read_only=True, allow_null=True, help_text="Адрес пункта самовывоза.")
    pickup_currency = serializers.CharField(read_only=True, help_text="Код валюты для самовывоза.")
    pickup_min_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Минимальная сумма заказа для самовывоза.",
    )
    images = SupplierImageSerializer(
        source="images.all",
        many=True,
        read_only=True,
        help_text="2026-09-13: галерея фото логотипа компании (несколько). См. logo_url выше.",
    )
    addresses = SupplierAddressSerializer(
        source="addresses.all", many=True, read_only=True, help_text="Адреса компании (несколько, типизированные)."
    )
    contacts = SupplierContactSerializer(
        source="contacts.all", many=True, read_only=True, help_text="Дополнительные контакты компании."
    )
    bank_accounts = SupplierBankAccountSerializer(
        source="bank_accounts.all", many=True, read_only=True, help_text="Банковские реквизиты компании."
    )
    schedule_rules = SupplierScheduleRuleSerializer(
        source="schedule_rules.all",
        many=True,
        read_only=True,
        help_text="Правила графика приёма/доставки и самовывоза.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания записи о компании.")
    my_role = serializers.SerializerMethodField(
        help_text=(
            "Роль вызывающего в этой компании: owner / manager. Заполняется только там, где есть "
            'контекст "чья это компания" (GET/POST /suppliers/mine) — null в остальных местах, где '
            "этот сериализатор используется без привязки к конкретному участнику (например, admin-листинг)."
        )
    )

    def get_my_role(self, obj):
        return getattr(obj, "my_role", None)
