"""Public catalog: products, brands, categories, countries, suppliers."""

from typing import Optional

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.catalog.category_placeholders import category_placeholder_path, placeholder_url
from api.v1.suppliers.package_composition import package_composition
from api.v1.suppliers.serializers import (
    PackageCompositionSerializer,
    SupplierAddressSerializer,
    SupplierContactSerializer,
    SupplierScheduleRuleSerializer,
)
from core.models import (
    EntityStatus,
)
from suppliers.models import ProductAvailability

from ._shared import _OFFER_CHOICES
from .common import UUIDListField

_SORT_CHOICES = ["default", "new", "with_offers", "in_stock_first", "price_asc", "price_desc", "name_asc", "name_desc"]


class CatalogProductQuerySerializer(serializers.Serializer):
    q = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Полнотекстовый поиск по названию товара."
    )
    supplier_id = UUIDListField(help_text="Фильтр по одному или нескольким поставщикам (мультивыбор).")
    category_id = UUIDListField(help_text="Фильтр по категории вкл. дочерние подкатегории и группы (мультивыбор).")
    brand_id = UUIDListField(help_text="Фильтр по бренду (мультивыбор).")
    country = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="Фильтр по стране происхождения товара (мультивыбор).",
    )
    availability = serializers.ListField(
        child=serializers.ChoiceField(choices=ProductAvailability.values),
        required=False,
        default=list,
        help_text="Фильтр по наличию: in_stock / on_order / out_of_stock (мультивыбор).",
    )
    offers = serializers.ListField(
        child=serializers.ChoiceField(choices=_OFFER_CHOICES),
        required=False,
        default=list,
        help_text="Фильтр по меткам: promo (акция) / discount (скидка) / new (новинка), мультивыбор.",
    )
    venue_id = serializers.UUIDField(required=False, help_text="ID заведения — нужен, чтобы посчитать is_favorite.")
    is_favorite = serializers.BooleanField(
        required=False, default=False, help_text="Показывать только товары в избранном (требует venue_id)."
    )
    wishlist_id = serializers.UUIDField(
        required=False,
        help_text="Показывать только товары из указанного вишлиста текущего пользователя (требует JWT-авторизации).",
    )
    price_min = serializers.DecimalField(
        required=False, max_digits=14, decimal_places=2, min_value=0, help_text="Минимальная цена товара, включительно."
    )
    price_max = serializers.DecimalField(
        required=False,
        max_digits=14,
        decimal_places=2,
        min_value=0,
        help_text="Максимальная цена товара, включительно.",
    )
    sort = serializers.ChoiceField(
        choices=_SORT_CHOICES,
        required=False,
        allow_null=True,
        default="default",
        help_text=(
            "Сортировка: default / new (новые сначала) / with_offers (с акциями/скидками сначала) / "
            "in_stock_first (в наличии сначала) / price_asc / price_desc / name_asc / name_desc."
        ),
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=500, default=100, help_text="Размер страницы, максимум 500."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")
    replace_item_id = serializers.UUIDField(
        required=False,
        help_text=(
            "«Ищем замену» из «Неразобранных»: ID позиции заказа (`id` строки unresolved-items). Требует JWT "
            "участника заведения этой позиции. Исключает текущего поставщика позиции и оставляет товары той же "
            "категории (вкл. подкатегории), что у её товара; если категории нет — ищет по `requested_name` "
            "позиции (когда `q` не передан). Остальные фильтры и сортировка работают поверх."
        ),
    )


class CatalogProductListQueryErrorSerializer(serializers.Serializer):
    """400 списка каталога: ошибки по полям, в ответе только параметры с ошибкой (остальные — так же)."""

    replace_item_id = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="«Must be a valid UUID.»"
    )
    venue_id = serializers.ListField(child=serializers.CharField(), required=False, help_text="«Must be a valid UUID.»")
    sort = serializers.ListField(
        child=serializers.CharField(), required=False, help_text='«"<значение>" is not a valid choice.»'
    )
    limit = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="«A valid integer is required.» / «Ensure this value is less than or equal to 500.»",
    )


