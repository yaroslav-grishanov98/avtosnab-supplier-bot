"""Venue cart, orders and supplier order requests."""

from decimal import Decimal
from typing import Optional

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.v1.catalog.selectors import (
    _group_items_by_supplier,
    _order_supplier_group_status,
    resolve_supplier_product_price,
    supplier_group_minimum_order,
)
from core.models import (
    EntityStatus,
    OrderItemAppliedPriceType,
    OrderItemStatus,
    OrderStatus,
    SupplierOrderRequestStatus,
    Unit,
    VenueAddressType,
)
from suppliers.models import SupplierProduct

from .common import UUIDListField, supplier_product_display_image_url, validate_supplier_product_qty


class OrderItemSerializer(serializers.Serializer):
    def to_representation(self, instance):
        data = super().to_representation(instance)
        if hasattr(instance, "_revision_content"):
            content = instance._revision_content
        else:
            saved = instance.order.revisions.filter(revision=instance.order.revision).first()
            content = saved.content if saved else None
        original = (
            next((item for item in content.get("items", []) if item["id"] == str(instance.pk)), None)
            if content
            else None
        )
        if original:
            for key in (
                "supplier_product_id",
                "supplier_id",
                "supplier_name",
                "supplier_product_name",
                "requested_name",
                "requested_qty",
                "requested_unit",
                "applied_price",
                "applied_currency",
                "applied_price_type",
                "applied_individual_price_id",
                "comment",
                "subdivision_id",
                "subdivision_name",
            ):
                data[key] = original.get(key)
        return data

    applied_currency = serializers.CharField(read_only=True, allow_null=True)
    id = serializers.UUIDField(read_only=True, help_text="ID позиции заказа.")
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID товара поставщика, если позиция привязана к каталогу."
    )
    supplier_product_deleted = serializers.BooleanField(
        read_only=True,
        help_text="true, если товар поставщика по этой позиции был удалён (не архивирован) после оформления заказа.",
    )
    # 2026-08-27, Vitya: не было способа сгруппировать позиции корзины по
    # поставщику — только supplier_name строкой, без стабильного id.
    supplier_id = serializers.SerializerMethodField()
    supplier_name = serializers.SerializerMethodField()
    supplier_product_name = serializers.SerializerMethodField()
    supplier_product_image_url = serializers.SerializerMethodField(
        help_text="2026-09-09, Витя: «у товаров в корзине нету картинки» — тот же гэп на уже "
        "отправленной заявке. Ссылка на изображение товара из каталога поставщика "
        "(SupplierProduct.image_url), а если своего фото нет — картинка-заглушка вида товара, как image_url "
        "в каталоге (TASK-125). null — позиция без привязки к каталогу."
    )
    # 2026-09-09, Витя: "должно быть еще и в айтемс" / "я же по массиву этому
    # итерируюсь" — фронт строит таблицу построчно из items, поэтому даты
    # самой заявки нужны прямо на позиции, не только один раз в корне ответа
    # (там же есть Order.created_at/requested_delivery_date). item.order уже
    # закэширован — эти позиции всегда приходят из order.items.all() (Django
    # сам проставляет known_related_objects для reverse FK), лишнего запроса
    # на позицию это не добавляет.
    order_created_at = serializers.SerializerMethodField(
        help_text="Дата и время создания заявки (то же значение, что в корне ответа заказа, "
        "продублировано на позицию для построчного рендера)."
    )
    order_requested_delivery_date = serializers.SerializerMethodField(
        help_text="Желаемая дата доставки, указанная заведением при отправке заявки (то же значение, "
        "что в корне ответа заказа). Не путать с confirmed_delivery_date этой позиции — requested это "
        "то, чего ХОТЕЛО заведение при отправке (может быть null, если не указывало вообще), а "
        "confirmed — дата, которую УКАЗАЛ ПОСТАВЩИК при принятии именно этой позиции (null, пока "
        "позиция не принята)."
    )
    # 2026-09-14, по замечанию пользователя на GET .../orders/{order_id}
    # (supplier_order_retrieve, тот же гэп применим и к venue-стороне): то
    # же venue_name, что в корне ответа (venue-/supplier-VenueNameSerializerMixin),
    # продублировано на позицию — тем же паттерном, что order_created_at/
    # order_requested_delivery_date выше. Требует obj.order.venue без
    # лишнего запроса — у detail-ручек (venue_order_detail_queryset/
    # supplier_order_detail_queryset) это уже select_related("venue") +
    # тот же кэшированный known_related_objects item.order, что у
    # order_created_at; у единственного другого места, отдающего голый
    # OrderItemSerializer (VenueOrderItemSetStatusView), это выставлено
    # вручную после refresh_from_db() — см. её код.
    venue_name = serializers.SerializerMethodField(
        help_text="Название заведения, оформившего заказ (то же значение, что venue_name в корне ответа, "
        "продублировано на позицию для построчного рендера)."
    )
    # 2026-09-28 (ТЗ панели «Неразобранные»): строка позиции без заказа вокруг —
    # фронту нужны order_id (действия .../orders/{order_id}/items/{item_id}/...),
    # venue_id (popup карточки заведения) и комментарий заявки («Комментарий покупателя»).
    order_id = serializers.UUIDField(
        read_only=True,
        help_text="ID заказа этой позиции — для действий .../orders/{order_id}/items/{item_id}/... "
        "(change-supplier, dismiss) из списка «Неразобранные».",
    )
    venue_id = serializers.UUIDField(
        source="order.venue_id",
        read_only=True,
        help_text="ID заведения, оформившего заказ — для popup карточки (GET /venues/{venue_id}).",
    )
    order_comment = serializers.CharField(
        source="order.comment",
        read_only=True,
        allow_null=True,
        help_text="Комментарий покупателя к заявке (Order.comment) — блок «Комментарий покупателя». Не путать "
        "с comment (комментарий к самой позиции) и supplier_comment (ответ поставщика).",
    )
    requested_name = serializers.CharField(
        read_only=True, help_text="Название товара, как его запросило заведение (может отличаться от каталога)."
    )
    requested_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, help_text="Запрошенное количество."
    )
    requested_unit = serializers.CharField(
        source="requested_unit.code", read_only=True, allow_null=True, help_text="Код единицы измерения запроса."
    )
    confirmed_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        read_only=True,
        allow_null=True,
        help_text="Подтверждённое поставщиком количество (после обработки заказа).",
    )
    # 2026-09-21, по просьбе пользователя: "поле сколько на остатке нужно
    # докупить товара... Всего 10 подтверждено 8 осталось 2" — вычисляется
    # на бэке, чтобы фронт не считал сам. Тот же критерий "неразобранности",
    # что у venue_unresolved_order_items_queryset (raw ТЗ §3.5/§16-18).
    remaining_qty = serializers.SerializerMethodField()
    confirmed_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Подтверждённая поставщиком цена за единицу.",
    )
    confirmed_delivery_date = serializers.DateField(
        read_only=True,
        allow_null=True,
        help_text="Дата поставки, указанная поставщиком при принятии заказа.",
    )
    applied_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="2026-09-07: цена за единицу, зафиксированная в момент отправки заявки (снимок "
        "resolve_supplier_product_price на тот момент) — null для позиций без supplier_product "
        "(поставщик вне системы/свободный текст).",
    )
    applied_price_type = serializers.ChoiceField(
        choices=OrderItemAppliedPriceType.choices,
        read_only=True,
        allow_null=True,
        help_text="base — применена базовая цена товара. individual — применена индивидуальная цена "
        "(см. applied_individual_price_id).",
    )
    applied_individual_price_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID индивидуальной цены (SupplierProductVenuePrice), применённой при отправке — "
        "заполнено только когда applied_price_type=individual.",
    )
    item_total = serializers.SerializerMethodField()
    # 2026-09-09 (кабинет поставщика, "Принять частично"): "проверить наличие
    # кратности поставки в API для позиции" — было только на CartItemSerializer
    # (до отправки), на уже отправленной заявке (эта позиция) отсутствовало —
    # тот же гэп, что раньше был у supplier_product_image_url. Фронту нужно и
    # то, и то для степпера фактического количества в модалке.
    min_order_qty = serializers.SerializerMethodField(
        help_text="Минимальное количество для заказа этого товара у поставщика. null, если не задано, либо "
        "позиция без привязки к каталогу."
    )
    delivery_multiple = serializers.SerializerMethodField(
        help_text="Кратность заказа (шаг количества) этого товара у поставщика — для степпера фактического "
        "количества в модалке «Принять частично». null, если не задано (количество можно менять "
        "произвольно), либо позиция без привязки к каталогу."
    )
    # 2026-09-21, пользователь: "в статусах у частично принятых почему-то
    # confirm" — OrderItemStatus на модели умышленно не различает полное и
    # частичное принятие (только confirmed, см. get_remaining_qty выше), но
    # в ответе API это уже вводит в заблуждение — фронт видит "confirmed" на
    # позиции, где реально принята только часть.
    # 2026-09-22, тот же пользователь, тот же класс проблемы: "почему если я
    # отклонил заказ поставщиком статус cancelled когда declined должен
    # быть" — на уровне заявки (SupplierOrderRequestSerializer.status) отказ
    # уже верно называется "declined", но на уровне ОТДЕЛЬНОЙ позиции
    # (это поле) модель знает только "cancelled" — той же лексики нет.
    # Оба случая — сама модель/БД не трогается (внутренняя бизнес-логика
    # везде полагается именно на CONFIRMED/CANCELLED + сравнение
    # confirmed_qty — переименование потребовало бы аудита каждого места,
    # где это сравнивается); вместо этого — то же самое переопределение
    # представления в API, что уже у remaining_qty.
    status = serializers.SerializerMethodField()
    # 2026-09-21, по просьбе пользователя: "нету даты когда поставщик
    # обработал заявку (изменил статус)".
    supplier_responded_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Когда поставщик принял решение по этой позиции (accept/decline, либо ручная простановка "
        "статуса для товара вне системы) — момент именно ЕГО ответа, не последнего изменения строки "
        "(отличается от смены поставщика через «Найти замену» или отмены заведением). null, пока позиция "
        "ещё submitted.",
    )
    cancellation_reason = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Причина отмены заведением (необязательна) — заполнено только при status=cancelled_by_venue.",
    )
    replaces_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="2026-09-03: ID позиции, невыполненный остаток которой эта позиция переотправляет другому "
        "поставщику — заполнено только у позиций, созданных через «Найти замену».",
    )
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий к позиции заказа.")
    # 2026-09-22, Дима (фронт): "к частично принятым товарам нужно
    # подтягивать коммент из заявки" — комментарий, который поставщик
    # оставил на POST .../accept (SupplierOrderRequest.supplier_comment,
    # 2026-09-22), не был виден на самой позиции ни на "Неразобранных", ни
    # где-либо ещё — только в ответе самого accept. Один комментарий на всю
    # заявку эхом на каждой её позиции (не только частично принятых — тот
    # же комментарий одинаково относится и к полностью принятым, раз он
    # общий на весь вызов, см. supplier_comment на SupplierOrderRequest).
    supplier_comment = serializers.SerializerMethodField()
    subdivision_id = serializers.SerializerMethodField()
    subdivision_name = serializers.SerializerMethodField()
    off_system_supplier_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID поставщика вне системы, если позиция оформлена на него."
    )
    off_system_supplier_name = serializers.SerializerMethodField()

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="ID поставщика по этой позиции."))
    def get_supplier_id(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.supplier_id if sp else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название поставщика по этой позиции."))
    def get_supplier_name(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.supplier.display_name if sp else None

    @extend_schema_field(
        serializers.CharField(allow_null=True, help_text="Название товара из каталога поставщика по этой позиции.")
    )
    def get_supplier_product_name(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.normalized_name if sp else None

    @extend_schema_field(serializers.URLField(allow_null=True, help_text="Ссылка на изображение товара."))
    def get_supplier_product_image_url(self, obj) -> Optional[str]:
        return supplier_product_display_image_url(obj.supplier_product, self.context.get("request"))

    @extend_schema_field(serializers.DateTimeField(help_text="Дата и время создания заявки."))
    def get_order_created_at(self, obj):
        # SerializerMethodField не прогоняет возврат через to_representation
        # сам — без явного вызова JSON-рендер отдал бы "+03:00" вместо "Z",
        # разойдясь с форматом created_at в корне ответа (там это настоящее
        # DateTimeField).
        return serializers.DateTimeField().to_representation(obj.order.created_at)

    @extend_schema_field(
        serializers.DateField(allow_null=True, help_text="Желаемая дата доставки, указанная заведением.")
    )
    def get_order_requested_delivery_date(self, obj):
        value = obj.order.requested_delivery_date
        return serializers.DateField().to_representation(value) if value else None

    @extend_schema_field(serializers.CharField(help_text="Название заведения, оформившего заказ."))
    def get_venue_name(self, obj) -> str:
        return obj.order.venue.display_name

    @extend_schema_field(
        serializers.CharField(
            allow_null=True,
            help_text="2026-09-22, Дима: комментарий поставщика, оставленный при принятии заявки "
            "(POST .../accept) — тот же supplier_comment, что на самой заявке, продублирован на каждую её "
            "позицию для построчного рендера (тем же паттерном, что order_created_at/venue_name выше). Для "
            "позиции поставщика вне системы (off_system_supplier_id задан) — то же поле, но источник другой: "
            "необязательный комментарий, переданный вручную в POST .../set-status (2026-09-25, задача #Лк1) "
            "— фронту не нужно различать источник. null — заявка ещё не принята/комментарий не оставлен.",
        )
    )
    def get_supplier_comment(self, obj) -> Optional[str]:
        if obj.off_system_supplier_id:
            return obj.off_system_status_comment
        return obj.supplier_request.supplier_comment if obj.supplier_request_id else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название подразделения."))
    def get_subdivision_name(self, obj) -> Optional[str]:
        if obj.subdivision_id:
            return obj.subdivision.name
        return obj.order.subdivision.name if obj.order.subdivision_id else None

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="Подразделение позиции или заявки."))
    def get_subdivision_id(self, obj) -> Optional[str]:
        subdivision_id = obj.subdivision_id or obj.order.subdivision_id
        return str(subdivision_id) if subdivision_id else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название поставщика вне системы."))
    def get_off_system_supplier_name(self, obj) -> Optional[str]:
        return obj.off_system_supplier.display_name if obj.off_system_supplier else None

    @extend_schema_field(
        serializers.DecimalField(max_digits=14, decimal_places=4, allow_null=True, help_text="Минимальное количество.")
    )
    def get_min_order_qty(self, obj) -> Optional[Decimal]:
        sp = obj.supplier_product
        return sp.min_order_qty if sp else None

    @extend_schema_field(
        serializers.DecimalField(max_digits=14, decimal_places=4, allow_null=True, help_text="Кратность заказа.")
    )
    def get_delivery_multiple(self, obj) -> Optional[Decimal]:
        sp = obj.supplier_product
        return sp.delivery_multiple if sp else None

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=2,
            allow_null=True,
            help_text="applied_price * (confirmed_qty, если позиция уже обработана поставщиком, иначе "
            "requested_qty). null, если applied_price не задана (поставщик вне системы/свободный текст).",
        )
    )
    def get_item_total(self, obj) -> Optional[Decimal]:
        if obj.applied_price is None:
            return None
        qty = obj.confirmed_qty if obj.confirmed_qty is not None else obj.requested_qty
        return obj.applied_price * qty

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=4,
            allow_null=True,
            help_text="requested_qty минус то, что реально подтверждено — сколько ещё нужно докупить по "
            "этой позиции. status=cancelled (поставщик отказался целиком) → весь requested_qty. "
            "status=confirmed с confirmed_qty < requested_qty (принято частично) → остаток. "
            "status=confirmed с confirmed_qty >= requested_qty (принято полностью) → 0. Для остальных "
            "статусов (submitted — ещё не отвечено, replaced/cancelled_by_venue — эта позиция больше не "
            "актуальна) — null, вопрос ещё не решён или неприменим.",
        )
    )
    def get_remaining_qty(self, obj) -> Optional[Decimal]:
        if obj.status == OrderItemStatus.CANCELLED:
            return obj.requested_qty
        if obj.status == OrderItemStatus.CONFIRMED and obj.confirmed_qty is not None:
            return max(obj.requested_qty - obj.confirmed_qty, Decimal("0"))
        return None

    @extend_schema_field(
        serializers.ChoiceField(
            choices=[
                ("submitted", "Submitted"),
                ("confirmed", "Confirmed"),
                ("partially_confirmed", "Partially confirmed"),
                ("declined", "Declined"),
                ("replaced", "Replaced"),
                ("cancelled_by_venue", "Cancelled by venue"),
            ],
            help_text="Статус позиции: submitted / confirmed (принято ПОЛНОСТЬЮ) / partially_confirmed "
            "(2026-09-21: принято частично — confirmed_qty < requested_qty; раньше в этом случае тоже "
            "отдавался confirmed, различить можно было только сравнением confirmed_qty с requested_qty на "
            "фронте) / declined (2026-09-22: поставщик отказался целиком — на уровне модели это то же "
            "значение, что cancelled, но заявка (SupplierOrderRequestSerializer.status) для этого же случая "
            "уже называет его declined, а не cancelled — здесь та же лексика, для консистентности) / "
            "replaced / cancelled_by_venue (заведение отменило до ответа поставщика, raw ТЗ §20-23 — "
            "отдельное значение, не путать с отказом поставщика). confirmed_qty=0 по этой позиции даёт ей "
            "declined, даже если остальные позиции заявки приняты.",
        )
    )
    def get_status(self, obj) -> str:
        if obj.status == OrderItemStatus.CANCELLED:
            return "declined"
        if (
            obj.status == OrderItemStatus.CONFIRMED
            and obj.confirmed_qty is not None
            and obj.confirmed_qty < obj.requested_qty
        ):
            return "partially_confirmed"
        return obj.status


