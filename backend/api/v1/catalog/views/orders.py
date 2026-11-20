"""Venue cart, orders and supplier order requests."""

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication, OptionalCoreUserJWTAuthentication
from api.v1.catalog.selectors import (
    cart_detail_queryset,
    resolve_supplier_order_request,
    resolve_venue_order,
    supplier_order_request_audit_log,
    supplier_order_request_detail_queryset,
    supplier_order_requests_queryset,
    venue_mine_supplier_order_rows,
    venue_mine_unresolved_order_items_queryset,
    venue_order_detail_queryset,
    venue_supplier_order_rows,
    venue_unresolved_order_items_queryset,
)
from api.v1.catalog.serializers import (
    ApprovalOrderDetailSerializer,
    AuditLogEntrySerializer,
    CartItemCreateSerializer,
    CartItemSerializer,
    CartItemUpdateSerializer,
    CartSubmitSerializer,
    NotFoundDetailSerializer,
    OrderItemCancelSerializer,
    OrderItemChangeSupplierSerializer,
    OrderItemDismissConflictSerializer,
    OrderItemSerializer,
    OrderItemSetStatusSerializer,
    SupplierOrderAcceptSerializer,
    SupplierOrderRequestDetailSerializer,
    SupplierOrderRequestListQuerySerializer,
    SupplierOrderRequestListWithVenueQuerySerializer,
    SupplierOrderRequestSerializer,
    VenueCartSerializer,
    VenueCartUpdateSerializer,
    VenueMineSupplierOrderRowSerializer,
    VenueMineUnresolvedOrderItemCountQuerySerializer,
    VenueMineUnresolvedOrderItemListQuerySerializer,
    VenueOrderDetailSerializer,
    VenueSupplierOrderRowSerializer,
    VenueUnresolvedOrderItemListQuerySerializer,
    validate_supplier_product_qty,
)
from api.v1.catalog.services import (
    accept_supplier_order_items,
    add_cart_item,
    cancel_order_item,
    cancel_supplier_order_request,
    change_order_item_supplier,
    complete_order,
    decline_supplier_order_items,
    dismiss_unresolved_order_item,
    get_or_create_cart,
    mark_supplier_order_request_viewed,
    remove_cart_item,
    set_off_system_order_item_status,
    submit_venue_cart,
    update_cart_item,
    update_venue_cart,
)
from api.v1.permissions import (
    CanInitiateAccountTransfer,
    IsCoreUser,
    IsVenueMember,
)
from core.models import (
    EntityStatus,
    OffSystemSupplier,
    Order,
    OrderItem,
    SupplierOrderRequest,
    User,
    Venue,
    VenueAddress,
    VenueCartItem,
    VenueSubdivision,
)
from suppliers.models import Supplier

from ._shared import _MasterProductPagination

_ORDER_PAGE_SCHEMA = inline_serializer(
    name="OrderPage",
    fields={
        "count": s.IntegerField(help_text="Всего заявок (одна строка = одна заявка одному поставщику)."),
        "limit": s.IntegerField(),
        "offset": s.IntegerField(),
        "results": VenueSupplierOrderRowSerializer(many=True),
    },
)


