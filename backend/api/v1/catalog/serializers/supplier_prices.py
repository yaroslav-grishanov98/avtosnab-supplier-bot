"""Supplier product prices per venue and individual prices."""

from decimal import Decimal
from typing import Optional

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.catalog.selectors import (
    supplier_product_venue_price_confirmed_qty,
)
from catalog.models import (
    SupplierProductVenuePriceSourceType,
    SupplierProductVenuePriceStatus,
)

from .common import UUIDListField


class SupplierProductVenuePriceSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID цены.")
    supplier_product_id = serializers.UUIDField(read_only=True, help_text="ID товара поставщика.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID компании (заведения), к которой относится цена.")
    venue_name = serializers.SerializerMethodField()
    group_id = serializers.UUIDField(
        read_only=True,
        help_text="Связывает строки, созданные одной операцией назначения цены нескольким заведениям "
        "(ТЗ §12/§46) — на фронте такие строки можно показать одной строкой в таблице.",
    )
    price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, help_text="Цена.")
    currency = serializers.CharField(read_only=True, help_text="Валюта (ISO 4217, например RUB).")
    valid_from = serializers.DateTimeField(read_only=True, help_text="Начало действия цены.")
    valid_to = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Окончание действия. null — бессрочно (пока не архивирована/заменена).",
    )
    price_state = serializers.SerializerMethodField()
    source_type = serializers.ChoiceField(
        choices=SupplierProductVenuePriceSourceType.choices,
        read_only=True,
        help_text="manual — назначена вручную. tender — по результатам распределения тендера (§30), предложение — "
        "source_tender_offer_id.",
    )
    source_reference = serializers.CharField(
        read_only=True, allow_null=True, help_text="Свободный текст источника, например «Тендер №42»."
    )
    source_tender_offer_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID предложения тендера, если цена назначена из тендера — структурная замена/дополнение "
        "к source_reference, делает «Тендер №...» кликабельным (§23 ТЗ).",
    )
    source_tender_id = serializers.SerializerMethodField()
    source_tender_item_id = serializers.SerializerMethodField()
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий.")
    created_by_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID пользователя, создавшего запись. null, если пользователь удалён."
    )
    status = serializers.ChoiceField(
        choices=SupplierProductVenuePriceStatus.choices,
        read_only=True,
        help_text="active — действующая цена. archived — заменена более новой ценой или архивирована вручную.",
    )
    confirmed_qty = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название компании (заведения)."))
    def get_venue_name(self, obj) -> Optional[str]:
        return obj.venue.display_name if obj.venue_id else None

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="ID тендера, если цена из тендера."))
    def get_source_tender_id(self, obj) -> Optional[str]:
        return obj.source_tender_offer.tender_item.tender_id if obj.source_tender_offer_id else None

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="ID позиции тендера, если цена из тендера."))
    def get_source_tender_item_id(self, obj) -> Optional[str]:
        return obj.source_tender_offer.tender_item_id if obj.source_tender_offer_id else None

    @extend_schema_field(
        serializers.ChoiceField(
            choices=["future", "active", "ended"],
            help_text="Вычисляемое состояние (не хранится в БД, ТЗ §9): future — valid_from ещё не наступил, "
            "active — status=active и сегодня входит в период, ended — valid_to уже в прошлом либо "
            "status=archived.",
        )
    )
    def get_price_state(self, obj) -> str:
        if obj.status == SupplierProductVenuePriceStatus.ARCHIVED:
            return "ended"
        now = timezone.now()
        if obj.valid_from > now:
            return "future"
        if obj.valid_to is not None and obj.valid_to <= now:
            return "ended"
        return "active"

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=4,
            help_text="ТЗ §42-45: суммарное количество, подтверждённое поставщиком по заявкам, где была "
            "применена именно эта версия цены (не восстанавливается по датам/сумме — только по прямой "
            "ссылке на эту запись).",
        )
    )
    def get_confirmed_qty(self, obj) -> Decimal:
        return supplier_product_venue_price_confirmed_qty(obj.id)


class SupplierProductVenuePriceOrderHistoryQuerySerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(required=False, help_text="Фильтр по заведению.")
    date_from = serializers.DateTimeField(required=False, help_text="Начало периода (включительно).")
    date_to = serializers.DateTimeField(required=False, help_text="Конец периода (включительно).")
    order_number = serializers.CharField(
        required=False, allow_blank=True, help_text="Поиск по номеру заявки (подстрока)."
    )
    limit = serializers.IntegerField(
        required=False, default=50, min_value=1, max_value=200, help_text="Размер страницы, максимум 200."
    )
    offset = serializers.IntegerField(required=False, default=0, min_value=0, help_text="Смещение для пагинации.")


