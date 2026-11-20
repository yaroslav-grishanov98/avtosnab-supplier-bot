"""Telegram bot endpoints: user binding, pending actions, bot login links."""

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import (
    CoreUserJWTAuthentication,
    OptionalCoreUserJWTAuthentication,
)
from api.v1.permissions import (
    CanInitiateAccountTransfer,
    IsCoreUser,
)
from api.v1.suppliers.bot_events import (
    ensure_supplier_notification_destination,
    send_supplier_bot_message,
)
from api.v1.suppliers.serializers import (
    BotPendingActionCreateSerializer,
    BotPendingActionSerializer,
    BotPendingActionUpdateSerializer,
    BotSupplierPriceUploadTextSerializer,
    BotUserBindingEnvelopeSerializer,
    BotUserBindingRegisterSerializer,
    BotUserBindingResolveQuerySerializer,
    BotUserBindingResolveSerializer,
    BotUserStateSerializer,
    BotUserStateUpdateSerializer,
    SupplierBotAccountConfirmSerializer,
    SupplierBotLoginConfirmResponseSerializer,
    SupplierPriceUploadResponseSerializer,
)
from api.v1.suppliers.services import (
    create_supplier_bot_link_token,
    create_supplier_price_upload_from_text,
    normalize_supplier_price_upload,
    start_supplier_price_upload_normalization,
)
from core.models import EntityStatus, User, UserRole
from notifications.telegram_miniapp import TelegramMiniAppError, validate_telegram_init_data
from suppliers.models import (
    BotPendingAction,
    BotUserBinding,
    BotUserState,
    SourceChannel,
    Supplier,
    SupplierBotLinkToken,
    SupplierUser,
    WorkflowEvent,
)

from ._shared import BOT_LOGIN_CONFIRM_DEDUP_WINDOW, _resolve_active_binding, logger

STATE_START = "start"


STATE_BOT_LINK_INVALID = "bot_link_invalid"


STATE_SUPPLIER_REGISTERED = "supplier_registered"


STATE_PENDING_PRICE_UPLOAD = "pending_price_upload"


STATE_PRICE_PROCESSING = "price_processing"


STATE_PRICE_PROCESSED = "price_processed"


STATE_ERROR = "error"


PENDING_REGISTRATION_STATUSES = ("pending_registration", "waiting_registration")


_BOT_REGISTER_ERROR_SCHEMA = inline_serializer(
    name="BotUserBindingRegisterError",
    fields={
        "success": s.BooleanField(default=False, help_text="Всегда false для этого ответа."),
        "code": s.CharField(help_text="Машиночитаемый код ошибки, например BOT_LINK_TOKEN_NOT_FOUND."),
        "state": s.CharField(help_text="Состояние диалога бота после ошибки."),
        "message": s.CharField(help_text="Человекочитаемое сообщение для показа пользователю бота."),
    },
)


_BOT_REGISTER_SUCCESS_SCHEMA = inline_serializer(
    name="BotUserBindingRegisterSuccess",
    fields={
        "success": s.BooleanField(default=True, help_text="Всегда true для этого ответа."),
        "state": s.CharField(help_text="Новое состояние диалога бота, обычно supplier_registered."),
        "binding": BotUserBindingResolveSerializer(help_text="Созданная/обновлённая привязка бота к поставщику."),
        "pending_action": BotPendingActionSerializer(
            allow_null=True, help_text="Отложенное действие (например, загрузка прайса), которое можно продолжить."
        ),
    },
)


_BOT_PENDING_ACTION_FOUND_SCHEMA = inline_serializer(
    name="BotPendingActionFound",
    fields={
        "exists": s.BooleanField(default=True, help_text="Всегда true для этого ответа."),
        "pending_action": BotPendingActionSerializer(help_text="Найденное отложенное действие бота."),
    },
)


_BOT_PENDING_ACTION_NOT_FOUND_SCHEMA = inline_serializer(
    name="BotPendingActionNotFound",
    fields={
        "exists": s.BooleanField(default=False, help_text="Всегда false для этого ответа."),
        "code": s.CharField(help_text="Машиночитаемый код, например BOT_PENDING_ACTION_NOT_FOUND."),
        "message": s.CharField(help_text="Человекочитаемое сообщение."),
    },
)


_BOT_PRICE_UPLOAD_TEXT_SUCCESS_SCHEMA = inline_serializer(
    name="BotSupplierPriceUploadTextSuccess",
    fields={
        "binding": BotUserBindingResolveSerializer(help_text="Привязка бота, от которой пришла загрузка."),
        "upload": SupplierPriceUploadResponseSerializer(help_text="Созданная загрузка прайса."),
        "normalization": s.DictField(
            help_text=(
                "Результат нормализации — форма зависит от run_async: {accepted, status} для "
                "асинхронного запуска (202), полная сводка (items_total/products_created/...) для "
                "синхронного (200)."
            )
        ),
        "status_url": s.CharField(help_text="Путь для опроса статуса загрузки: GET /suppliers/uploads/{upload_id}."),
    },
)