_VENUE_MINE_ORDER_PAGE_SCHEMA = inline_serializer(
    name="VenueMineOrderPage",
    fields={
        "count": s.IntegerField(help_text="Всего заявок по всем заведениям аккаунта."),
        "limit": s.IntegerField(),
        "offset": s.IntegerField(),
        "results": VenueMineSupplierOrderRowSerializer(many=True),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}],
        summary="Моя корзина в этом заведении",
        description=(
            "2026-08-28: корзина — отдельная от заказа сущность (VenueCart), всегда существует и "
            "создаётся при первом обращении. В ответе нет `order_number`/`status`/confirmed-полей — их у "
            "ещё не отправленной заявки принципиально не может быть.\n\n"
            "2026-09-18, пользователь: «нужно получать корзину по ид заведения» — разворот решения "
            "2026-09-08 (тогда корзина стала singleton на пользователя на все заведения сразу). Теперь "
            "снова одна корзина на каждую пару (пользователь, заведение) — товары разных заведений "
            "больше не могут молча смешаться в одной корзине."
        ),
        responses={200: VenueCartSerializer},
    ),
    patch=extend_schema(
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}],
        summary="Изменить корзину",
        description=(
            "Обновляет комментарий и/или желаемую дату доставки на корзине. 2026-09-09 (Витя: "
            "«вынести вообще из корзины и принимать параметрами в /cart/submit»): подразделение-"
            "получатель, адрес доставки и контакты для связи больше не состояние корзины — теперь это "
            "параметры POST .../cart/submit."
        ),
        request=VenueCartUpdateSerializer,
        responses={200: VenueCartSerializer},
    ),
)
class VenueCartView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        cart = get_or_create_cart(request.user, venue)
        cart = get_object_or_404(cart_detail_queryset(request.user.id, venue_id), pk=cart.pk)
        return Response(VenueCartSerializer(cart, context={"request": request}).data)

    def patch(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        cart = get_or_create_cart(request.user, venue)
        ser = VenueCartUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cart = update_venue_cart(cart, **ser.validated_data)
        cart = get_object_or_404(cart_detail_queryset(request.user.id, venue_id), pk=cart.pk)
        return Response(VenueCartSerializer(cart, context={"request": request}).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_orders_list",
        tags=["Заказы"],
        summary="История заказов заведения",
        description=(
            '2026-09-17, по прямой просьбе пользователя: "у тебя должны с бэка отдаваться на каждого '
            'поставщика заявки, а не одна заявка на двух и более поставщиков" — раньше одна строка '
            "отвечала одному Order (с suppliers[] внутри, если поставщиков несколько), теперь одна строка "
            "= одна заявка/группа одному поставщику (тот же принцип, что уже был у поставщика, "
            "GET /suppliers/{supplier_id}/orders) — заказ с 3 поставщиками даёт 3 строки в этой истории, "
            "каждая со своим номером/статусом. У вне-системных поставщиков и позиций без распознанного "
            "поставщика нет персистентной сущности заявки — они тоже получают свою строку, но с "
            "синтетическим id (см. VenueSupplierOrderRow.id) и number=null. Каждая строка — только "
            "сводка, без items — переход в order_id (GET .../orders/{order_id}) отдаёт позиции всех "
            "поставщиков этого заказа, сгруппированные (supplier_groups[].items)."
        ),
        parameters=[SupplierOrderRequestListQuerySerializer],
        responses={200: _ORDER_PAGE_SCHEMA},
    ),
)
class VenueOrderView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        query_ser = SupplierOrderRequestListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        filters = query_ser.validated_data
        limit = filters["limit"]
        offset = filters["offset"]

        rows = venue_supplier_order_rows(venue_id, filters)
        total = len(rows)
        page = rows[offset : offset + limit]

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": VenueSupplierOrderRowSerializer(page, many=True).data,
            }
        )


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_mine_orders_list",
        tags=["Заказы"],
        summary="История заказов по всем моим заведениям",
        description=(
            "Агрегирует заявки всех заведений, где текущий пользователь — активный участник (owner/manager), "
            "в одну ленту — для self-service аккаунта с несколькими заведениями (см. GET /venues/mine). "
            "Каждая строка несёт venue_id/venue_name, чтобы отличать, какое заведение оформило заказ. Для "
            "одного конкретного заведения используйте GET /venues/{venue_id}/orders. 2026-09-17: та же "
            "группировка по заявке/поставщику, что теперь у него — см. его описание. 2026-09-18: добавлен "
            "мультивыбор venue_id — сузить ленту до одного или нескольких конкретных заведений."
        ),
        parameters=[SupplierOrderRequestListWithVenueQuerySerializer],
        responses={
            200: _VENUE_MINE_ORDER_PAGE_SCHEMA,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
        },
    ),
)
class VenueMineOrdersView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query_ser = SupplierOrderRequestListWithVenueQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        filters = query_ser.validated_data
        limit = filters["limit"]
        offset = filters["offset"]

        rows = venue_mine_supplier_order_rows(user_id=request.user.id, filters=filters)
        total = len(rows)
        page = rows[offset : offset + limit]

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": VenueMineSupplierOrderRowSerializer(page, many=True).data,
            }
        )


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_order_retrieve",
        tags=["Заказы"],
        summary="Детали заказа",
        description="Возвращает сохранённый заказ с позициями в items и supplier_groups[].items. Несёт venue_name — та же причина, "
        "что у GET /venues/{venue_id}/orders.",
        responses={200: ApprovalOrderDetailSerializer},
    ),
)
class VenueOrderDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id, order_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(venue_order_detail_queryset(venue_id), venue_id, order_id)
        from api.v1.catalog.approval import public_detail

        return Response(public_detail(order, request.user))