class OrderItemSetStatusSerializer(serializers.Serializer):
    """Для позиций поставщика вне системы — снабженец/юзер ставит статус
    вручную, нет accept/decline от реального поставщика (2026-08-26 sync call)."""

    status = serializers.ChoiceField(
        choices=[OrderItemStatus.SUBMITTED, OrderItemStatus.CONFIRMED, OrderItemStatus.CANCELLED],
        help_text="Новый статус позиции: submitted / confirmed / cancelled.",
    )
    confirmed_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="Фактическое количество.",
    )
    confirmed_delivery_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Дата поставки, проставленная вручную. Обязательна при `status=confirmed`.",
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
        help_text="Необязательный комментарий (2026-09-25, задача #Лк1: «Отказать в поставке?» — тот же UX, "
        "что supplier_comment реального поставщика). Приходит в ответе как supplier_comment — то же поле, "
        "что у обычного поставщика, фронту не нужно различать источник. Пустая строка очищает.",
    )


class OrderItemCancelSerializer(serializers.Serializer):
    """Raw ТЗ §20: "Причина отмены: необязательна" — единственное поле, ни
    для отмены одной позиции, ни всей заявки заведением больше ничего не
    нужно (id позиции/поставщика уже в URL)."""

    reason = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Причина отмены."
    )


