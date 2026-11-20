from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.catalog.services import (
    accept_supplier_order_items,
    decline_supplier_order_items,
    mark_supplier_order_request_viewed,
)
from api.v1.permissions import HasServiceToken, IsCoreUser
from core.models import EntityStatus, OrderItem, OrderItemStatus, SupplierOrderRequest, UserRole, Venue

from .models import (
    NotificationActionLog,
    NotificationAudience,
    NotificationDeliveryStatus,
    NotificationDestination,
    NotificationInboxDelivery,
    NotificationInboxEvent,
    NotificationInboxRead,
    NotificationSupplierAction,
    NotificationSupplierActionType,
)
from .serializers import (
    NotificationBotConnectRequestSerializer,
    NotificationBotStatusQuerySerializer,
    NotificationBotStatusResponseSerializer,
    NotificationBotTestDeliveryRequestSerializer,
    NotificationBotTestDeliveryResponseSerializer,
    NotificationDeliveryReceiptRequestSerializer,
    NotificationDeliveryReceiptResponseSerializer,
    NotificationDeliveryReviewRequestSerializer,
    NotificationDeliveryReviewResponseSerializer,
    NotificationDestinationCreateSerializer,
    NotificationDestinationDataSerializer,
    NotificationDestinationPatchRequestSerializer,
    NotificationHistorySerializer,
    NotificationInboxListResponseSerializer,
    NotificationInboxQuerySerializer,
    NotificationInboxReadAllRequestSerializer,
    NotificationInboxReadAllResponseSerializer,
    NotificationInboxReadResponseSerializer,
    NotificationLinkTokenCreateRequestSerializer,
    NotificationLinkTokenDataSerializer,
    NotificationRuleDataSerializer,
    NotificationRuleUpdateRequestSerializer,
    NotificationRuleUpdateSerializer,
    NotificationSupplierActionRequestSerializer,
    NotificationSupplierActionResponseSerializer,
    NotificationSupplierRequestBotDataSerializer,
    NotificationTelegramMiniAppPartialRequestSerializer,
    NotificationTelegramMiniAppRequestSerializer,
)
from .services import (
    BUYER_EVENTS,
    SUPPLIER_EVENTS,
    accessible_venue_ids,
    confirm_delivery_receipts,
    connect_chat_from_bot,
    create_destination,
    create_link_token,
    get_daily_delivery_review,
    record_notification_action,
    update_destination_rules,
)
from .telegram_miniapp import (
    read_supplier_request_launch_token,
    telegram_user_is_chat_member,
    validate_telegram_init_data,
)


def destination_data(destination):
    """Собрать JSON-представление подключённого чата."""
    return {
        "id": str(destination.id),
        "audience": destination.audience,
        "supplier_id": str(destination.supplier_id) if destination.supplier_id else None,
        "channel": destination.channel,
        "destination_type": destination.destination_type,
        "chat_id": destination.chat_id,
        "title": destination.title,
        "created_at": destination.created_at,
        "connected_user_id": destination.connected_user_id,
        "connected_username": destination.connected_username,
        "connected_user_name": destination.connected_user_name,
        "is_active": destination.is_active,
        "venue_ids": [str(row.venue_id) for row in destination.venue_scopes.all()],
        "rules": [
            {
                "event": rule.event,
                "is_enabled": rule.is_enabled,
                "send_time": rule.send_time.strftime("%H:%M"),
            }
            for rule in destination.rules.all().order_by("event")
        ],
    }


def require_audience_role(user, audience):
    """Проверить, может ли пользователь настраивать выбранную аудиторию."""
    expected = UserRole.VENUE_USER if audience == NotificationAudience.BUYER else UserRole.SUPPLIER
    return user.role in (expected, UserRole.ADMIN)


class NotificationDestinationListView(APIView):
    """Управлять списком подключённых чатов уведомлений пользователя."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_list_destinations",
        summary="Получить подключённые чаты",
        description="Требуется JWT пользователя АвтоСнаб в заголовке `Authorization: Bearer <JWT>`.",
        parameters=[
            OpenApiParameter(
                name="audience",
                type=str,
                required=False,
                enum=[NotificationAudience.BUYER, NotificationAudience.SUPPLIER],
                description="Показывать чаты только заведений (`buyer`) или поставщика (`supplier`).",
            )
        ],
        responses={
            200: NotificationDestinationDataSerializer(many=True),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def get(self, request):
        rows = NotificationDestination.objects.filter(created_by=request.user).prefetch_related("venue_scopes", "rules")
        audience = request.query_params.get("audience")
        if audience in NotificationAudience.values:
            rows = rows.filter(audience=audience)
        return Response([destination_data(row) for row in rows.order_by("created_at")])

    @extend_schema(
        operation_id="notifications_create_group_destination",
        summary="Подключить групповой чат вручную",
        description=(
            "Требуется JWT пользователя АвтоСнаб. Этот endpoint принимает только групповые чаты. "
            "Личный чат подключается через одноразовый токен: сначала `POST /link-tokens`, затем бот "
            "вызывает `POST /bot/connect`."
        ),
        request=NotificationDestinationCreateSerializer,
        responses={
            201: NotificationDestinationDataSerializer,
            400: OpenApiResponse(description="Некорректные данные или недоступное заведение/поставщик."),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            403: OpenApiResponse(description="У пользователя нет доступа к выбранной роли."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def post(self, request):
        serializer = NotificationDestinationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if not require_audience_role(request.user, values["audience"]):
            return Response({"detail": "У этой учётной записи нет доступа к выбранной роли."}, status=403)
        destination = create_destination(user=request.user, values=values)
        return Response(destination_data(destination), status=status.HTTP_201_CREATED)


class NotificationDestinationVerifyView(APIView):
    """Обновить список подключений и записать проверку чатов в историю."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_verify_destinations",
        summary="Проверить подключённые чаты",
        parameters=[OpenApiParameter(name="audience", type=str, required=False)],
        request=None,
        responses={200: NotificationDestinationDataSerializer(many=True)},
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def post(self, request):
        rows = NotificationDestination.objects.filter(created_by=request.user).prefetch_related("venue_scopes", "rules")
        audience = request.query_params.get("audience")
        if audience in NotificationAudience.values:
            rows = rows.filter(audience=audience)
        rows = list(rows.order_by("created_at"))
        for destination in rows:
            record_notification_action(
                destination=destination,
                action="chat_checked",
                actor_id=str(request.user.id),
                actor_name=request.user.full_name,
                details={"is_active": destination.is_active},
            )
        return Response([destination_data(row) for row in rows])


