"""Tenders: buyer and supplier side."""

from typing import Optional

from django.core.files.storage import default_storage
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.catalog.selectors import (
    SUPPLIER_PRODUCT_NOT_FOUND_MESSAGE,
    price_per_position_unit,
    venue_label,
)
from catalog.models import (
    MasterProduct,
    SupplierTenderMyWork,
    TenderAccess,
    TenderChangedField,
    TenderChangeType,
    TenderOfferAutofillMatchStatus,
    TenderStatus,
    TenderVenuePriceSource,
)
from core.models import (
    EntityStatus,
    User,
)
from suppliers.models import Supplier, SupplierProduct

from .common import UUIDListField


class TenderListQuerySerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(required=True, help_text="ID владельца учётной записи, чьи тендеры запрашиваются.")
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=200,
        help_text="Поиск: номер тендера («22», «№22») или часть названия без учёта регистра.",
    )


class TenderFileSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID файла тендера.")
    name = serializers.CharField(read_only=True, help_text="Имя файла, как его загрузили.")
    url = serializers.SerializerMethodField(
        help_text="Постоянная публичная ссылка на файл: открывается без токена (ADR-0011)."
    )
    size = serializers.IntegerField(read_only=True, help_text="Размер в байтах.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Когда загружен.")

    @extend_schema_field(serializers.CharField())
    def get_url(self, obj) -> str:
        return default_storage.url(obj.storage_path)


class TenderFileUploadSerializer(serializers.Serializer):
    file = serializers.FileField(
        help_text="Документ или спецификация: PDF, DOC, DOCX, XLS, XLSX, JPG, PNG, до 20 МБ. Один файл за запрос. "
        "Неподходящий формат, размер или 21-й файл — 400 с detail."
    )


class TenderSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID тендера.")
    number = serializers.IntegerField(
        read_only=True, help_text="Сквозной номер тендера на платформе («Тендер №15»), выдаётся при создании."
    )
    owner_id = serializers.UUIDField(source="created_by_id", read_only=True, help_text="ID владельца учётной записи.")
    name = serializers.CharField(read_only=True, help_text="Название тендера.")
    description = serializers.CharField(read_only=True, allow_null=True, help_text="Описание тендера.")
    accepts_offers_until = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Срок приёма предложений. null — без срока (тендер завершают вручную).",
    )
    published_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Дата публикации (§3.1). null — черновик или тендер опубликован до 28.09, когда дата не сохранялась.",
    )
    access = serializers.ChoiceField(
        choices=TenderAccess.choices, read_only=True, help_text="Доступ: открытый / по приглашению."
    )
    invited_supplier_ids = serializers.SerializerMethodField(
        help_text="ID приглашённых поставщиков — заполнено только при access=invite_only."
    )
    status = serializers.ChoiceField(
        choices=TenderStatus.choices,
        read_only=True,
        help_text="Статус тендера: черновик (не виден поставщикам) / активен / завершён.",
    )
    items_count = serializers.IntegerField(
        read_only=True, help_text="Количество товаров в тендере, включая снятые с публикации."
    )
    offers_count = serializers.IntegerField(
        read_only=True,
        help_text="Количество действующих предложений поставщиков по всем товарам тендера, включая снятые с "
        "публикации товары. Одно предложение — один поставщик по одному товару; изменённое считается один раз, "
        "снятое поставщиком (§67) — не считается.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания тендера.")
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата и время последнего изменения тендера.")
    files = TenderFileSerializer(
        many=True, read_only=True, help_text="Файлы тендера — документы, спецификации (§6), старые сверху."
    )

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_invited_supplier_ids(self, obj) -> list[str]:
        return [supplier.id for supplier in obj.invited_suppliers.all()]


class TenderChangeSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID записи истории.")
    change_type = serializers.ChoiceField(
        choices=TenderChangeType.choices,
        read_only=True,
        help_text="Событие: создан, опубликован, изменено поле тендера, добавлен товар, изменено поле товара, "
        "товар опубликован или снят с публикации, файл добавлен или удалён, предложение отправлено, изменено, "
        "снято или возвращено (§76), завершён.",
    )
    changed_field = serializers.ChoiceField(
        choices=TenderChangedField.choices,
        read_only=True,
        allow_null=True,
        help_text="Изменённое поле — у updated, item_updated, offer_submitted и offer_updated; у остальных событий null.",
    )
    tender_item_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Товар тендера — у событий товара; иначе null."
    )
    item_name = serializers.SerializerMethodField(help_text="Название мастер-товара позиции; null — событие тендера.")
    tender_offer_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Предложение поставщика — у событий offer_*; иначе null (§76)."
    )
    supplier_name = serializers.SerializerMethodField(
        help_text="Поставщик предложения — у событий offer_*; иначе null (§76)."
    )
    old_value = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Прежнее значение строкой: срок — ISO 8601, списки — названия через «, ». null — не было.",
    )
    new_value = serializers.CharField(
        read_only=True, allow_null=True, help_text="Новое значение в том же виде. null — поле очищено."
    )
    actor_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Кто изменил. null — система (завершение по сроку)."
    )
    actor_name = serializers.SerializerMethodField(help_text="Имя того, кто изменил. null — система.")
    occurred_at = serializers.DateTimeField(read_only=True, help_text="Когда. У завершения по сроку — сам срок приёма.")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_item_name(self, obj) -> Optional[str]:
        return obj.tender_item.master_product.canonical_name if obj.tender_item_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_supplier_name(self, obj) -> Optional[str]:
        return obj.tender_offer.supplier.display_name if obj.tender_offer_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_actor_name(self, obj) -> Optional[str]:
        return (obj.actor.full_name or obj.actor.email) if obj.actor_id else None


class TenderDistributionHistorySerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="Заведение, в которое распределили.")
    venue_name = serializers.CharField(read_only=True, help_text="Название заведения, как в кабинете покупателя.")
    supplier_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="Поставщик. null — товар удалён.")
    supplier_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название поставщика.")
    supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Товар поставщика — название из прайса. null — товар удалён."
    )
    price = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True, help_text="Цена, зафиксированная распределением."
    )
    currency = serializers.CharField(read_only=True, help_text="Валюта.")
    distributed_at = serializers.DateTimeField(read_only=True, help_text="Когда распределили.")
    distributed_by_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Кто распределил. null — неизвестно."
    )
    is_current = serializers.BooleanField(
        read_only=True,
        help_text="Распределение действует: его цена — действующая для товара и заведения, и Справочник компании "
        "сейчас указывает на этот товар.",
    )


class TenderListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True, help_text="Общее количество тендеров.")
    next = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на следующую страницу.")
    previous = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на предыдущую страницу.")
    results = TenderSerializer(many=True, read_only=True)


class TenderCompletionSummarySerializer(serializers.Serializer):
    items_count = serializers.IntegerField(read_only=True, help_text="Количество товарных позиций тендера.")
    offers_count = serializers.IntegerField(
        read_only=True,
        help_text="Количество действующих предложений поставщиков по всем позициям; снятые не считаются.",
    )
    distributed = serializers.IntegerField(
        read_only=True,
        help_text="Слотов позиция×заведение, куда уже распределён товар поставщика (по текущему состоянию "
        "Справочника компании).",
    )
    undistributed = serializers.IntegerField(
        read_only=True, help_text="Слотов позиция×заведение, куда распределение ещё не произведено."
    )


class TenderActiveConflictEntrySerializer(serializers.Serializer):
    master_product_id = serializers.UUIDField(help_text="Мастер-товар, участвующий в конфликте.")
    master_product_name = serializers.CharField(help_text="Название мастер-товара.")
    venue_id = serializers.UUIDField(help_text="Заведение, участвующее в конфликте.")
    venue_name = serializers.CharField(help_text="Название заведения.")
    tender_id = serializers.UUIDField(help_text="Другой активный тендер, уже удерживающий эту связку.")
    tender_number = serializers.IntegerField(help_text="Номер этого тендера («Уже участвует в Тендере №15»).")
    tender_name = serializers.CharField(help_text="Название другого активного тендера.")


class TenderActiveConflictSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="Человекочитаемое пояснение конфликта (raw ТЗ §12).")
    conflicts = TenderActiveConflictEntrySerializer(
        many=True, help_text="Каждая связка Мастер-товар+Заведение, уже активная в другом тендере."
    )


class TenderItemCreateEntrySerializer(serializers.Serializer):
    master_product_id = serializers.PrimaryKeyRelatedField(
        queryset=MasterProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="master_product",
        help_text="ID мастер-товара, который становится этой позицией тендера.",
    )
    product_kind_label = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Вид товара (например «Сыры»).",
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Комментарий / требования к позиции."
    )
    venue_ids = UUIDListField(
        required=False,
        allow_empty=True,
        allow_null=True,
        default=None,
        help_text="Заведения, для которых покупатель собирает предложения по этому товару. Не передано (или null) — "
        "подбираются сами: действующие связи мастер-товара с заведениями владельца, кроме занятых другим активным "
        "тендером (§10, ADR-0007). [] — позиция без заведений: в черновике можно, опубликовать нельзя (§12-16).",
    )


# TASK-93: a whole Master-directory selection fits; the URL-length limit is why the
# availability endpoint is a POST.
MAX_VENUE_AVAILABILITY_MASTER_PRODUCTS = 200


class TenderSupplierSearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=200,
        help_text="Часть названия, юридического названия или ИНН, без учёта регистра. Не передано — все.",
    )


class TenderVenueAvailabilityRequestSerializer(serializers.Serializer):
    owner_id = serializers.UUIDField(help_text="ID владельца учётной записи, для которого собирается тендер.")
    master_product_ids = UUIDListField(
        allow_empty=False,
        help_text="Мастер-товары будущих позиций тендера (1–200), обычно выбранные в Мастер-справочнике. "
        "Ответ — в том же порядке.",
    )

    def validate_master_product_ids(self, value):
        if len(value) > MAX_VENUE_AVAILABILITY_MASTER_PRODUCTS:
            raise serializers.ValidationError(
                f"Не больше {MAX_VENUE_AVAILABILITY_MASTER_PRODUCTS} мастер-товаров за запрос."
            )
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        ids = attrs["master_product_ids"]
        owned = MasterProduct.objects.filter(id__in=ids, owner_id=attrs["owner_id"], status=EntityStatus.ACTIVE).count()
        if owned != len(ids):
            raise serializers.ValidationError(
                {"master_product_ids": "Есть мастер-товары, которых нет у этого владельца или которые удалены."}
            )
        return attrs


class TenderVenueAvailabilityVenueSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения аккаунта.")
    venue_name = serializers.CharField(read_only=True, help_text="Название заведения.")
    linked = serializers.BooleanField(
        read_only=True,
        help_text="Мастер-товар используется в этом заведении — есть связанный товар в Справочнике компании (§6). "
        "Автоподбор берёт linked и не занятые.",
    )
    busy_tender_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Активный тендер, в котором эта связка «мастер-товар + заведение» уже участвует (§8). null — свободно. "
        "Занятое заведение в ручном выборе недоступно (§11).",
    )
    busy_tender_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название этого активного тендера для подсказки. null — свободно."
    )
    busy_tender_number = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="Номер этого активного тендера («Тендере №15»). null — свободно."
    )


class TenderVenueAvailabilitySerializer(serializers.Serializer):
    master_product_id = serializers.UUIDField(read_only=True, help_text="Мастер-товар из запроса.")
    venues = TenderVenueAvailabilityVenueSerializer(
        many=True, read_only=True, help_text="Все действующие заведения аккаунта по алфавиту."
    )


class TenderPublishRejectionSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="Почему тендер не опубликован.")
    tender_item_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Позиции без заведений (§16) — только в этом случае.",
    )