class OrderItemChangeSupplierSerializer(serializers.Serializer):
    """2026-09-03, «Найти замену» с 2026-09-15 (raw ТЗ «ЛК Заведения» §18-19):
    переотправляет невыполненный остаток позиции (отклонённой или принятой
    частично) новому товару поставщика, выбранному в маркетплейсе — см.
    change_order_item_supplier()."""

    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        help_text="ID нового товара поставщика (выбирается в маркетплейсе — не сам поставщик) — заменит "
        "невыполненный остаток этой позиции.",
    )


class OrderAuthorSerializer(serializers.Serializer):
    """Автор заявки: из первой ревизии, иначе создатель заказа."""

    user_id = serializers.UUIDField(read_only=True, help_text="ID пользователя-автора заявки.")
    name = serializers.CharField(read_only=True, help_text="Имя автора на момент создания заявки.")


class OrderApprovalSummarySerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["not_required", "pending", "rework", "approved", "cancelled"])
    type = serializers.ChoiceField(choices=["one_of", "sequential"], allow_null=True)
    cycle_id = serializers.UUIDField(allow_null=True)
    cycle_number = serializers.IntegerField(allow_null=True)
    current_step = serializers.IntegerField(allow_null=True)
    steps_count = serializers.IntegerField(allow_null=True)
    requires_my_decision = serializers.BooleanField()
    restart_required_on_change = serializers.BooleanField()
    available_actions = serializers.ListField(child=serializers.CharField())


class OrderSerializer(serializers.Serializer):
    revision = serializers.IntegerField(read_only=True)
    business_updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
    author = serializers.SerializerMethodField()
    approval = serializers.SerializerMethodField()

    @extend_schema_field(OrderAuthorSerializer(allow_null=True))
    def get_author(self, obj):
        saved = obj.revisions.order_by("revision").first()
        if saved and saved.content.get("_author_id"):
            return {"user_id": saved.content["_author_id"], "name": saved.content["_author_name"]}
        if obj.created_by_user_id:
            return {"user_id": str(obj.created_by_user_id), "name": obj.created_by_user.full_name}
        return None

    @extend_schema_field(OrderApprovalSummarySerializer)
    def get_approval(self, obj):
        from api.v1.catalog.approval import approval_data

        request = self.context.get("request")
        return approval_data(obj, getattr(request, "user", None))

    id = serializers.UUIDField(read_only=True, help_text="ID заказа.")
    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, оформившего заказ.")
    order_number = serializers.CharField(read_only=True, help_text="Номер заказа, вида ORD-XXXXXXXXXX.")
    status = serializers.ChoiceField(
        choices=OrderStatus.choices,
        read_only=True,
        help_text="Статус заказа: awaiting_dispatch (ожидает внутреннего согласования), submitted (отправлен поставщикам) / confirmed (хотя бы одна позиция "
        "подтверждена, ни одна не осталась submitted) / completed (завершён вручную создателем заявки) / "
        "cancelled (все поставщики отказались).",
    )
    requested_delivery_date = serializers.DateField(
        read_only=True, allow_null=True, help_text="Желаемая дата доставки."
    )
    submitted_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Дата и время отправки заказа поставщику."
    )
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий к заказу.")
    items_count = serializers.IntegerField(read_only=True, default=0, help_text="Количество позиций в заказе.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания заказа.")
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата и время последнего изменения заказа.")


class OrderSupplierSummarySerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID поставщика группы. null — поставщик вне системы (см. is_off_system) или позиция без "
        "распознанного поставщика.",
    )
    supplier_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название поставщика группы.")
    is_off_system = serializers.BooleanField(read_only=True, help_text="Группа — поставщик вне системы.")
    status = serializers.ChoiceField(
        choices=SupplierOrderRequestStatus.choices,
        read_only=True,
        help_text="2026-09-15: статус ЭТОЙ группы (этого поставщика) в рамках заказа — submitted / confirmed / "
        "partially_confirmed / declined / completed, та же логика, что у статуса заявки поставщику (см. "
        "GET /suppliers/{supplier_id}/orders), но видна и заведению. Отдельно от Order.status верхнего "
        "уровня — тот грубее (не различает целиком/частично принято) и общий для всех поставщиков заказа.",
    )


class OrderSupplierGroupSerializer(OrderSupplierSummarySerializer):
    items = OrderItemSerializer(many=True, read_only=True, help_text="Позиции этой группы.")


class _VenueNameSerializerMixin(serializers.Serializer):
    """Общий venue_name для серилизаторов заказа, где заведение уже
    select_related("venue") в queryset'е вызывающей стороны — не примешивать
    к базовым OrderSerializer/OrderDetailSerializer напрямую: часть их
    использующих queryset'ов (2026-09-14: уже не supplier_orders_queryset/
    supplier_order_detail_queryset — оба получили select_related("venue")
    для SupplierOrderSerializer/SupplierOrderDetailSerializer ниже) может
    не select_related'ить venue, что устроило бы N+1 на каждой строке.

    Наследуется от serializers.Serializer (не голый mixin) — DRF-метаклассу
    (SerializerMetaclass._get_declared_fields) для сбора полей из базовых
    классов нужен атрибут _declared_fields, который есть только у того, что
    само прошло через эту метаклассу; голый mixin-класс её поле бы просто
    тихо проигнорировал."""

    venue_name = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(help_text="Название заведения, оформившего заказ."))
    def get_venue_name(self, obj) -> str:
        return obj.venue.display_name


class VenueSupplierOrderRowSerializer(serializers.Serializer):
    """2026-09-17, по прямой просьбе пользователя: "у тебя должны с бэка
    отдаваться на каждого поставщика заявки, а не одна заявка на двух и
    более поставщиков" — заменяет прежний VenueOrderSerializer (одна строка
    на Order с suppliers[] внутри). Теперь одна строка списка = одна
    заявка/группа поставщика (venue_supplier_order_rows в selectors.py) —
    тот же принцип, что уже был у поставщика (SupplierOrderRequestSerializer,
    GET /suppliers/{supplier_id}/orders). Сериализует dict'ы (venue_supplier_
    order_rows объединяет два разнородных источника — персистентные
    SupplierOrderRequest для обычных поставщиков и вычисленные группы для
    вне-системных/нераспознанных — единой лазy queryset тут не получить),
    не ORM-объекты напрямую, но обычные декларативные поля читают ключи
    dict'а так же, как атрибуты объекта (DRF Field.get_attribute)."""

    id = serializers.UUIDField(
        read_only=True,
        help_text="ID заявки поставщику. У вне-системных поставщиков (is_off_system) и позиций без "
        "распознанного поставщика — синтетический стабильный id (персистентной сущности заявки для них "
        "нет), НЕ пригоден для запросов вида GET .../orders/{id} — используйте order_id.",
    )
    number = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Номер заявки, вида REQ-XXXXXXXXXX. null у вне-системных поставщиков/позиций без "
        "распознанного поставщика — своего номера нет.",
    )
    order_id = serializers.UUIDField(read_only=True, help_text="ID заказа заведения, частью которого является заявка.")
    order_number = serializers.CharField(read_only=True, help_text="Номер заказа заведения, вида ORD-XXXXXXXXXX.")
    supplier_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID поставщика (обычного или вне-системного — см. is_off_system). null — позиция без "
        "распознанного поставщика.",
    )
    supplier_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название поставщика.")
    is_off_system = serializers.BooleanField(read_only=True, help_text="Поставщик вне системы.")
    status = serializers.ChoiceField(
        choices=SupplierOrderRequestStatus.choices,
        read_only=True,
        help_text="Статус заявки: submitted (отправлено поставщику) / confirmed (подтверждена поставщиком) / "
        "partially_confirmed (принята частично) / declined (поставщик отказался) / completed (завершена). "
        "Та же логика, что у SupplierOrderRequest.status на стороне поставщика.",
    )
    viewed_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text='"Просмотрено" — дата/время первого открытия детали заявки поставщиком. null — ещё не '
        "открывалась, либо вне-системный поставщик (признак не отслеживается).",
    )
    items_count = serializers.IntegerField(read_only=True, help_text="Количество позиций в заявке.")
    requested_delivery_date = serializers.DateField(
        read_only=True, allow_null=True, help_text="Желаемая дата доставки, указанная заведением при отправке заказа."
    )
    minimum_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="2026-09-25 (задача #Лк1): минимальная сумма заказа у этого поставщика — включая "
        "поставщика вне системы, если для него задан лимит. null — позиция без распознанного поставщика, "
        "либо лимит не задан.",
    )
    currency = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Код валюты minimum_order_amount, обычно RUB. null вместе с minimum_order_amount.",
    )
    created_at = serializers.DateTimeField(
        read_only=True,
        help_text="Дата и время создания заявки. Для вне-системных поставщиков — момент отправки заказа "
        "(своей заявки-сущности с отдельным created_at у них нет).",
    )
    updated_at = serializers.DateTimeField(
        read_only=True,
        help_text="Дата и время последнего изменения заявки. Для вне-системных поставщиков — момент "
        "отправки заказа (не отслеживается отдельно).",
    )


class VenueMineSupplierOrderRowSerializer(VenueSupplierOrderRowSerializer):
    """2026-09-17: GET /venues/mine/orders агрегирует заявки сразу по всем
    заведениям аккаунта — venue_id одной строки не говорит, какое это
    заведение, поэтому добавлено venue_id/venue_name (в отличие от
    VenueSupplierOrderRowSerializer, где venue_id уже известен из URL)."""

    venue_id = serializers.UUIDField(read_only=True, help_text="ID заведения, оформившего заказ.")
    venue_name = serializers.CharField(read_only=True, help_text="Название заведения, оформившего заказ.")


class CartDeliveryAddressSerializer(serializers.Serializer):
    """Один из уже зарегистрированных адресов заведения (ведутся в кабинете
    заведения) — не свободный текст, только ссылка на существующую запись."""

    id = serializers.UUIDField(read_only=True, help_text="ID адреса.")
    address_type = serializers.ChoiceField(
        choices=VenueAddressType.choices, read_only=True, help_text="Тип адреса: юридический / фактический / домашний."
    )
    address = serializers.CharField(read_only=True, allow_null=True, help_text="Полная строка адреса одним полем.")
    country = serializers.CharField(read_only=True, allow_null=True, help_text="Страна.")
    region = serializers.CharField(read_only=True, allow_null=True, help_text="Регион.")
    district = serializers.CharField(read_only=True, allow_null=True, help_text="Район.")
    locality = serializers.CharField(read_only=True, allow_null=True, help_text="Населённый пункт.")
    street = serializers.CharField(read_only=True, allow_null=True, help_text="Улица, номер дома.")
    postal_code = serializers.CharField(read_only=True, allow_null=True, help_text="Почтовый индекс.")
    apartment_office = serializers.CharField(read_only=True, allow_null=True, help_text="Квартира, офис, комната.")