def _get_owned_venue_entity(model, entity_id, venue_id):
    """2026-09-18: subdivision_id/off_system_supplier_id on a cart item must
    belong to exactly the cart's own venue (URL venue_id) — tightened from
    2026-09-08's "any venue the caller manages", now that the cart itself is
    (user, venue)-scoped again and IsVenueMember already gates access to
    this specific venue."""
    return get_object_or_404(model, pk=entity_id, venue_id=venue_id, status=EntityStatus.ACTIVE)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}],
        summary="Добавить позицию в корзину",
        description=(
            "Добавляет товар в корзину этого заведения (создаётся автоматически при первом "
            "обращении). Если товара нет в каталоге — передайте только `requested_name` и `requested_qty` "
            "(поле «Не нашли товар?»). `subdivision_id`/`off_system_supplier_id` — опционально, должны "
            "принадлежать именно этому заведению. 2026-09-29: для товара с реальным `supplier_product_id` "
            "`requested_qty` проверяется против его `min_order_qty`/`delivery_multiple` (400, если меньше "
            "минимума или не кратно) — раньше бэкенд принимал любое количество."
        ),
        request=CartItemCreateSerializer,
        responses={
            201: CartItemSerializer,
            400: OpenApiResponse(
                description='Некорректное количество: {"requested_qty": ["Количество должно быть кратно 16 для '
                'этого товара."]}'
            ),
        },
    ),
)
class VenueCartItemView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        cart = get_or_create_cart(request.user, venue)
        ser = CartItemCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        subdivision = None
        if ser.validated_data.get("subdivision_id"):
            subdivision = _get_owned_venue_entity(VenueSubdivision, ser.validated_data["subdivision_id"], venue_id)
        off_system_supplier = None
        if ser.validated_data.get("off_system_supplier_id"):
            off_system_supplier = _get_owned_venue_entity(
                OffSystemSupplier, ser.validated_data["off_system_supplier_id"], venue_id
            )
        item = add_cart_item(
            cart=cart,
            supplier_product=ser.validated_data.get("supplier_product"),
            requested_name=ser.validated_data["requested_name"],
            requested_qty=ser.validated_data["requested_qty"],
            requested_unit=ser.validated_data.get("requested_unit"),
            comment=ser.validated_data.get("comment"),
            subdivision=subdivision,
            off_system_supplier=off_system_supplier,
        )
        return Response(CartItemSerializer(item, context={"request": request}).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Заказы"],
        summary="Изменить позицию корзины",
        description="Обновляет количество, комментарий или подразделение позиции корзины. 2026-09-29: если у "
        "позиции есть реальный `supplier_product`, новое `requested_qty` проверяется против его "
        "`min_order_qty`/`delivery_multiple` — та же проверка, что на создании позиции.",
        request=CartItemUpdateSerializer,
        responses={
            200: CartItemSerializer,
            400: OpenApiResponse(
                description='Некорректное количество: {"requested_qty": ["Количество должно быть кратно 16 для '
                'этого товара."]}'
            ),
        },
    ),
    delete=extend_schema(
        tags=["Заказы"],
        summary="Удалить позицию из корзины",
        description="Физически удаляет позицию корзины.",
        responses={204: None},
    ),
)
class VenueCartItemDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def patch(self, request, venue_id, item_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        cart = get_or_create_cart(request.user, venue)
        item = get_object_or_404(VenueCartItem, pk=item_id, cart=cart)
        ser = CartItemUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        if "requested_qty" in data:
            validate_supplier_product_qty(supplier_product=item.supplier_product, requested_qty=data["requested_qty"])
        if "subdivision_id" in data:
            subdivision_id = data.pop("subdivision_id")
            if subdivision_id:
                _get_owned_venue_entity(VenueSubdivision, subdivision_id, venue_id)
            data["subdivision_id"] = subdivision_id
        item = update_cart_item(item, **data)
        return Response(CartItemSerializer(item, context={"request": request}).data)

    def delete(self, request, venue_id, item_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        cart = get_or_create_cart(request.user, venue)
        item = get_object_or_404(VenueCartItem, pk=item_id, cart=cart)
        remove_cart_item(item)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}],
        summary="Отправить корзину",
        description=(
            "Создаёт заказ (`Order`: `submitted` в direct или `awaiting_dispatch` при согласовании) из текущих позиций корзины ЭТОГО "
            "заведения, копирует `comment`/`requested_delivery_date`, затем очищает корзину (сама корзина "
            "остаётся — можно сразу начинать собирать следующую заявку). `subdivision_id`/"
            "`delivery_address_id`/`contacts` (2026-09-09, по просьбе Вити) — необязательны, должны "
            "принадлежать этому же заведению; раньше были отдельным состоянием на самой корзине (PATCH "
            ".../cart, POST/DELETE .../cart/contacts — обе ручки убраны). Возвращает 409, если корзина "
            "пуста."
        ),
        request=CartSubmitSerializer,
        responses={
            200: ApprovalOrderDetailSerializer,
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description='Нет активного membership в этом заведении: {"detail": "..."}'),
            404: OpenApiResponse(description='Заведения/подразделения/адреса с таким id нет: {"detail": "Not found."}'),
            409: inline_serializer(
                name="CartSubmitConflict",
                fields={"detail": s.CharField(help_text="Корзина пуста.")},
            ),
        },
    ),
)
class VenueCartSubmitView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id):
        venue = get_object_or_404(Venue, pk=venue_id)
        if not isinstance(request.user, User):
            raise PermissionDenied(
                "Requires a core.User account — carts are account-scoped, not a service-token action."
            )
        ser = CartSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        subdivision = None
        subdivision_id = ser.validated_data.get("subdivision_id")
        if subdivision_id:
            subdivision = get_object_or_404(
                VenueSubdivision, pk=subdivision_id, venue_id=venue_id, status=EntityStatus.ACTIVE
            )
        delivery_address = None
        delivery_address_id = ser.validated_data.get("delivery_address_id")
        if delivery_address_id:
            delivery_address = get_object_or_404(VenueAddress, pk=delivery_address_id, venue_id=venue_id)

        cart = get_or_create_cart(request.user, venue)
        order, error = submit_venue_cart(
            cart,
            command_id=ser.validated_data["command_id"],
            expected_settings_version=ser.validated_data["expected_settings_version"],
            subdivision=subdivision,
            delivery_address=delivery_address,
            contacts=ser.validated_data.get("contacts"),
            actor=request.user,
        )
        if error:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        order.items_count = order.items.count()
        from api.v1.catalog.approval import public_detail

        return Response(public_detail(order, request.user))


