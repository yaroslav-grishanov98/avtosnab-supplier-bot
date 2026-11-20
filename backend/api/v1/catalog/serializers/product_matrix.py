"""Venue product matrix (company directory) and supplier matching."""

from decimal import Decimal
from typing import Optional

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.catalog.selectors import (
    catalog_visible_supplier_products,
    resolve_supplier_product_price,
)
from api.v1.measure import NetValueField
from catalog.models import (
    SupplierMatchStatus,
    VenueProductMatrixItemSource,
    VenueProductMatrixSupplierMatch,
)
from core.models import (
    EntityStatus,
    Unit,
)
from suppliers.models import SupplierProduct

from ._shared import _OFFER_CHOICES


class VenueProductMatrixSupplierMatchStateSerializer(serializers.Serializer):
    """TASK-148: состояние массового подбора строки — колонка «Товар поставщика» без выбранного товара."""

    status = serializers.ChoiceField(
        choices=SupplierMatchStatus.choices,
        read_only=True,
        help_text="searching — «Подбираем...»; found — «Нашли N вариантов» (N = result_count); not_found — "
        "«Не нашли»; error — «Ошибка подбора». found/not_found/error кликабельны — открывают каталог.",
    )
    result_count = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Сколько товаров каталога нашёл подбор (N). null — пока searching или при error.",
    )
    searched_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Время последнего поиска; null, пока подбор идёт."
    )
    error = serializers.CharField(
        read_only=True, allow_null=True, help_text="Причина ошибки подбора (только при status=error), иначе null."
    )


class VenueProductMatrixItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID строки товарной матрицы.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения.")
    subdivision_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID подразделения этого заведения (из GET /venues/{venue_id}/subdivisions), к которому "
        "относится строка. null — подразделение не указано.",
    )
    subdivision_name = serializers.SerializerMethodField()
    requested_name = serializers.CharField(read_only=True, help_text="Название товара в справочнике заведения.")
    requested_unit_label = serializers.CharField(
        read_only=True, allow_null=True, help_text="Единица измерения в справочнике заведения (свободный текст)."
    )
    note = serializers.CharField(read_only=True, allow_null=True, help_text="Примечание к товару.")
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий к заказу.")
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID привязанного товара поставщика, если сопоставлен."
    )
    supplier_product_deleted = serializers.BooleanField(
        read_only=True,
        help_text="true, если привязанный товар поставщика был удалён (не архивирован) — строка отвязалась автоматически.",
    )
    supplier_product_name = serializers.SerializerMethodField()
    supplier_name = serializers.SerializerMethodField()
    supplier_unit = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    delivery_multiple = serializers.SerializerMethodField()
    minimum_order_amount = serializers.DecimalField(
        source="supplier_product.supplier.delivery_min_order_amount",
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Минимальная сумма заказа у поставщика привязанного товара (Supplier.delivery_min_order_amount, "
        "в валюте поставщика, обычно RUB) — та же, что minimum_order_amount группы в корзине. null — лимит не "
        "задан, либо строка без привязки к товару поставщика.",
    )
    master_product_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text=(
            "ID мастер-товара из мастер-справочника владельца, к которому привязана эта строка; "
            "null, если строка не привязана ни к одному мастер-товару."
        ),
    )
    master_product_name = serializers.SerializerMethodField()
    list_ids = serializers.SerializerMethodField()
    source = serializers.ChoiceField(
        choices=VenueProductMatrixItemSource.choices,
        read_only=True,
        help_text=(
            "Источник строки: accounting_system — импортирована из учётной системы, "
            "manual — добавлена вручную пользователем, "
            "master_catalog — создана привязкой мастер-товара к этой компании."
        ),
    )
    needs_review = serializers.BooleanField(
        read_only=True,
        help_text="Требует ли строка ручной проверки пользователем (например, после автоматического сопоставления).",
    )
    master_match_state = serializers.SerializerMethodField()
    supplier_match = serializers.SerializerMethodField()
    missing_from_latest_import = serializers.BooleanField(
        read_only=True,
        help_text="true, если товар не встретился ни в одной строке последней загрузки прайса из учётной "
        "системы (§30 ТЗ) — не удалён и не архивирован автоматически.",
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус строки: active / archived.",
    )

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название подразделения."))
    def get_subdivision_name(self, obj) -> Optional[str]:
        return obj.subdivision.name if obj.subdivision_id else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название товара поставщика."))
    def get_supplier_product_name(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.normalized_name if sp else None

    @extend_schema_field(
        serializers.CharField(
            allow_null=True,
            help_text="Название мастер-товара (canonical_name), к которому "
            "привязана эта строка — то же значение, что показывает Мастер-справочник. null вместе с "
            "master_product_id.",
        )
    )
    def get_master_product_name(self, obj) -> Optional[str]:
        return obj.master_product.canonical_name if obj.master_product_id else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название поставщика привязанного товара."))
    def get_supplier_name(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.supplier.display_name if sp else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Код единицы измерения товара поставщика."))
    def get_supplier_unit(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.unit.code if sp and sp.unit else None

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=2,
            allow_null=True,
            help_text="Действующая цена товара поставщика для этой компании — индивидуальная "
            "(§35 ТЗ, TASK-44), если такая активна, иначе базовая current_price поставщика.",
        )
    )
    def get_price(self, obj):
        sp = obj.supplier_product
        if sp is None:
            return None
        # venue_product_matrix_queryset prefetches this per venue (one extra
        # query for the whole page); other callers of this serializer (e.g.
        # venue_product_list_items_queryset) don't, so fall back to the
        # per-row resolver rather than assume the attribute exists.
        prefetched = getattr(sp, "_matrix_venue_prices", None)
        if prefetched is not None:
            return prefetched[0].price if prefetched else sp.current_price
        price, _currency, _individual_id = resolve_supplier_product_price(supplier_product=sp, venue_id=obj.venue_id)
        return price

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=4,
            allow_null=True,
            help_text="Кратность заказа (шаг количества) этого товара у поставщика. null — не задано "
            "(количество можно менять произвольно), либо строка без привязки к товару поставщика.",
        )
    )
    def get_delivery_multiple(self, obj) -> Optional[Decimal]:
        sp = obj.supplier_product
        return sp.delivery_multiple if sp else None

    @extend_schema_field(
        serializers.ChoiceField(
            choices=["matched", "needs_review", "not_found"],
            help_text=(
                "Состояние связи с Мастер-справочником: matched — связан; needs_review — требуется выбор "
                "пользователя; not_found — связи нет и проверка не назначена."
            ),
        )
    )
    def get_master_match_state(self, obj) -> str:
        if obj.master_product_id:
            return "matched"
        if obj.needs_review:
            return "needs_review"
        return "not_found"

    @extend_schema_field(
        VenueProductMatrixSupplierMatchStateSerializer(
            allow_null=True,
            help_text="TASK-148: состояние массового подбора товара поставщика. null — подбор не запускался («-») "
            "или товар поставщика уже выбран (тогда показывается supplier_product_name / supplier_name).",
        )
    )
    def get_supplier_match(self, obj):
        if obj.supplier_product_id:
            return None
        try:
            match = obj.supplier_match
        except VenueProductMatrixSupplierMatch.DoesNotExist:
            return None
        return VenueProductMatrixSupplierMatchStateSerializer(match).data

    @extend_schema_field(
        serializers.ListField(
            child=serializers.UUIDField(), help_text="ID списков («Списки»), в которые входит этот товар компании."
        )
    )
    def get_list_ids(self, obj) -> list:
        return [product_list.id for product_list in obj.lists.all() if product_list.status == EntityStatus.ACTIVE]