class NotificationDestinationDetailView(APIView):
    """Получать, изменять и отключать один чат уведомлений."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get_object(self, request, destination_id):
        return get_object_or_404(
            NotificationDestination.objects.prefetch_related("venue_scopes", "rules"),
            pk=destination_id,
            created_by=request.user,
        )

    @extend_schema(
        operation_id="notifications_get_destination",
        summary="Получить настройки чата",
        description="Доступно только пользователю, который подключил чат.",
        responses={
            200: NotificationDestinationDataSerializer,
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            404: OpenApiResponse(description="Чат не найден или принадлежит другому пользователю."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def get(self, request, destination_id):
        return Response(destination_data(self.get_object(request, destination_id)))

    @extend_schema(
        operation_id="notifications_update_destination",
        summary="Изменить подключённый чат",
        description="Можно изменить название, активность и доступные заведению области уведомлений.",
        request=NotificationDestinationPatchRequestSerializer,
        responses={
            200: NotificationDestinationDataSerializer,
            400: OpenApiResponse(description="Некорректные поля или недоступное заведение."),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            404: OpenApiResponse(description="Чат не найден или принадлежит другому пользователю."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def patch(self, request, destination_id):
        destination = self.get_object(request, destination_id)
        if "title" in request.data:
            title = str(request.data["title"]).strip()
            if len(title) > 255:
                return Response({"title": "Не более 255 символов."}, status=400)
            destination.title = title
        if "is_active" in request.data:
            if not isinstance(request.data["is_active"], bool):
                return Response({"is_active": "Укажите true или false."}, status=400)
            destination.is_active = request.data["is_active"]
        if "venue_ids" in request.data:
            if destination.audience != NotificationAudience.BUYER:
                return Response({"venue_ids": "Уведомления поставщика не привязаны к заведениям."}, status=400)
            try:
                venue_ids = {str(value) for value in request.data["venue_ids"]}
            except TypeError:
                return Response({"venue_ids": "Ожидается список ID заведений."}, status=400)
            allowed_ids = {str(value) for value in accessible_venue_ids(request.user)}
            if not venue_ids or not venue_ids.issubset(allowed_ids):
                return Response({"venue_ids": "Выберите доступные заведения."}, status=400)
            selected = Venue.objects.filter(pk__in=venue_ids, status=EntityStatus.ACTIVE)
            destination.venues.set(selected)
        destination.save()
        record_notification_action(
            destination=destination,
            action=(
                ("destination_enabled" if destination.is_active else "destination_disabled")
                if set(request.data.keys()) == {"is_active"}
                else "destination_updated"
            ),
            actor_id=str(request.user.id),
            actor_name=request.user.full_name,
            details={"fields": sorted(request.data.keys())},
        )
        destination.refresh_from_db()
        return Response(destination_data(destination))

    @extend_schema(
        operation_id="notifications_delete_destination",
        summary="Отключить чат",
        responses={
            204: OpenApiResponse(description="Чат удалён."),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            404: OpenApiResponse(description="Чат не найден или принадлежит другому пользователю."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def delete(self, request, destination_id):
        destination = self.get_object(request, destination_id)
        record_notification_action(
            destination=destination,
            action="destination_deleted",
            actor_id=str(request.user.id),
            actor_name=request.user.full_name,
            details={"destination_type": destination.destination_type},
        )
        destination.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationRulesView(APIView):
    """Получать и сохранять правила отправки для чата."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_get_destination_rules",
        summary="Получить правила уведомлений",
        responses={
            200: NotificationRuleDataSerializer(many=True),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            404: OpenApiResponse(description="Чат не найден или принадлежит другому пользователю."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def get(self, request, destination_id):
        destination = get_object_or_404(NotificationDestination, pk=destination_id, created_by=request.user)
        return Response(destination_data(destination)["rules"])

    @extend_schema(
        operation_id="notifications_update_destination_rules",
        summary="Изменить правила уведомлений",
        request=NotificationRuleUpdateRequestSerializer,
        responses={
            200: NotificationRuleDataSerializer(many=True),
            400: OpenApiResponse(description="Неизвестный тип события или некорректные настройки."),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            404: OpenApiResponse(description="Чат не найден или принадлежит другому пользователю."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def put(self, request, destination_id):
        destination = get_object_or_404(NotificationDestination, pk=destination_id, created_by=request.user)
        serializer = NotificationRuleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        update_destination_rules(destination=destination, rows=serializer.validated_data["rules"])
        destination.refresh_from_db()
        return Response(destination_data(destination)["rules"])


class NotificationLinkTokenView(APIView):
    """Выдавать одноразовые ссылки подключения Telegram и MAX."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_create_link_token",
        summary="Создать одноразовую ссылку подключения",
        description=(
            "Для покупателя user должен иметь доступ к заведению. Для поставщика передайте `supplier_id`. "
            "Токен передаётся боту в параметре запуска, а бот завершает привязку через `/bot/connect`."
        ),
        request=NotificationLinkTokenCreateRequestSerializer,
        responses={
            201: NotificationLinkTokenDataSerializer,
            400: OpenApiResponse(description="Недостаточно обязательных полей или некорректный тип чата."),
            401: OpenApiResponse(description="JWT отсутствует, просрочен или недействителен."),
            403: OpenApiResponse(description="У пользователя нет доступа к этому поставщику/заведению."),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def post(self, request):
        audience = request.data.get("audience")
        channel = request.data.get("channel")
        destination_type = request.data.get("destination_type", "personal")
        supplier_id = request.data.get("supplier_id")
        if audience not in NotificationAudience.values:
            return Response({"audience": "Выберите buyer или supplier."}, status=400)
        if channel not in ("telegram", "max"):
            return Response({"channel": "Выберите telegram или max."}, status=400)
        if destination_type not in ("personal", "group"):
            return Response({"destination_type": "Выберите личный или групповой чат."}, status=400)
        if audience == NotificationAudience.SUPPLIER:
            try:
                supplier_id = UUID(str(supplier_id))
            except (TypeError, ValueError):
                return Response({"supplier_id": "Укажите корректный ID компании-поставщика."}, status=400)
        if not require_audience_role(request.user, audience):
            return Response({"detail": "У этой учётной записи нет доступа к выбранной роли."}, status=403)
        token = create_link_token(
            user=request.user,
            audience=audience,
            channel=channel,
            destination_type=destination_type,
            supplier_id=supplier_id,
        )
        return Response(
            {
                "token": token.token,
                "channel": token.channel,
                "destination_type": token.destination_type,
                "expires_at": token.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


class NotificationBotConnectView(APIView):
    """Принимать от бота фактический ID подключаемого чата."""

    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="notifications_connect_chat_from_bot",
        summary="Завершить подключение чата из бота",
        description=(
            "Требуется `SERVICE_API_TOKEN`: `Authorization: Bearer <SERVICE_API_TOKEN>` или заголовок "
            "`X-Service-Token`. Бот вызывает endpoint после получения реального chat ID и одноразового токена."
        ),
        request=NotificationBotConnectRequestSerializer,
        responses={
            200: NotificationDestinationDataSerializer,
            201: NotificationDestinationDataSerializer,
            400: OpenApiResponse(description="Токен истёк/использован или поля подключения некорректны."),
            403: OpenApiResponse(description="SERVICE_API_TOKEN отсутствует или неверен."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    def post(self, request):
        """Проверить токен и завершить подключение чата."""
        serializer = NotificationBotConnectRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            destination = connect_chat_from_bot(
                channel=values["channel"],
                chat_id=values["chat_id"],
                token=values["token"],
                destination_type=values["destination_type"],
                title=values.get("title", ""),
                user_id=values.get("user_id", ""),
                username=values.get("username", ""),
                display_name=values.get("display_name", ""),
            )
        except ValidationError as exc:
            detail = exc.detail
            if isinstance(detail, dict) and "destination_type" in detail:
                code = "wrong_chat_type"
            elif isinstance(detail, dict) and "chat_id" in detail:
                code = "already_connected"
            else:
                code = "link_invalid"
            return Response({"code": code, "detail": detail}, status=status.HTTP_400_BAD_REQUEST)
        payload = destination_data(destination)
        if getattr(destination, "_notification_reused", False):
            payload["reused"] = True
            return Response(payload, status=status.HTTP_200_OK)
        return Response(payload, status=status.HTTP_201_CREATED)


class NotificationBotStatusView(APIView):
    """Вернуть боту состояние подключения конкретного чата."""

    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="notifications_bot_status",
        summary="Проверить подключение чата из бота",
        parameters=[
            OpenApiParameter(name="channel", type=str, required=True),
            OpenApiParameter(name="chat_id", type=str, required=True),
        ],
        responses={
            200: NotificationBotStatusResponseSerializer,
            400: OpenApiResponse(description="Канал или ID чата не указаны."),
            403: OpenApiResponse(description="SERVICE_API_TOKEN отсутствует или неверен."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    def get(self, request):
        """Найти подключение по каналу и Chat ID."""
        serializer = NotificationBotStatusQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        destination = (
            NotificationDestination.objects.filter(
                channel=values["channel"],
                chat_id=values["chat_id"],
            )
            .prefetch_related("rules", "venue_scopes")
            .first()
        )
        if destination is None:
            payload = {
                "connected": False,
                "status": "not_connected",
                "destination": None,
                "available_events": [],
            }
        else:
            events = BUYER_EVENTS if destination.audience == NotificationAudience.BUYER else SUPPLIER_EVENTS
            payload = {
                "connected": bool(destination.is_active),
                "status": "connected" if destination.is_active else "disabled",
                "destination": destination_data(destination),
                "available_events": list(events),
            }
            if destination.is_active:
                record_notification_action(
                    destination=destination,
                    action="status_checked",
                    details={"source": "bot"},
                )
        return Response(NotificationBotStatusResponseSerializer(payload).data)


class NotificationBotTestDeliveryView(APIView):
    """Проверить подключённый чат перед отправкой тестового сообщения."""

    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="notifications_bot_test_delivery",
        summary="Проверить тестовую доставку",
        request=NotificationBotTestDeliveryRequestSerializer,
        responses={
            200: NotificationBotTestDeliveryResponseSerializer,
            400: OpenApiResponse(description="Канал или ID чата не указаны."),
            403: OpenApiResponse(description="SERVICE_API_TOKEN отсутствует или неверен."),
            404: OpenApiResponse(description="Чат не подключён."),
            409: OpenApiResponse(description="Подключение отключено."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    def post(self, request):
        """Проверить активное подключение и записать тестовую доставку."""
        serializer = NotificationBotTestDeliveryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        destination = NotificationDestination.objects.filter(
            channel=values["channel"],
            chat_id=values["chat_id"],
        ).first()
        if destination is None:
            return Response({"detail": "Чат не подключён.", "code": "not_connected"}, status=status.HTTP_404_NOT_FOUND)
        if not destination.is_active:
            return Response({"detail": "Подключение отключено.", "code": "disabled"}, status=status.HTTP_409_CONFLICT)
        record_notification_action(
            destination=destination,
            action="test_delivery",
            details={"source": "bot"},
        )
        return Response(
            NotificationBotTestDeliveryResponseSerializer(
                {"ok": True, "message": "Чат готов принять тестовое сообщение."}
            ).data
        )


def _notification_inbox_event_data(notification):
    """Собрать событие ленты, персональное прочтение и состояние каналов."""
    read_state = next(iter(notification.current_user_reads), None)
    delivery_rows = {}
    for link in notification.delivery_links.all():
        delivery = link.delivery
        delivery_rows.setdefault(delivery.destination.channel, []).append(delivery)

    delivery_data = []
    for channel, deliveries in delivery_rows.items():
        statuses = {delivery.status for delivery in deliveries}
        if NotificationDeliveryStatus.FAILED in statuses:
            status_value = NotificationDeliveryStatus.FAILED
            status_message = "Не удалось доставить. Проверьте подключение."
        elif NotificationDeliveryStatus.PROCESSING in statuses:
            status_value = NotificationDeliveryStatus.PROCESSING
            status_message = "Отправляем сообщение."
        elif NotificationDeliveryStatus.PENDING in statuses:
            status_value = NotificationDeliveryStatus.PENDING
            status_message = (
                "Повторяем отправку." if any(delivery.attempts for delivery in deliveries) else "Ожидает отправки."
            )
        else:
            status_value = NotificationDeliveryStatus.SENT
            status_message = "Отправлено."
        sent_at_values = [delivery.sent_at for delivery in deliveries if delivery.sent_at]
        delivery_data.append(
            {
                "channel": channel,
                "status": status_value,
                "attempts": max(delivery.attempts for delivery in deliveries),
                "sent_at": max(sent_at_values)
                if sent_at_values and status_value == NotificationDeliveryStatus.SENT
                else None,
                "status_message": status_message,
            }
        )

    return {
        "id": notification.id,
        "event_type": notification.event_type,
        "title": notification.get_event_type_display(),
        "created_at": notification.created_at,
        "venue_id": notification.venue_id,
        "venue_name": notification.venue.display_name,
        "payload": notification.payload,
        "object_type": notification.object_type,
        "object_id": notification.object_id,
        "is_read": read_state is not None,
        "read_at": read_state.read_at if read_state else None,
        "deliveries": delivery_data,
    }


class NotificationInboxView(APIView):
    """Возвращает сотруднику ленту бизнес-событий доступных заведений."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_inbox",
        summary="Лента уведомлений заведений",
        parameters=[NotificationInboxQuerySerializer],
        responses={200: NotificationInboxListResponseSerializer},
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def get(self, request):
        """Вернуть отфильтрованную страницу событий и счётчик непрочитанных."""
        query = NotificationInboxQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data
        venue_ids = accessible_venue_ids(request.user)
        if "venue_id" in filters:
            if filters["venue_id"] not in venue_ids:
                raise ValidationError({"venue_id": "У вас нет доступа к этому заведению."})
            venue_ids = {filters["venue_id"]}

        base = NotificationInboxEvent.objects.filter(venue_id__in=venue_ids)
        unread_count = base.exclude(read_states__user=request.user).distinct().count()
        queryset = base.select_related("venue").prefetch_related(
            Prefetch(
                "read_states",
                queryset=NotificationInboxRead.objects.filter(user=request.user),
                to_attr="current_user_reads",
            ),
            Prefetch(
                "delivery_links",
                queryset=NotificationInboxDelivery.objects.select_related("delivery__destination"),
            ),
        )
        if filters.get("channel"):
            queryset = queryset.filter(deliveries__destination__channel=filters["channel"]).distinct()
        if "unread" in request.query_params:
            if filters["unread"]:
                queryset = queryset.exclude(read_states__user=request.user)
            else:
                queryset = queryset.filter(read_states__user=request.user)
        count = queryset.count()
        notifications = list(
            queryset.order_by("-created_at", "-id")[filters["offset"] : filters["offset"] + filters["limit"]]
        )
        data = [_notification_inbox_event_data(item) for item in notifications]
        return Response(
            NotificationInboxListResponseSerializer(
                {"results": data, "count": count, "unread_count": unread_count}
            ).data
        )


class NotificationInboxReadView(APIView):
    """Отмечает одно событие прочитанным только для текущего сотрудника."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_inbox_read",
        summary="Отметить уведомление прочитанным",
        request=None,
        responses={
            200: NotificationInboxReadResponseSerializer,
            404: OpenApiResponse(description="Уведомление не найдено"),
        },
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def post(self, request, notification_id):
        """Отметить событие как прочитанное, если оно доступно пользователю."""
        notification = get_object_or_404(
            NotificationInboxEvent.objects.filter(venue_id__in=accessible_venue_ids(request.user)),
            pk=notification_id,
        )
        read_state, _ = NotificationInboxRead.objects.get_or_create(
            notification=notification,
            user=request.user,
        )
        return Response(
            NotificationInboxReadResponseSerializer(
                {"id": notification.id, "is_read": True, "read_at": read_state.read_at}
            ).data
        )


class NotificationInboxReadAllView(APIView):
    """Отмечает доступные события прочитанными для текущего сотрудника."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_inbox_read_all",
        summary="Отметить все уведомления прочитанными",
        request=NotificationInboxReadAllRequestSerializer,
        responses={200: NotificationInboxReadAllResponseSerializer},
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    @transaction.atomic
    def post(self, request):
        """Отметить все непрочитанные события в доступных заведениях."""
        body = NotificationInboxReadAllRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        venue_ids = accessible_venue_ids(request.user)
        if body.validated_data.get("venue_id"):
            venue_id = body.validated_data["venue_id"]
            if venue_id not in venue_ids:
                raise ValidationError({"venue_id": "У вас нет доступа к этому заведению."})
            venue_ids = {venue_id}

        unread = NotificationInboxEvent.objects.filter(venue_id__in=venue_ids).exclude(read_states__user=request.user)
        unread_ids = list(unread.values_list("id", flat=True))
        NotificationInboxRead.objects.bulk_create(
            [
                NotificationInboxRead(notification_id=notification_id, user=request.user)
                for notification_id in unread_ids
            ],
            ignore_conflicts=True,
        )
        return Response(
            NotificationInboxReadAllResponseSerializer({"marked_read": len(unread_ids), "unread_count": 0}).data
        )


class NotificationHistoryView(APIView):
    """Вернуть владельцу кабинета историю действий по его чатам уведомлений."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @extend_schema(
        operation_id="notifications_history",
        summary="История действий по уведомлениям",
        parameters=[
            OpenApiParameter(name="audience", type=str, required=False),
            OpenApiParameter(name="channel", type=str, required=False),
            OpenApiParameter(name="limit", type=int, required=False),
        ],
        responses={200: NotificationHistorySerializer(many=True)},
        auth=[{"CoreUserJWT": []}],
        tags=["Уведомления"],
    )
    def get(self, request):
        rows = NotificationActionLog.objects.filter(created_by=request.user).select_related("destination")
        audience = request.query_params.get("audience")
        channel = request.query_params.get("channel")
        if audience in NotificationAudience.values:
            rows = rows.filter(destination__audience=audience)
        if channel in ("telegram", "max"):
            rows = rows.filter(channel=channel)
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 100)
        except (TypeError, ValueError):
            limit = 100
        data = [
            {
                "id": row.id,
                "created_at": row.created_at,
                "action": row.action,
                "channel": row.channel,
                "destination_type": row.destination_type,
                "chat_id": row.chat_id,
                "actor_id": row.actor_id,
                "actor_name": row.actor_name,
                "details": row.details,
                "destination_id": row.destination_id,
            }
            for row in rows.order_by("-created_at")[:limit]
        ]
        return Response(NotificationHistorySerializer(instance=data, many=True).data)


class NotificationSupplierActionView(APIView):
    """Принимать ответ поставщика на заявку из чата."""

    permission_classes = [HasServiceToken]

    @transaction.atomic
    @extend_schema(
        operation_id="notifications_respond_to_supplier_request",
        summary="Передать ответ поставщика на заявку",
        description=(
            "Требуется `SERVICE_API_TOKEN`. Чат должен быть привязан к компании-поставщику. Для `accept` "
            "укажите дату поставки или `delivery_date_unknown=true`. Для `partial` дополнительно "
            "передаются количества по ID позиций."
        ),
        request=NotificationSupplierActionRequestSerializer,
        responses={
            200: NotificationSupplierActionResponseSerializer,
            400: OpenApiResponse(description="Некорректный ответ, дата поставки или количества."),
            403: OpenApiResponse(description="SERVICE_API_TOKEN неверен или чат не подключён к поставщику."),
            404: OpenApiResponse(description="Заявка поставщику не найдена."),
            409: OpenApiResponse(description="По заявке уже ответили или бизнес-операция невозможна."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    def post(self, request, request_id):
        serializer = NotificationSupplierActionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        supplier_request = get_object_or_404(
            SupplierOrderRequest.objects.select_for_update().select_related("order"), pk=request_id
        )
        channel = data["channel"]
        chat_id = data["chat_id"].strip()
        if channel not in ("telegram", "max") or not chat_id:
            return Response({"detail": "Для ответа нужны channel и chat_id исходного чата."}, status=400)
        destination = NotificationDestination.objects.filter(
            audience=NotificationAudience.SUPPLIER,
            supplier_id=supplier_request.supplier_id,
            channel=channel,
            chat_id=chat_id,
            is_active=True,
        ).first()
        if destination is None:
            return Response({"detail": "Этот чат не подключён к компании-поставщику."}, status=403)
        if NotificationSupplierAction.objects.filter(supplier_request=supplier_request).exists():
            return Response({"detail": "По этой заявке уже ответили."}, status=409)
        action = data["action"]
        order = supplier_request.order
        supplier_id = supplier_request.supplier_id
        comment = str(data.get("comment") or "").strip()[:1000]
        if action == "decline":
            succeeded, error = decline_supplier_order_items(
                order=order, supplier_id=supplier_id, actor=None, comment=comment
            )
        elif action in ("accept", "partial"):
            delivery_date = data.get("confirmed_delivery_date")
            delivery_date_unknown = data.get("delivery_date_unknown", False)
            if not delivery_date and not delivery_date_unknown:
                return Response({"confirmed_delivery_date": "Укажите дату поставки."}, status=400)
            parsed_date = delivery_date
            quantities = data.get("item_quantities") or {}
            if not isinstance(quantities, dict):
                return Response({"item_quantities": "Ожидается объект с количеством по каждой позиции."}, status=400)
            normalized_quantities = {}
            if action == "partial":
                try:
                    normalized_quantities = {UUID(key): Decimal(str(value)) for key, value in quantities.items()}
                except (TypeError, ValueError, InvalidOperation):
                    return Response({"item_quantities": "Укажите корректный ID позиции и её количество."}, status=400)
                if not normalized_quantities:
                    return Response({"item_quantities": "Укажите количество хотя бы для одной позиции."}, status=400)
                if any(quantity < 0 for quantity in normalized_quantities.values()):
                    return Response({"item_quantities": "Количество не может быть отрицательным."}, status=400)
                pending_quantities = dict(
                    OrderItem.objects.filter(
                        supplier_request=supplier_request, status=OrderItemStatus.SUBMITTED
                    ).values_list("id", "requested_qty")
                )
                if not any(
                    item_id in pending_quantities and Decimal("0") < quantity < pending_quantities[item_id]
                    for item_id, quantity in normalized_quantities.items()
                ):
                    return Response(
                        {"item_quantities": "Для частичного ответа укажите количество меньше заказанного."},
                        status=400,
                    )
            succeeded, error = accept_supplier_order_items(
                order=order,
                supplier_id=supplier_id,
                confirmed_delivery_date=parsed_date,
                item_quantities=normalized_quantities,
                comment=comment,
                actor=None,
                delivery_date_unknown=delivery_date_unknown,
            )
        else:
            return Response({"action": "Выберите accept, partial или decline."}, status=400)
        if not succeeded:
            return Response({"detail": error}, status=409)
        mark_supplier_order_request_viewed(supplier_request, actor=None)
        NotificationSupplierAction.objects.create(
            supplier_request=supplier_request,
            action=NotificationSupplierActionType(action),
            response={**request.data, "comment": comment},
        )
        record_notification_action(
            destination=destination,
            action=f"supplier_{action}",
            actor_id=str(request.data.get("responded_by") or ""),
            actor_name=str(request.data.get("responded_by") or ""),
            details={"supplier_request_id": str(supplier_request.id)},
        )
        from .tasks import kick_remove_supplier_action_buttons

        labels = {"accept": "принята", "partial": "частично принята", "decline": "отклонена"}
        transaction.on_commit(lambda: kick_remove_supplier_action_buttons(str(supplier_request.id), labels[action]))
        supplier_request.refresh_from_db(fields=["status"])
        return Response({"ok": True, "request_id": str(supplier_request.id), "status": supplier_request.status})


class NotificationDeliveryReceiptView(APIView):
    """Принимать подтверждение поставки из чек-листа заведения."""

    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="notifications_confirm_delivery_receipt",
        summary="Подтвердить получение поставки из чек-листа",
        description=(
            "Требуется SERVICE_API_TOKEN. Чат должен быть подключён к заведению, а заявка — иметь "
            "подтверждённые поставщиком позиции на указанную дату. Повторное нажатие безопасно."
        ),
        request=NotificationDeliveryReceiptRequestSerializer,
        responses={
            200: NotificationDeliveryReceiptResponseSerializer,
            400: OpenApiResponse(description="Некорректная дата или параметры."),
            403: OpenApiResponse(description="Чат не подключён к заведению."),
            404: OpenApiResponse(description="Подтверждённых поставок на дату нет."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    @transaction.atomic
    def post(self, request):
        serializer = NotificationDeliveryReceiptRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = confirm_delivery_receipts(**data)
        except ValidationError as exc:
            detail = exc.detail
            if isinstance(detail, dict) and "chat_id" in detail:
                return Response(detail, status=403)
            return Response(detail, status=404)
        return Response({"ok": True, **result})


class NotificationDeliveryReviewView(APIView):
    """Открывать заявки ежедневного чек-листа перед фиксацией прихода."""

    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="notifications_review_delivery_checklist",
        summary="Открыть карточки поставок из чек-листа",
        description=(
            "Требуется SERVICE_API_TOKEN. Возвращает заявки для scope=all или выбранного поставщика. "
            "После просмотра результат фиксируется отдельным запросом подтверждения."
        ),
        parameters=[
            OpenApiParameter(name="channel", type=str, required=True, enum=["telegram", "max"]),
            OpenApiParameter(name="chat_id", type=str, required=True),
            OpenApiParameter(name="delivery_date", type=str, required=False),
            OpenApiParameter(name="scope", type=str, required=False, default="all"),
        ],
        responses={
            200: NotificationDeliveryReviewResponseSerializer,
            400: OpenApiResponse(description="Некорректные параметры чек-листа."),
            403: OpenApiResponse(description="Чат не подключён к заведению."),
            404: OpenApiResponse(description="Чек-лист за указанную дату не найден."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    def get(self, request):
        serializer = NotificationDeliveryReviewRequestSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        try:
            result = get_daily_delivery_review(**serializer.validated_data)
        except ValidationError as exc:
            detail = exc.detail
            if isinstance(detail, dict) and "chat_id" in detail:
                return Response(detail, status=403)
            return Response(detail, status=404)
        return Response(result)


class NotificationSupplierRequestBotDetailView(APIView):
    """Возвращать поставщику данные его заявки для Mini App."""

    """Expose only the pending lines needed for the supplier bot's partial reply."""

    permission_classes = [HasServiceToken]

    @extend_schema(
        operation_id="notifications_get_supplier_request_for_bot",
        summary="Получить позиции заявки для ответа в боте",
        description=(
            "Требуется `SERVICE_API_TOKEN`. Данные доступны только чату, подключённому к поставщику этой заявки."
        ),
        parameters=[
            OpenApiParameter(name="channel", type=str, required=True, enum=["telegram", "max"]),
            OpenApiParameter(name="chat_id", type=str, required=True),
        ],
        responses={
            200: NotificationSupplierRequestBotDataSerializer,
            400: OpenApiResponse(description="Не указаны channel или chat_id."),
            403: OpenApiResponse(description="Чат не подключён к поставщику этой заявки."),
            404: OpenApiResponse(description="Заявка не найдена."),
        },
        auth=[{"ServiceApiToken": []}, {"ServiceApiTokenHeader": []}],
        tags=["Уведомления"],
    )
    def get(self, request, request_id):
        supplier_request = get_object_or_404(
            SupplierOrderRequest.objects.select_related("order").prefetch_related("items__requested_unit"),
            pk=request_id,
        )
        channel = request.query_params.get("channel")
        chat_id = str(request.query_params.get("chat_id") or "").strip()
        if channel not in ("telegram", "max") or not chat_id:
            return Response({"detail": "Укажите channel и chat_id подключённого чата."}, status=400)
        if not NotificationDestination.objects.filter(
            audience=NotificationAudience.SUPPLIER,
            supplier_id=supplier_request.supplier_id,
            channel=channel,
            chat_id=chat_id,
            is_active=True,
        ).exists():
            return Response({"detail": "Этот чат не подключён к компании-поставщику."}, status=403)
        return Response(
            {
                "id": supplier_request.id,
                "order_number": supplier_request.order.order_number,
                "venue": supplier_request.order.venue.display_name,
                "items": [
                    {
                        "id": item.id,
                        "name": item.requested_name,
                        "quantity": str(item.requested_qty),
                        "unit": item.requested_unit.name if item.requested_unit_id else "",
                    }
                    for item in supplier_request.items.all()
                    if item.status == OrderItemStatus.SUBMITTED
                ],
            }
        )


class NotificationSupplierMiniAppView(APIView):
    """Проверять Telegram Mini App и сохранять частичный ответ поставщика."""

    authentication_classes = []
    permission_classes = []

    def _context(self, request, *, body=None):
        body = body if isinstance(body, dict) else {}
        init_data = request.headers.get("X-Telegram-Init-Data") or body.get("init_data", "")
        launch_token = request.query_params.get("launch_token") or body.get("launch_token", "")
        try:
            telegram = validate_telegram_init_data(init_data)
            context = read_supplier_request_launch_token(launch_token)
        except (ValueError, TypeError):
            return None
        try:
            supplier_request = SupplierOrderRequest.objects.select_related(
                "order", "order__venue", "order__delivery_address", "supplier"
            ).get(pk=context["request_id"], supplier_id=context["supplier_id"])
        except (SupplierOrderRequest.DoesNotExist, ValueError):
            return None
        destination = NotificationDestination.objects.filter(
            audience=NotificationAudience.SUPPLIER,
            supplier_id=supplier_request.supplier_id,
            channel="telegram",
            chat_id=context["chat_id"],
            is_active=True,
        ).first()
        if destination is None:
            return None
        user_id = telegram["user_id"]
        if destination.destination_type == "personal":
            authorized = destination.chat_id == str(user_id)
        else:
            authorized = telegram_user_is_chat_member(chat_id=destination.chat_id, user_id=user_id)
        if not authorized:
            return None
        return supplier_request, telegram

    @extend_schema(
        operation_id="notifications_supplier_miniapp_detail",
        summary="Получить заявку для Telegram Mini App",
        description="Telegram `initData` проверяется подписью бота; доступ ограничен связанным чатом поставщика.",
        parameters=[
            OpenApiParameter(name="launch_token", type=str, required=True),
            OpenApiParameter(name="X-Telegram-Init-Data", type=str, location=OpenApiParameter.HEADER, required=True),
        ],
        responses={
            200: NotificationTelegramMiniAppRequestSerializer,
            403: OpenApiResponse(description="Нет доступа."),
            409: OpenApiResponse(description="Заявка уже обработана или в ней нет ожидающих ответа позиций."),
        },
        auth=[],
        tags=["Уведомления"],
    )
    def get(self, request):
        context = self._context(request)
        if context is None:
            return Response({"detail": "Нет доступа к этой заявке."}, status=403)
        supplier_request, _telegram = context
        if NotificationSupplierAction.objects.filter(supplier_request=supplier_request).exists():
            return Response({"detail": "По этой заявке уже ответили."}, status=409)
        items = list(
            supplier_request.items.filter(status=OrderItemStatus.SUBMITTED)
            .select_related("requested_unit", "subdivision")
            .order_by("created_at", "id")
        )
        if not items:
            return Response({"detail": "В заявке нет позиций, ожидающих ответа."}, status=409)
        mark_supplier_order_request_viewed(supplier_request, actor=None)
        from decimal import Decimal

        request_total = sum(((item.applied_price or Decimal("0")) * item.requested_qty for item in items), Decimal("0"))
        has_complete_prices = all(item.applied_price is not None for item in items)
        minimum = supplier_request.supplier.delivery_min_order_amount
        address = supplier_request.order.delivery_address
        address_parts = [address.locality, address.street, address.apartment_office, address.address] if address else []
        delivery_address = ", ".join(dict.fromkeys(part.strip() for part in address_parts if part and part.strip()))
        return Response(
            {
                "id": supplier_request.id,
                "request_number": supplier_request.number,
                "order_number": supplier_request.order.order_number,
                "venue": supplier_request.order.venue.display_name,
                "delivery_address": delivery_address,
                "buyer_comment": supplier_request.order.comment or "",
                "total_amount": request_total if has_complete_prices else None,
                "currency": supplier_request.supplier.delivery_currency or "RUB",
                "minimum_order_amount": minimum,
                "minimum_shortfall": (
                    max(minimum - request_total, Decimal("0")) if minimum is not None and has_complete_prices else None
                ),
                "items": [
                    {
                        "id": item.id,
                        "name": item.requested_name,
                        "quantity": str(item.requested_qty),
                        "unit": item.requested_unit.name if item.requested_unit_id else "",
                        "unit_price": item.applied_price,
                        "line_total": (
                            item.applied_price * item.requested_qty if item.applied_price is not None else None
                        ),
                        "subdivision": (
                            item.subdivision.name
                            if item.subdivision_id
                            else supplier_request.order.subdivision.name
                            if supplier_request.order.subdivision_id
                            else ""
                        ),
                    }
                    for item in items
                ],
            }
        )

    @extend_schema(
        operation_id="notifications_supplier_miniapp_partial_response",
        summary="Частично подтвердить заявку из Telegram Mini App",
        description="Сохраняет количество, дату и необязательный комментарий через бизнес-сервис АвтоСнаба.",
        request=NotificationTelegramMiniAppPartialRequestSerializer,
        responses={
            200: NotificationSupplierActionResponseSerializer,
            400: OpenApiResponse(description="Некорректная дата, количество или комментарий."),
            403: OpenApiResponse(description="Нет доступа."),
            409: OpenApiResponse(description="Заявка уже обработана или список товаров изменился."),
        },
        auth=[],
        tags=["Уведомления"],
    )
    @transaction.atomic
    def post(self, request):
        payload = NotificationTelegramMiniAppPartialRequestSerializer(data=request.data)
        if not payload.is_valid():
            return Response({"detail": "Проверьте дату, количества и комментарий."}, status=400)
        data = payload.validated_data
        context = self._context(request, body=data)
        if context is None:
            return Response({"detail": "Нет доступа к этой заявке."}, status=403)
        supplier_request, telegram = context
        supplier_request = (
            SupplierOrderRequest.objects.select_for_update().select_related("order").get(pk=supplier_request.pk)
        )
        if NotificationSupplierAction.objects.filter(supplier_request=supplier_request).exists():
            return Response({"detail": "По этой заявке уже ответили."}, status=409)
        try:
            quantities = {UUID(str(key)): Decimal(str(value)) for key, value in data["item_quantities"].items()}
        except (TypeError, ValueError):
            return Response({"detail": "Проверьте дату и количество товаров."}, status=400)
        delivery_date = data["confirmed_delivery_date"]
        if any(not value.is_finite() or value < 0 for value in quantities.values()):
            return Response({"detail": "Количество не может быть отрицательным."}, status=400)
        pending = dict(
            supplier_request.items.filter(status=OrderItemStatus.SUBMITTED).values_list("id", "requested_qty")
        )
        if set(quantities) - set(pending):
            return Response({"detail": "В заявке изменился список товаров. Откройте её заново."}, status=409)
        if any(quantity > pending[item_id] for item_id, quantity in quantities.items()):
            return Response({"detail": "Количество не может быть больше заказанного."}, status=400)
        if not any(Decimal("0") < quantity < pending[item_id] for item_id, quantity in quantities.items()):
            return Response({"detail": "Уменьшите количество хотя бы одного товара."}, status=400)
        comment = data.get("comment", "").strip()
        succeeded, error = accept_supplier_order_items(
            order=supplier_request.order,
            supplier_id=supplier_request.supplier_id,
            confirmed_delivery_date=delivery_date,
            item_quantities=quantities,
            comment=comment,
            actor=None,
        )
        if not succeeded:
            return Response({"detail": error}, status=409)
        mark_supplier_order_request_viewed(supplier_request, actor=None)
        user = telegram["user"]
        responded_by = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        if not responded_by and user.get("username"):
            responded_by = f"@{user['username']}"
        response = {
            "action": "partial",
            "confirmed_delivery_date": delivery_date.isoformat(),
            "item_quantities": {str(key): str(value) for key, value in quantities.items()},
            "comment": comment,
            "responded_by": responded_by or "Участник чата",
        }
        NotificationSupplierAction.objects.create(
            supplier_request=supplier_request,
            action=NotificationSupplierActionType.PARTIAL,
            response=response,
        )
        from .tasks import kick_remove_supplier_action_buttons

        transaction.on_commit(lambda: kick_remove_supplier_action_buttons(str(supplier_request.id), "частично принята"))
        return Response({"ok": True, "request_id": str(supplier_request.id)})