_SUPPLIER_ORDER_REQUEST_PAGE_SCHEMA = inline_serializer(
    name="SupplierOrderRequestPage",
    fields={
        "count": s.IntegerField(help_text="Всего заявок этому поставщику"),
        "limit": s.IntegerField(),
        "offset": s.IntegerField(),
        "results": SupplierOrderRequestSerializer(many=True),
    },
)


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_orders_list",
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}, {"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        summary="Пришедшие заявки поставщика",
        description=(
            "Раз ТЗ «ЛК Заведения» §3.3: заявка — SupplierOrderRequest, персистентная сущность со своим "
            "номером/статусом/признаком «Просмотрено», одна на пару (заказ, поставщик). Заказ заведения "
            "может включать несколько поставщиков сразу (§3.2) — здесь одна строка = одна заявка ЭТОМУ "
            "поставщику, `items_count` считает только его позиции. 2026-09-18, Дима (фронт): заявки этому "
            "поставщику приходят от разных заведений — добавлен мультивыбор venue_id, чтобы сузить ленту "
            "до одного или нескольких конкретных заведений."
        ),
        # 2026-09-15, пользователь: "поправь описание энамов по статусам" —
        # раньше здесь был отдельный вручную набранный OpenApiParameter для
        # status (голый type=string, без реального enum и без расшифровки
        # каждого значения), расходящийся с SupplierOrderRequestListQuerySerializer,
        # которым query params на самом деле парсятся. Передача самого
        # серилизатора — тот же паттерн, что ProductSuggestionQuerySerializer
        # ниже — выводит параметры (включая реальный enum и help_text) прямо
        # из него, без дублирования и расхождений.
        parameters=[SupplierOrderRequestListWithVenueQuerySerializer],
        responses={
            200: _SUPPLIER_ORDER_REQUEST_PAGE_SCHEMA,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(description='Поставщика с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class SupplierOrderView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id):
        get_object_or_404(Supplier, pk=supplier_id)
        query_ser = SupplierOrderRequestListWithVenueQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        filters = query_ser.validated_data
        limit = filters["limit"]
        offset = filters["offset"]

        queryset = supplier_order_requests_queryset(supplier_id, filters)
        total = queryset.count()
        page = queryset[offset : offset + limit]

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": SupplierOrderRequestSerializer(page, many=True, context={"request": request}).data,
            }
        )


@extend_schema_view(
    get=extend_schema(
        operation_id="supplier_order_retrieve",
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}, {"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        summary="Детали заявки для поставщика",
        description=(
            "Возвращает заявку этому поставщику — `items` содержит только его собственные позиции. "
            "Первое открытие этой ручки помечает заявку как «Просмотрено» (raw ТЗ §13) — повторные не "
            "меняют `viewed_at`; доставка Telegram/MAX-уведомления сама по себе просмотром не считается."
        ),
        responses={
            200: SupplierOrderRequestDetailSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description='Поставщика нет, либо заявки этому поставщику по этому заказу нет: {"detail": "Not found."}'
            ),
        },
    ),
)
class SupplierOrderDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id, order_id):
        get_object_or_404(Supplier, pk=supplier_id)
        supplier_request = resolve_supplier_order_request(supplier_order_request_detail_queryset(supplier_id), order_id)
        mark_supplier_order_request_viewed(supplier_request, actor=request.user)
        return Response(SupplierOrderRequestDetailSerializer(supplier_request, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}, {"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        summary="Принять заявку (позиции этого поставщика)",
        description=(
            "Переводит свои позиции этой заявки со статусом `submitted` в `confirmed`. По умолчанию — целиком "
            "(`confirmed_qty` = `requested_qty`) для всех своих позиций; необязательное поле `items` позволяет "
            "принять часть позиций частично или точечно отказаться от конкретной позиции в этом же вызове "
            "(2026-09-03, закрывает открытый вопрос «смена поставщика / частичный отказ» с созвона 2026-08-21 — "
            "вторая половина, переотправка невыполненного остатка другому поставщику, отдельной ручкой "
            "`change-supplier`, см. ниже). Обязательно требует `confirmed_delivery_date`. Позиции других "
            "поставщиков в том же заказе не трогает. Статус ЭТОЙ заявки становится `confirmed` (все позиции "
            "подтверждены целиком), `partially_confirmed` (часть позиций отклонена/принята не полностью) или "
            "`declined` (все позиции отклонены — например, если все confirmed_qty=0). `Order.status` (весь "
            "заказ заведения) переходит дальше `submitted` только когда ВСЕ поставщики заказа ответили.\n\n"
            "2026-09-22: необязательное поле `comment` — комментарий поставщика на весь этот вызов (не на "
            "отдельную позицию), возвращается в ответе как `supplier_comment`."
        ),
        request=SupplierOrderAcceptSerializer,
        responses={
            200: SupplierOrderRequestDetailSerializer,
            400: OpenApiResponse(
                description='confirmed_delivery_date не передан: {"confirmed_delivery_date": ["This field is '
                'required."]}, либо в items — не своя/не submitted позиция: {"items": ["Item ... is not a '
                'pending item of this supplier in this order."]}, либо confirmed_qty больше requested_qty: '
                '{"items": ["confirmed_qty for item ... exceeds requested_qty."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description='Поставщика нет, либо заявки по этому заказу нет: {"detail": "Not found."}'
            ),
            409: OpenApiResponse(
                description='Заказ не в статусе submitted: {"detail": "Order is ..., only submitted orders can be '
                'accepted."}, либо у этого поставщика в заказе нет позиций в статусе submitted: '
                '{"detail": "No pending items for this supplier in this order."}'
            ),
        },
    ),
)
class SupplierOrderAcceptView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id, order_id):
        get_object_or_404(Supplier, pk=supplier_id)
        supplier_request = resolve_supplier_order_request(
            SupplierOrderRequest.objects.filter(supplier_id=supplier_id), order_id
        )
        ser = SupplierOrderAcceptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item_quantities = {row["item_id"]: row["confirmed_qty"] for row in ser.validated_data.get("items", [])}
        ok, error = accept_supplier_order_items(
            order=supplier_request.order,
            supplier_id=supplier_id,
            confirmed_delivery_date=ser.validated_data["confirmed_delivery_date"],
            item_quantities=item_quantities,
            comment=ser.validated_data.get("comment"),
            actor=request.user,
        )
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        supplier_request = get_object_or_404(
            supplier_order_request_detail_queryset(supplier_id), pk=supplier_request.pk
        )
        return Response(SupplierOrderRequestDetailSerializer(supplier_request, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        auth=[{"CoreUserJWT": []}, {"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        summary="Отклонить заявку (позиции этого поставщика)",
        description=(
            "Переводит все свои позиции этой заявки со статусом `submitted` в `cancelled`. Позиции других "
            "поставщиков в том же заказе не трогает. Статус заявки становится `declined` — та же логика "
            "перехода `Order.status`, что и у accept."
        ),
        request=None,
        responses={
            200: SupplierOrderRequestDetailSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description="JWT валиден, но не сервис-токен/admin/активный участник этого поставщика."
            ),
            404: OpenApiResponse(
                description='Поставщика нет, либо заявки по этому заказу нет: {"detail": "Not found."}'
            ),
            409: OpenApiResponse(
                description="Заказ не в статусе submitted, либо у этого поставщика в заказе нет позиций в "
                'статусе submitted: {"detail": "..."}'
            ),
        },
    ),
)
class SupplierOrderDeclineView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id, order_id):
        get_object_or_404(Supplier, pk=supplier_id)
        supplier_request = resolve_supplier_order_request(
            SupplierOrderRequest.objects.filter(supplier_id=supplier_id), order_id
        )
        ok, error = decline_supplier_order_items(
            order=supplier_request.order, supplier_id=supplier_id, actor=request.user
        )
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        supplier_request = get_object_or_404(
            supplier_order_request_detail_queryset(supplier_id), pk=supplier_request.pk
        )
        return Response(SupplierOrderRequestDetailSerializer(supplier_request, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        summary="Завершить заявку",
        description=(
            "2026-08-26 sync call: пятый статус со скриншота истории заказов — ставится вручную создателем "
            "заявки, когда весь путь товара пройден (полноценной автоматической приёмки пока нет). "
            "Требует `Order.status == confirmed`."
        ),
        request=None,
        responses={
            200: VenueOrderDetailSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения или заказа с таким id нет: {"detail": "Not found."}'),
            409: OpenApiResponse(
                description='Заказ не в статусе confirmed: {"detail": "Order is ..., only confirmed orders can '
                'be completed."}'
            ),
        },
    ),
)
class VenueOrderCompleteView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, order_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        ok, error = complete_order(order, actor=request.user)
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        order = get_object_or_404(venue_order_detail_queryset(venue_id), pk=order.pk)
        return Response(VenueOrderDetailSerializer(order, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        summary="Вручную установить статус позиции у поставщика вне системы",
        description=(
            "2026-08-26 sync call: у поставщиков вне системы нет аккаунта — нет accept/decline, снабженец/юзер "
            "меняет статус позиции сам. Работает только для позиций с `off_system_supplier_id` — на обычные "
            "позиции статус меняет только сам поставщик через accept/decline."
        ),
        request=OrderItemSetStatusSerializer,
        responses={
            200: OrderItemSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            400: OpenApiResponse(
                description='`status=confirmed` без даты поставки: {"confirmed_delivery_date": "Укажите дату '
                'поставки, чтобы принять заявку."}; либо невалидные поля запроса.'
            ),
            404: OpenApiResponse(description='Заведения, заказа или позиции с таким id нет: {"detail": "Not found."}'),
            409: OpenApiResponse(
                description="Состояние не позволяет менять статус, `detail` — одна из причин: "
                '"This item is not assigned to an off-system supplier." (позиция не у поставщика вне системы); '
                '"Order awaits approval and dispatch." (заказ ждёт согласования); '
                '"Позиция отменена заведением или заменена, её статус больше нельзя менять."; '
                '"Заказ завершён, статусы его позиций больше нельзя менять."'
            ),
        },
    ),
)
class VenueOrderItemSetStatusView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, order_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        item = get_object_or_404(OrderItem, pk=item_id, order=order)
        ser = OrderItemSetStatusSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ok, error = set_off_system_order_item_status(
            item=item,
            status=ser.validated_data["status"],
            confirmed_qty=ser.validated_data.get("confirmed_qty"),
            confirmed_delivery_date=ser.validated_data.get("confirmed_delivery_date"),
            comment=ser.validated_data.get("comment"),
        )
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        item.refresh_from_db()
        # 2026-09-14: refresh_from_db() выше сбрасывает закэшированный
        # item.order, а OrderItemSerializer.get_venue_name читает
        # obj.order.venue — без явного select_related здесь это было бы
        # 2 лишних запроса (order, затем venue) на каждый вызов.
        item.order = Order.objects.select_related("venue").get(pk=order.pk)
        return Response(OrderItemSerializer(item, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        summary="Найти замену позиции (невыполненный остаток)",
        description=(
            "2026-09-03, переименовано 2026-09-15 (raw ТЗ «ЛК Заведения» §18-19: пользователь не выбирает "
            "поставщика напрямую — выбирает новый товар в маркетплейсе, поставщик определяется товаром; "
            "старое название «Сменить поставщика» — тот же URL/эндпоинт, только терминология). Доступно "
            "только для позиции в статусе `cancelled` (поставщик отказался) либо `confirmed` с "
            "`confirmed_qty` меньше `requested_qty` (принято частично) — переотправляет именно невыполненный "
            "остаток по выбранному `supplier_product_id`. Создаёт в этом же заказе новую позицию со статусом "
            "`submitted` у поставщика этого товара (дальше проходит обычный цикл accept/decline, `replaces_id` "
            "указывает на исходную позицию, и у нового поставщика появляется собственная заявка — "
            "SupplierOrderRequest), исходную позицию переводит в `replaced`. Если заказ был уже "
            "`confirmed`/`cancelled`, из-за новой ожидающей позиции возвращает его в `submitted`."
        ),
        request=OrderItemChangeSupplierSerializer,
        responses={
            200: VenueOrderDetailSerializer,
            400: OpenApiResponse(
                description='Товар поставщика не найден/архивный: {"supplier_product_id": ["Invalid pk ... - '
                'object does not exist."]}'
            ),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения, заказа или позиции с таким id нет: {"detail": "Not found."}'),
            409: OpenApiResponse(
                description='Заказ уже завершён: {"detail": "Order is completed, cannot change supplier for its '
                'items."}, либо позиция не в подходящем статусе: {"detail": "Item is ..., can only change '
                'supplier for a declined or partially-confirmed item."}, либо тот же товар поставщика: '
                '{"detail": "New supplier product is the same as the current one."}'
            ),
        },
    ),
)
class VenueOrderItemChangeSupplierView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, order_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        item = get_object_or_404(OrderItem.objects.select_related("supplier_product"), pk=item_id, order=order)
        ser = OrderItemChangeSupplierSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_item, error = change_order_item_supplier(
            item=item, new_supplier_product=ser.validated_data["supplier_product"]
        )
        if new_item is None:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        order = get_object_or_404(venue_order_detail_queryset(venue_id), pk=order.pk)
        return Response(VenueOrderDetailSerializer(order, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        summary="Отменить позицию заказа (заведение)",
        description=(
            "Raw ТЗ «ЛК Заведения» §20-21: заведение отменяет ОДНУ ещё не обработанную поставщиком позицию "
            "(status=submitted) до его ответа. `reason` необязателен. Статус становится `cancelled_by_venue` "
            "— НЕ `cancelled` (тот означает отказ поставщика, §23) — и позиция НЕ попадает в «Неразобранные» "
            "(GET .../orders/unresolved-items), потому что потребность отменил сам пользователь."
        ),
        request=OrderItemCancelSerializer,
        responses={
            200: OrderItemSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения, заказа или позиции с таким id нет: {"detail": "Not found."}'),
            409: OpenApiResponse(
                description='Позиция уже не в статусе submitted (поставщик уже ответил): {"detail": "Item is '
                '..., can only cancel a pending item."}'
            ),
        },
    ),
)
class VenueOrderItemCancelView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, order_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        item = get_object_or_404(OrderItem, pk=item_id, order=order)
        ser = OrderItemCancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ok, error = cancel_order_item(item=item, reason=ser.validated_data.get("reason"), actor=request.user)
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        item.refresh_from_db()
        # 2026-09-14: тот же нюанс, что у VenueOrderItemSetStatusView —
        # refresh_from_db() сбрасывает закэшированный item.order.
        item.order = Order.objects.select_related("venue").get(pk=order.pk)
        return Response(OrderItemSerializer(item, context={"request": request}).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Заказы"],
        summary="Отменить заявку целиком (заведение)",
        description=(
            "Raw ТЗ §20/§22: заведение отменяет ВСЕ ещё не обработанные поставщиком позиции этой заявки "
            "(одного поставщика в этом заказе) одним вызовом, до его ответа. `reason` необязателен. Как и у "
            "отмены одной позиции — статус становится `cancelled_by_venue`, позиции не попадают в "
            "«Неразобранные». Другие поставщики того же заказа не затрагиваются."
        ),
        request=OrderItemCancelSerializer,
        responses={
            200: VenueOrderDetailSerializer,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(
                description='Заведения, заказа или заявки этому поставщику нет: {"detail": "Not found."}'
            ),
            409: OpenApiResponse(
                description='У этой заявки нет позиций в статусе submitted: {"detail": "No pending items in '
                'this supplier order request."}'
            ),
        },
    ),
)
class VenueSupplierOrderRequestCancelView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, order_id, supplier_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        supplier_request = get_object_or_404(SupplierOrderRequest, order=order, supplier_id=supplier_id)
        ser = OrderItemCancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ok, error = cancel_supplier_order_request(
            supplier_request=supplier_request, reason=ser.validated_data.get("reason"), actor=request.user
        )
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        order = get_object_or_404(venue_order_detail_queryset(venue_id), pk=order.pk)
        return Response(VenueOrderDetailSerializer(order, context={"request": request}).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Заказы"],
        summary="История заявки поставщику",
        description=(
            "Raw ТЗ §29 «История»: отправка поставщику, первое открытие поставщиком, подтверждение/"
            "частичное подтверждение/отказ поставщика, отмена заведением (всей заявки и/или отдельных "
            "позиций), завершение — в одной хронологии. Не зависит только от текущего статуса заявки: "
            "видна целиком, даже если заявка потом ещё несколько раз сменила статус."
        ),
        responses={
            200: AuditLogEntrySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(
                description='Заведения, заказа или заявки этому поставщику нет: {"detail": "Not found."}'
            ),
        },
    ),
)
class VenueSupplierOrderRequestAuditLogView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id, order_id, supplier_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        supplier_request = get_object_or_404(SupplierOrderRequest, order=order, supplier_id=supplier_id)
        return Response(AuditLogEntrySerializer(supplier_order_request_audit_log(supplier_request.id), many=True).data)


