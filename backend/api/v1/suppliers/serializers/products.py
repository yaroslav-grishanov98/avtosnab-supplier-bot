"""Supplier products: list, card, images, autofill and bulk actions."""

from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.suppliers.package_composition import package_composition
from api.v1.suppliers.selectors import PRICE_CHANGE_CHOICES, PRODUCT_ORDERING_FIELDS
from catalog.models import Brand
from core.models import EntityStatus, Unit, UnitKind
from suppliers.models import (
    PriceStatus,
    PriceType,
    ProductAvailability,
)

from .common import PackageCompositionSerializer
from .reference import SupplierUnitSerializer


# Defined before SupplierProductListItemSerializer: its get_images is decorated with
# @extend_schema_field(SupplierProductImageSerializer(...)), and decorator arguments are
# evaluated when the class body runs. One class for the product list and the gallery
# endpoints — they return the same image rows.
class SupplierProductImageSerializer(serializers.Serializer):
    """Одно фото из галереи товара поставщика. `position` задаёт порядок (0 — главное фото),
    менять его — эндпоинтом изменения порядка фото."""

    id = serializers.UUIDField(read_only=True, help_text="ID фото.")
    image_url = serializers.URLField(read_only=True, help_text="Ссылка на фото.")
    position = serializers.IntegerField(read_only=True, help_text="Порядок в галерее. 0 — главное фото.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время загрузки.")


def _category_group_and_subgroup_names(category):
    """Группа/Подгруппа — the two ancestor levels above a product's own
    (leaf, "Вид") category, root-most first. Relies on
    select_related("category__parent__parent") already joining exactly this
    depth (api/v1/suppliers/selectors.py) — no extra queries, and doesn't
    walk further than what's prefetched. Shallower categories (fewer than 2
    ancestors) leave the corresponding name(s) None; the leaf's own name
    (Вид) is exposed separately as category_name regardless of depth.

    2026-08-15: the previous ancestors-list version kept climbing
    (`node = node.parent`) one hop past the second ancestor before the loop
    condition could stop it, reading category.parent.parent.parent — one
    level deeper than select_related() joins. On real prod data most leaf
    categories are exactly 2 levels deep, so that always fired a lazy query:
    one per product row, 50 extra queries for a 50-item page (measured via
    temporary perf logging, ~47ms round-trip each — the actual cause of
    GET /suppliers/products?limit=50 taking 2.6s+). Reading exactly
    category.parent and category.parent.parent (never a level further)
    stays inside the prefetched depth."""
    if category is None:
        return None, None
    parent = category.parent
    if parent is None:
        return None, None
    grandparent = parent.parent
    if grandparent is None:
        return parent.name, None
    return grandparent.name, parent.name


class SupplierProductListQuerySerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(required=False, help_text="Фильтр по конкретному поставщику.")
    status = serializers.ChoiceField(
        choices=EntityStatus.values, required=False, help_text="Фильтр по статусу товара: active / archived и т.п."
    )
    q = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Поиск по названию товара."
    )
    updated_since = serializers.DateTimeField(
        required=False, help_text="Только товары, изменённые после этой даты/времени."
    )
    availability = serializers.ChoiceField(
        choices=ProductAvailability.choices,
        required=False,
        help_text="Фильтр по доступности: in_stock / on_order / out_of_stock.",
    )
    brand_id = serializers.UUIDField(required=False, help_text="Фильтр по бренду.")
    category_id = serializers.UUIDField(required=False, help_text="Фильтр по категории.")
    price_min = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, help_text="Минимальная актуальная цена."
    )
    price_max = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, help_text="Максимальная актуальная цена."
    )
    price_change = serializers.ChoiceField(
        choices=PRICE_CHANGE_CHOICES,
        required=False,
        help_text=(
            "Фильтр по направлению последнего изменения цены (increased/decreased/unchanged) — сравнение "
            "текущей активной цены с последней замещённой. Товары без истории цены под фильтр не попадают."
        ),
    )
    ordering = serializers.CharField(
        required=False,
        trim_whitespace=True,
        help_text=(
            "Сортировка по любому полю ответа — имя поля напрямую (по возрастанию) или с "
            "минусом впереди (по убыванию), например 'current_price' / '-current_price'. "
            f"Допустимые поля: {', '.join(sorted(PRODUCT_ORDERING_FIELDS))}. "
            "По умолчанию — новые загрузки/изменения сначала."
        ),
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=500, default=100, help_text="Размер страницы, максимум 500."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")

    def validate_ordering(self, value):
        field = value[1:] if value.startswith("-") else value
        if field not in PRODUCT_ORDERING_FIELDS:
            raise serializers.ValidationError(
                f"Неизвестное поле сортировки '{field}'. Допустимые: {', '.join(sorted(PRODUCT_ORDERING_FIELDS))}."
            )
        return value


class SupplierProductListItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID товара поставщика.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    supplier_name = serializers.CharField(
        source="supplier.display_name", read_only=True, help_text="Название компании-поставщика."
    )
    supplier_raw_name = serializers.CharField(
        source="supplier_name", read_only=True, help_text="Исходное название товара из прайса поставщика."
    )
    supplier_normalized_name = serializers.CharField(
        source="normalized_name", read_only=True, help_text="Обработанное название товара."
    )
    product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID эталонного товара каталога, если сопоставлен."
    )
    product_name = serializers.CharField(
        source="product.canonical_name",
        read_only=True,
        allow_null=True,
        help_text="Название эталонного товара каталога, если сопоставлен.",
    )
    current_price = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text="Актуальная цена."
    )
    previous_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Цена до последнего изменения (для подсветки роста/падения).",
    )
    unit = serializers.CharField(
        source="unit.code", read_only=True, allow_null=True, help_text="Код единицы измерения."
    )
    country_of_origin = serializers.CharField(read_only=True, allow_null=True, help_text="Страна производства.")
    # 2026-08-15: раньше отдельные weight_value/weight_unit и volume_value/
    # volume_unit — объединены в одно net_value/net_unit (см. миграцию
    # suppliers/0039). Единица определяет, что значит число — вес или объём.
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, allow_null=True, help_text="Вес/объём нетто."
    )
    net_unit = serializers.CharField(
        source="net_unit.code",
        read_only=True,
        allow_null=True,
        help_text="Единица измерения веса/объёма нетто (кг, г, л, мл и т.д.).",
    )
    package = serializers.CharField(read_only=True, allow_null=True, help_text="Расфасовка/упаковка товара.")
    image_url = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text=(
            "Ссылка на изображение товара — раньше не отдавалась в списке/ответе вообще, хотя "
            "PATCH и POST /products/{id}/image её проставляют."
        ),
    )
    images = serializers.SerializerMethodField(help_text="Галерея фото, отсортирована по position (0 — главное).")
    package_composition = serializers.SerializerMethodField()
    min_order_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, allow_null=True, help_text="Минимальное количество для заказа."
    )
    delivery_multiple = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, allow_null=True, help_text="Кратность поставки."
    )
    brand = serializers.CharField(
        source="brand.canonical_name", read_only=True, allow_null=True, help_text="Название бренда."
    )
    category_id = serializers.UUIDField(
        source="category.id", read_only=True, allow_null=True, help_text="ID категории."
    )
    category_name = serializers.CharField(
        source="category.name", read_only=True, allow_null=True, help_text="Название категории (Вид в макете)."
    )
    category_group_name = serializers.SerializerMethodField(
        help_text="Название группы — верхний уровень категории (Группа в макете)."
    )
    category_subgroup_name = serializers.SerializerMethodField(
        help_text="Название подгруппы — средний уровень категории (Подгруппа в макете)."
    )
    availability = serializers.ChoiceField(
        choices=ProductAvailability.choices,
        read_only=True,
        help_text="Доступность: in_stock / on_order / out_of_stock.",
    )
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий к товару.")
    short_description = serializers.CharField(
        read_only=True, allow_null=True, help_text="Краткое описание товара для покупателя."
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус записи товара: active / archived.",
    )
    created_at = serializers.DateTimeField(
        read_only=True,
        help_text=(
            "Дата и время создания записи — в отличие от uploaded_at заполнено всегда, включая товары, "
            "добавленные вручную (POST /suppliers/products), у которых нет загрузки прайса."
        ),
    )
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата и время последнего обновления.")
    uploaded_at = serializers.DateTimeField(
        # 2026-08-14: source is the queryset's last_upload_uploaded_at annotation
        # (F("last_upload__uploaded_at")), not a nested "last_upload.uploaded_at"
        # traversal — reading .last_upload here would force Django to fetch the
        # full SupplierPriceUpload row (raw_payload/ai_payload JSONFields, up to
        # ~150KB each in prod) for every product row. select_related("last_upload")
        # was deliberately removed from supplier_products_queryset for exactly this
        # reason — see the comment there and
        # docs/wiki/logs/2026-08-14-suppliers-products-slow-query-root-cause.md.
        source="last_upload_uploaded_at",
        read_only=True,
        allow_null=True,
        help_text=(
            "Дата и время последней загрузки прайса, из которой пришёл товар — null для товаров, "
            "добавленных вручную (POST /suppliers/products), не ошибка."
        ),
    )

    def get_category_group_name(self, obj):
        return _category_group_and_subgroup_names(obj.category)[0]

    def get_category_subgroup_name(self, obj):
        return _category_group_and_subgroup_names(obj.category)[1]

    @extend_schema_field(SupplierProductImageSerializer(many=True))
    def get_images(self, obj):
        return SupplierProductImageSerializer(obj.images.all(), many=True).data

    @extend_schema_field(
        PackageCompositionSerializer(allow_null=True, help_text="Состав упаковки; null, если не заполнен.")
    )
    def get_package_composition(self, obj):
        composition = package_composition(obj)
        return PackageCompositionSerializer(composition).data if composition else None


class SupplierProductPriceHistoryItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID записи истории цены.")
    price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, help_text="Цена.")
    previous_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Цена из предыдущей (более старой) записи истории — null для самой первой записи.",
    )
    change_percent = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Изменение в процентах относительно previous_price (может быть отрицательным). Null, если менять не с чем.",
    )
    currency = serializers.CharField(read_only=True, help_text="Валюта, обычно RUB.")
    price_type = serializers.ChoiceField(choices=PriceType.choices, read_only=True, help_text="price_list / invoice.")
    status = serializers.ChoiceField(
        choices=PriceStatus.choices,
        read_only=True,
        help_text="active — текущая цена, superseded — сменена новой, cancelled — отменена (архивация).",
    )
    valid_from = serializers.DateTimeField(read_only=True, help_text="С какого момента действует эта цена.")
    valid_to = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="До какого момента действовала — null, если ещё активна."
    )
    source = serializers.SerializerMethodField(
        help_text="upload — цена пришла из загруженного прайса, manual — введена вручную через API/кабинет."
    )
    supplier_name = serializers.CharField(read_only=True, help_text="Компания-поставщик, выставившая эту цену.")
    comment = serializers.CharField(
        read_only=True, allow_null=True, help_text="Причина/комментарий к изменению цены, если указан."
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания записи истории цены.")

    def get_source(self, obj):
        return "upload" if obj.upload_item_id else "manual"


class SupplierProductCreateSerializer(serializers.Serializer):
    """2026-08-08: ручное добавление товара (раньше товары появлялись только
    через загрузку прайса). Покрывает те же 13 столбцов таблицы товаров,
    что и SupplierProductListItemSerializer — brand/category_path резолвятся
    так же, как при парсинге прайса (get_or_create по названию)."""

    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен при вызове с сервис-токеном; игнорируется при cabinet JWT."
    )
    # 2026-08-10: Витя — POST требовал supplier_raw_name, а фронт при ручном
    # добавлении товара присылает только supplier_normalized_name (у пользователя
    # одно название, а не два). Симметрично с фолбэком normalized->raw ниже:
    # оба поля необязательны, но хотя бы одно обязано быть — см. validate().
    supplier_raw_name = serializers.CharField(
        required=False, trim_whitespace=True, help_text="Название товара, как оно будет у поставщика."
    )
    supplier_normalized_name = serializers.CharField(
        required=False,
        trim_whitespace=True,
        help_text="Обработанное название, видимое покупателю. По умолчанию — то же, что supplier_raw_name.",
    )
    brand = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Название бренда — свободный текст. Резолвится через get_or_create по названию (новый "
        "бренд создаётся автоматически, как при загрузке прайса). Взаимоисключимо с brand_id — если "
        "выбран существующий бренд из автокомплита (GET /suppliers/brands), используйте brand_id.",
    )
    brand_id = serializers.PrimaryKeyRelatedField(
        queryset=Brand.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        help_text="ID уже существующего бренда, если выбран из автокомплита (GET /suppliers/brands), а не "
        "введён текстом. Если переданы оба поля, приоритет у brand_id.",
    )
    country_of_origin = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Страна производства."
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        source="unit",
        help_text="ID единицы измерения.",
    )
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Цена за единицу измерения.",
    )
    price_comment = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Причина/комментарий к этой цене (для истории изменения цены). Учитывается только вместе с price.",
    )
    delivery_multiple = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True, help_text="Кратность заказа."
    )
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True, help_text="Вес/объём нетто."
    )
    net_unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        source="net_unit",
        help_text="Единица измерения веса/объёма нетто (кг, г, л, мл и т.д.).",
    )
    min_order_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Минимальное количество для покупки.",
    )
    availability = serializers.ChoiceField(
        choices=ProductAvailability.choices,
        required=False,
        help_text="Статус наличия: in_stock / on_order / out_of_stock. По умолчанию in_stock.",
    )
    category_path = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        help_text="Путь категории от группы к виду товара, например [«Бакалея», «Крупы», «Рис»].",
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Комментарий к товару."
    )
    # TASK-111: «Состав упаковки» — «8 шт. × 2 кг = 16 кг/кор.». Заполняется целиком:
    # pack_items_count + pack_item_value + pack_item_unit_id вместе, тара — по желанию.
    pack_items_count = serializers.IntegerField(
        min_value=1,
        required=False,
        allow_null=True,
        help_text="Состав упаковки: сколько штук в упаковке (8 в «8 шт. × 2 кг»). Только вместе с pack_item_value "
        "и pack_item_unit_id; null во всех трёх очищает состав.",
    )
    pack_item_value = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        allow_null=True,
        help_text="Состав упаковки: вес/объём/количество одной штуки (2 в «8 шт. × 2 кг»).",
    )
    pack_item_unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE).exclude(kind=UnitKind.PACKAGE),
        required=False,
        allow_null=True,
        source="pack_item_unit",
        help_text="Состав упаковки: единица одной штуки (кг в «8 шт. × 2 кг») — вес, объём или штуки, не тара.",
    )
    pack_type_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE, kind=UnitKind.PACKAGE),
        required=False,
        allow_null=True,
        source="pack_type",
        help_text="Состав упаковки: тара (кор в «…= 16 кг/кор.») — единица вида package из GET /suppliers/units?kind=package. "
        "Необязательна.",
    )
    package = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Расфасовка/упаковка товара."
    )
    image_url = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Ссылка на изображение товара.",
    )

    def validate(self, attrs):
        if not attrs.get("supplier_raw_name") and not attrs.get("supplier_normalized_name"):
            raise serializers.ValidationError(
                {"supplier_raw_name": "Укажите supplier_raw_name или supplier_normalized_name."}
            )
        return attrs