class SupplierProductVenuePriceOrderHistoryItemSerializer(serializers.Serializer):
    """§26 ТЗ: одна строка таблицы «История заказов» = одна позиция (OrderItem)
    из заявки, снапшотнутая на эту конкретную индивидуальную цену — колонки
    Дата/Заявка/Заведение/Заказано/Подтверждено/Сумма из примера макета."""

    order_id = serializers.UUIDField(read_only=True, help_text="ID заявки.")
    order_number = serializers.CharField(source="order.order_number", read_only=True, help_text="Номер заявки.")
    order_date = serializers.DateTimeField(source="order.created_at", read_only=True, help_text="Дата заявки.")
    venue_id = serializers.UUIDField(
        source="order.venue_id", read_only=True, help_text="ID заведения, оформившего заявку."
    )
    venue_name = serializers.CharField(
        source="order.venue.display_name", read_only=True, help_text="Название заведения."
    )
    requested_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, help_text="Заказанное количество."
    )
    confirmed_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Подтверждённое поставщиком количество. null — заявка ещё не обработана/отменена.",
    )
    amount = serializers.SerializerMethodField()

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=2,
            allow_null=True,
            help_text="Сумма по этой строке = applied_price * confirmed_qty (снапшот цены на момент "
            "оформления заявки, §38-39). null, если ещё не подтверждено количество или не "
            "зафиксирована применённая цена.",
        )
    )
    def get_amount(self, obj) -> Optional[str]:
        if obj.confirmed_qty is None or obj.applied_price is None:
            return None
        # SerializerMethodField bypasses DecimalField.to_representation() (the thing that
        # normally coerces decimals to fixed-precision strings everywhere else in this
        # project) — a raw Decimal returned here falls through to DRF's JSON encoder, which
        # renders it as a bare float with full precision instead of the documented
        # "type: string, format: decimal" 2-place string. Quantize and stringify explicitly.
        amount = obj.applied_price * obj.confirmed_qty
        return str(amount.quantize(Decimal("0.01")))


class SupplierProductVenuePriceConflictSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, для которого уже есть активная цена.")
    venue_name = serializers.SerializerMethodField()
    price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, help_text="Текущая цена.")
    valid_from = serializers.DateTimeField(read_only=True, help_text="Начало действия текущей цены.")
    valid_to = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Окончание действия. null — бессрочно."
    )

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название заведения."))
    def get_venue_name(self, obj) -> Optional[str]:
        return obj.venue.display_name


class SupplierProductIndividualPriceCreateSerializer(serializers.Serializer):
    venue_ids = UUIDListField(help_text="ID компаний (venue), к которым применяется цена — минимум одна.")
    price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, help_text="Цена.")
    currency = serializers.ChoiceField(
        choices=[("RUB", "RUB")],
        required=False,
        default="RUB",
        help_text=(
            "Валюта (ISO 4217). Сейчас поддерживается только RUB — весь каталог ведётся в рублях; "
            "поле оставлено для будущих валют. По умолчанию RUB."
        ),
    )
    valid_from = serializers.DateTimeField(required=False, help_text="Начало действия. Не указано — текущее время.")
    valid_to = serializers.DateTimeField(
        required=False, allow_null=True, help_text="Окончание действия. Не указано/null — бессрочно."
    )
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Комментарий.")
    confirm_replace = serializers.BooleanField(
        required=False,
        default=False,
        help_text="ТЗ §18-20: если для какого-то из venue_ids уже есть активная пересекающаяся по периоду "
        "цена, первый вызов вернёт 409 со списком конфликтов и ничего не запишет. Повторить с "
        "confirm_replace=true, чтобы заменить конфликтующие цены и создать остальные.",
    )

    def validate_venue_ids(self, value):
        if not value:
            raise serializers.ValidationError("Укажите хотя бы одну компанию.")
        return value


class SupplierProductVenuePriceUpdateSerializer(serializers.Serializer):
    price = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, help_text="Новая цена."
    )
    valid_from = serializers.DateTimeField(
        required=False, help_text="Новое начало действия (должно остаться в будущем)."
    )
    valid_to = serializers.DateTimeField(required=False, allow_null=True, help_text="Новое окончание действия.")
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Комментарий."
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class SupplierProductVenuePriceQuerySerializer(serializers.Serializer):
    supplier_product_id = serializers.UUIDField(help_text="ID товара поставщика.")
    venue_id = serializers.UUIDField(help_text="ID компании (venue).")


class SupplierProductIndividualPriceQuerySerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(
        required=False, help_text="Ограничить список одной компанией. Не указано — все компании этого товара."
    )


class SupplierProductVenuePriceListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True, help_text="Общее количество записей.")
    next = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на следующую страницу.")
    previous = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на предыдущую страницу.")
    results = SupplierProductVenuePriceSerializer(many=True, read_only=True)