class VenueProductMatrixSupplierMatchFiltersSerializer(serializers.Serializer):
    """Автоматически определённые фильтры каталога — те же имена и форматы, что у query-параметров
    GET /catalog/products. В ответе только выставленные ключи."""

    category_id = serializers.ListField(
        child=serializers.UUIDField(), required=False, help_text="Фильтр «Категории» (category_id каталога)."
    )
    brand_id = serializers.ListField(
        child=serializers.UUIDField(), required=False, help_text="Фильтр «Бренды» (brand_id каталога)."
    )
    country = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Фильтр «Страна» (country каталога)."
    )
    offers = serializers.ListField(
        child=serializers.ChoiceField(choices=_OFFER_CHOICES),
        required=False,
        help_text="Фильтр меток: promo / discount / new (offers каталога).",
    )
    price_min = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, help_text="Минимальная цена (price_min каталога)."
    )
    price_max = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, help_text="Максимальная цена (price_max каталога)."
    )


class VenueProductMatrixSupplierMatchSearchSerializer(serializers.Serializer):
    q = serializers.CharField(
        read_only=True,
        help_text="Поисковая строка каталога (q). Без сохранённого подбора — название товара справочника.",
    )
    filters = VenueProductMatrixSupplierMatchFiltersSerializer(
        read_only=True,
        help_text="Фильтры каталога, выставленные автоматически; {} — фильтров нет. Поставщик, наличие и "
        "сортировка автоматически не задаются никогда.",
    )


class VenueProductMatrixSupplierMatchSourceSerializer(serializers.Serializer):
    """Блок «Подбираем товар для» в шапке каталога (ТЗ §11) — показывать только непустые значения."""

    item_id = serializers.UUIDField(read_only=True, help_text="ID строки справочника (товара компании).")
    requested_name = serializers.CharField(read_only=True, help_text="Название товара в справочнике.")
    unit_label = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Ед. изм.: requested_unit_label строки, иначе код единицы (unit); null — нет.",
    )
    master_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID мастер-товара; null — нет."
    )
    master_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Мастер-товар (canonical_name); null — нет."
    )
    note = serializers.CharField(read_only=True, allow_null=True, help_text="Примечание строки; null — нет.")