class SupplierProductUpdateSerializer(serializers.Serializer):
    supplier_raw_name = serializers.CharField(
        required=False, trim_whitespace=True, help_text="Исходное название товара, как в прайсе поставщика."
    )
    supplier_normalized_name = serializers.CharField(
        required=False, trim_whitespace=True, help_text="Обработанное название товара, видимое покупателю."
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        source="unit",
        help_text="ID единицы измерения.",
    )
    # 2026-08-27: было в SupplierProductCreateSerializer, но не в PATCH — сервис
    # (update_supplier_product) уже поддерживал "brand" через ту же
    # get_or_create-по-названию логику (_resolve_brand), что и создание/импорт
    # прайса, просто сериализатор не объявлял поле, и DRF его молча отбрасывал.
    # Нужно было для мультивыбора в таблице товаров (Витя): "вдруг бренда не
    # будет такого в системе" — новый бренд создаётся автоматически, как и при
    # ручном добавлении товара.
    brand = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Название бренда — свободный текст. Резолвится через get_or_create по названию (новый "
        "бренд создаётся автоматически). Взаимоисключимо с brand_id — если выбран существующий бренд из "
        "автокомплита (GET /suppliers/brands), используйте brand_id.",
    )
    # 2026-08-28: у GET /suppliers/brands (автокомплит) не было парного поля,
    # чтобы отправить обратно выбранный id — было только это, текстовое.
    brand_id = serializers.PrimaryKeyRelatedField(
        queryset=Brand.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        help_text="ID уже существующего бренда, если выбран из автокомплита, а не введён текстом. Если "
        "переданы оба поля, приоритет у brand_id.",
    )
    country_of_origin = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Страна производства."
    )
    net_value = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True, help_text="Вес/объём нетто."
    )
    net_unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        source="net_unit",
        help_text="Единица измерения веса/объёма нетто (кг, г, л, мл и т.д.).",
    )
    # TASK-111: «Состав упаковки» — «8 шт. × 2 кг = 16 кг/кор.». Заполняется целиком:
    # pack_items_count + pack_item_value + pack_item_unit_id вместе, тара — по желанию.
    pack_items_count = serializers.IntegerField(
        min_value=1,
        required=False,
        allow_null=True,
        help_text="Состав упаковки: сколько штук в упаковке (8 в «8 шт. × 2 кг»). Только вместе с pack_item_value "
        "и pack_item_unit_id; null во всех трёх очищает состав.",
    )
    pack_item_value = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        allow_null=True,
        help_text="Состав упаковки: вес/объём/количество одной штуки (2 в «8 шт. × 2 кг»).",
    )
    pack_item_unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE).exclude(kind=UnitKind.PACKAGE),
        required=False,
        allow_null=True,
        source="pack_item_unit",
        help_text="Состав упаковки: единица одной штуки (кг в «8 шт. × 2 кг») — вес, объём или штуки, не тара.",
    )
    pack_type_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE, kind=UnitKind.PACKAGE),
        required=False,
        allow_null=True,
        source="pack_type",
        help_text="Состав упаковки: тара (кор в «…= 16 кг/кор.») — единица вида package из GET /suppliers/units?kind=package. "
        "Необязательна.",
    )
    package = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Расфасовка/упаковка товара."
    )
    min_order_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Минимальное количество для заказа.",
    )
    delivery_multiple = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Кратность поставки.",
    )
    status = serializers.ChoiceField(choices=EntityStatus.values, required=False, help_text="Новый статус товара.")
    availability = serializers.ChoiceField(
        choices=ProductAvailability.values,
        required=False,
        help_text="Доступность товара для покупателя: in_stock / on_order / out_of_stock. Не зависит от факта физического наличия — поставщик управляет этим вручную.",
    )
    price = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, help_text="Новая цена товара."
    )
    price_comment = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Причина/комментарий к этой цене (для истории изменения цены). Учитывается только вместе с price.",
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Комментарий к товару."
    )
    short_description = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        style={"base_template": "textarea.html"},
        help_text="Краткое описание товара для покупателя (§5 ТЗ). Не заполняется автоматически.",
    )
    image_url = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Ссылка на изображение товара.",
    )
    category_path = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        help_text="Путь категории от группы к виду товара, например [«Бакалея», «Крупы», «Рис»].",
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class SupplierProductAutofillRequestSerializer(serializers.Serializer):
    """TASK-87/TASK-69 задача 6: вход для рекомендации карточки товара.
    Ничего не сохраняет — поставщик ещё печатает название, товар может даже
    не существовать (тот же supplier_id-паттерн, что у SupplierProductCreateSerializer:
    обязателен при сервис-токене, игнорируется при cabinet JWT)."""

    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен при вызове с сервис-токеном; игнорируется при cabinet JWT."
    )
    name = serializers.CharField(
        trim_whitespace=True, min_length=1, help_text="Название товара, как его печатает поставщик."
    )
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        required=False,
        allow_null=True,
        source="unit",
        help_text="Уже выбранная поставщиком единица измерения, если есть — LLM не станет её заменять, "
        "предложит unit только когда это поле пусто.",
    )


class SupplierProductAutofillCandidateSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID товара в общем каталоге (catalog.Product).")
    canonical_name = serializers.CharField(read_only=True, help_text="Название товара в общем каталоге.")
    unit = SupplierUnitSerializer(
        source="base_unit", read_only=True, allow_null=True, help_text="Единица измерения товара-кандидата."
    )


class SupplierProductAutofillResponseSerializer(serializers.Serializer):
    available = serializers.BooleanField(
        read_only=True,
        help_text="False, если рекомендация недоступна (LLM выключен/не ответил/вернул невалидные данные) — "
        "остальные поля в этом случае нейтральны, это не ошибка, а признак «попробуйте заполнить вручную».",
    )
    normalized_name = serializers.CharField(
        read_only=True,
        help_text="Предложенное нормализованное название для поиска. Не заменяет исходное название товара "
        "автоматически — это подсказка, применяет её пользователь.",
    )
    suggested_unit = SupplierUnitSerializer(
        read_only=True,
        allow_null=True,
        help_text="Предложенная единица измерения. null, если в запросе unit_id уже был задан (LLM не "
        "заменяет уже выбранную единицу), либо LLM не смогла определить единицу уверенно.",
    )
    candidates = SupplierProductAutofillCandidateSerializer(
        many=True,
        read_only=True,
        help_text="Похожие товары из общего каталога, ранжированные LLM — помогают не создавать дубликат. "
        "Пустой список — либо не нашлось похожих, либо рекомендация недоступна (см. available).",
    )


class SupplierProductBulkIdsSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        help_text="ID товаров для массовой операции.",
    )


class SupplierProductArchiveAllSerializer(serializers.Serializer):
    """2026-08-04: "Мои компании" — архивировать все товары компании одной
    кнопкой, без захода в таблицу товаров. supplier_id нужен только при
    вызове с сервис-токеном/staff (нет cabinet-сессии, привязанной к одной
    компании) — при cabinet JWT игнорируется, берётся из сессии."""

    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен при вызове с сервис-токеном; игнорируется при cabinet JWT."
    )


class SupplierProductArchiveAllResponseSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    archived_count = serializers.IntegerField(read_only=True, help_text="Сколько товаров было переведено в archived.")


class SupplierProductDeleteAllResponseSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    deleted_count = serializers.IntegerField(
        read_only=True,
        help_text="Сколько товаров было переведено в archived (2026-09-23: DELETE больше не удаляет физически, "
        "название поля осталось прежним ради обратной совместимости контракта).",
    )


class SupplierProductBulkDeleteResponseSerializer(serializers.Serializer):
    deleted_count = serializers.IntegerField(
        read_only=True,
        help_text="Сколько товаров было переведено в archived (2026-09-23: DELETE больше не удаляет физически, "
        "название поля осталось прежним ради обратной совместимости контракта).",
    )
    ids = serializers.ListField(
        child=serializers.UUIDField(), read_only=True, help_text="Обработанные id (тот же список, что в запросе)."
    )


class SupplierProductRestoreAllSerializer(serializers.Serializer):
    """2026-08-04: обратная операция к archive-all — восстановить все
    архивные товары компании одной кнопкой. Тот же supplier_id-контракт,
    что у SupplierProductArchiveAllSerializer."""

    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен при вызове с сервис-токеном; игнорируется при cabinet JWT."
    )


class SupplierProductRestoreAllResponseSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    restored_count = serializers.IntegerField(read_only=True, help_text="Сколько товаров было возвращено в active.")