_VENUE_UNRESOLVED_ITEMS_PAGE_SCHEMA = inline_serializer(
    name="VenueUnresolvedOrderItemPage",
    fields={
        "count": s.IntegerField(help_text="Всего неразобранных позиций этого заведения"),
        "next": s.URLField(allow_null=True),
        "previous": s.URLField(allow_null=True),
        "results": OrderItemSerializer(many=True),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Заказы"],
        summary="Неразобранные позиции заведения",
        description=(
            "Raw ТЗ §3.5/§16-18 «Неразобранные»: позиции, которым нужна замена — поставщик полностью "
            "отказался (`status=cancelled`), либо подтвердил меньше запрошенного (`status=confirmed` с "
            "`confirmed_qty < requested_qty`; неудовлетворённый остаток = `requested_qty - confirmed_qty`). "
            "НЕ включает `cancelled_by_venue` (заведение отменило само, §23) и `replaced` (остаток уже "
            "переотправлен через «Найти замену», см. `replaces_id`). Это не подбор нового поставщика — "
            "пользователь сам ищет товар в маркетплейсе (`POST /cart/items` с новым `supplier_product_id`, "
            "либо `.../items/{item_id}/change-supplier`, если нужно сохранить связь с исходной позицией).\n\n"
            "2026-09-21: `q` (поиск по названию товара, подстрока) и `subdivision_id` (фильтр по "
            "подразделению) — тот же паттерн, что на GET /venues/{venue_id}/product-matrix.\n\n"
            "2026-09-28: `supplier_id`/`off_system_supplier_id` — фильтр «Все поставщики»; позиции, убранные "
            "через POST .../items/{item_id}/dismiss («Убрать из списка»), сюда больше не попадают. В строке "
            "есть `order_id`/`venue_id` для действий и popup, `order_comment` — комментарий покупателя. По "
            "всем заведениям пользователя — GET /venues/mine/orders/unresolved-items."
        ),
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, description="Номер страницы."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Размер страницы (макс. 100, по умолч. 20)."),
            OpenApiParameter("q", OpenApiTypes.STR, description="Поиск по названию товара (подстрока)."),
            OpenApiParameter(
                "subdivision_id",
                OpenApiTypes.UUID,
                description="Фильтр по подразделению этого заведения. Без фильтра — все неразобранные позиции.",
            ),
            OpenApiParameter(
                "supplier_id", OpenApiTypes.UUID, description="Фильтр «Все поставщики»: поставщик платформы."
            ),
            OpenApiParameter(
                "off_system_supplier_id", OpenApiTypes.UUID, description="Фильтр по поставщику вне системы."
            ),
        ],
        responses={
            200: _VENUE_UNRESOLVED_ITEMS_PAGE_SCHEMA,
            400: OpenApiResponse(description="Некорректные query-параметры (например, subdivision_id не UUID)."),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueUnresolvedOrderItemListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        query_ser = VenueUnresolvedOrderItemListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        queryset = venue_unresolved_order_items_queryset(
            venue_id,
            q=query_ser.validated_data.get("q"),
            subdivision_id=query_ser.validated_data.get("subdivision_id"),
            supplier_id=query_ser.validated_data.get("supplier_id"),
            off_system_supplier_id=query_ser.validated_data.get("off_system_supplier_id"),
        )
        paginator = _MasterProductPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(OrderItemSerializer(page, many=True, context={"request": request}).data)


_VENUE_UNRESOLVED_ITEMS_COUNT_SCHEMA = inline_serializer(
    name="VenueUnresolvedOrderItemCount",
    fields={
        "count": s.IntegerField(help_text="Всего неразобранных позиций этого заведения."),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Заказы"],
        summary="Счётчик неразобранных позиций заведения",
        description=(
            "2026-09-18, пользователь: нужна ручка получения кол-ва не разобранных заказов по id заведения — "
            "лёгкий счётчик для бейджа в интерфейсе, без похода за самим списком. Тот же критерий, что "
            "GET .../orders/unresolved-items (raw ТЗ §3.5/§16-18): поставщик полностью отказался "
            "(`status=cancelled`) либо подтвердил меньше запрошенного (`status=confirmed` с "
            "`confirmed_qty < requested_qty`). Считает позиции, не заказы — заказ с 3 неразобранными "
            "позициями даёт count=3, а не 1."
        ),
        responses={
            200: _VENUE_UNRESOLVED_ITEMS_COUNT_SCHEMA,
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="Не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueUnresolvedOrderItemCountView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        count = venue_unresolved_order_items_queryset(venue_id).count()
        return Response({"count": count})


_VENUE_MINE_UNRESOLVED_ITEMS_PAGE_SCHEMA = inline_serializer(
    name="VenueMineUnresolvedOrderItemPage",
    fields={
        "count": s.IntegerField(help_text="Всего неразобранных позиций по выбранным заведениям."),
        "next": s.URLField(allow_null=True, help_text="Ссылка на следующую страницу, null — последняя."),
        "previous": s.URLField(allow_null=True, help_text="Ссылка на предыдущую страницу, null — первая."),
        "results": OrderItemSerializer(many=True),
    },
)


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_mine_unresolved_order_items_list",
        tags=["Заказы"],
        summary="Неразобранные позиции по всем моим заведениям",
        description=(
            "2026-09-28, ТЗ панели «Неразобранные»: тот же критерий, что GET /venues/{venue_id}/orders/"
            "unresolved-items (отказ поставщика или подтверждено меньше запрошенного, без убранных через "
            "«Убрать из списка»), но по всем заведениям, где пользователь — активный участник. Фильтры: "
            "`venue_id` («Все заведения», мультивыбор), `supplier_id`/`off_system_supplier_id` («Все "
            "поставщики»), `q` (поиск по названию). Каждая строка несёт `venue_id`/`venue_name`, "
            "`subdivision_name`, `order_id`. Только JWT пользователя (сервис-токен не подходит — нет «моих» "
            "заведений)."
        ),
        parameters=[VenueMineUnresolvedOrderItemListQuerySerializer],
        responses={
            200: _VENUE_MINE_UNRESOLVED_ITEMS_PAGE_SCHEMA,
            400: OpenApiResponse(
                response=inline_serializer(
                    name="VenueMineUnresolvedOrderItemQueryError",
                    fields={
                        "venue_id": s.ListField(
                            child=s.CharField(), required=False, help_text="«Must be a valid UUID.»"
                        ),
                        "supplier_id": s.ListField(
                            child=s.CharField(), required=False, help_text="«Must be a valid UUID.»"
                        ),
                    },
                ),
                description="Некорректный query-параметр (не UUID).",
            ),
            401: OpenApiResponse(
                response=NotFoundDetailSerializer,
                description='Нет JWT, либо JWT невалиден/просрочен: {"detail": "..."}.',
            ),
            404: OpenApiResponse(
                response=NotFoundDetailSerializer,
                description='Страницы с таким номером нет: {"detail": "Invalid page."}.',
            ),
        },
    ),
)
class VenueMineUnresolvedOrderItemListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query_ser = VenueMineUnresolvedOrderItemListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        filters = query_ser.validated_data
        queryset = venue_mine_unresolved_order_items_queryset(
            user_id=request.user.id,
            venue_ids=filters.get("venue_id"),
            q=filters.get("q"),
            supplier_id=filters.get("supplier_id"),
            off_system_supplier_id=filters.get("off_system_supplier_id"),
        )
        paginator = _MasterProductPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(OrderItemSerializer(page, many=True, context={"request": request}).data)


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_mine_unresolved_order_items_count",
        tags=["Заказы"],
        summary="Счётчик неразобранных позиций по всем моим заведениям",
        description=(
            "2026-09-28: бейдж рядом с заголовком «Неразобранные» — тот же критерий и та же выборка "
            "заведений, что GET /venues/mine/orders/unresolved-items (без фильтров поиска/поставщика)."
        ),
        parameters=[VenueMineUnresolvedOrderItemCountQuerySerializer],
        responses={
            200: _VENUE_UNRESOLVED_ITEMS_COUNT_SCHEMA,
            400: OpenApiResponse(
                response=inline_serializer(
                    name="VenueMineUnresolvedOrderItemCountQueryError",
                    fields={"venue_id": s.ListField(child=s.CharField(), help_text="«Must be a valid UUID.»")},
                ),
                description="venue_id — не UUID.",
            ),
            401: OpenApiResponse(
                response=NotFoundDetailSerializer,
                description='Нет JWT, либо JWT невалиден/просрочен: {"detail": "..."}.',
            ),
        },
    ),
)
class VenueMineUnresolvedOrderItemCountView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query_ser = VenueMineUnresolvedOrderItemCountQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        count = venue_mine_unresolved_order_items_queryset(
            user_id=request.user.id, venue_ids=query_ser.validated_data.get("venue_id")
        ).count()
        return Response({"count": count})


@extend_schema_view(
    post=extend_schema(
        operation_id="venue_order_item_dismiss",
        tags=["Заказы"],
        summary="Убрать позицию из «Неразобранных»",
        description=(
            "2026-09-28, ТЗ панели «Неразобранные», кнопка «Убрать из списка»: навсегда убирает позицию из "
            "«Неразобранных» (списки и счётчики), ничего не удаляя и не меняя — статус, количества, ответ "
            "поставщика и заказ остаются как были. Вернуть нельзя. Тела запроса нет. «Ищем замену» — это "
            "существующий POST .../items/{item_id}/change-supplier."
        ),
        request=None,
        responses={
            200: OrderItemSerializer,
            401: OpenApiResponse(
                response=NotFoundDetailSerializer,
                description='Нет JWT/сервис-токена, либо JWT невалиден/просрочен: {"detail": "..."}.',
            ),
            403: OpenApiResponse(
                response=NotFoundDetailSerializer,
                description='Не сервис-токен/admin/участник этого заведения: {"detail": "Requires a service '
                'token, staff account, or membership of this venue."}.',
            ),
            404: OpenApiResponse(
                response=NotFoundDetailSerializer,
                description='Заведения, заказа или позиции с таким id нет: {"detail": "Not found."}.',
            ),
            409: OpenApiResponse(
                response=OrderItemDismissConflictSerializer,
                description="Позиция не в «Неразобранных» (ещё ждёт ответа, принята полностью, заменена или "
                "отменена заведением), либо уже убрана.",
            ),
        },
    ),
)
class VenueOrderItemDismissView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def post(self, request, venue_id, order_id, item_id):
        get_object_or_404(Venue, pk=venue_id)
        order = resolve_venue_order(Order.objects.filter(venue_id=venue_id), venue_id, order_id)
        item = get_object_or_404(OrderItem, pk=item_id, order=order)
        ok, error = dismiss_unresolved_order_item(item=item, actor=request.user)
        if not ok:
            return Response({"detail": error}, status=status.HTTP_409_CONFLICT)
        item.order = Order.objects.select_related("venue").get(pk=order.pk)
        return Response(OrderItemSerializer(item, context={"request": request}).data)