class CartContactSerializer(serializers.Serializer):
    """Контакт для связи по заявке — их может быть несколько на один заказ.
    Read-only снимок на Order (см. CartSubmitContactSerializer — вход при
    submit, эта же форма, но для вывода уже сохранённого контакта)."""

    id = serializers.UUIDField(read_only=True, help_text="ID контакта.")
    full_name = serializers.CharField(read_only=True, allow_null=True, help_text="Имя и фамилия контактного лица.")
    email = serializers.EmailField(read_only=True, allow_null=True, help_text="Email для связи по этой заявке.")
    phone = serializers.CharField(read_only=True, allow_null=True, help_text="Телефон для связи по этой заявке.")


class CartSubmitContactSerializer(serializers.Serializer):
    """2026-09-09 (Витя: "вынести вообще из корзины и принимать параметрами
    в /cart/submit"): контакт для связи, один из списка `contacts` в теле
    POST /cart/submit — раньше добавлялся отдельными вызовами на корзину
    (POST/DELETE .../cart/contacts, теперь убраны)."""

    full_name = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Имя и фамилия."
    )
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True, help_text="Email для связи.")
    phone = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=True, help_text="Телефон для связи."
    )

    def validate(self, attrs):
        if not attrs.get("full_name") and not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError("Укажите хотя бы одно из: full_name, email, phone.")
        return attrs


class OrderDetailSerializer(OrderSerializer):
    # 2026-09-09, Витя (обсуждение дат заявки): найдено при разборе его JSON —
    # items_count наследовался с OrderSerializer как обычный IntegerField
    # (default=0), рассчитанный на то, что вызывающая сторона проставит его
    # аннотацией (как в venue_orders_queryset — там items не грузятся целиком,
    # только count() ради производительности списка). Ни один из 6 мест, где
    # используется OrderDetailSerializer, этого не делал — здесь items и так
    # уже полностью загружены (items ниже), поэтому просто считаем длину уже
    # загруженного списка, без доп. запроса.
    items_count = serializers.SerializerMethodField(help_text="Количество позиций в заказе.")
    items = OrderItemSerializer(many=True, read_only=True, help_text="Позиции заказа плоским списком.")
    # 2026-08-27, Vitya: "в корзине нет группировки по поставщикам" — те же
    # items, сгруппированные по поставщику (обычному/вне системы/без
    # распознанного поставщика), для отрисовки корзины блоками, как в
    # прототипе с созвона 2026-08-26.
    supplier_groups = serializers.SerializerMethodField(
        help_text="Те же items, сгруппированные по поставщику (в порядке появления первой позиции группы)."
    )

    @extend_schema_field(serializers.IntegerField(help_text="Количество позиций в заказе."))
    def get_items_count(self, obj) -> int:
        return len(obj.items.all())

    # 2026-08-30: снимок subdivision/delivery_address/contacts на момент
    # отправки заявки — те же поля, что на VenueCartSerializer, но здесь
    # read-only и без возможности редактирования (заказ уже отправлен).
    subdivision_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID подразделения-получателя на момент отправки заявки."
    )
    subdivision_name = serializers.SerializerMethodField()
    delivery_address_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID адреса доставки на момент отправки заявки."
    )
    delivery_address = CartDeliveryAddressSerializer(
        read_only=True, allow_null=True, help_text="Развёрнутый адрес доставки. null, если не был выбран."
    )
    contacts = CartContactSerializer(
        many=True, read_only=True, help_text="Контакты для связи, зафиксированные на момент отправки заявки."
    )

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название подразделения-получателя."))
    def get_subdivision_name(self, obj) -> Optional[str]:
        return obj.subdivision.name if obj.subdivision else None

    @extend_schema_field(OrderSupplierGroupSerializer(many=True))
    def get_supplier_groups(self, obj):
        return [
            {
                **group,
                "status": _order_supplier_group_status(group["items"], obj.status),
                "items": OrderItemSerializer(group["items"], many=True, context=self.context).data,
            }
            for group in _group_items_by_supplier(obj.items.all())
        ]


class VenueOrderDetailSerializer(_VenueNameSerializerMixin, OrderDetailSerializer):
    """2026-09-09: тот же venue_name, что VenueOrderSerializer выше, но для
    детали одного заказа (GET /venues/{venue_id}/orders/{order_id} и то, что
    возвращает то же самое после действий заведения — complete, смена
    поставщика позиции). venue_order_detail_queryset уже select_related("venue")."""


class ApprovalOrderDetailSerializer(VenueOrderDetailSerializer):
    """Public order detail with both flat and supplier-grouped positions."""


class SupplierOrderRequestSerializer(serializers.Serializer):
    """2026-09-15 (raw ТЗ "ЛК Заведения" §3.3): "заявка" — теперь
    SupplierOrderRequest, персистентная сущность со своим номером/статусом/
    Просмотрено, а не вычисляемый фильтр Order.items по supplier_id
    (заменяет прежний SupplierOrderSerializer, который сериализовал Order).
    Все поля самого заказа (venue/order_number/даты/адрес) читаются через
    obj.order — supplier_order_requests_queryset уже select_related
    ("order__venue")."""

    id = serializers.UUIDField(read_only=True, help_text="ID заявки поставщику.")
    number = serializers.CharField(read_only=True, help_text="Номер заявки, вида REQ-XXXXXXXXXX.")
    order_id = serializers.UUIDField(read_only=True, help_text="ID заказа заведения, частью которого является заявка.")
    order_number = serializers.SerializerMethodField()
    supplier_id = serializers.UUIDField(
        read_only=True, help_text="ID поставщика — того же, что supplier_id в URL этой ручки."
    )
    supplier_name = serializers.SerializerMethodField()
    venue_id = serializers.SerializerMethodField()
    venue_name = serializers.SerializerMethodField()
    status = serializers.ChoiceField(
        choices=SupplierOrderRequestStatus.choices,
        read_only=True,
        help_text="Статус заявки: submitted (отправлено поставщику) / confirmed (подтверждена поставщиком) / "
        "partially_confirmed (принята частично) / declined (поставщик отказался) / completed (завершена). "
        "Вычисляется автоматически из состояния позиций — нельзя выставить напрямую.",
    )
    viewed_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text='"Просмотрено" — дата/время первого открытия детали заявки поставщиком. null — ещё не '
        "открывалась. Повторное открытие значение не меняет.",
    )
    supplier_comment = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="2026-09-22: комментарий поставщика, оставленный при принятии заявки (POST .../accept). "
        "null, пока заявка не принята, либо комментарий не был передан. Не путать с comment на детали заказа "
        "(SupplierOrderRequestDetailSerializer) — тот принадлежит заведению, задаётся при отправке заявки.",
    )
    items_count = serializers.IntegerField(read_only=True, default=0, help_text="Количество позиций в заявке.")
    requested_delivery_date = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания заявки.")
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата и время последнего изменения заявки.")

    @extend_schema_field(serializers.CharField(help_text="Номер заказа заведения, вида ORD-XXXXXXXXXX."))
    def get_order_number(self, obj) -> str:
        return obj.order.order_number

    @extend_schema_field(serializers.CharField(help_text="Название поставщика этой заявки."))
    def get_supplier_name(self, obj) -> str:
        return obj.supplier.display_name

    @extend_schema_field(serializers.UUIDField(help_text="ID заведения, оформившего заказ."))
    def get_venue_id(self, obj) -> str:
        return obj.order.venue_id

    @extend_schema_field(serializers.CharField(help_text="Название заведения, оформившего заказ."))
    def get_venue_name(self, obj) -> str:
        return obj.order.venue.display_name

    @extend_schema_field(
        serializers.DateField(allow_null=True, help_text="Желаемая дата доставки, указанная заведением.")
    )
    def get_requested_delivery_date(self, obj):
        value = obj.order.requested_delivery_date
        return serializers.DateField().to_representation(value) if value else None