class SupplierProductBulkUpdateSerializer(SupplierProductUpdateSerializer):
    """Те же поля, что у одиночного PATCH /suppliers/products/{id}, плюс список id —
    применяются одинаково ко всем товарам в списке одним запросом (2026-08-02: у таблицы
    товаров во фронте не было ни одной ручки для мультивыбора)."""

    ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        help_text="ID товаров, к которым применить изменения.",
    )

    def validate(self, attrs):
        update_fields = {key: value for key, value in attrs.items() if key != "ids"}
        if not update_fields:
            raise serializers.ValidationError("At least one field besides ids must be provided.")
        return attrs


class SupplierProductBulkUpdateAllSerializer(SupplierProductUpdateSerializer):
    """2026-08-27, Vitya: bulk-actions в таблице товаров умеют выбрать "все
    товары" одной галочкой — тем же полям, что у SupplierProductBulkUpdateSerializer,
    не нужен список id, только supplier_id (тот же контракт, что у
    SupplierProductArchiveAllSerializer: обязателен только при вызове с
    сервис-токеном/staff, при cabinet JWT берётся из сессии)."""

    supplier_id = serializers.UUIDField(
        required=False, help_text="Обязателен при вызове с сервис-токеном; игнорируется при cabinet JWT."
    )

    def validate(self, attrs):
        update_fields = {key: value for key, value in attrs.items() if key != "supplier_id"}
        if not update_fields:
            raise serializers.ValidationError("At least one field besides supplier_id must be provided.")
        return attrs


class SupplierProductBulkUpdateAllResponseSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    updated_count = serializers.IntegerField(read_only=True, help_text="Сколько активных товаров было изменено.")


_ALLOWED_PRODUCT_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


_MAX_PRODUCT_IMAGE_SIZE = 5 * 1024 * 1024  # 5 МБ — тот же лимит, что и у фото товара (api/v1/views.py)


class SupplierProductImageUploadSerializer(serializers.Serializer):
    """2026-08-03: Витя — нужна ручка загрузки файла картинки для товара в
    таблице, не только текстовое поле image_url в PATCH. Единственная
    существующая загрузка картинок (`api/v1/views.py::supplier_product_image_upload`,
    легаси-прокси под shadowed-страницей `/supplier/<id>/`) пишет файл напрямую
    в файловую систему контейнера, минуя default_storage/S3 — тот же класс
    бага, что был у логотипа до 2026-07-29. Эта ручка — часть настоящего API,
    через default_storage, как и логотип.

    2026-09-13: теперь добавляет фото в галерею (SupplierProductImage), а не
    заменяет единственный image_url — по прямой просьбе пользователя."""

    image = serializers.FileField(write_only=True, help_text="Файл изображения: JPEG, PNG, WebP или GIF, до 5 МБ.")

    def validate_image(self, value):
        if value.content_type not in _ALLOWED_PRODUCT_IMAGE_CONTENT_TYPES:
            raise serializers.ValidationError("Допустимые форматы: JPEG, PNG, WebP, GIF.")
        if value.size > _MAX_PRODUCT_IMAGE_SIZE:
            raise serializers.ValidationError("Максимальный размер файла — 5 МБ.")
        return value


class SupplierProductImageReorderSerializer(serializers.Serializer):
    """§4 ТЗ: пользователь может изменить порядок фотографий, после чего
    первое становится главным — эндпоинта на это не было (галерея из
    2026-09-13 упорядочивала только по `created_at`)."""

    ordered_image_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        help_text="ID всех фото товара в новом порядке — первый элемент становится главным фото.",
    )


class SupplierCatalogSearchEventSerializer(serializers.Serializer):
    search_id = serializers.UUIDField(help_text="ID поисковой выдачи, полученный от catalog-search.")
    event_type = serializers.ChoiceField(
        choices=("open", "click", "contact_supplier", "negative_feedback", "zero_results_feedback"),
        help_text="Действие поставщика после поиска.",
    )
    product_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="ID товара, с которым связано действие, если применимо."
    )
    position = serializers.IntegerField(
        required=False, min_value=1, allow_null=True, help_text="Позиция товара в выдаче, если применимо."
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, help_text="Комментарий к событию, если есть."
    )