class CatalogProductReplaceItemNotFoundSerializer(serializers.Serializer):
    detail = serializers.CharField(
        read_only=True,
        help_text="«Позиция заказа для замены не найдена.» — позиции с таким replace_item_id нет или "
        "пользователь не участник её заведения.",
    )


class CatalogProductReplaceItemAuthSerializer(serializers.Serializer):
    detail = serializers.CharField(
        read_only=True,
        help_text="«Для replace_item_id нужна авторизация.» — replace_item_id передан без JWT (или JWT невалиден).",
    )


class ProductWishlistMembershipSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID вишлиста.")
    name = serializers.CharField(read_only=True, help_text="Название вишлиста, заданное пользователем.")


class CatalogProductSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID товара поставщика (SupplierProduct).")
    supplier_sku = serializers.CharField(
        read_only=True, allow_null=True, help_text="Артикул или код товара в системе поставщика."
    )
    system_sku = serializers.CharField(read_only=True, allow_null=True, help_text="Внутренний артикул АвтоСнаб.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика, у которого этот товар.")
    supplier_name = serializers.CharField(
        source="supplier.display_name", read_only=True, help_text="Название компании-поставщика."
    )
    raw_name = serializers.CharField(
        source="supplier_name",
        read_only=True,
        help_text="Исходное название товара — как его прислал поставщик в прайсе.",
    )
    normalized_name = serializers.CharField(
        read_only=True, help_text="Обработанное название товара, которое показывается покупателю."
    )
    current_price = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text="Актуальная цена товара в рублях."
    )
    unit = serializers.CharField(
        source="unit.code", read_only=True, allow_null=True, help_text="Код единицы измерения (шт, кг, л и т.п.)."
    )
    unit_id = serializers.UUIDField(
        source="unit.id", read_only=True, allow_null=True, help_text="ID единицы измерения."
    )
    package = serializers.CharField(
        read_only=True, allow_null=True, help_text="Расфасовка/упаковка товара (например, «1 кг», «уп. 12 шт»)."
    )
    min_order_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Минимальное количество для заказа.",
    )
    delivery_multiple = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Кратность поставки — заказывать можно только кратно этому числу.",
    )
    brand_id = serializers.UUIDField(source="brand.id", read_only=True, allow_null=True, help_text="ID бренда товара.")
    brand = serializers.CharField(
        source="brand.canonical_name", read_only=True, allow_null=True, help_text="Название бренда товара."
    )
    category_id = serializers.UUIDField(
        source="category.id",
        read_only=True,
        allow_null=True,
        help_text="ID категории (вида) товара, самый нижний уровень.",
    )
    category = serializers.CharField(
        source="category.name", read_only=True, allow_null=True, help_text="Название категории (вида) товара."
    )
    parent_category_id = serializers.SerializerMethodField()
    parent_category = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    group = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    image_is_placeholder = serializers.SerializerMethodField()
    country_of_origin = serializers.CharField(read_only=True, allow_null=True, help_text="Страна происхождения товара.")
    availability = serializers.ChoiceField(
        choices=ProductAvailability.choices,
        read_only=True,
        help_text="Наличие у поставщика: in_stock (в наличии) / on_order (под заказ) / out_of_stock (нет в наличии).",
    )
    is_promo = serializers.BooleanField(read_only=True, help_text="Товар участвует в акции.")
    is_discount = serializers.BooleanField(read_only=True, help_text="На товар действует скидка.")
    is_new = serializers.BooleanField(read_only=True, help_text="Товар помечен как новинка.")
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий поставщика к товару.")
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.ARCHIVED],
        read_only=True,
        help_text="Статус записи товара: active / archived.",
    )
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата и время последнего обновления записи.")
    is_favorite = serializers.SerializerMethodField()
    wishlists = serializers.SerializerMethodField()

    def _placeholder_path(self, obj):
        if obj.image_url:
            return None
        return category_placeholder_path(obj.category)

    @extend_schema_field(
        serializers.URLField(
            allow_null=True,
            help_text="Ссылка на изображение товара. Если у товара нет своего фото — картинка его вида товара, "
            "а без категории или для вида без своей картинки — общая картинка «продукты» (заглушка для "
            "прототипа, см. image_is_placeholder); null, только если файлов заглушек нет. Заглушка "
            "отдаётся ссылкой без схемы (//host/static/...), берёт протокол страницы.",
        )
    )
    def get_image_url(self, obj) -> Optional[str]:
        if obj.image_url:
            return obj.image_url
        path = self._placeholder_path(obj)
        return placeholder_url(path, self.context.get("request")) if path else None

    @extend_schema_field(
        serializers.BooleanField(
            help_text="true — image_url это общая картинка вида товара (например «макароны»), а не фото именно "
            "этого товара от поставщика. Временная заглушка прототипа."
        )
    )
    def get_image_is_placeholder(self, obj) -> bool:
        return self._placeholder_path(obj) is not None

    @extend_schema_field(
        serializers.BooleanField(
            allow_null=True,
            help_text="В избранном ли товар у заведения. null, если запрос сделан без venue_id.",
        )
    )
    def get_is_favorite(self, obj) -> Optional[bool]:
        if hasattr(obj, "is_favorite"):
            return bool(obj.is_favorite)
        return None

    @extend_schema_field(
        ProductWishlistMembershipSerializer(
            many=True,
            help_text="Вишлисты текущего пользователя (по JWT), в которые уже добавлен товар. Пустой список без JWT.",
        )
    )
    def get_wishlists(self, obj) -> list:
        return self.context.get("wishlist_map", {}).get(obj.id, [])

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="ID родительской категории (подгруппы)."))
    def get_parent_category_id(self, obj) -> Optional[str]:
        if obj.category and obj.category.parent_id:
            return str(obj.category.parent_id)
        return None

    @extend_schema_field(
        serializers.CharField(allow_null=True, help_text="Название родительской категории (подгруппы).")
    )
    def get_parent_category(self, obj) -> Optional[str]:
        if obj.category and obj.category.parent:
            return obj.category.parent.name
        return None

    @extend_schema_field(
        serializers.UUIDField(allow_null=True, help_text="ID группы товара — верхний уровень дерева категорий.")
    )
    def get_group_id(self, obj) -> Optional[str]:
        if obj.category and obj.category.parent and obj.category.parent.parent_id:
            return str(obj.category.parent.parent_id)
        return None

    @extend_schema_field(
        serializers.CharField(allow_null=True, help_text="Название группы товара — верхний уровень дерева категорий.")
    )
    def get_group(self, obj) -> Optional[str]:
        if obj.category and obj.category.parent and obj.category.parent.parent:
            return obj.category.parent.parent.name
        return None


class CatalogProductImageSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID фото.")
    image_url = serializers.URLField(read_only=True, help_text="Ссылка на фото.")
    position = serializers.IntegerField(read_only=True, help_text="Порядок в галерее. 0 — главное фото.")


class CatalogProductDetailSerializer(CatalogProductSerializer):
    """Карточка товара каталога — все поля строки списка плюс то, что есть только в
    модалке карточки (макет 2026-09-28): галерея, краткое описание, вес/объём нетто,
    состав упаковки."""

    short_description = serializers.CharField(
        read_only=True, allow_null=True, help_text="Краткое описание товара от поставщика (блок «Краткое описание»)."
    )
    net_value = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Вес/объём нетто одной товарной единицы (16 в «16 кг»).",
    )
    net_unit_id = serializers.UUIDField(
        source="net_unit.id", read_only=True, allow_null=True, help_text="ID единицы веса/объёма нетто."
    )
    net_unit = serializers.CharField(
        source="net_unit.code", read_only=True, allow_null=True, help_text="Код единицы веса/объёма нетто (кг, л, …)."
    )
    package_composition = serializers.SerializerMethodField()

    images = CatalogProductImageSerializer(
        source="images.all",
        many=True,
        read_only=True,
        help_text="Галерея фото товара, отсортирована по position (0 — главное). Пустой список, если фото нет; "
        "image_url выше — URL главного фото.",
    )

    @extend_schema_field(
        PackageCompositionSerializer(
            allow_null=True,
            help_text="Состав упаковки («8 шт. × 2 кг = 16 кг/кор.» — готовая строка в display); null, если "
            "поставщик его не заполнил.",
        )
    )
    def get_package_composition(self, obj):
        composition = package_composition(obj)
        return PackageCompositionSerializer(composition).data if composition else None


class CatalogProductDetailQuerySerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(
        required=False,
        help_text="ID заведения — нужен, чтобы посчитать is_favorite. Без него is_favorite = null.",
    )


class CatalogProductNotFoundSerializer(serializers.Serializer):
    detail = serializers.CharField(
        read_only=True,
        help_text="«No SupplierProduct matches the given query.» — товара нет, он в архиве, без цены или его "
        "поставщик удалён/снят с публикации.",
    )


class CatalogProductDetailQueryErrorSerializer(serializers.Serializer):
    venue_id = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="«Must be a valid UUID.» — venue_id передан, но это не UUID.",
    )


class CatalogBrandSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID бренда.")
    name = serializers.CharField(source="canonical_name", read_only=True, help_text="Название бренда.")


class CatalogCategorySerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID категории (вида товара).")
    name = serializers.CharField(read_only=True, help_text="Название категории (вида товара).")
    slug = serializers.CharField(read_only=True, help_text="URL-совместимый идентификатор категории.")
    parent_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID родительской подгруппы, если есть."
    )
    parent_name = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    group_name = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название родительской подгруппы."))
    def get_parent_name(self, obj) -> Optional[str]:
        return obj.parent.name if obj.parent else None

    @extend_schema_field(
        serializers.UUIDField(allow_null=True, help_text="ID группы товаров — верхний уровень дерева категорий.")
    )
    def get_group_id(self, obj) -> Optional[str]:
        if obj.parent and obj.parent.parent_id:
            return str(obj.parent.parent_id)
        return None

    @extend_schema_field(
        serializers.CharField(allow_null=True, help_text="Название группы товаров — верхний уровень дерева категорий.")
    )
    def get_group_name(self, obj) -> Optional[str]:
        if obj.parent and obj.parent.parent:
            return obj.parent.parent.name
        return None


class CatalogSupplierListQuerySerializer(serializers.Serializer):
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Поиск по display_name / legal_name / ИНН.",
    )


class CatalogSupplierSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    name = serializers.CharField(source="display_name", read_only=True, help_text="Публичное название компании.")
    legal_name = serializers.CharField(read_only=True, allow_null=True, help_text="Юридическое название компании.")
    inn = serializers.CharField(read_only=True, allow_null=True, help_text="ИНН компании.")


class CatalogSupplierDetailSerializer(serializers.Serializer):
    """2026-09-15, пользователь: карточка поставщика по клику на название в
    истории заказов заведения. Публично-безопасное подмножество полей
    SupplierDetailSerializer (кабинет самого поставщика, api/v1/suppliers/
    serializers.py) — те же реквизиты/контакты/условия доставки, но БЕЗ
    bank_accounts (банковские реквизиты — не для чужих глаз), status/
    is_published (внутреннее системное состояние, не для карточки) и
    my_role (нет контекста участника). Доступен по id заказа, поэтому не
    фильтруется по is_published/status — заказ мог быть оформлен у уже
    снятого с публикации/деактивированного поставщика, карточка всё равно
    должна открываться."""

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
    logo_url = serializers.CharField(read_only=True, allow_null=True, help_text="Ссылка на логотип компании.")
    description = serializers.CharField(read_only=True, allow_null=True, help_text="Описание компании.")
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
    addresses = SupplierAddressSerializer(
        source="addresses.all", many=True, read_only=True, help_text="Адреса компании (несколько, типизированные)."
    )
    contacts = SupplierContactSerializer(
        source="contacts.all", many=True, read_only=True, help_text="Дополнительные контакты компании."
    )
    schedule_rules = SupplierScheduleRuleSerializer(
        source="schedule_rules.all",
        many=True,
        read_only=True,
        help_text="Правила графика приёма/доставки и самовывоза.",
    )


class CatalogCountrySerializer(serializers.Serializer):
    country = serializers.CharField(help_text="Страна происхождения товара.")