class SupplierOrderRequestDetailSerializer(SupplierOrderRequestSerializer):
    def to_representation(self, instance):
        data = super().to_representation(instance)
        saved = instance.order.revisions.filter(revision=instance.order.revision).first()
        if saved:
            for key in ("delivery_address", "subdivision_id", "subdivision_name", "contacts", "comment"):
                data[key] = saved.content.get(key)
        return data

    items = OrderItemSerializer(many=True, read_only=True, help_text="Позиции этой заявки.")
    comment = serializers.SerializerMethodField()
    subdivision_id = serializers.SerializerMethodField()
    subdivision_name = serializers.SerializerMethodField()
    delivery_address = CartDeliveryAddressSerializer(
        source="order.delivery_address",
        read_only=True,
        allow_null=True,
        help_text="Развёрнутый адрес доставки. null, если не был выбран.",
    )
    contacts = CartContactSerializer(
        source="order.contacts",
        many=True,
        read_only=True,
        help_text="Контакты для связи, зафиксированные на момент отправки заказа.",
    )
    supplier_groups = serializers.SerializerMethodField(
        help_text="2026-09-15: восстановлено после замечания пользователя ('куда делось supplier_groups') — "
        "до перехода на SupplierOrderRequest это поле наследовалось от OrderDetailSerializer. Заявка всегда "
        "одного поставщика (это её собственный), поэтому массив содержит не больше одного элемента — "
        "те же items, что в плоском поле items выше, просто в старой обёртке для фронта."
    )

    @extend_schema_field(serializers.IntegerField(help_text="Количество позиций в заявке."))
    def get_items_count(self, obj) -> int:
        return len(obj.items.all())

    @extend_schema_field(OrderSupplierGroupSerializer(many=True))
    def get_supplier_groups(self, obj):
        # obj.status — статус самой этой SupplierOrderRequest, уже
        # корректно вычислен и персистентен (_recompute_supplier_order_
        # request_status), в отличие от _order_supplier_group_status,
        # которым пользуются venue-стороны без своей заявки-сущности.
        return [
            {
                **group,
                "status": obj.status,
                "items": OrderItemSerializer(group["items"], many=True, context=self.context).data,
            }
            for group in _group_items_by_supplier(obj.items.all())
        ]

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Комментарий к заказу."))
    def get_comment(self, obj):
        return obj.order.comment

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="ID подразделения-получателя."))
    def get_subdivision_id(self, obj):
        return obj.order.subdivision_id

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название подразделения-получателя."))
    def get_subdivision_name(self, obj):
        return obj.order.subdivision.name if obj.order.subdivision_id else None


def _resolved_cart_item_price(item):
    """2026-09-23 (Дима: "цена у заведения как есть отображается а нужно
    индивидуальную показывать") — до отправки заявка ещё не зафиксировала
    applied_price (см. OrderItemSerializer), так что цена корзины до сих пор
    читалась напрямую из supplier_product.current_price, игнорируя активную
    SupplierProductVenuePrice — тот же баг, что TASK-44 закрыл для
    Справочника компании, но обошёл корзину (другой сериализатор).
    Резолвится через resolve_supplier_product_price (§35, приоритет
    индивидуальной цены) и кэшируется на самом объекте позиции: subtotal
    группы (get_supplier_groups) и price/item_total этой же позиции не
    должны бить в БД дважды за один и тот же item."""
    if not hasattr(item, "_resolved_venue_price"):
        sp = item.supplier_product
        if sp is None:
            item._resolved_venue_price = None
        else:
            price, _currency, _individual_id = resolve_supplier_product_price(
                supplier_product=sp, venue_id=item.cart.venue_id
            )
            item._resolved_venue_price = price
    return item._resolved_venue_price


class CartItemSerializer(serializers.Serializer):
    """Позиция корзины — в отличие от OrderItemSerializer, принципиально без
    confirmed_qty/confirmed_price/confirmed_delivery_date/status: этих полей
    у ещё не отправленной заявке не может быть (Vitya, 2026-08-28: "как в
    корзине такое может быть до отправки заявки поставщику?")."""

    id = serializers.UUIDField(read_only=True, help_text="ID позиции корзины.")
    supplier_product_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID товара поставщика, если позиция привязана к каталогу."
    )
    supplier_product_deleted = serializers.BooleanField(
        read_only=True,
        help_text="true, если привязанный товар поставщика был удалён (не архивирован) — позицию нужно перепривязать.",
    )
    supplier_id = serializers.SerializerMethodField()
    supplier_name = serializers.SerializerMethodField()
    supplier_product_name = serializers.SerializerMethodField()
    supplier_product_image_url = serializers.SerializerMethodField(
        help_text="2026-09-09, Витя: «у товаров в корзине нету картинки» — ссылка на изображение "
        "товара из каталога поставщика (SupplierProduct.image_url), а если своего фото нет — картинка-заглушка "
        "вида товара, как image_url в каталоге (TASK-125). null — позиция без привязки к каталогу."
    )
    requested_name = serializers.CharField(
        read_only=True, help_text="Название товара, как его запросило заведение (может отличаться от каталога)."
    )
    requested_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, read_only=True, help_text="Запрошенное количество."
    )
    requested_unit = serializers.CharField(
        source="requested_unit.code", read_only=True, allow_null=True, help_text="Код единицы измерения запроса."
    )
    price = serializers.SerializerMethodField()
    item_total = serializers.SerializerMethodField()
    min_order_qty = serializers.SerializerMethodField()
    delivery_multiple = serializers.SerializerMethodField()
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий к позиции корзины.")
    subdivision_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID подразделения заведения, указанного для этой позиции."
    )
    subdivision_name = serializers.SerializerMethodField()
    off_system_supplier_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="ID поставщика вне системы, если позиция оформлена на него."
    )
    off_system_supplier_name = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField(
        read_only=True,
        help_text="Момент последнего изменения ЭТОЙ позиции, с микросекундной точностью. "
        "2026-09-08: добавлено для защиты от гонки ответов на клиенте — при быстром вводе количества "
        "(зажатая кнопка «+») уходит несколько PATCH подряд, и ответы могут прийти не в том порядке, в "
        "каком отправлялись. Применяйте пришедший ответ, только если его updated_at новее уже "
        "отрисованного для этой позиции; более старый ответ отбрасывайте.",
    )

    @extend_schema_field(serializers.UUIDField(allow_null=True, help_text="ID поставщика по этой позиции."))
    def get_supplier_id(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.supplier_id if sp else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название поставщика по этой позиции."))
    def get_supplier_name(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.supplier.display_name if sp else None

    @extend_schema_field(
        serializers.CharField(allow_null=True, help_text="Название товара из каталога поставщика по этой позиции.")
    )
    def get_supplier_product_name(self, obj) -> Optional[str]:
        sp = obj.supplier_product
        return sp.normalized_name if sp else None

    @extend_schema_field(serializers.URLField(allow_null=True, help_text="Ссылка на изображение товара."))
    def get_supplier_product_image_url(self, obj) -> Optional[str]:
        return supplier_product_display_image_url(obj.supplier_product, self.context.get("request"))

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=2,
            allow_null=True,
            help_text="Действующая цена за единицу у поставщика для этого заведения — индивидуальная "
            "(§35 ТЗ, TASK-44), если такая активна, иначе базовая цена товара поставщика. null — позиция "
            "без привязки к каталогу (поставщик вне системы или ненайденный товар).",
        )
    )
    def get_price(self, obj) -> Optional[Decimal]:
        return _resolved_cart_item_price(obj)

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=2,
            allow_null=True,
            help_text="price * requested_qty. null, если price не задана.",
        )
    )
    def get_item_total(self, obj) -> Optional[Decimal]:
        price = _resolved_cart_item_price(obj)
        if price is None:
            return None
        return price * obj.requested_qty

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=4,
            allow_null=True,
            help_text="Минимальное количество для заказа этого товара у поставщика. null, если не задано.",
        )
    )
    def get_min_order_qty(self, obj) -> Optional[Decimal]:
        sp = obj.supplier_product
        return sp.min_order_qty if sp else None

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=14,
            decimal_places=4,
            allow_null=True,
            help_text="Кратность заказа (шаг количества) этого товара у поставщика. null, если не задано — "
            "количество можно менять произвольно.",
        )
    )
    def get_delivery_multiple(self, obj) -> Optional[Decimal]:
        sp = obj.supplier_product
        return sp.delivery_multiple if sp else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название подразделения."))
    def get_subdivision_name(self, obj) -> Optional[str]:
        return obj.subdivision.name if obj.subdivision else None

    @extend_schema_field(serializers.CharField(allow_null=True, help_text="Название поставщика вне системы."))
    def get_off_system_supplier_name(self, obj) -> Optional[str]:
        return obj.off_system_supplier.display_name if obj.off_system_supplier else None


class CartSupplierGroupSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID поставщика группы. null — поставщик вне системы (см. is_off_system) или позиция без "
        "распознанного поставщика.",
    )
    supplier_name = serializers.CharField(read_only=True, allow_null=True, help_text="Название поставщика группы.")
    is_off_system = serializers.BooleanField(read_only=True, help_text="Группа — поставщик вне системы.")
    subtotal = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Сумма по позициям группы с известной ценой (без учёта позиций без цены). null, если ни у "
        "одной позиции группы нет цены.",
    )
    minimum_order_amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Минимальная сумма заказа у этого поставщика — включая поставщика вне системы, если для "
        "него задан лимит (2026-09-25, задача #Лк1). null — позиция без распознанного поставщика, либо "
        "лимит не задан.",
    )
    currency = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Код валюты minimum_order_amount, обычно RUB. null вместе с minimum_order_amount.",
    )
    remaining_to_minimum = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Сколько не хватает до minimum_order_amount, 0 — если уже достигнут. null вместе с "
        "minimum_order_amount.",
    )
    delivery_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="Стоимость доставки этого поставщика (raw ТЗ §8) — показывается отдельно от subtotal "
        "(товары), НЕ участвует в minimum_order_amount/remaining_to_minimum (§8-9: минимальная сумма "
        "считается только по товарам). null — поставщик вне системы, без распознанного поставщика, либо "
        "стоимость доставки не задана.",
    )
    total_with_delivery = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="subtotal + delivery_cost (delivery_cost считается за 0, если не задана). null, если у "
        "subtotal тоже null (ни у одной позиции группы нет цены).",
    )
    items = CartItemSerializer(many=True, read_only=True, help_text="Позиции этой группы.")


class VenueCartSerializer(serializers.Serializer):
    """2026-09-09 (Витя: "вынести вообще из корзины и принимать параметрами
    в /cart/submit"): у корзины больше нет venue_id/subdivision_id/
    delivery_address_id/contacts — все они venue-owned и теперь существуют
    только как параметры POST /cart/submit (см. CartSubmitSerializer),
    а не как состояние, которое можно накопить на самой корзине заранее."""

    id = serializers.UUIDField(read_only=True, help_text="ID корзины.")
    requested_delivery_date = serializers.DateField(
        read_only=True, allow_null=True, help_text="Желаемая дата доставки — при отправке копируется в заказ."
    )
    comment = serializers.CharField(read_only=True, allow_null=True, help_text="Комментарий к корзине.")
    items = serializers.SerializerMethodField(
        help_text="Позиции корзины плоским списком, в стабильном порядке добавления (по created_at, "
        "затем id). 2026-09-08: раньше порядка не было вообще — правка количества у одной позиции "
        "могла переставить местами другие, нетронутые.",
    )
    supplier_groups = serializers.SerializerMethodField(
        help_text="Те же items, сгруппированные по поставщику (в порядке появления первой позиции "
        "группы — то есть тоже стабильном, см. items)."
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания корзины.")
    updated_at = serializers.DateTimeField(read_only=True, help_text="Дата и время последнего изменения корзины.")

    @extend_schema_field(CartItemSerializer(many=True))
    def get_items(self, obj):
        items = list(obj.items.all())
        for item in items:
            # obj (эта же корзина) уже в памяти — кэшируем FK, чтобы
            # _resolved_cart_item_price не бил в БД за Cart на каждую позицию.
            item.cart = obj
        return CartItemSerializer(items, many=True, context=self.context).data

    @extend_schema_field(CartSupplierGroupSerializer(many=True))
    def get_supplier_groups(self, obj):
        result = []
        for group in _group_items_by_supplier(obj.items.all()):
            items = group["items"]
            for item in items:
                item.cart = obj
            subtotal = None
            for item in items:
                price = _resolved_cart_item_price(item)
                if price is not None:
                    item_total = price * item.requested_qty
                    subtotal = item_total if subtotal is None else subtotal + item_total

            minimum_order_amount, currency = supplier_group_minimum_order(items) if items else (None, None)
            # 2026-09-25 (задача #Лк1): у поставщика вне системы нет delivery_cost
            # (не показан на макете, нет откуда его брать без прайса/API) — только
            # минимум, проставленный вручную (см. supplier_group_minimum_order).
            delivery_cost = (
                items[0].supplier_product.supplier.delivery_cost if items and items[0].supplier_product else None
            )

            remaining_to_minimum = None
            if minimum_order_amount is not None:
                remaining_to_minimum = max(minimum_order_amount - (subtotal or Decimal("0")), Decimal("0"))

            total_with_delivery = None
            if subtotal is not None:
                total_with_delivery = subtotal + (delivery_cost or Decimal("0"))

            result.append(
                {
                    **group,
                    "subtotal": subtotal,
                    "minimum_order_amount": minimum_order_amount,
                    "currency": currency,
                    "remaining_to_minimum": remaining_to_minimum,
                    "delivery_cost": delivery_cost,
                    "total_with_delivery": total_with_delivery,
                    "items": CartItemSerializer(items, many=True, context=self.context).data,
                }
            )
        return result


class VenueCartUpdateSerializer(serializers.Serializer):
    """2026-09-09: subdivision_id/delivery_address_id/venue_id убраны отсюда —
    venue-owned поля корзины переехали в CartSubmitSerializer (см. там)."""

    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Новый комментарий к корзине."
    )
    requested_delivery_date = serializers.DateField(
        required=False, allow_null=True, help_text="Новая желаемая дата доставки."
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class CartSubmitSerializer(serializers.Serializer):
    """2026-09-09 (Витя: "можешь плиз это вынести вообще из корзины и
    принимать параметрами в /api/v1/catalog/cart/submit" — про
    subdivision_id/delivery_address_id/contacts): venue-owned поля заявки
    логичнее принимать одним вызовом, а не размазывать по отдельным
    PATCH/POST на корзину, которые надо было делать заранее и в правильном
    порядке. 2026-09-18: venue_id убран отсюда — корзина снова
    (user, venue)-scoped, venue берётся из URL (venues/{venue_id}/cart/submit),
    как и сама корзина."""

    command_id = serializers.UUIDField()
    expected_settings_version = serializers.IntegerField(min_value=1)

    subdivision_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="ID подразделения-получателя (из GET /venues/{venue_id}/subdivisions), должен принадлежать venue_id.",
    )
    delivery_address_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="ID адреса доставки (из GET /venues/{venue_id}/addresses), должен принадлежать venue_id.",
    )
    contacts = CartSubmitContactSerializer(
        many=True, required=False, help_text="Контакты для связи по этой заявке — можно несколько."
    )


