"""Mirrors the "access settings" subset of api.v1.suppliers.serializers for
the venue cabinet — profile, address/contact/bank/schedule, team/invite/
join/transfer. No dadata-backed inn-lookup/address-lookup (supplier-specific
integration, not mirrored) — see api/v1/venues/services.py docstring and
docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md."""

from typing import Optional

from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.models import (
    AuditActorType,
    AuditLogAction,
    EntityStatus,
    VenueAddressType,
    VenueContactType,
    VenueEntityType,
    VenueJoinRequestStatus,
    VenueUserInviteStatus,
    VenueUserRole,
)


class VenueListQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=500, default=100, help_text="Размер страницы, максимум 500."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")


class VenueAddressSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID адреса.")
    address_type = serializers.ChoiceField(
        choices=VenueAddressType.choices, help_text="Тип адреса: юридический / фактический / домашний."
    )
    address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Полная строка адреса одним полем."
    )
    country = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Страна.")
    region = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Регион.")
    district = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Район.")
    locality = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Населённый пункт."
    )
    street = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Улица, номер дома."
    )
    postal_code = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Почтовый индекс."
    )
    apartment_office = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Квартира, офис, комната, этаж."
    )
    created_at = serializers.DateTimeField(read_only=True)


class VenueImageSerializer(serializers.Serializer):
    """2026-09-13: одно фото из галереи логотипа заведения — POST .../cabinet/logo
    теперь добавляет фото в галерею, а не заменяет единственный logo_url."""

    id = serializers.UUIDField(read_only=True, help_text="ID фото.")
    image_url = serializers.URLField(read_only=True, help_text="Ссылка на фото.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время загрузки.")


class VenueContactSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID контакта.")
    contact_type = serializers.ChoiceField(
        choices=VenueContactType.choices, help_text="Тип контакта: email / телефон / мессенджер."
    )
    messenger_type = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text='Название мессенджера (например, "MAX", "Telegram", "WhatsApp") — только для contact_type=messenger.',
    )
    value = serializers.CharField(trim_whitespace=True, help_text="Значение — email/телефон/юзернейм.")
    created_at = serializers.DateTimeField(read_only=True)


class VenueBankAccountSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID банковского счёта.")
    bank_name = serializers.CharField(trim_whitespace=True, help_text="Наименование банка.")
    account_number = serializers.CharField(trim_whitespace=True, help_text="Расчётный счёт.")
    bik = serializers.CharField(trim_whitespace=True, help_text="БИК банка.")
    correspondent_account = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Корреспондентский счёт."
    )
    is_primary = serializers.BooleanField(required=False, help_text="Основной счёт заведения.")
    created_at = serializers.DateTimeField(read_only=True)


class VenueCreateSerializer(serializers.Serializer):
    display_name = serializers.CharField(trim_whitespace=True, help_text="Публичное название заведения.")
    legal_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Полное юридическое название. По умолчанию равно display_name.",
    )
    short_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Сокращённое юридическое название."
    )
    entity_type = serializers.ChoiceField(
        choices=VenueEntityType.choices,
        required=False,
        allow_null=True,
        help_text="Тип организации: организация (юрлицо) / ИП / физ. лицо.",
    )
    inn = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ИНН заведения, если известен."
    )
    kpp = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="КПП заведения, если известен."
    )
    ogrn = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ОГРН заведения, если известен."
    )
    legal_address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Юридический адрес."
    )
    okved = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Код ОКВЭД основного вида деятельности."
    )
    okpo = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Код ОКПО.")
    registration_date = serializers.DateField(
        required=False, allow_null=True, help_text="Дата государственной регистрации заведения."
    )
    general_director = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ФИО генерального директора."
    )
    logo_url = serializers.URLField(required=False, allow_blank=True, help_text="Ссылка на логотип заведения.")
    description = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Описание заведения."
    )
    phone = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Контактный телефон."
    )
    email = serializers.EmailField(required=False, allow_blank=True, help_text="Контактный email.")
    website = serializers.URLField(required=False, allow_blank=True, help_text="Сайт заведения.")
    addresses = VenueAddressSerializer(
        many=True, required=False, help_text="Адреса заведения — можно передать сразу здесь одним запросом."
    )
    contacts = VenueContactSerializer(
        many=True, required=False, help_text="Доп. контакты заведения — можно передать сразу при создании."
    )
    bank_accounts = VenueBankAccountSerializer(
        many=True, required=False, help_text="Банковские реквизиты — можно передать сразу при создании."
    )


class VenueDetailSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    display_name = serializers.CharField(read_only=True, help_text="Публичное название заведения.")
    legal_name = serializers.CharField(read_only=True, help_text="Полное юридическое название заведения.")
    short_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Сокращённое юридическое название заведения."
    )
    entity_type = serializers.CharField(read_only=True, allow_null=True, help_text="Тип организации.")
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН заведения.")
    kpp = serializers.CharField(read_only=True, allow_null=True, help_text="КПП заведения.")
    ogrn = serializers.CharField(read_only=True, allow_null=True, help_text="ОГРН заведения.")
    legal_address = serializers.CharField(read_only=True, allow_null=True, help_text="Юридический адрес.")
    okved = serializers.CharField(read_only=True, allow_null=True, help_text="Код ОКВЭД основного вида деятельности.")
    okpo = serializers.CharField(read_only=True, allow_null=True, help_text="Код ОКПО.")
    registration_date = serializers.DateField(
        read_only=True, allow_null=True, help_text="Дата государственной регистрации заведения."
    )
    general_director = serializers.CharField(read_only=True, allow_null=True, help_text="ФИО генерального директора.")
    logo_url = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Ссылка на логотип заведения — самое раннее (первое загруженное) фото из галереи "
        "`images` ниже, null если галерея пуста.",
    )
    description = serializers.CharField(read_only=True, allow_null=True, help_text="Описание заведения.")
    phone = serializers.CharField(read_only=True, allow_null=True, help_text="Контактный телефон.")
    email = serializers.CharField(read_only=True, allow_null=True, help_text="Контактный email.")
    website = serializers.CharField(read_only=True, allow_null=True, help_text="Сайт заведения.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Системный статус заведения: active / inactive / archived. Управляется платформой.",
    )
    images = VenueImageSerializer(
        source="images.all",
        many=True,
        read_only=True,
        help_text="2026-09-13: галерея фото логотипа заведения (несколько). См. logo_url выше.",
    )
    addresses = VenueAddressSerializer(
        source="addresses.all", many=True, read_only=True, help_text="Адреса заведения (несколько, типизированные)."
    )
    contacts = VenueContactSerializer(
        source="contacts.all", many=True, read_only=True, help_text="Дополнительные контакты заведения."
    )
    bank_accounts = VenueBankAccountSerializer(
        source="bank_accounts.all", many=True, read_only=True, help_text="Банковские реквизиты заведения."
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания записи о заведении.")
    my_role = serializers.SerializerMethodField(
        help_text=(
            "Роль вызывающего в этом заведении: owner / manager. Заполняется только там, где есть "
            'контекст "чьё это заведение" (GET/POST /venues/mine) — null в остальных местах.'
        )
    )

    def get_my_role(self, obj):
        return getattr(obj, "my_role", None)


class VenueDetailErrorSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True, help_text="Текст ошибки.")


class VenueCabinetSessionFromLoginRequestSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(
        required=False,
        help_text=(
            "Какое из своих заведений открыть в кабинете — обязательно указывать, если у пользователя "
            "несколько активных заведений (GET /venues/mine). Если не передан, берётся последнее созданное."
        ),
    )


class VenueCabinetSessionSerializer(serializers.Serializer):
    access = serializers.CharField(read_only=True, help_text="JWT access-токен кабинета (заголовок Authorization).")
    refresh = serializers.CharField(read_only=True, help_text="JWT refresh-токен кабинета.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения этого кабинета.")
    venue_name = serializers.CharField(read_only=True, help_text="Название заведения.")


class VenueCabinetUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(
        required=False, trim_whitespace=True, help_text="Публичное название заведения."
    )
    legal_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Полное юридическое название заведения."
    )
    short_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Сокращённое юридическое название."
    )
    entity_type = serializers.ChoiceField(
        choices=VenueEntityType.choices, required=False, allow_null=True, help_text="Тип организации."
    )
    inn = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="ИНН заведения.")
    kpp = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="КПП заведения.")
    ogrn = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="ОГРН заведения.")
    legal_address = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Юридический адрес."
    )
    okved = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Код ОКВЭД основного вида деятельности."
    )
    okpo = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Код ОКПО.")
    registration_date = serializers.DateField(
        required=False, allow_null=True, help_text="Дата государственной регистрации заведения."
    )
    general_director = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ФИО генерального директора."
    )
    logo_url = serializers.URLField(required=False, allow_blank=True, help_text="Ссылка на логотип заведения.")
    description = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Описание заведения."
    )
    phone = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Контактный телефон."
    )
    email = serializers.EmailField(required=False, allow_blank=True, help_text="Контактный email.")
    website = serializers.URLField(required=False, allow_blank=True, help_text="Сайт заведения.")
    metadata = serializers.JSONField(
        required=False, help_text="Произвольные доп. поля заведения (объединяются с уже сохранёнными)."
    )
    addresses = VenueAddressSerializer(
        many=True,
        required=False,
        help_text=(
            "Если передано — ПОЛНОСТЬЮ заменяет текущий список адресов. Чтобы не трогать — не передавайте "
            "поле. Точечное редактирование одного адреса — через POST/PATCH/DELETE .../cabinet/addresses."
        ),
    )
    contacts = VenueContactSerializer(
        many=True, required=False, help_text="Если передано — полностью заменяет текущий список доп. контактов."
    )
    bank_accounts = VenueBankAccountSerializer(
        many=True, required=False, help_text="Если передано — полностью заменяет текущий список банковских счетов."
    )


_ALLOWED_LOGO_CONTENT_TYPES = {"image/jpeg", "image/png"}
_MAX_LOGO_SIZE = 3 * 1024 * 1024  # 3 МБ, мирроор supplier logo upload


class VenueLogoUploadSerializer(serializers.Serializer):
    logo = serializers.FileField(write_only=True, help_text="Файл логотипа: JPEG или PNG, до 3 МБ.")

    def validate_logo(self, value):
        if value.content_type not in _ALLOWED_LOGO_CONTENT_TYPES:
            raise serializers.ValidationError("Допустимые форматы: JPEG, PNG.")
        if value.size > _MAX_LOGO_SIZE:
            raise serializers.ValidationError("Максимальный размер файла — 3 МБ.")
        return value


class VenueAccountTransferInitiateSerializer(serializers.Serializer):
    target_email = serializers.EmailField(help_text="Email реального владельца, которому передаётся аккаунт.")
    initiated_by_user_id = serializers.UUIDField(
        required=False,
        help_text="ID инициатора передачи. Не нужен при вызове с JWT — берётся из токена автоматически.",
    )


class VenueAccountTransferClaimSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        min_length=8,
        trim_whitespace=False,
        write_only=True,
        help_text="Пароль для нового владельца, минимум 8 символов.",
    )
    confirm_password = serializers.CharField(
        min_length=8, trim_whitespace=False, write_only=True, help_text="Повтор пароля — должен совпадать."
    )

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Пароли не совпадают."})
        return attrs


class VenueUserSerializer(serializers.Serializer):
    membership_id = serializers.UUIDField(source="id", read_only=True)
    status = serializers.CharField(read_only=True)
    can_be_approver = serializers.SerializerMethodField()

    def get_can_be_approver(self, obj) -> bool:
        return obj.status == "active" and obj.user.status == "active"

    id = serializers.UUIDField(read_only=True, help_text="ID членства (VenueUser), используется для revoke.")
    user_id = serializers.UUIDField(source="user.id", read_only=True, help_text="ID core.User.")
    email = serializers.EmailField(source="user.email", read_only=True, help_text="Email участника.")
    full_name = serializers.CharField(source="user.full_name", read_only=True, help_text="ФИО участника.")
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices,
        read_only=True,
        help_text=(
            "owner / manager — права одинаковые, owner нужен только для правила «у заведения ровно "
            "один активный owner»."
        ),
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Когда получил доступ.")


class VenueUserRoleChangeSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=VenueUserRole.choices, help_text="Новая роль участника.")


class VenueUserInviteSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID приглашения.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    target_email = serializers.EmailField(read_only=True, help_text="Email, на который отправлено приглашение.")
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices,
        read_only=True,
        help_text="Роль, которую получит приглашённый после принятия.",
    )
    status = serializers.ChoiceField(
        choices=VenueUserInviteStatus.choices,
        read_only=True,
        help_text=(
            "pending — ожидает принятия. claimed — уже принято. cancelled — отменено (на этот email "
            "создано более новое приглашение). expired — истёк срок."
        ),
    )
    invited_by_email = serializers.SerializerMethodField(help_text="Email пригласившего. null, если не известен.")
    expires_at = serializers.DateTimeField(read_only=True, help_text="Срок действия приглашения.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания приглашения.")

    @extend_schema_field(serializers.EmailField(allow_null=True))
    def get_invited_by_email(self, obj) -> Optional[str]:
        return obj.invited_by.email if obj.invited_by else None


class VenueUserInviteCreateSerializer(serializers.Serializer):
    target_email = serializers.EmailField(help_text="Email сотрудника, которого приглашаем в кабинет заведения.")
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices,
        required=False,
        default=VenueUserRole.MANAGER,
        help_text=(
            "Роль, которую получит приглашённый после принятия. owner — заведение может иметь только "
            "одного активного владельца, поэтому принятие такого приглашения деактивирует текущего."
        ),
    )


class VenueInviteLinkSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID ссылки-приглашения.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    token = serializers.CharField(read_only=True, help_text="Токен ссылки.")
    url = serializers.SerializerMethodField(help_text="Полный URL ссылки-приглашения для отправки.")
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices,
        read_only=True,
        help_text="Роль, которую получит присоединившийся по ссылке.",
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE],
        read_only=True,
        help_text="active — ссылка рабочая, inactive — отключена.",
    )
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True, help_text="null — без срока действия.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания ссылки.")

    @extend_schema_field(serializers.URLField())
    def get_url(self, obj) -> str:
        return f"{settings.VENUE_INVITE_LINK_BASE_URL.rstrip('/')}/{obj.token}"


class VenueUserInvitePreviewResponseSerializer(serializers.Serializer):
    """GET /venues/invites/{token} — read-only превью до accept, не меняет статус инвайта."""

    target_email = serializers.EmailField(read_only=True, help_text="Email, на который отправлено приглашение.")
    venue_name = serializers.CharField(read_only=True, help_text="Отображаемое имя заведения.")
    venue_inn = serializers.CharField(
        read_only=True, allow_null=True, help_text="ИНН заведения. null, если не заполнен."
    )
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices,
        read_only=True,
        help_text=(
            "Роль, которую получит приглашённый после принятия. owner — заведение может иметь только "
            "одного активного владельца, принятие такого приглашения деактивирует текущего. manager — "
            "обычный сотрудник."
        ),
    )
    status = serializers.ChoiceField(
        choices=VenueUserInviteStatus.choices,
        read_only=True,
        help_text=(
            "pending — ожидает принятия. claimed — уже принято, повторный POST .../accept вернёт 400. "
            "cancelled — отменено (на этот email создано более новое приглашение). expired — истёк срок "
            "(проставляется лениво, при попытке accept после expires_at)."
        ),
    )
    is_expired = serializers.BooleanField(
        read_only=True,
        help_text=(
            "true, если expires_at уже в прошлом. Не то же самое, что status=expired — тот "
            "проставляется в БД только при попытке accept, is_expired здесь вычисляется на лету."
        ),
    )
    account_exists = serializers.BooleanField(
        read_only=True, help_text="true, если на target_email уже есть активный аккаунт с role=venue_user."
    )


class VenueUserInviteAcceptResponseSerializer(serializers.Serializer):
    """Общий ответ POST /venues/invites/{token}/accept и POST /venues/invite-link/{token}/join."""

    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, доступ к которому получен.")
    user_id = serializers.UUIDField(read_only=True, help_text="ID core.User, принявшего приглашение.")
    email = serializers.EmailField(read_only=True, help_text="Email принявшего аккаунта.")
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices, read_only=True, help_text="Роль, полученная в заведении."
    )


class VenueInviteLinkPreviewResponseSerializer(serializers.Serializer):
    """GET /venues/invite-link/{token} — read-only превью постоянной ссылки-приглашения."""

    venue_name = serializers.CharField(read_only=True, help_text="Отображаемое имя заведения.")
    venue_inn = serializers.CharField(
        read_only=True, allow_null=True, help_text="ИНН заведения. null, если не заполнен."
    )
    role = serializers.ChoiceField(
        choices=VenueUserRole.choices,
        read_only=True,
        help_text="Роль, которую получит присоединившийся по ссылке. owner / manager — см. описание в create-инвайте.",
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE],
        read_only=True,
        help_text="active — ссылка рабочая. inactive — отключена владельцем, join вернёт 400.",
    )
    is_expired = serializers.BooleanField(
        read_only=True,
        help_text="true, если задан expires_at и он уже в прошлом. Бессрочная ссылка (expires_at=null) — всегда false.",
    )


class VenueInnLookupQuerySerializer(serializers.Serializer):
    inn = serializers.CharField(trim_whitespace=True, help_text="ИНН для проверки, 10 или 12 цифр.")


class VenueInnLookupResponseSerializer(serializers.Serializer):
    """Mirrors suppliers.SupplierInnLookupResponseSerializer field-for-field
    — same dadata payload shape, only entity_type points at VenueEntityType
    instead of SupplierEntityType (2026-08-19, Vitya asked for the venue
    side of the same autofill suppliers already have)."""

    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН компании (из запроса).")
    kpp = serializers.CharField(read_only=True, allow_null=True, help_text="КПП компании, если применим.")
    ogrn = serializers.CharField(read_only=True, allow_null=True, help_text="ОГРН компании.")
    legal_name = serializers.CharField(read_only=True, allow_null=True, help_text="Полное юридическое название.")
    short_name = serializers.CharField(read_only=True, allow_null=True, help_text="Сокращённое юридическое название.")
    entity_type = serializers.ChoiceField(
        choices=VenueEntityType.choices,
        read_only=True,
        allow_null=True,
        help_text=(
            "Тип организации (Venue.entity_type) — organization для ЮЛ, individual_entrepreneur для ИП, "
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
        help_text="Тип адреса (VenueAddressType) — всегда 'legal', т.к. dadata отдаёт адрес из реестра.",
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


class VenueAddressLookupQuerySerializer(serializers.Serializer):
    query = serializers.CharField(trim_whitespace=True, help_text="Часть адреса (как в поле ввода, по мере набора).")


class VenueAddressSuggestionSerializer(serializers.Serializer):
    """Mirrors suppliers.SupplierAddressSuggestionSerializer — no
    role-specific fields at all here, duplicated only to keep venues
    self-contained (this app never imports from api.v1.suppliers, see
    docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md)."""

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


class VenueInviteLinkUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=VenueUserRole.choices, required=False, help_text="Роль присоединившегося.")
    expires_at = serializers.DateTimeField(
        required=False, allow_null=True, help_text="null — снять срок действия (ссылка бессрочна)."
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE],
        required=False,
        help_text="active — включить, inactive — выключить ссылку.",
    )


class VenueSearchByInnQuerySerializer(serializers.Serializer):
    inn = serializers.CharField(trim_whitespace=True, help_text="ИНН заведения для поиска, 10 или 12 цифр.")


class VenueSearchByInnResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID найденного заведения.")
    display_name = serializers.CharField(read_only=True, help_text="Публичное название заведения.")
    legal_name = serializers.CharField(read_only=True, help_text="Юридическое название заведения.")
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН заведения.")


_ALLOWED_JOIN_REQUEST_DOCUMENT_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
_MAX_JOIN_REQUEST_DOCUMENT_SIZE = 20 * 1024 * 1024  # 20 МБ, мирроор supplier join-request
_MAX_JOIN_REQUEST_DOCUMENTS = 5


class VenueJoinRequestCreateSerializer(serializers.Serializer):
    message = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Описание ситуации: кто вы в заведении, почему запрашиваете доступ.",
    )
    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Контактный телефон для этой заявки (может отличаться от телефона на аккаунте).",
    )
    email = serializers.EmailField(
        required=False,
        allow_blank=True,
        help_text="Контактный email для этой заявки (может отличаться от email на аккаунте).",
    )
    documents = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
        help_text=(
            "Подтверждающие документы (скан доверенности, справка и т.п.) — PDF, JPEG, PNG или WebP, "
            f"до {_MAX_JOIN_REQUEST_DOCUMENTS} файлов, до 20 МБ каждый. Multipart-запрос."
        ),
    )

    def validate_documents(self, value):
        if len(value) > _MAX_JOIN_REQUEST_DOCUMENTS:
            raise serializers.ValidationError(f"Не больше {_MAX_JOIN_REQUEST_DOCUMENTS} файлов за раз.")
        for document_file in value:
            if document_file.content_type not in _ALLOWED_JOIN_REQUEST_DOCUMENT_CONTENT_TYPES:
                raise serializers.ValidationError("Допустимые форматы документов: PDF, JPEG, PNG, WebP.")
            if document_file.size > _MAX_JOIN_REQUEST_DOCUMENT_SIZE:
                raise serializers.ValidationError("Максимальный размер файла — 20 МБ.")
        return value


class VenueJoinRequestSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID заявки.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, к которому запрашивается доступ.")
    venue_display_name = serializers.CharField(
        read_only=True, source="venue.display_name", help_text="Название заведения."
    )
    requested_by_id = serializers.UUIDField(read_only=True, help_text="ID пользователя, подавшего заявку.")
    requested_by_full_name = serializers.CharField(
        read_only=True, source="requested_by.full_name", help_text="ФИО подавшего заявку."
    )
    requested_by_email = serializers.EmailField(
        read_only=True, source="requested_by.email", help_text="Email аккаунта подавшего заявку."
    )
    message = serializers.CharField(
        read_only=True, allow_null=True, help_text="Описание ситуации, указанное при подаче заявки."
    )
    phone = serializers.CharField(read_only=True, allow_null=True, help_text="Контактный телефон для этой заявки.")
    email = serializers.EmailField(read_only=True, allow_null=True, help_text="Контактный email для этой заявки.")
    documents = serializers.ListField(
        child=serializers.CharField(), read_only=True, help_text="Ссылки на подтверждающие документы."
    )
    status = serializers.ChoiceField(
        choices=VenueJoinRequestStatus.choices,
        read_only=True,
        help_text="pending — на рассмотрении, approved — одобрена, rejected — отклонена.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время подачи заявки.")


class AuditLogEntrySerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=AuditLogAction.choices,
        read_only=True,
        help_text="Тип действия: deleted (удалено) / archived (архивировано).",
    )
    summary = serializers.CharField(read_only=True, help_text="Название сущности на момент действия.")
    actor_type = serializers.ChoiceField(
        choices=AuditActorType.choices,
        read_only=True,
        help_text="Кто выполнил действие: core_user (пользователь), supplier (кабинет поставщика), "
        "venue (кабинет заведения), service (сервисный токен).",
    )
    actor_label = serializers.CharField(read_only=True, help_text="Email или название, выполнившего действие.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время действия.")
