"""Serializers and helpers shared across catalog domains."""

from typing import Optional

from rest_framework import serializers

from api.v1.catalog.category_placeholders import category_placeholder_path, placeholder_url
from core.models import (
    AuditActorType,
    AuditLogAction,
)


def _validate_import_upload_size(value):
    if value.size > 10 * 1024 * 1024:
        raise serializers.ValidationError("Файл слишком большой. Максимум — 10 МБ.")
    return value


class UUIDListField(serializers.ListField):
    child = serializers.UUIDField(help_text="ID элемента.")

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        if not kwargs["required"]:
            kwargs.setdefault("default", list)
        super().__init__(**kwargs)


def supplier_product_display_image_url(supplier_product, request=None) -> Optional[str]:
    """Картинка товара для позиций корзины/заказа — как image_url в каталоге (TASK-125): своё фото товара,
    иначе картинка-заглушка его вида товара; null — позиция без привязки к товару поставщика."""
    if supplier_product is None:
        return None
    if supplier_product.image_url:
        return supplier_product.image_url
    path = category_placeholder_path(supplier_product.category)
    return placeholder_url(path, request) if path else None


def validate_supplier_product_qty(*, supplier_product, requested_qty):
    """2026-09-29, пользователь (баг-репорт с фронта, скриншоты «Кратность заказа 16 шт» —
    клик «+» отдавал 17): бэкенд не проверял requested_qty против min_order_qty/
    delivery_multiple товара поставщика вообще — любое количество проходило. Само значение
    "шага" кнопок +/- считает фронтенд (отдельный, не отслеживаемый в этом репозитории
    проект) — здесь только серверная защита от невалидного количества независимо от того,
    что прислал клиент. Только для позиций с реальным supplier_product — у товара вне
    системы (off_system_supplier) и у свободного текста (requested_name без привязки) нет
    ни того, ни другого поля."""
    if supplier_product is None:
        return
    min_order_qty = supplier_product.min_order_qty
    if min_order_qty is not None and requested_qty < min_order_qty:
        raise serializers.ValidationError(
            {"requested_qty": [f"Минимальное количество для этого товара — {min_order_qty}."]}
        )
    delivery_multiple = supplier_product.delivery_multiple
    if delivery_multiple and requested_qty % delivery_multiple != 0:
        raise serializers.ValidationError(
            {"requested_qty": [f"Количество должно быть кратно {delivery_multiple} для этого товара."]}
        )


class NotFoundDetailSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True, help_text="Сообщение 404, например «Not found.».")


class ProductSuggestionQuerySerializer(serializers.Serializer):
    name = serializers.CharField(
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        max_length=500,
        help_text=(
            "Название добавляемого товара — значение поля «Название товара в УС» "
            "(requested_name) из формы. Без q используется для автоматических рекомендаций "
            "(recommended)."
        ),
    )
    q = serializers.CharField(
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        max_length=500,
        help_text="Ручной поисковый запрос. Если передан, выдача строится по нему (search_results).",
    )
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=50,
        default=10,
        help_text="Размер страницы. Ограничивает и search_results/results, и топ recommended.",
    )
    offset = serializers.IntegerField(
        required=False,
        min_value=0,
        default=0,
        help_text=(
            "Смещение для постраничного доступа. Применяется только к результатам ручного "
            "поиска (search_results на master-product-suggestions, results на "
            "supplier-product-suggestions) — recommended на master-product-suggestions всегда "
            "топ-N по score и offset не учитывает."
        ),
    )

    def validate(self, attrs):
        if not attrs.get("name") and not attrs.get("q"):
            raise serializers.ValidationError("Передайте name для рекомендаций или q для ручного поиска.")
        return attrs


class ImportDecisionsErrorSerializer(serializers.Serializer):
    decisions = serializers.ListField(
        child=serializers.JSONField(),
        required=False,
        help_text="Ошибки решений — всегда список. Элемент-строка — ошибка проверки всей пачки перед записью "
        "(цель не найдена, чужая или архивная; единица измерения неактивна; несколько строк заменяют одну "
        "карточку; пустой список decisions; превышен лимит решений за запрос). Элемент-объект — полевые "
        "ошибки решения с тем же индексом в decisions "
        '(например {"unit_id": ["Обязателен при action=replace."]}); пустой объект — у решения ошибок нет.',
    )


class VenueSearchQuerySerializer(serializers.Serializer):
    """2026-09-23, по просьбе пользователя: поиск заведений по ИНН и/или
    названию для кабинета поставщика (выбор venue_ids перед назначением
    индивидуальной цены). Один общий `q` матчит и название, и ИНН
    подстрокой — см. venue_search_queryset."""

    q = serializers.CharField(
        min_length=2,
        trim_whitespace=True,
        help_text="Поиск по названию (публичному или юридическому) либо ИНН заведения — подстрока, минимум 2 символа.",
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=100, default=20, help_text="Размер страницы, максимум 100."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")


class VenueSearchResultSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID заведения — то, что передаётся в venue_ids.")
    display_name = serializers.CharField(read_only=True, help_text="Публичное название заведения.")
    legal_name = serializers.CharField(read_only=True, allow_null=True, help_text="Юридическое название заведения.")
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН заведения.")


class AuditLogEntrySerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=AuditLogAction.choices,
        read_only=True,
        help_text="Тип действия: deleted / archived — большинство сущностей; replaced — название и единица "
        "измерения мастер-товара / товара компании заменены при импорте (summary: «старое» (ед.) → «новое» "
        "(ед.)); для заявки поставщику (raw ТЗ §29) — submitted / viewed / confirmed / partially_confirmed / "
        "declined / cancelled_by_venue / completed.",
    )
    summary = serializers.CharField(
        read_only=True,
        help_text="Для action=replaced — «старое» (ед.) → «новое» (ед.); для остальных действий — название "
        "сущности на момент действия.",
    )
    actor_type = serializers.ChoiceField(
        choices=AuditActorType.choices,
        read_only=True,
        help_text="Кто выполнил действие: core_user (пользователь), supplier (кабинет поставщика), "
        "venue (кабинет заведения), service (сервисный токен).",
    )
    actor_label = serializers.CharField(read_only=True, help_text="Email или название, выполнившего действие.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время действия.")