def _future_deadline(value):
    """Raw ТЗ §7: «До даты» — a date ahead; a deadline equal to now has already passed (sync_tender_completion)."""
    if value is not None and value <= timezone.now():
        raise serializers.ValidationError("Срок приёма предложений должен быть в будущем.")
    return value


class TenderCreateSerializer(serializers.Serializer):
    owner_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(status=EntityStatus.ACTIVE),
        source="owner",
        help_text="ID владельца учётной записи, которому будет принадлежать тендер.",
    )
    name = serializers.CharField(trim_whitespace=True, max_length=500, help_text="Название тендера.")
    description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Описание тендера (общие условия)."
    )
    accepts_offers_until = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Срок приёма предложений. Не передавайте (или передайте null) для «без срока».",
    )
    access = serializers.ChoiceField(choices=TenderAccess.choices, help_text="Доступ: открытый / по приглашению.")
    invited_suppliers = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(status=EntityStatus.ACTIVE),
        many=True,
        required=False,
        help_text="ID приглашённых поставщиков — обязательно и непусто при access=invite_only, "
        "игнорируется при access=open.",
    )
    items = TenderItemCreateEntrySerializer(
        many=True,
        required=False,
        allow_empty=True,
        help_text="Опционально — сразу добавить позиции (raw ТЗ §5.2-5.3, кнопка «Собрать предложения»). "
        "Тендер и позиции создаются одним атомарным вызовом: если хотя бы одна позиция не проходит "
        "валидацию, не создаётся вообще ничего, включая сам тендер.",
    )

    def validate_accepts_offers_until(self, value):
        return _future_deadline(value)

    def validate(self, attrs):
        access = attrs.get("access")
        invited_suppliers = attrs.get("invited_suppliers") or []
        if access == TenderAccess.INVITE_ONLY and not invited_suppliers:
            raise serializers.ValidationError({"invited_suppliers": "Обязательно и непусто при access=invite_only."})
        if access == TenderAccess.OPEN and invited_suppliers:
            raise serializers.ValidationError({"invited_suppliers": "Недопустимо при access=open."})
        return attrs


class TenderUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, trim_whitespace=True, max_length=500, help_text="Название тендера.")
    description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Описание тендера (общие условия)."
    )
    accepts_offers_until = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Срок приёма предложений; null — без срока. В черновике — любой. У опубликованного тендера — только "
        "позже текущего или null; сократить срок или поставить его тендеру без срока нельзя (§16).",
    )
    access = serializers.ChoiceField(
        choices=TenderAccess.choices,
        required=False,
        help_text="Доступ: открытый / по приглашению. В черновике меняется: при переходе на open приглашённые "
        "очищаются. У опубликованного тендера не меняется — передайте текущий.",
    )
    invited_suppliers = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(status=EntityStatus.ACTIVE),
        many=True,
        required=False,
        help_text="Полный список ID приглашённых поставщиков: непусто при access=invite_only, недопустимо при "
        "access=open. В черновике — любой; у опубликованного тендера поставщиков можно только добавить: список "
        "должен содержать всех уже приглашённых (§16).",
    )

    def validate_accepts_offers_until(self, value):
        return _future_deadline(value)


class TenderItemUpdateSerializer(serializers.Serializer):
    product_kind_label = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Вид товара (например «Сыры»). Пустая строка или null — очистить.",
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Комментарий / требования к позиции. Пустая строка или null — очистить.",
    )
    venue_ids = serializers.ListField(
        child=serializers.UUIDField(help_text="ID заведения."),
        required=False,
        help_text="Полный список заведений позиции. В черновике — любой, включая пустой (публикация потребует хотя "
        "бы одно). У опубликованного тендера заведения можно только добавить: список должен содержать все текущие "
        "(§16); новые не должны быть заняты этим мастер-товаром в другом активном тендере (409, §12).",
    )


class TenderItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID позиции тендера.")
    tender_id = serializers.UUIDField(read_only=True, help_text="ID тендера.")
    master_product_id = serializers.UUIDField(read_only=True, help_text="ID мастер-товара этой позиции.")
    master_product_name = serializers.CharField(
        source="master_product.canonical_name", read_only=True, help_text="Название мастер-товара."
    )
    unit_code = serializers.SerializerMethodField(help_text="Код единицы измерения. null — у мастер-товара не задана.")
    product_kind_label = serializers.CharField(read_only=True, allow_null=True, help_text="Вид товара.")
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий / требования к позиции.")
    venue_ids = serializers.SerializerMethodField(help_text="Заведения, для которых собираются предложения.")
    published = serializers.BooleanField(
        read_only=True,
        help_text="TASK-59 (raw ТЗ §18/§75): позиция видна поставщикам. Снята с публикации — скрыта, "
        "но не удалена (история предложений сохраняется).",
    )
    offers_count = serializers.IntegerField(
        read_only=True,
        help_text="Действующие предложения поставщиков по этой позиции: одно на поставщика, снятые (§67) не считаются.",
    )
    distributed_venues_count = serializers.IntegerField(
        read_only=True,
        help_text="Распределено: в скольких заведениях позиции сейчас действует распределение предложений этой "
        "позиции, включая снятые, — товар в Справочнике компании и цена, зафиксированная из предложения. Тот же товар "
        "из другого тендера не считается. «N из M»: M — длина venue_ids.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время добавления позиции.")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_unit_code(self, obj) -> Optional[str]:
        return obj.master_product.unit.code if obj.master_product.unit_id else None

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_venue_ids(self, obj) -> list[str]:
        return [v.id for v in obj.venues.all()]


class TenderItemsCreateSerializer(serializers.Serializer):
    items = TenderItemCreateEntrySerializer(
        many=True, allow_empty=False, help_text="Добавляемые позиции, минимум одна."
    )


class TenderOfferSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID предложения.")
    tender_item_id = serializers.UUIDField(read_only=True, help_text="ID позиции тендера.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    supplier_name = serializers.CharField(
        source="supplier.display_name", read_only=True, help_text="Название компании-поставщика (§21 «Поставщик»)."
    )
    supplier_product_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="ID предложенного товара.")
    supplier_product_name = serializers.SerializerMethodField(help_text="Название предложенного товара.")
    supplier_product_raw_name = serializers.SerializerMethodField(
        help_text="Исходное название товара из прайса поставщика — различает фасовки одного товара "
        "с одинаковым supplier_product_name. null — товар поставщика удалён."
    )
    supplier_product_net_value = serializers.DecimalField(
        source="supplier_product.net_value",
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Вес/объём нетто предложенного товара (§21). null — не заполнен или товар удалён.",
    )
    supplier_product_net_unit = serializers.CharField(
        source="supplier_product.net_unit.code",
        read_only=True,
        allow_null=True,
        help_text="Код единицы нетто (кг, л и т.д.). null — не заполнена или товар удалён.",
    )
    supplier_product_delivery_multiple = serializers.DecimalField(
        source="supplier_product.delivery_multiple",
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Кратность поставки (§21). null — не заполнена или товар удалён.",
    )
    supplier_product_min_order_qty = serializers.DecimalField(
        source="supplier_product.min_order_qty",
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Минимальное количество (§21). null — не заполнено или товар удалён.",
    )
    price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, help_text="Цена предложения.")
    price_per_unit = serializers.SerializerMethodField(
        help_text="Цена за единицу позиции (unit_code мастер-товара) — для сравнения фасовок: та же единица — как "
        "есть; та же природа (г↔кг, мл↔л) — пересчёт; штуки/упаковки с нетто той же природы — цена / нетто. "
        "Округление до копеек. null — не сводится или у позиции нет единицы (ADR-0016)."
    )
    currency = serializers.CharField(read_only=True, help_text="Валюта (ISO 4217).")
    venue_ids = serializers.SerializerMethodField(help_text="Заведения, куда поставщик готов поставлять.")
    distributed_venue_ids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
        help_text="Заведения позиции, куда это предложение сейчас распределено: в Справочнике компании стоит его "
        "товар, и действующая цена для заведения зафиксирована из этого предложения. Тот же товар, назначенный "
        "другим тендером или вручную, не считается (§23: пусто — «Не назначено»; совпадает с пересечением venue_ids "
        "позиции и предложения — «Все доступные»).",
    )
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий поставщика.")
    withdrawn_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Когда поставщик снял предложение («Не предлагаем», §67). null — действует. Покупателю снятые "
        "не приходят; поставщик видит своё снятое с прежними значениями и возвращает его повторной подачей.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата первой подачи предложения.")
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата последнего изменения предложения.")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_supplier_product_name(self, obj) -> Optional[str]:
        return obj.supplier_product.normalized_name if obj.supplier_product_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_supplier_product_raw_name(self, obj) -> Optional[str]:
        return obj.supplier_product.supplier_name if obj.supplier_product_id else None

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_venue_ids(self, obj) -> list[str]:
        return [v.id for v in obj.venues.all()]

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True))
    def get_price_per_unit(self, obj) -> Optional[str]:
        value = price_per_position_unit(obj.price, obj.supplier_product, obj.tender_item.master_product.unit)
        return None if value is None else str(value)


class TenderOfferUpsertSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        error_messages={"does_not_exist": SUPPLIER_PRODUCT_NOT_FOUND_MESSAGE},
        help_text="ID действующего товара из каталога этой компании-поставщика. Чужой, архивный или удалённый — 400 "
        "«Товар не найден в каталоге компании.»",
    )
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        help_text="Итоговая цена для покупателя; АвтоСнаб ничего сверху не добавляет (ТЗ §55).",
    )
    currency = serializers.ChoiceField(
        choices=[("RUB", "RUB")], required=False, default="RUB", help_text="Валюта. Сейчас только RUB."
    )
    venue_ids = UUIDListField(
        required=True, allow_empty=False, help_text="Заведения, куда поставщик готов поставлять. Минимум одно."
    )
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Комментарий.")


class TenderItemForSupplierSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID позиции тендера.")
    master_product_name = serializers.CharField(
        source="master_product.canonical_name", read_only=True, help_text="Название товара."
    )
    unit_code = serializers.SerializerMethodField(help_text="Код единицы измерения. null — не задана.")
    product_kind_label = serializers.CharField(read_only=True, allow_null=True, help_text="Вид товара.")
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Требования к позиции.")
    venue_ids = serializers.SerializerMethodField(help_text="Заведения, для которых собираются предложения.")
    my_offer = serializers.SerializerMethodField(help_text="Текущее предложение этого поставщика. null — ещё нет.")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_unit_code(self, obj) -> Optional[str]:
        return obj.master_product.unit.code if obj.master_product.unit_id else None

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_venue_ids(self, obj) -> list[str]:
        return [v.id for v in obj.venues.all()]

    @extend_schema_field(TenderOfferSerializer(allow_null=True))
    def get_my_offer(self, obj) -> Optional[dict]:
        offers = obj.my_offers
        return TenderOfferSerializer(offers[0]).data if offers else None


class TenderCustomerVenueSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID заведения покупателя.")
    display_name = serializers.SerializerMethodField(
        help_text="Название заведения, как в кабинете покупателя: короткое, иначе полное."
    )

    @extend_schema_field(serializers.CharField())
    def get_display_name(self, obj) -> str:
        return venue_label(obj)


# Suppliers never see drafts. A separate choice set also keeps drf-spectacular
# from renaming Tender's TenderStatusEnum, which a shared set would force.
SUPPLIER_VISIBLE_TENDER_STATUSES = [
    (TenderStatus.ACTIVE, TenderStatus.ACTIVE.label),
    (TenderStatus.COMPLETED, TenderStatus.COMPLETED.label),
]


class SupplierTenderCompanySerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="Компания-поставщик пользователя, которая видит тендер.")
    name = serializers.CharField(read_only=True, help_text="Название компании.")
    my_offers_count = serializers.IntegerField(
        read_only=True, help_text="Опубликованные позиции, по которым эта компания отправила предложение."
    )


class SupplierTenderSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID тендера.")
    number = serializers.IntegerField(read_only=True, help_text="Сквозной номер тендера на платформе («Тендер №15»).")
    name = serializers.CharField(read_only=True, help_text="Название тендера.")
    description = serializers.CharField(read_only=True, allow_null=True, help_text="Описание тендера.")
    accepts_offers_until = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Срок приёма предложений. null — без срока."
    )
    published_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Дата публикации (§3.1); список поставщика отсортирован по ней, новые сверху. null — опубликован до "
        "28.09, когда дата не сохранялась (в списке — по дате создания).",
    )
    access = serializers.ChoiceField(
        choices=TenderAccess.choices, read_only=True, help_text="Доступ: открытый / по приглашению."
    )
    status = serializers.ChoiceField(
        choices=SUPPLIER_VISIBLE_TENDER_STATUSES,
        read_only=True,
        help_text="active — приём предложений; completed — завершён, только чтение.",
    )
    customer_venues = serializers.SerializerMethodField(
        help_text="Заказчик: уникальные заведения покупателя по опубликованным товарам тендера, по алфавиту "
        "(ADR-0003). Пусто, если опубликованных товаров нет."
    )
    published_items_count = serializers.IntegerField(
        read_only=True, help_text="Количество товаров тендера, видимых поставщику (опубликованных)."
    )
    my_offers_count = serializers.IntegerField(
        read_only=True,
        help_text="Сколько опубликованных товаров уже закрыто действующим предложением этого поставщика (в общем "
        "списке suppliers/mine/tenders — хотя бы одной компании пользователя); снятые («Не предлагаем», §67) не "
        "считаются. Не больше published_items_count.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата создания тендера.")
    files = TenderFileSerializer(
        many=True, read_only=True, help_text="Файлы тендера от покупателя — документы, спецификации (§53)."
    )
    companies = serializers.SerializerMethodField(
        help_text="Только в общем списке suppliers/mine/tenders (ADR-0015): компании пользователя, которые видят "
        "тендер — у открытого все, у закрытого приглашённые; по названию. В ответах по одной компании — null."
    )

    @extend_schema_field(SupplierTenderCompanySerializer(many=True, allow_null=True))
    def get_companies(self, obj) -> Optional[list[dict]]:
        companies = getattr(obj, "companies", None)
        return None if companies is None else SupplierTenderCompanySerializer(companies, many=True).data

    @extend_schema_field(TenderCustomerVenueSerializer(many=True))
    def get_customer_venues(self, obj) -> list[dict]:
        venues = {venue.id: venue for item in obj.published_items for venue in item.venues.all()}
        ordered = sorted(venues.values(), key=lambda venue: (venue_label(venue), str(venue.id)))
        return TenderCustomerVenueSerializer(ordered, many=True).data


class SupplierTenderListQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=SUPPLIER_VISIBLE_TENDER_STATUSES,
        required=False,
        default=TenderStatus.ACTIVE,
        help_text="Вкладка (§46): active — приём предложений идёт; completed — завершён, в том числе по истёкшему "
        "сроку. По умолчанию active.",
    )
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Поиск (§48): номер тендера («22», «№22»), название тендера, заведение-заказчик или мастер-товар "
        "опубликованных позиций.",
    )
    my_work = serializers.ChoiceField(
        choices=SupplierTenderMyWork.choices,
        required=False,
        help_text="«Моя работа»: not_started — ни одной отвеченной опубликованной позиции; in_progress — ответил не "
        "на все; done — на все; participating — in_progress и done вместе. Не передано — все тендеры.",
    )


class SupplierTenderListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True, help_text="Общее количество тендеров во вкладке.")
    next = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на следующую страницу.")
    previous = serializers.URLField(read_only=True, allow_null=True, help_text="Ссылка на предыдущую страницу.")
    results = SupplierTenderSerializer(many=True, read_only=True)


class TenderItemCurrentProductSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="Заведение позиции.")
    venue_name = serializers.CharField(read_only=True, help_text="Название заведения, как в кабинете покупателя.")
    requested_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Как товар назван в справочнике заведения (строка по мастер-товару позиции). null — строки нет.",
    )
    supplier_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Поставщик товара в справочнике заведения. null — товара нет."
    )
    supplier_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название поставщика.")
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Товар поставщика в справочнике заведения сейчас."
    )
    supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название товара из прайса поставщика."
    )
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Текущая цена в заведении: индивидуальная, иначе базовая. null — цены или товара нет.",
    )
    currency = serializers.CharField(read_only=True, allow_null=True, help_text="Валюта. null — цены нет.")
    price_source = serializers.ChoiceField(
        choices=TenderVenuePriceSource.choices,
        read_only=True,
        allow_null=True,
        help_text="individual — индивидуальная цена для заведения; base — базовая цена товара. null — цены нет.",
    )


class TenderVenuePriceSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения позиции.")
    venue_name = serializers.CharField(read_only=True, help_text="Название заведения.")
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Текущая цена товара поставщика для этого заведения. null — у товара нет ни индивидуальной "
        "цены для заведения, ни базовой.",
    )
    currency = serializers.CharField(read_only=True, allow_null=True, help_text="Валюта. null — цены нет.")
    price_source = serializers.ChoiceField(
        choices=TenderVenuePriceSource.choices,
        read_only=True,
        allow_null=True,
        help_text="individual — действующая индивидуальная цена для заведения; base — базовая цена товара. "
        "null — цены нет.",
    )


class TenderItemVenuePricesQuerySerializer(serializers.Serializer):
    supplier_product_id = serializers.UUIDField(help_text="Товар из собственного каталога поставщика.")


class TenderOfferAutofillSuggestionSerializer(serializers.Serializer):
    tender_item_id = serializers.UUIDField(read_only=True, help_text="ID позиции тендера.")
    match_status = serializers.ChoiceField(
        choices=TenderOfferAutofillMatchStatus.choices,
        read_only=True,
        help_text="found — есть подсказка (товар+цена+заведения); not_found — нет связанного товара "
        "поставщика или для него нигде не резолвится цена.",
    )
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Предложенный товар поставщика. null — not_found."
    )
    supplier_product_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Название предложенного товара. null — not_found."
    )
    supplier_product_raw_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Исходное название предложенного товара из прайса поставщика. null — not_found.",
    )
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Минимум резолвнутой цены по заведениям позиции (raw ТЗ §58-59). null — not_found.",
    )
    currency = serializers.CharField(read_only=True, allow_null=True, help_text="Валюта. null — not_found.")
    venue_ids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
        help_text="Заведения, предвыбранные автозаполнением — всегда полный список заведений позиции "
        "(raw ТЗ §62-64, автовыбор подтверждён Павлом). Пустой список — not_found.",
    )
    venue_prices = TenderVenuePriceSerializer(
        many=True,
        read_only=True,
        help_text="Raw ТЗ §58-60: текущая цена предложенного товара по каждому заведению позиции, по названию "
        "заведения; price — минимум из них. Разные цены — повод для «Есть разные цены по заведениям». "
        "Пустой список — not_found.",
    )


class TenderDistributionRequestSerializer(serializers.Serializer):
    venue_ids = UUIDListField(
        required=True,
        allow_empty=False,
        help_text="Заведения, которым назначается это предложение — подмножество venues позиции ∩ venues "
        "предложения. Минимум одно.",
    )


class TenderDistributionQuerySerializer(serializers.Serializer):
    venue_id = UUIDListField(
        source="venue_ids",
        required=True,
        allow_empty=False,
        help_text="Заведения для превью — подмножество venues позиции ∩ venues предложения. Минимум одно.",
    )