def _log_workflow_event(
    *,
    event_type,
    user_id=None,
    chat_id,
    supplier=None,
    upload_id=None,
    state_before=None,
    state_after=None,
    payload_json=None,
    error_message=None,
):
    WorkflowEvent.objects.create(
        event_type=event_type,
        user_id=user_id or None,
        chat_id=chat_id,
        supplier=supplier,
        upload_id=upload_id,
        state_before=state_before,
        state_after=state_after,
        payload_json=payload_json or {},
        error_message=error_message or None,
    )


def _get_current_state(*, platform, chat_id):
    bot_state = BotUserState.objects.filter(platform=platform, chat_id=chat_id).order_by("-updated_at").first()
    return bot_state.state if bot_state else STATE_START


@extend_schema_view(
    get=extend_schema(
        tags=["Бот"],
        summary="Получить привязку бота по chat_id или telegram_user_id",
        responses={200: BotUserBindingEnvelopeSerializer, 404: _BOT_PENDING_ACTION_NOT_FOUND_SCHEMA},
    ),
)
class BotUserBindingResolveView(APIView):
    def get(self, request):
        serializer = BotUserBindingResolveQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        binding = _resolve_active_binding(
            channel=data["platform"],
            chat_id=data.get("chat_id"),
            telegram_user_id=data.get("telegram_user_id"),
            # Account-level binding exists before the first company is
            # created. Endpoints that mutate company data still pass True.
            require_active_supplier=False,
        )
        if binding is None:
            return Response(
                {
                    "exists": False,
                    "code": "BOT_USER_BINDING_NOT_FOUND",
                    "message": "No BotUserBinding matches the given query.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            BotUserBindingEnvelopeSerializer(
                {
                    "exists": True,
                    "binding": binding,
                }
            ).data
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Bot"],
        summary="Accept a text price list from a linked bot user",
        description=(
            "Resolves the supplier by platform/chat_id, creates SupplierPriceUpload, and starts normalization. "
            "The shared post-commit pipeline exposes saved products through catalog API and queues Qdrant sync."
        ),
        request=BotSupplierPriceUploadTextSerializer,
        responses={
            200: _BOT_PRICE_UPLOAD_TEXT_SUCCESS_SCHEMA,
            202: _BOT_PRICE_UPLOAD_TEXT_SUCCESS_SCHEMA,
            404: _BOT_PENDING_ACTION_NOT_FOUND_SCHEMA,
        },
    ),
)
class BotSupplierPriceUploadTextView(APIView):
    def post(self, request):
        serializer = BotSupplierPriceUploadTextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        binding = _resolve_active_binding(
            channel=data["platform"],
            chat_id=data.get("chat_id"),
            telegram_user_id=data.get("telegram_user_id"),
            require_active_supplier=True,
        )
        if binding is None:
            return Response(
                {
                    "exists": False,
                    "code": "BOT_USER_BINDING_NOT_FOUND",
                    "message": "No active bot binding matches the given platform and user.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        state_before = _get_current_state(platform=data["platform"], chat_id=data["chat_id"])
        upload = create_supplier_price_upload_from_text(
            data={
                "supplier": binding.supplier,
                "source_channel": data["platform"],
                "text": data["text"],
                "telegram_message_id": data.get("message_id") or "",
            }
        )
        BotUserState.objects.update_or_create(
            platform=data["platform"],
            chat_id=data["chat_id"],
            defaults={
                "telegram_user_id": data.get("telegram_user_id") or None,
                "state": STATE_PRICE_PROCESSING,
                "payload": {"upload_id": str(upload.id), "binding_id": str(binding.id)},
            },
        )
        _log_workflow_event(
            event_type="bot_price_upload_received",
            user_id=data.get("telegram_user_id"),
            chat_id=data["chat_id"],
            supplier=binding.supplier,
            upload_id=upload.id,
            state_before=state_before,
            state_after=STATE_PRICE_PROCESSING,
            payload_json={"binding_id": str(binding.id), "message_id": data.get("message_id") or ""},
        )

        if data["run_async"]:
            normalization = start_supplier_price_upload_normalization(
                upload_id=upload.id,
                use_ai=data["use_ai"],
            )
            response_status = status.HTTP_202_ACCEPTED if normalization.get("accepted") else status.HTTP_200_OK
        else:
            normalization = normalize_supplier_price_upload(
                upload_id=upload.id,
                use_ai=data["use_ai"],
            )
            BotUserState.objects.update_or_create(
                platform=data["platform"],
                chat_id=data["chat_id"],
                defaults={
                    "telegram_user_id": data.get("telegram_user_id") or None,
                    "state": STATE_PRICE_PROCESSED,
                    "payload": {
                        "upload_id": str(upload.id),
                        "binding_id": str(binding.id),
                        "status": normalization.get("status"),
                    },
                },
            )
            response_status = status.HTTP_200_OK

        upload.refresh_from_db()
        return Response(
            {
                "binding": BotUserBindingResolveSerializer(binding).data,
                "upload": SupplierPriceUploadResponseSerializer(upload).data,
                "normalization": normalization,
                "status_url": f"/api/v1/suppliers/uploads/{upload.id}",
            },
            status=response_status,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Зарегистрировать поставщика по deep-link токену",
        description=(
            'Вызывается ботом на /start <token> — токен приходит из кнопки "Присоединиться к '
            'Telegram-боту" в ЛК поставщика (см. SupplierBotLinkView). Токен одноразовый.'
        ),
        request=BotUserBindingRegisterSerializer,
        responses={
            200: _BOT_REGISTER_SUCCESS_SCHEMA,
            404: _BOT_REGISTER_ERROR_SCHEMA,
            409: _BOT_REGISTER_ERROR_SCHEMA,
        },
    ),
)
class BotUserBindingRegisterView(APIView):
    def post(self, request):
        serializer = BotUserBindingRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        state_before = _get_current_state(platform=data["platform"], chat_id=data["chat_id"])

        with transaction.atomic():
            link_token = (
                SupplierBotLinkToken.objects.select_for_update(of=("self",))
                .select_related("supplier")
                .filter(token=data["link_token"])
                .first()
            )
            if link_token is None:
                error_code = "BOT_LINK_TOKEN_NOT_FOUND"
                error_message = "Bot link token was not found."
            elif link_token.used:
                error_code = "BOT_LINK_TOKEN_USED"
                error_message = "Bot link token was already used."
            elif link_token.expires_at <= timezone.now():
                error_code = "BOT_LINK_TOKEN_EXPIRED"
                error_message = "Bot link token has expired."
            else:
                error_code = None
                error_message = None

            if error_code is not None:
                BotUserState.objects.update_or_create(
                    platform=data["platform"],
                    chat_id=data["chat_id"],
                    defaults={
                        "telegram_user_id": data.get("telegram_user_id") or None,
                        "state": STATE_BOT_LINK_INVALID,
                        "payload": {"error_code": error_code},
                    },
                )
                _log_workflow_event(
                    event_type="supplier_registration_invalid_link_token",
                    user_id=data.get("telegram_user_id"),
                    chat_id=data["chat_id"],
                    state_before=state_before,
                    state_after=STATE_BOT_LINK_INVALID,
                    payload_json={"error_code": error_code},
                    error_message=error_message,
                )
                return Response(
                    {
                        "success": False,
                        "code": error_code,
                        "state": STATE_BOT_LINK_INVALID,
                        "message": "Ссылка недействительна или устарела. Вернитесь в личный кабинет и "
                        "нажмите «Присоединиться к Telegram-боту» ещё раз.",
                    },
                    status=status.HTTP_404_NOT_FOUND if link_token is None else status.HTTP_409_CONFLICT,
                )

            supplier = link_token.supplier
            if supplier.status != EntityStatus.ACTIVE:
                BotUserState.objects.update_or_create(
                    platform=data["platform"],
                    chat_id=data["chat_id"],
                    defaults={
                        "telegram_user_id": data.get("telegram_user_id") or None,
                        "state": STATE_ERROR,
                        "payload": {"supplier_id": str(supplier.id), "error_code": "SUPPLIER_INACTIVE"},
                    },
                )
                _log_workflow_event(
                    event_type="supplier_registration_inactive_supplier",
                    user_id=data.get("telegram_user_id"),
                    chat_id=data["chat_id"],
                    supplier=supplier,
                    state_before=state_before,
                    state_after=STATE_ERROR,
                    payload_json={"link_token_id": str(link_token.id), "supplier_status": supplier.status},
                    error_message="Supplier resolved via link token but is not active.",
                )
                return Response(
                    {
                        "success": False,
                        "code": "SUPPLIER_INACTIVE",
                        "state": STATE_ERROR,
                        "message": "Поставщик неактивен, свяжитесь с менеджером АвтоСнаб.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            binding, _ = BotUserBinding.objects.select_for_update().get_or_create(
                channel=data["platform"],
                chat_id=data["chat_id"],
                defaults={
                    "supplier": supplier,
                    "user": link_token.created_by,
                    "external_user_id": data.get("telegram_user_id") or None,
                    "username": data.get("username") or None,
                    "status": EntityStatus.ACTIVE,
                    "metadata": {
                        "first_name": data.get("first_name") or "",
                        "last_name": data.get("last_name") or "",
                        "registered_via": "bot_link_token",
                    },
                },
            )
            if binding.supplier_id != supplier.id:
                BotUserState.objects.update_or_create(
                    platform=data["platform"],
                    chat_id=data["chat_id"],
                    defaults={
                        "telegram_user_id": data.get("telegram_user_id") or None,
                        "state": STATE_ERROR,
                        "payload": {
                            "existing_supplier_id": str(binding.supplier_id),
                            "requested_supplier_id": str(supplier.id),
                            "error_code": "BOT_USER_BINDING_CONFLICT",
                        },
                    },
                )
                _log_workflow_event(
                    event_type="supplier_registration_binding_conflict",
                    user_id=data.get("telegram_user_id"),
                    chat_id=data["chat_id"],
                    supplier=supplier,
                    state_before=state_before,
                    state_after=STATE_ERROR,
                    payload_json={
                        "existing_supplier_id": str(binding.supplier_id),
                        "requested_supplier_id": str(supplier.id),
                    },
                    error_message="Bot chat is already linked to another supplier.",
                )
                return Response(
                    {
                        "success": False,
                        "code": "BOT_USER_BINDING_CONFLICT",
                        "state": STATE_ERROR,
                        "message": "Этот чат уже привязан к другой компании. Свяжитесь с менеджером АвтоСнаб.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            update_fields = []
            if binding.status != EntityStatus.ACTIVE:
                binding.status = EntityStatus.ACTIVE
                update_fields.append("status")
            if link_token.created_by_id and binding.user_id != link_token.created_by_id:
                # 2026-08-04: раньше user никогда не проставлялся (даже при
                # первом создании) — профиль поставщика ("Telegram —
                # заполняется автоматически") не мог его показать. Теперь
                # проставляется всегда: и при создании (defaults выше), и
                # при повторном /start тем же чатом — та же логика, что
                # у "перепривязка = последняя активная личность".
                binding.user = link_token.created_by
                update_fields.append("user")
            if update_fields:
                binding.save(update_fields=[*update_fields, "updated_at"])

            link_token.used = True
            link_token.save(update_fields=["used", "updated_at"])

            BotUserState.objects.update_or_create(
                platform=data["platform"],
                chat_id=data["chat_id"],
                defaults={
                    "telegram_user_id": data.get("telegram_user_id") or None,
                    "state": STATE_SUPPLIER_REGISTERED,
                    "payload": {"binding_id": str(binding.id), "supplier_id": str(binding.supplier_id)},
                },
            )
            pending_action = (
                BotPendingAction.objects.select_for_update()
                .filter(
                    platform=data["platform"],
                    chat_id=data["chat_id"],
                    action_type="upload_price",
                    status__in=PENDING_REGISTRATION_STATUSES,
                )
                .order_by("-created_at")
                .first()
            )
            if pending_action:
                pending_action.supplier = supplier
                pending_action.status = "ready_for_processing"
                pending_action.save(update_fields=["supplier", "status", "updated_at"])
            _log_workflow_event(
                event_type="supplier_registration_success",
                user_id=data.get("telegram_user_id"),
                chat_id=data["chat_id"],
                supplier=supplier,
                upload_id=pending_action.id if pending_action else None,
                state_before=state_before,
                state_after=STATE_SUPPLIER_REGISTERED,
                payload_json={"link_token_id": str(link_token.id), "binding_id": str(binding.id)},
            )

        return Response(
            {
                "success": True,
                "state": STATE_SUPPLIER_REGISTERED,
                "binding": BotUserBindingResolveSerializer(binding).data,
                "pending_action": BotPendingActionSerializer(pending_action).data if pending_action else None,
            }
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Бот"],
        summary="Найти ожидающее действие бота",
        parameters=[
            OpenApiParameter("platform", OpenApiTypes.STR, description="Канал бота: telegram / max."),
            OpenApiParameter("channel", OpenApiTypes.STR, description="Синоним platform, для совместимости."),
            OpenApiParameter("telegram_user_id", OpenApiTypes.STR, description="ID пользователя в Telegram/Max."),
            OpenApiParameter("chat_id", OpenApiTypes.STR, description="ID чата с ботом."),
            OpenApiParameter("action_type", OpenApiTypes.STR, description="Тип действия, по умолчанию upload_price."),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                description=(
                    "Статус(ы) действия через запятую, по умолчанию pending_registration. "
                    "Ищется первое совпадение среди перечисленных статусов."
                ),
            ),
        ],
        responses={200: _BOT_PENDING_ACTION_FOUND_SCHEMA, 404: _BOT_PENDING_ACTION_NOT_FOUND_SCHEMA},
    ),
    post=extend_schema(
        tags=["Бот"],
        summary="Создать ожидающее действие (upload_price и др.)",
        request=BotPendingActionCreateSerializer,
        responses={200: BotPendingActionSerializer, 201: BotPendingActionSerializer},
    ),
)
class BotPendingActionCreateView(APIView):
    def get(self, request):
        serializer = BotUserBindingResolveQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        action_type = request.query_params.get("action_type", "upload_price")
        action_status = request.query_params.get("status", "pending_registration")
        statuses = [item.strip() for item in action_status.split(",") if item.strip()]

        pending_action = (
            BotPendingAction.objects.filter(
                platform=data["platform"],
                chat_id=data.get("chat_id") or "",
                action_type=action_type,
                status__in=statuses,
            )
            .order_by("-created_at")
            .first()
        )
        if pending_action is None:
            return Response(
                {
                    "exists": False,
                    "code": "BOT_PENDING_ACTION_NOT_FOUND",
                    "message": "No BotPendingAction matches the given query.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"exists": True, "pending_action": BotPendingActionSerializer(pending_action).data})

    def post(self, request):
        serializer = BotPendingActionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        action_status = data.get("status") or "pending_registration"
        lookup = {
            "platform": data["platform"],
            "chat_id": data["chat_id"],
            "action_type": data["action_type"],
        }
        if data.get("file_id"):
            lookup["file_id"] = data["file_id"]
        elif data.get("message_id"):
            lookup["message_id"] = data["message_id"]

        state_before = _get_current_state(platform=data["platform"], chat_id=data["chat_id"])
        with transaction.atomic():
            pending_action = None
            if len(lookup) > 3:
                pending_action = (
                    BotPendingAction.objects.select_for_update()
                    .filter(
                        **lookup, status__in=("pending_registration", "waiting_registration", "ready_for_processing")
                    )
                    .order_by("-created_at")
                    .first()
                )
            if pending_action is None:
                pending_action = BotPendingAction.objects.create(
                    platform=data["platform"],
                    chat_id=data["chat_id"],
                    action_type=data["action_type"],
                    file_id=data.get("file_id") or None,
                    message_id=data.get("message_id") or None,
                    status=action_status,
                )
                response_status = status.HTTP_201_CREATED
            else:
                response_status = status.HTTP_200_OK

            pending_action.telegram_user_id = data.get("telegram_user_id") or None
            pending_action.file_name = data.get("file_name") or None
            pending_action.mime_type = data.get("mime_type") or None
            pending_action.status = action_status
            pending_action.raw_payload = data.get("raw_payload") or {}
            pending_action.expires_at = data.get("expires_at")
            pending_action.save(
                update_fields=[
                    "telegram_user_id",
                    "file_name",
                    "mime_type",
                    "status",
                    "raw_payload",
                    "expires_at",
                    "updated_at",
                ]
            )
            BotUserState.objects.update_or_create(
                platform=data["platform"],
                chat_id=data["chat_id"],
                defaults={
                    "telegram_user_id": data.get("telegram_user_id") or None,
                    "state": STATE_PENDING_PRICE_UPLOAD,
                    "payload": {"pending_action_id": str(pending_action.id), "file_id": pending_action.file_id},
                },
            )
            _log_workflow_event(
                event_type="pending_price_upload_saved",
                user_id=data.get("telegram_user_id"),
                chat_id=data["chat_id"],
                upload_id=pending_action.id,
                state_before=state_before,
                state_after=STATE_PENDING_PRICE_UPLOAD,
                payload_json={
                    "action_type": data["action_type"],
                    "file_id": pending_action.file_id,
                    "file_name": pending_action.file_name,
                    "status": pending_action.status,
                },
            )

        return Response(BotPendingActionSerializer(pending_action).data, status=response_status)


@extend_schema_view(
    patch=extend_schema(
        tags=["Бот"],
        summary="Обновить статус ожидающего действия",
        request=BotPendingActionUpdateSerializer,
        responses={200: BotPendingActionSerializer},
    ),
    delete=extend_schema(tags=["Бот"], summary="Удалить ожидающее действие", responses={204: None}),
)
class BotPendingActionDetailView(APIView):
    def patch(self, request, id):
        serializer = BotPendingActionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        pending_action = get_object_or_404(BotPendingAction, pk=id)
        state_before = _get_current_state(platform=pending_action.platform, chat_id=pending_action.chat_id)
        update_fields = ["updated_at"]
        if "status" in data and data["status"]:
            pending_action.status = data["status"]
            update_fields.append("status")
        if "supplier" in data:
            pending_action.supplier = data["supplier"]
            update_fields.append("supplier")
        if "raw_payload" in data:
            pending_action.raw_payload = data["raw_payload"] or {}
            update_fields.append("raw_payload")
        pending_action.save(update_fields=update_fields)

        state_after = {
            "price_processing": STATE_PRICE_PROCESSING,
            "price_processed": STATE_PRICE_PROCESSED,
            "error": STATE_ERROR,
        }.get(pending_action.status, state_before)
        if state_after != state_before:
            BotUserState.objects.update_or_create(
                platform=pending_action.platform,
                chat_id=pending_action.chat_id,
                defaults={
                    "telegram_user_id": pending_action.telegram_user_id,
                    "state": state_after,
                    "payload": {"pending_action_id": str(pending_action.id), "status": pending_action.status},
                },
            )

        _log_workflow_event(
            event_type="pending_price_upload_status_updated",
            user_id=pending_action.telegram_user_id,
            chat_id=pending_action.chat_id,
            supplier=pending_action.supplier,
            upload_id=pending_action.id,
            state_before=state_before,
            state_after=state_after,
            payload_json={"status": pending_action.status},
        )
        return Response(BotPendingActionSerializer(pending_action).data)

    def delete(self, request, id):
        pending_action = get_object_or_404(BotPendingAction, pk=id)
        state_before = _get_current_state(platform=pending_action.platform, chat_id=pending_action.chat_id)
        _log_workflow_event(
            event_type="pending_price_upload_deleted",
            user_id=pending_action.telegram_user_id,
            chat_id=pending_action.chat_id,
            supplier=pending_action.supplier,
            upload_id=pending_action.id,
            state_before=state_before,
            state_after=STATE_START,
            payload_json={"status": pending_action.status},
        )
        pending_action.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Бот"],
        summary="Получить текущее состояние бота для чата",
        responses={200: BotUserStateSerializer},
    ),
    put=extend_schema(
        tags=["Бот"],
        summary="Установить состояние бота",
        request=BotUserStateUpdateSerializer,
        responses={200: BotUserStateSerializer},
    ),
    delete=extend_schema(tags=["Бот"], summary="Сбросить состояние бота", responses={204: None}),
)
class BotUserStateCurrentView(APIView):
    def get(self, request):
        serializer = BotUserBindingResolveQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        bot_state = (
            BotUserState.objects.filter(
                platform=data["platform"],
                chat_id=data.get("chat_id") or "",
            )
            .order_by("-updated_at")
            .first()
        )
        if bot_state is None:
            return Response({"exists": False, "state": STATE_START, "bot_user_state": None})
        return Response(
            {
                "exists": True,
                "state": bot_state.state,
                "bot_user_state": BotUserStateSerializer(bot_state).data,
            }
        )

    def put(self, request):
        serializer = BotUserStateUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(
                "bot_user_state.update.invalid payload=%s errors=%s",
                request.data,
                serializer.errors,
            )
            serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        bot_state, _ = BotUserState.objects.update_or_create(
            platform=data["platform"],
            chat_id=data["chat_id"],
            defaults={
                "telegram_user_id": data.get("telegram_user_id") or None,
                "state": data["state"],
                "payload": data.get("payload") or {},
            },
        )
        return Response(BotUserStateSerializer(bot_state).data)

    def delete(self, request):
        serializer = BotUserBindingResolveQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        BotUserState.objects.filter(
            platform=data["platform"],
            chat_id=data.get("chat_id") or "",
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Получить ссылку для присоединения к Telegram-боту",
        description=(
            "Генерирует одноразовый токен и deep-link на бота "
            '(https://t.me/<бот>?start=<token>) — кнопка "Присоединиться к Telegram-боту" в ЛК. '
            "Открытие ссылки автоматически привязывает Telegram-аккаунт пользователя к этой компании "
            "(см. BotUserBindingRegisterView). Ссылка одноразовая, TTL — SUPPLIER_BOT_LINK_TTL_MINUTES "
            "(по умолчанию 30 минут). Требует сервис-токен, admin или активного участника (owner или "
            "manager) этого поставщика."
        ),
        request=None,
        responses={
            201: inline_serializer(
                name="SupplierBotLinkResponse",
                fields={
                    "token": s.CharField(help_text="Сырой токен (совпадает с ?start= в deep_link)."),
                    "deep_link": s.CharField(help_text="Готовая ссылка на Telegram-бота для кнопки в ЛК."),
                    "expires_at": s.DateTimeField(help_text="Когда ссылка перестанет действовать."),
                },
            )
        },
    ),
)
class SupplierBotLinkView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id):
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        created_by = request.user if isinstance(request.user, User) else None
        result = create_supplier_bot_link_token(supplier=supplier, created_by=created_by)
        return Response(result, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Привязать Telegram к аккаунту поставщика после веб-входа",
        description=(
            "Вызывается Telegram Mini App сразу после входа или регистрации. "
            "CoreUser JWT определяет аккаунт, а подписанный Telegram initData — пользователя и private chat. "
            "Работает до создания первой компании: supplier_id в привязке в этом случае равен null."
        ),
        request=SupplierBotAccountConfirmSerializer,
        responses={
            200: SupplierBotLoginConfirmResponseSerializer,
            400: OpenApiResponse(description="Telegram initData невалиден или просрочен"),
            403: OpenApiResponse(description="Аккаунт не является аккаунтом поставщика"),
            409: OpenApiResponse(description="Telegram-чат уже принадлежит другому аккаунту"),
        },
    )
)
class SupplierBotAccountLoginConfirmView(APIView):
    """Create an account-level binding before the first company exists."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def post(self, request):
        if request.user.role != UserRole.SUPPLIER:
            raise PermissionDenied("Requires a supplier account.")

        serializer = SupplierBotAccountConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            telegram = validate_telegram_init_data(
                serializer.validated_data["init_data"],
                bot_token=getattr(settings, "TELEGRAM_BOT_TOKEN", ""),
            )
        except TelegramMiniAppError as error:
            raise ValidationError({"init_data": "Telegram authorization data is invalid or expired."}) from error

        telegram_user = telegram["user"]
        telegram_user_id = str(telegram["user_id"])
        # The onboarding Mini App is opened in a private dialog. Telegram's
        # private chat id equals the user id, so frontend never supplies a
        # trusted chat id itself.
        chat_id = telegram_user_id
        now = timezone.now()

        memberships = list(
            SupplierUser.objects.filter(
                user=request.user,
                status=EntityStatus.ACTIVE,
                supplier__status=EntityStatus.ACTIVE,
            ).select_related("supplier")[:2]
        )
        only_supplier = memberships[0].supplier if len(memberships) == 1 else None

        with transaction.atomic():
            existing = (
                BotUserBinding.objects.select_for_update()
                .filter(channel=SourceChannel.TELEGRAM, chat_id=chat_id)
                .first()
            )
            if existing and existing.user_id and existing.user_id != request.user.id:
                return Response(
                    {"detail": "Этот Telegram-чат уже привязан к другому аккаунту."},
                    status=status.HTTP_409_CONFLICT,
                )
            if (
                existing
                and existing.supplier_id
                and not SupplierUser.objects.filter(
                    supplier_id=existing.supplier_id,
                    user=request.user,
                    status=EntityStatus.ACTIVE,
                ).exists()
            ):
                return Response(
                    {"detail": "Этот Telegram-чат уже привязан к другой компании."},
                    status=status.HTTP_409_CONFLICT,
                )

            should_notify = True
            if existing:
                last_confirmed_raw = (existing.metadata or {}).get("last_account_confirmed_at")
                if last_confirmed_raw:
                    last_confirmed_at = parse_datetime(last_confirmed_raw)
                    if last_confirmed_at and (now - last_confirmed_at) < BOT_LOGIN_CONFIRM_DEDUP_WINDOW:
                        should_notify = False

            selected_supplier = existing.supplier if existing and existing.supplier_id else only_supplier
            metadata = {
                **(existing.metadata if existing else {}),
                "last_account_confirmed_at": now.isoformat(),
                "registered_via": "telegram_mini_app",
            }
            binding, created = BotUserBinding.objects.update_or_create(
                channel=SourceChannel.TELEGRAM,
                chat_id=chat_id,
                defaults={
                    "supplier": selected_supplier,
                    "user": request.user,
                    "external_user_id": telegram_user_id,
                    "username": telegram_user.get("username") or None,
                    "status": EntityStatus.ACTIVE,
                    "metadata": metadata,
                },
            )

            if selected_supplier is not None:
                ensure_supplier_notification_destination(
                    user=request.user,
                    supplier=selected_supplier,
                    binding=binding,
                )
            payload = {"binding_id": str(binding.id), "account_user_id": str(request.user.id)}
            state_name = "registered"
            if selected_supplier is not None:
                payload["selected_company_id"] = str(selected_supplier.id)
                state_name = STATE_SUPPLIER_REGISTERED
            BotUserState.objects.update_or_create(
                platform=SourceChannel.TELEGRAM,
                chat_id=chat_id,
                defaults={
                    "telegram_user_id": telegram_user_id,
                    "state": state_name,
                    "payload": payload,
                },
            )

        message_sent = False
        if should_notify:
            if selected_supplier is None:
                text = (
                    "Авторизация завершена ✅\n\n"
                    "Аккаунт привязан к Telegram. Теперь добавьте компанию по ИНН — в боте или в личном кабинете."
                )
            else:
                text = f"Авторизация завершена ✅\n\nКомпания «{selected_supplier.display_name}» выбрана в боте."
            message_sent, send_error = send_supplier_bot_message(chat_id=chat_id, text=text)
            if not message_sent:
                logger.warning(
                    "bot_account_confirm.notify_failed chat_id=%s user_id=%s error=%s",
                    chat_id,
                    request.user.id,
                    send_error,
                )

        return Response(
            SupplierBotLoginConfirmResponseSerializer(
                {
                    "binding_id": binding.id,
                    "created": created,
                    "notified": should_notify,
                    "message_sent": message_sent,
                }
            ).data
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Прямая привязка Telegram-пользователя к поставщику (без link_token)",
        description=(
            "Используется, когда пользователь регистрируется через Mini App в Telegram. "
            "Фронт после успешной регистрации/добавления компании отправляет telegram_user_id "
            "и supplier_id — бэкенд создаёт BotUserBinding напрямую, без одноразового токена. "
            "После создания привязки бэкенд автоматически отправляет приветственное сообщение "
            "в чат Telegram через Bot API."
        ),
        request=inline_serializer(
            name="BotUserBindingDirectRegisterRequest",
            fields={
                "platform": s.ChoiceField(
                    choices=SourceChannel.values,
                    default="telegram",
                    help_text="Платформа: telegram или max",
                ),
                "telegram_user_id": s.CharField(help_text="Telegram user_id пользователя"),
                "chat_id": s.CharField(help_text="Telegram chat_id"),
                "supplier_id": s.UUIDField(help_text="UUID поставщика"),
                "username": s.CharField(
                    required=False,
                    allow_blank=True,
                    help_text="Telegram @username (опционально)",
                ),
                "first_name": s.CharField(
                    required=False,
                    allow_blank=True,
                    help_text="Имя из Telegram (опционально)",
                ),
                "last_name": s.CharField(
                    required=False,
                    allow_blank=True,
                    help_text="Фамилия из Telegram (опционально)",
                ),
            },
        ),
        responses={
            200: BotUserBindingEnvelopeSerializer,
            201: BotUserBindingEnvelopeSerializer,
            400: OpenApiResponse(description="Невалидные данные или supplier не найден"),
            409: OpenApiResponse(description="Чат уже привязан к другому поставщику"),
        },
    ),
)
class BotUserBindingDirectRegisterView(APIView):
    """
    POST /api/v1/bot-user-bindings/direct-register

    Прямая привязка Telegram-пользователя к поставщику без link_token.
    После успешной привязки отправляет сообщение в чат через Telegram Bot API.
    """

    def post(self, request):
        platform = request.data.get("platform", "telegram")
        telegram_user_id = str(request.data.get("telegram_user_id", "")).strip()
        chat_id = str(request.data.get("chat_id", "")).strip()
        supplier_id = request.data.get("supplier_id")
        username = request.data.get("username", "")
        first_name = request.data.get("first_name", "")
        last_name = request.data.get("last_name", "")

        if not telegram_user_id:
            return Response({"detail": "telegram_user_id обязателен."}, status=status.HTTP_400_BAD_REQUEST)
        if not chat_id:
            return Response({"detail": "chat_id обязателен."}, status=status.HTTP_400_BAD_REQUEST)
        if not supplier_id:
            return Response({"detail": "supplier_id обязателен."}, status=status.HTTP_400_BAD_REQUEST)

        supplier = Supplier.objects.filter(pk=supplier_id, status=EntityStatus.ACTIVE).first()
        if supplier is None:
            return Response({"detail": "Поставщик не найден или неактивен."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            existing = BotUserBinding.objects.filter(
                channel=platform,
                chat_id=chat_id,
                status=EntityStatus.ACTIVE,
            ).first()

            if existing and existing.supplier_id != supplier.id:
                return Response(
                    {
                        "detail": "Этот Telegram-чат уже привязан к другой компании.",
                        "existing_supplier_id": str(existing.supplier_id),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            binding, created = BotUserBinding.objects.update_or_create(
                channel=platform,
                chat_id=chat_id,
                defaults={
                    "supplier": supplier,
                    "external_user_id": telegram_user_id,
                    "username": username or None,
                    "status": EntityStatus.ACTIVE,
                    "metadata": {
                        "first_name": first_name or "",
                        "last_name": last_name or "",
                        "registered_via": "mini_app_direct",
                    },
                },
            )

            state_before = _get_current_state(platform=platform, chat_id=chat_id)
            BotUserState.objects.update_or_create(
                platform=platform,
                chat_id=chat_id,
                defaults={
                    "telegram_user_id": telegram_user_id,
                    "state": STATE_SUPPLIER_REGISTERED,
                    "payload": {"binding_id": str(binding.id), "supplier_id": str(supplier.id)},
                },
            )

            _log_workflow_event(
                event_type="supplier_direct_registration",
                user_id=telegram_user_id,
                chat_id=chat_id,
                supplier=supplier,
                state_before=state_before,
                state_after=STATE_SUPPLIER_REGISTERED,
                payload_json={
                    "binding_id": str(binding.id),
                    "supplier_id": str(supplier.id),
                    "registered_via": "mini_app_direct",
                },
            )

        # Отправляем сообщение в Telegram после успешной привязки
        sent, send_error = send_supplier_bot_message(
            chat_id=chat_id,
            text=(
                "🎉 Регистрация завершена!\n\n"
                "Давайте добавим вашу первую компанию. "
                "Напишите или скажите голосом ИНН — я заполню всё автоматически."
            ),
        )
        if not sent:
            logger.warning(
                "bot_direct_register.notify_failed chat_id=%s supplier_id=%s error=%s",
                chat_id,
                supplier.id,
                send_error,
            )

        response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(
            BotUserBindingEnvelopeSerializer({"exists": True, "binding": binding}).data,
            status=response_status,
        )