class VenueProductMatrixSupplierMatchContextSerializer(serializers.Serializer):
    source = VenueProductMatrixSupplierMatchSourceSerializer(read_only=True, help_text="Исходный товар справочника.")
    search = VenueProductMatrixSupplierMatchSearchSerializer(
        read_only=True,
        help_text="Стартовое состояние каталога: подставить q и filters в GET /catalog/products. Дальше "
        "пользователь меняет что угодно; при открытии другой строки — брать её search заново.",
    )
    match = VenueProductMatrixSupplierMatchStateSerializer(
        read_only=True,
        allow_null=True,
        help_text="Состояние подбора после пересчёта по актуальному каталогу — обновите им строку таблицы. "
        "null — подбора не было или товар уже выбран.",
    )


class VenueProductMatrixSupplierMatchSelectSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=catalog_visible_supplier_products(),
        source="supplier_product",
        help_text="ID товара из каталога (кнопка «Выбрать»). Должен быть виден в каталоге: активный, с ценой, "
        "у активного опубликованного поставщика.",
    )


class VenueProductMatrixSupplierMatchSelectErrorSerializer(serializers.Serializer):
    supplier_product_id = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text='«This field is required.» / «Must be a valid UUID.» / «Invalid pk "<id>" - object does not '
        "exist.» — товара нет или он не виден в каталоге (архив, без цены, поставщик снят с публикации).",
    )


class VenueProductMatrixSupplierMatchDetailErrorSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True, help_text="Текст ошибки.")


class VenueProductMatrixListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True, help_text="Общее количество строк товарной матрицы.")
    next = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на следующую страницу.")
    previous = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на предыдущую страницу.")
    results = VenueProductMatrixItemSerializer(many=True, read_only=True)


class VenueProductMatrixListQuerySerializer(serializers.Serializer):
    filter = serializers.ChoiceField(
        choices=["all", "needs_review", "no_master_product"],
        required=False,
        default="all",
        help_text=(
            "Все (по умолчанию) / Нужно проверить (needs_review=true) / Без Мастер-товара (master_product не привязан)."
        ),
    )
    q = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Поиск по названию товара (подстрока)."
    )
    subdivision_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр по подразделению этого заведения (из GET /venues/{venue_id}/subdivisions). Без "
        "фильтра — все строки, независимо от подразделения (включая без него).",
    )


class VenueProductMatrixMasterProductDecisionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=["link_existing", "create_new", "unlink"],
        help_text="Выбрать существующий мастер-товар, создать новый или снять текущую связь.",
    )
    master_product_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Обязателен для action=link_existing."
    )
    canonical_name = serializers.CharField(
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        max_length=500,
        help_text="Название нового мастер-товара; если не передано, используется название товара компании.",
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        source="unit",
        required=False,
        allow_null=True,
        help_text="Единица измерения нового мастер-товара. Используется только для create_new.",
    )
    net_value = NetValueField(
        max_digits=14, decimal_places=4, required=False, allow_null=True, help_text="Нетто нового мастер-товара."
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        action = attrs["action"]
        if action == "link_existing" and not attrs.get("master_product_id"):
            raise serializers.ValidationError({"master_product_id": "Обязателен для action=link_existing."})
        if action != "link_existing" and attrs.get("master_product_id"):
            raise serializers.ValidationError({"master_product_id": f"Не используется для action={action}."})
        create_fields = {"canonical_name", "unit", "net_value", "note"}
        if action != "create_new" and create_fields.intersection(attrs):
            raise serializers.ValidationError("Поля нового мастер-товара используются только для action=create_new.")
        return attrs


class VenueProductMatrixItemCreateSerializer(serializers.Serializer):
    requested_name = serializers.CharField(
        trim_whitespace=True, max_length=500, help_text="Название товара в справочнике компании."
    )
    requested_unit_label = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Единица измерения в справочнике компании."
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Примечание к товару.")
    subdivision_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="ID подразделения этого заведения (из GET /venues/{venue_id}/subdivisions), должен "
        "принадлежать этому заведению.",
    )
    master_product_decision = VenueProductMatrixMasterProductDecisionSerializer(
        required=False,
        help_text=(
            "Необязательный явный выбор из master-product-suggestions. Если передан, товар создаётся и "
            "связывается/создаёт мастер-товар атомарно без скрытого автоматического решения."
        ),
    )


class VenueProductMatrixItemUpdateSerializer(serializers.Serializer):
    requested_name = serializers.CharField(
        required=False, trim_whitespace=True, max_length=500, help_text="Новое название товара."
    )
    requested_unit_label = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Новая единица измерения."
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Новое примечание.")
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Новый комментарий к заказу."
    )
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        required=False,
        allow_null=True,
        help_text="ID товара поставщика для привязки («прикрепить товар»). null — снять привязку.",
    )
    subdivision_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="ID подразделения этого заведения (из GET /venues/{venue_id}/subdivisions), должен "
        "принадлежать этому заведению. null — снять привязку к подразделению.",
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs
