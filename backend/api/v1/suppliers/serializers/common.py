"""Serializers shared across supplier domains."""

from rest_framework import serializers

from core.models import AuditActorType, AuditLogAction
from suppliers.models import (
    SupplierAddressType,
    SupplierContactType,
    SupplierScheduleType,
    Weekday,
)


def _validate_import_upload_size(value):
    if value.size > 10 * 1024 * 1024:
        raise serializers.ValidationError("Файл слишком большой. Максимум — 10 МБ.")
    return value


# TASK-111: общий для кабинета поставщика и каталога — данные собирает
# api.v1.suppliers.package_composition.package_composition().
class PackageCompositionSerializer(serializers.Serializer):
    """Состав упаковки товара: «8 шт. × 2 кг = 16 кг/кор.»."""

    items_count = serializers.IntegerField(read_only=True, help_text="Сколько штук в упаковке (8).")
    item_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, help_text="Вес/объём/количество одной штуки (2)."
    )
    item_unit_id = serializers.UUIDField(read_only=True, help_text="ID единицы одной штуки.")
    item_unit = serializers.CharField(read_only=True, help_text="Код единицы одной штуки (кг).")
    total_value = serializers.DecimalField(
        max_digits=18,
        decimal_places=4,
        read_only=True,
        help_text="Итого в упаковке = items_count × item_value, в item_unit (16).",
    )
    pack_type_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="ID тары, null если не указана.")
    pack_type = serializers.CharField(
        read_only=True, allow_null=True, help_text="Код тары (кор), null если не указана."
    )
    display = serializers.CharField(read_only=True, help_text="Готовая строка для показа: «8 шт. × 2 кг = 16 кг/кор.».")


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


_ALLOWED_LOGO_CONTENT_TYPES = {"image/jpeg", "image/png"}


_MAX_LOGO_SIZE = 3 * 1024 * 1024  # 3 МБ, PNG/JPG — сужено 2026-08-03 до лимита из Figma-макета (было 5 МБ + WebP/GIF)


class SupplierLogoUploadSerializer(serializers.Serializer):
    logo = serializers.FileField(write_only=True, help_text="Файл логотипа: JPEG или PNG, до 3 МБ.")

    def validate_logo(self, value):
        if value.content_type not in _ALLOWED_LOGO_CONTENT_TYPES:
            raise serializers.ValidationError("Допустимые форматы: JPEG, PNG.")
        if value.size > _MAX_LOGO_SIZE:
            raise serializers.ValidationError("Максимальный размер файла — 3 МБ.")
        return value


class SupplierImageSerializer(serializers.Serializer):
    """2026-09-13: одно фото из галереи логотипа поставщика — POST .../cabinet/logo
    теперь добавляет фото в галерею, а не заменяет единственный logo_url."""

    id = serializers.UUIDField(read_only=True, help_text="ID фото.")
    image_url = serializers.URLField(read_only=True, help_text="Ссылка на фото.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время загрузки.")


class SupplierAddressSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID адреса.")
    address_type = serializers.ChoiceField(
        choices=SupplierAddressType.choices, help_text="Тип адреса: юридический / фактический / домашний."
    )
    address = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text=(
            "Полная строка адреса одним полем (например, «Красноярский край, г. Красноярск, "
            "ул. Новосибирская, стр.4, оф. 32») — основной ввод/отображение. Поля ниже — необязательная "
            "развёрнутая детализация («Раскрыть» в макете), не замена этого поля."
        ),
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
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания адреса.")


class SupplierContactSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID контакта.")
    contact_type = serializers.ChoiceField(
        choices=SupplierContactType.choices, help_text="Тип контакта: email / телефон / мессенджер."
    )
    messenger_type = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text='Название мессенджера (например, "MAX", "Telegram", "WhatsApp") — только для contact_type=messenger.',
    )
    value = serializers.CharField(trim_whitespace=True, help_text="Значение — email/телефон/юзернейм.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания контакта.")


class SupplierBankAccountSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID банковского счёта.")
    bank_name = serializers.CharField(trim_whitespace=True, help_text="Наименование банка.")
    account_number = serializers.CharField(trim_whitespace=True, help_text="Расчётный счёт.")
    bik = serializers.CharField(trim_whitespace=True, help_text="БИК банка.")
    correspondent_account = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Корреспондентский счёт."
    )
    is_primary = serializers.BooleanField(required=False, help_text="Основной счёт компании.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания счёта.")


class SupplierScheduleRuleSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID строки графика.")
    schedule_type = serializers.ChoiceField(
        choices=SupplierScheduleType.choices,
        help_text="Тип графика: приём и доставка заказа / приём и готовность для самовывоза.",
    )
    weekday = serializers.ChoiceField(choices=Weekday.choices, help_text="День недели приёма заказа.")
    accepts_from = serializers.TimeField(help_text="Приём заказов с (время).")
    accepts_until = serializers.TimeField(help_text="Приём заказов до (время).")
    fulfillment_weekday = serializers.ChoiceField(
        choices=Weekday.choices,
        required=False,
        allow_null=True,
        help_text="День недели доставки/готовности заказа (если отличается от дня приёма).",
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Комментарий к правилу графика."
    )
    is_active = serializers.BooleanField(
        required=False, help_text="Активно ли правило — выключить, не удаляя строку. По умолчанию true."
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания правила графика.")