class CartItemCreateSerializer(serializers.Serializer):
    supplier_product_id = serializers.PrimaryKeyRelatedField(
        queryset=SupplierProduct.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier_product",
        required=False,
        allow_null=True,
        help_text="ID товара из каталога поставщика. Можно не указывать, если товара нет в каталоге — тогда обязателен requested_name.",
    )
    requested_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Название товара, если он не выбирается из каталога (подставляется автоматически из supplier_product_id, если не задано).",
    )
    requested_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, help_text="Запрашиваемое количество."
    )
    requested_unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.filter(status=EntityStatus.ACTIVE),
        source="requested_unit",
        required=False,
        allow_null=True,
        help_text="ID единицы измерения запроса.",
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Комментарий к позиции корзины."
    )
    subdivision_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="ID подразделения этого заведения (из GET /venues/{venue_id}/subdivisions).",
    )
    off_system_supplier_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="ID поставщика вне системы этого заведения (из GET /venues/{venue_id}/off-system-suppliers). "
        "Оформляет позицию без цены/единицы и без реального обмена статусами с поставщиком.",
    )

    def validate(self, attrs):
        if not attrs.get("supplier_product") and not attrs.get("requested_name"):
            raise serializers.ValidationError("Either supplier_product_id or requested_name must be provided.")
        if attrs.get("supplier_product") and not attrs.get("requested_name"):
            attrs["requested_name"] = attrs["supplier_product"].normalized_name
        validate_supplier_product_qty(
            supplier_product=attrs.get("supplier_product"), requested_qty=attrs["requested_qty"]
        )
        return attrs


class CartItemUpdateSerializer(serializers.Serializer):
    requested_qty = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, help_text="Новое запрашиваемое количество."
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Новый комментарий к позиции."
    )
    subdivision_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Новый ID подразделения этого заведения (или null, чтобы снять)."
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class SupplierOrderRequestListQuerySerializer(serializers.Serializer):
    """SupplierOrderRequestStatus (5 значений) — отдельный набор от
    OrderStatus выше, статус заявки поставщику ≠ статус заказа заведения
    (raw ТЗ §3.2 vs §3.3)."""

    status = serializers.ChoiceField(
        choices=SupplierOrderRequestStatus.choices,
        required=False,
        help_text="Фильтр по статусу заявки: submitted (отправлено поставщику) / confirmed (подтверждена "
        "поставщиком) / partially_confirmed (принята частично) / declined (поставщик отказался) / "
        "completed (завершена).",
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=200, default=50, help_text="Размер страницы, максимум 200."
    )
    offset = serializers.IntegerField(required=False, min_value=0, default=0, help_text="Смещение для пагинации.")


class SupplierOrderRequestListWithVenueQuerySerializer(SupplierOrderRequestListQuerySerializer):
    """2026-09-18, Дима (фронт): "История заявок... отображаются все заявки
    всех заведений... должен быть фильтр: Заведения (с мультиселектом)" —
    для venue_mine_orders_list (агрегирует заявки по всем заведениям
    пользователя) и supplier_orders_list (поставщик получает заявки от
    множества разных заведений). Не добавлен на venue_orders_list
    (GET /venues/{venue_id}/orders) — там venue уже зафиксирован в URL,
    фильтр по нему был бы бессмысленным."""

    venue_id = UUIDListField(help_text="Фильтр по заведению(ям) — мультивыбор. Без фильтра — все заведения.")


class VenueUnresolvedOrderItemListQuerySerializer(serializers.Serializer):
    """2026-09-21, по просьбе пользователя — фильтр по подразделению и поиск
    по названию товара на GET .../orders/unresolved-items, тем же паттерном,
    что уже на VenueProductMatrixListQuerySerializer (`q`/`subdivision_id`)."""

    q = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Поиск по названию товара (подстрока)."
    )
    subdivision_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр по подразделению этого заведения (из GET /venues/{venue_id}/subdivisions). Без "
        "фильтра — все неразобранные позиции, независимо от подразделения (включая без него).",
    )
    supplier_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр «Все поставщики»: только позиции этого поставщика платформы. Без фильтра — все.",
    )
    off_system_supplier_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр по поставщику вне системы (off_system_supplier_id позиции). Без фильтра — все.",
    )


class VenueMineUnresolvedOrderItemListQuerySerializer(serializers.Serializer):
    """2026-09-28 (ТЗ панели «Неразобранные»): query для «Неразобранных» по всем
    заведениям пользователя — фильтры «Все заведения», «Все поставщики» и поиск."""

    q = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Поиск по названию товара (подстрока)."
    )
    venue_id = UUIDListField(
        help_text="Фильтр «Все заведения» — мультивыбор. Без фильтра — все заведения пользователя; чужое "
        "заведение молча игнорируется."
    )
    supplier_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр «Все поставщики»: только позиции этого поставщика платформы. Без фильтра — все.",
    )
    off_system_supplier_id = serializers.UUIDField(
        required=False,
        help_text="Фильтр по поставщику вне системы (off_system_supplier_id позиции). Без фильтра — все.",
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1, help_text="Номер страницы.")
    page_size = serializers.IntegerField(
        required=False, min_value=1, max_value=100, default=20, help_text="Размер страницы (макс. 100)."
    )


class VenueMineUnresolvedOrderItemCountQuerySerializer(serializers.Serializer):
    venue_id = UUIDListField(
        help_text="Считать только по этим заведениям (мультивыбор). Без фильтра — по всем заведениям пользователя."
    )


class OrderItemDismissConflictSerializer(serializers.Serializer):
    detail = serializers.CharField(
        read_only=True,
        help_text="«Позиция не в «Неразобранных»: убрать можно только отказ или частично принятую позицию.» "
        "либо «Позиция уже убрана из «Неразобранных».»",
    )


class SupplierOrderAcceptItemSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(help_text="ID позиции этого заказа у этого поставщика (в статусе submitted).")
    confirmed_qty = serializers.DecimalField(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        help_text="Подтверждаемое количество. 0 — отказаться именно от этой позиции, не трогая остальные "
        "позиции этого поставщика в этом же вызове; меньше requested_qty — принять частично (остаток можно "
        "переотправить другому поставщику через POST .../items/{item_id}/change-supplier); равно "
        "requested_qty — то же самое, что не указывать эту позицию вовсе.",
    )


class SupplierOrderAcceptSerializer(serializers.Serializer):
    confirmed_delivery_date = serializers.DateField(
        help_text="Дата поставки, которую поставщик обязуется выдержать. Обязательное поле — "
        "2026-08-21 sync call: поставщик всегда указывает дату поставки при принятии заказа."
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="2026-09-22: необязательный комментарий поставщика на весь этот вызов accept "
        "(не на отдельную позицию) — например, пояснение к частичному принятию или задержке "
        "поставки. Приходит в ответе как supplier_comment (не comment — это имя уже занято "
        "комментарием ЗАВЕДЕНИЯ к заказу). Пустая строка очищает ранее сохранённый комментарий.",
    )
    items = SupplierOrderAcceptItemSerializer(
        many=True,
        required=False,
        help_text="2026-09-03: точечные количества по позициям — частичный приём и/или точечный отказ в "
        "рамках одного вызова. Позиции этого поставщика, не перечисленные здесь, принимаются целиком "
        "(confirmed_qty = requested_qty), как раньше.",
    )

    def validate_items(self, value):
        ids = [row["item_id"] for row in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Duplicate item_id in items.")
        return value
