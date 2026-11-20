"""Supplier cabinet session and settings."""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import (
    CoreUserJWTAuthentication,
    SupplierCabinetJWTAuthentication,
)
from api.v1.permissions import (
    IsCoreUser,
    IsSupplierCabinet,
    IsSupplierCabinetActiveMember,
    is_supplier_cabinet_active_member,
)
from api.v1.suppliers.bot_events import (
    ensure_supplier_notification_destination,
    send_supplier_bot_message,
)
from api.v1.suppliers.serializers import (
    SupplierAddressSerializer,
    SupplierBankAccountSerializer,
    SupplierBotConnectionSerializer,
    SupplierBotConnectionsListSerializer,
    SupplierBotLinkGenerateSerializer,
    SupplierBotLinkResponseSerializer,
    SupplierBotLoginConfirmResponseSerializer,
    SupplierBotLoginConfirmSerializer,
    SupplierCabinetSerializer,
    SupplierCabinetSessionFromLoginRequestSerializer,
    SupplierCabinetSessionRequestSerializer,
    SupplierCabinetSessionSerializer,
    SupplierCabinetUpdateSerializer,
    SupplierContactSerializer,
    SupplierImageSerializer,
    SupplierLogoUploadSerializer,
    SupplierScheduleRuleSerializer,
)
from api.v1.suppliers.services import (
    create_supplier_address,
    create_supplier_bank_account,
    create_supplier_bot_link_token,
    create_supplier_contact,
    create_supplier_schedule_rule,
    delete_supplier_address,
    delete_supplier_bank_account,
    delete_supplier_cabinet,
    delete_supplier_contact,
    delete_supplier_image,
    delete_supplier_schedule_rule,
    exchange_cabinet_session_token,
    get_supplier_cabinet_snapshot,
    issue_supplier_cabinet_tokens,
    update_supplier_address,
    update_supplier_bank_account,
    update_supplier_cabinet,
    update_supplier_contact,
    update_supplier_schedule_rule,
    upload_supplier_logo,
)
from core.models import EntityStatus
from suppliers.models import (
    BotUserBinding,
    CabinetSession,
    SourceChannel,
    Supplier,
    SupplierUser,
)

from ._shared import BOT_LOGIN_CONFIRM_DEDUP_WINDOW, _resolve_active_binding, logger


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Получить токен для входа в кабинет поставщика",
        description=(
            "Создаёт одноразовую сессию для входа в веб-кабинет поставщика по Telegram chat_id "
            "(с фолбэком на telegram_user_id, если chat_id привязки успел устареть — тот же "
            "паттерн, что в BotUserBindingResolveView). Токен действует 30 минут и может быть "
            "использован один раз."
        ),
        request=inline_serializer(
            name="CabinetTokenRequest",
            fields={
                "platform": s.ChoiceField(choices=SourceChannel.values, default="telegram"),
                "chat_id": s.CharField(required=False, help_text="Telegram chat_id пользователя"),
                "telegram_user_id": s.CharField(
                    required=False, help_text="Telegram user_id — фолбэк, если поиск по chat_id не даст результата"
                ),
            },
        ),
        responses={
            200: inline_serializer(
                name="CabinetTokenResponse",
                fields={
                    "token": s.CharField(help_text="Одноразовый токен для /supplier/cabinet?token=…"),
                    "expires_at": s.DateTimeField(help_text="Время истечения токена (ISO 8601)"),
                    "supplier_id": s.UUIDField(help_text="UUID поставщика"),
                    "supplier_name": s.CharField(help_text="Отображаемое имя поставщика"),
                },
            ),
            400: OpenApiResponse(description="ни chat_id, ни telegram_user_id не переданы"),
            404: OpenApiResponse(description="Активная привязка бота не найдена"),
        },
    ),
)
class CabinetTokenView(APIView):
    def post(self, request):
        platform = request.data.get("platform", "telegram")
        chat_id = request.data.get("chat_id")
        telegram_user_id = request.data.get("telegram_user_id")
        if not chat_id and not telegram_user_id:
            return Response({"detail": "chat_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        binding = _resolve_active_binding(
            channel=platform,
            chat_id=str(chat_id) if chat_id else None,
            telegram_user_id=str(telegram_user_id) if telegram_user_id else None,
        )

        if not binding:
            return Response({"detail": "Binding not found."}, status=status.HTTP_404_NOT_FOUND)

        import secrets
        from datetime import timedelta

        from django.utils import timezone

        token = secrets.token_urlsafe(32)
        expires_at = timezone.now() + timedelta(minutes=30)

        CabinetSession.objects.create(
            token=token,
            binding=binding,
            expires_at=expires_at,
        )

        return Response(
            {
                "token": token,
                "expires_at": expires_at.isoformat(),
                "supplier_id": str(binding.supplier_id),
                "supplier_name": binding.supplier.display_name,
            }
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Обменять одноразовый токен кабинета на JWT",
        description=(
            "Принимает одноразовый токен, выданный ботом через POST cabinet/token, и "
            "возвращает JWT-пару (access/refresh) кабинета поставщика вместо Django "
            "session cookie — для использования отдельным frontend-приложением."
        ),
        request=SupplierCabinetSessionRequestSerializer,
        responses={
            200: SupplierCabinetSessionSerializer,
            400: OpenApiResponse(description="token не передан"),
            404: OpenApiResponse(description="Токен истёк, уже использован или не найден"),
        },
    ),
)
class SupplierCabinetSessionView(APIView):
    """Public — the one-time token itself is the credential, not SERVICE_API_TOKEN."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SupplierCabinetSessionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = exchange_cabinet_session_token(token=serializer.validated_data["token"])
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        return Response(SupplierCabinetSessionSerializer(result).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Обменять JWT входа (email+пароль/OAuth) на JWT кабинета",
        description=(
            "Для поставщика, залогиненного через сайт напрямую (POST /auth/supplier/login, "
            "Яндекс, VK ID) — в отличие от cabinet/session, здесь не нужен одноразовый токен "
            "от бота. Требует активное членство (SupplierUser) хотя бы в одной компании, "
            "иначе 404. Если компаний несколько (GET /suppliers/mine) — передайте supplier_id "
            "явно, чтобы выбрать нужную; без него берётся последняя созданная (для обратной "
            "совместимости, когда компания ровно одна)."
        ),
        request=SupplierCabinetSessionFromLoginRequestSerializer,
        responses={
            200: SupplierCabinetSessionSerializer,
            404: OpenApiResponse(
                description="У пользователя нет привязанной активной компании, либо указанный "
                "supplier_id ему не принадлежит"
            ),
        },
    ),
)
class SupplierCabinetSessionFromLoginView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def post(self, request):
        serializer = SupplierCabinetSessionFromLoginRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_supplier_id = serializer.validated_data.get("supplier_id")

        supplier_ids = SupplierUser.objects.filter(user=request.user, status=EntityStatus.ACTIVE).values_list(
            "supplier_id", flat=True
        )
        queryset = Supplier.objects.filter(id__in=supplier_ids)
        if requested_supplier_id is not None:
            supplier = queryset.filter(id=requested_supplier_id).first()
            if supplier is None:
                return Response(
                    {"detail": "Эта компания не привязана к вашему аккаунту, либо не существует."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            supplier = queryset.order_by("-created_at").first()
            if supplier is None:
                return Response({"detail": "У вас пока нет привязанной компании."}, status=status.HTTP_404_NOT_FOUND)

        tokens = issue_supplier_cabinet_tokens(supplier, user=request.user)
        result = {**tokens, "supplier_id": str(supplier.pk), "supplier_name": supplier.display_name}
        return Response(SupplierCabinetSessionSerializer(result).data)


# 2026-08-01: реестровые (юр.) поля и bank_accounts меняют либо юридическую
# идентичность компании, либо куда уходят деньги — требуют идентифицированного
# активного участника (cabinet-сессия из веб-логина, не бот), даже хотя PATCH
# cabinet/me в остальном открыт любому активному участнику. 2026-08-05: owner и
# manager больше не различаются по правам — гейт остался, но теперь пропускает
# любую активную роль, лишь бы сессия была привязана к реальному человеку.
_IDENTIFIED_MEMBER_ONLY_CABINET_FIELDS = frozenset(
    {
        "legal_name",
        "short_name",
        "entity_type",
        "inn",
        "kpp",
        "ogrn",
        "legal_address",
        "okved",
        "okpo",
        "registration_date",
        "general_director",
        "bank_accounts",
    }
)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Данные кабинета текущего поставщика",
        description="Карточка компании, счётчик активных товаров и последние 20 загрузок прайса.",
        responses={200: SupplierCabinetSerializer},
    ),
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Обновить карточку компании текущего поставщика",
        description=(
            "Большинство полей открыты любому активному участнику компании (owner и manager имеют "
            "одинаковые права). Реестровые поля (legal_name/short_name/entity_type/inn/kpp/ogrn/"
            "legal_address/okved/okpo/registration_date/general_director) и bank_accounts требуют, "
            "чтобы cabinet-сессия была привязана к идентифицированному активному участнику, вошедшему "
            "через веб-кабинет (cabinet/session-from-login) — те же причины и то же ограничение "
            "cabinet-JWT из бота, что и у DELETE (см. ниже). Если хотя бы одно из этих полей передано "
            "не через идентифицированную сессию — 403 на весь запрос, не частичное применение."
        ),
        request=SupplierCabinetUpdateSerializer,
        responses={
            200: SupplierCabinetSerializer,
            403: OpenApiResponse(description="Поле изменено не идентифицированным активным участником"),
        },
    ),
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить свою компанию",
        description=(
            "Мягкое удаление — переводит компанию в статус archived (не физическое удаление строки: "
            "у компании могут быть прайсы/товары/заказы, которые нельзя молча стереть). Компания "
            "перестаёт быть видна в каталоге и пропадает из /suppliers/mine; этот же cabinet-токен "
            "сразу перестаёт работать (SupplierCabinetJWTAuthentication пускает только status=active) "
            "— повторный вход в удалённую компанию невозможен через обычный флоу. Отменяется через "
            "POST /suppliers/{id}/restore (owner или manager) или администратором через Django admin. "
            "Требует идентифицированного активного участника компании (IsSupplierCabinetActiveMember) "
            "— cabinet-сессия, выданная через Telegram/Max-бота, не знает, какой участник за ней стоит, "
            "и получит 403; удалить компанию можно только войдя через веб-кабинет "
            "(cabinet/session-from-login)."
        ),
        responses={
            204: OpenApiResponse(description="Компания переведена в archived"),
            403: OpenApiResponse(
                description="Требуется идентифицированный активный участник, вошедший через веб-кабинет"
            ),
        },
    ),
)
class SupplierCabinetView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsSupplierCabinetActiveMember()]
        return super().get_permissions()

    def get(self, request):
        snapshot = get_supplier_cabinet_snapshot(request.user)
        return Response(SupplierCabinetSerializer(snapshot).data)

    def patch(self, request):
        serializer = SupplierCabinetUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        attempted_identified_only_fields = _IDENTIFIED_MEMBER_ONLY_CABINET_FIELDS & set(serializer.validated_data)
        if attempted_identified_only_fields and not is_supplier_cabinet_active_member(request):
            raise PermissionDenied(
                "Требуется идентифицированный активный участник компании, вошедший через веб-кабинет, "
                "чтобы менять: " + ", ".join(sorted(attempted_identified_only_fields))
            )
        # 2026-08-15: update_supplier_cabinet() re-fetches its own Supplier
        # instance internally (select_for_update) rather than mutating
        # request.user in place — use its return value, not the stale
        # request.user, or the PATCH response echoes pre-update field values
        # even though the DB write succeeded (found while mirroring this
        # view for the venue cabinet, see VenueCabinetView.patch).
        supplier = update_supplier_cabinet(supplier=request.user, data=serializer.validated_data)
        snapshot = get_supplier_cabinet_snapshot(supplier)
        return Response(SupplierCabinetSerializer(snapshot).data)

    def delete(self, request):
        delete_supplier_cabinet(supplier=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список фото логотипа моей компании",
        description="2026-09-13: логотип — галерея из нескольких фото, не одно изображение. "
        "`Supplier.logo_url` (в остальном API) синхронизирован с самым ранним (первым загруженным) "
        "фото из этой галереи.",
        responses={200: SupplierImageSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Добавить фото в галерею логотипа моей компании",
        description=(
            "Multipart-загрузка файла логотипа — сохраняет файл и добавляет его в галерею. "
            "2026-09-13: каждый вызов ДОБАВЛЯЕТ фото, не заменяет предыдущее (раньше — заменял). "
            "Первое загруженное фото становится значением Supplier.logo_url."
        ),
        request=SupplierLogoUploadSerializer,
        responses={201: SupplierImageSerializer},
    ),
)
class SupplierCabinetLogoUploadView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        return Response(SupplierImageSerializer(request.user.images.all(), many=True).data)

    def post(self, request):
        serializer = SupplierLogoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = upload_supplier_logo(supplier=request.user, logo_file=serializer.validated_data["logo"])
        return Response(SupplierImageSerializer(image).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить фото из галереи логотипа моей компании",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class SupplierCabinetLogoDetailView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def delete(self, request, image_id):
        delete_supplier_image(supplier=request.user, image_id=image_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список адресов моей компании",
        description="Типизированные адреса (юридический/фактический/домашний) — компания может иметь несколько.",
        responses={200: SupplierAddressSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Добавить адрес моей компании",
        request=SupplierAddressSerializer,
        responses={201: SupplierAddressSerializer},
    ),
)
class SupplierCabinetAddressListView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get(self, request):
        return Response(SupplierAddressSerializer(request.user.addresses.all(), many=True).data)

    def post(self, request):
        serializer = SupplierAddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        address = create_supplier_address(supplier=request.user, data=serializer.validated_data)
        return Response(SupplierAddressSerializer(address).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Изменить адрес моей компании",
        request=SupplierAddressSerializer,
        responses={200: SupplierAddressSerializer},
    ),
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить адрес моей компании",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class SupplierCabinetAddressDetailView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def patch(self, request, id):
        serializer = SupplierAddressSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        address = update_supplier_address(supplier=request.user, address_id=id, data=serializer.validated_data)
        return Response(SupplierAddressSerializer(address).data)

    def delete(self, request, id):
        delete_supplier_address(supplier=request.user, address_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список доп. контактов моей компании",
        description="Дополнительные email/телефоны/мессенджеры — компания может иметь несколько.",
        responses={200: SupplierContactSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Добавить контакт моей компании",
        request=SupplierContactSerializer,
        responses={201: SupplierContactSerializer},
    ),
)
class SupplierCabinetContactListView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get(self, request):
        return Response(SupplierContactSerializer(request.user.contacts.all(), many=True).data)

    def post(self, request):
        serializer = SupplierContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        contact = create_supplier_contact(supplier=request.user, data=serializer.validated_data)
        return Response(SupplierContactSerializer(contact).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Изменить контакт моей компании",
        request=SupplierContactSerializer,
        responses={200: SupplierContactSerializer},
    ),
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить контакт моей компании",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class SupplierCabinetContactDetailView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def patch(self, request, id):
        serializer = SupplierContactSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        contact = update_supplier_contact(supplier=request.user, contact_id=id, data=serializer.validated_data)
        return Response(SupplierContactSerializer(contact).data)

    def delete(self, request, id):
        delete_supplier_contact(supplier=request.user, contact_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список банковских реквизитов моей компании",
        description="Можно добавить несколько расчётных счетов.",
        responses={200: SupplierBankAccountSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Добавить банковский счёт моей компании",
        description=(
            "Требует идентифицированного активного участника компании (owner или manager), "
            "вошедшего через веб-кабинет — то же ограничение, что и у bank_accounts в PATCH "
            "cabinet/me (2026-08-01, куда уходят деньги не может менять анонимная bot-сессия)."
        ),
        request=SupplierBankAccountSerializer,
        responses={
            201: SupplierBankAccountSerializer,
            403: OpenApiResponse(description="Требуется идентифицированный активный участник компании"),
        },
    ),
)
class SupplierCabinetBankAccountListView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsSupplierCabinetActiveMember()]
        return super().get_permissions()

    def get(self, request):
        return Response(SupplierBankAccountSerializer(request.user.bank_accounts.all(), many=True).data)

    def post(self, request):
        serializer = SupplierBankAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = create_supplier_bank_account(supplier=request.user, data=serializer.validated_data)
        return Response(SupplierBankAccountSerializer(account).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Изменить банковский счёт моей компании",
        description=(
            "Требует идентифицированного активного участника компании (owner или manager), "
            "вошедшего через веб-кабинет (2026-08-01)."
        ),
        request=SupplierBankAccountSerializer,
        responses={
            200: SupplierBankAccountSerializer,
            403: OpenApiResponse(description="Требуется идентифицированный активный участник компании"),
        },
    ),
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить банковский счёт моей компании",
        description=(
            "Требует идентифицированного активного участника компании (owner или manager), "
            "вошедшего через веб-кабинет (2026-08-01)."
        ),
        responses={
            204: OpenApiResponse(description="Удалено"),
            403: OpenApiResponse(description="Требуется идентифицированный активный участник компании"),
        },
    ),
)
class SupplierCabinetBankAccountDetailView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinetActiveMember]

    def patch(self, request, id):
        serializer = SupplierBankAccountSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        account = update_supplier_bank_account(
            supplier=request.user, bank_account_id=id, data=serializer.validated_data
        )
        return Response(SupplierBankAccountSerializer(account).data)

    def delete(self, request, id):
        delete_supplier_bank_account(supplier=request.user, bank_account_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список правил графика моей компании",
        description="Строки недельного графика приёма/доставки или приёма/готовности для самовывоза.",
        responses={200: SupplierScheduleRuleSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Добавить правило графика моей компании",
        request=SupplierScheduleRuleSerializer,
        responses={201: SupplierScheduleRuleSerializer},
    ),
)
class SupplierCabinetScheduleRuleListView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get(self, request):
        return Response(SupplierScheduleRuleSerializer(request.user.schedule_rules.all(), many=True).data)

    def post(self, request):
        serializer = SupplierScheduleRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = create_supplier_schedule_rule(supplier=request.user, data=serializer.validated_data)
        return Response(SupplierScheduleRuleSerializer(rule).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Изменить правило графика моей компании",
        request=SupplierScheduleRuleSerializer,
        responses={200: SupplierScheduleRuleSerializer},
    ),
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить правило графика моей компании",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class SupplierCabinetScheduleRuleDetailView(APIView):
    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def patch(self, request, id):
        serializer = SupplierScheduleRuleSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        rule = update_supplier_schedule_rule(supplier=request.user, rule_id=id, data=serializer.validated_data)
        return Response(SupplierScheduleRuleSerializer(rule).data)

    def delete(self, request, id):
        delete_supplier_schedule_rule(supplier=request.user, rule_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _is_bot_binding_owner(binding, supplier):
    """
    Проверяет, является ли эта привязка бота подключением владельца компании.
    Владелец определяется по метаданным: registered_via == "bot_link_token" и is_owner == True.

    Используется чтобы:
    - Показать признак is_owner = true в таблице (кнопка «Удалить» будет заблокирована)
    - Запретить удаление владельца через DELETE-запрос
    """
    if not binding.metadata:
        return False
    return binding.metadata.get("registered_via") == "bot_link_token" and binding.metadata.get("is_owner", False)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список подключенных агентов к боту «Поставщик»",
        description=(
            "Возвращает таблицу со всеми агентами (включая владельца), "
            "подключенными к боту компании через Telegram или Max."
        ),
        responses={200: SupplierBotConnectionsListSerializer},
    ),
)
class SupplierCabinetBotConnectionsView(APIView):
    """
    GET /api/v1/suppliers/cabinet/bot-connections

    Возвращает:
    - Общую статистику (total_count, telegram_count, max_count)
    - Массив connections для отрисовки таблицы в модальном окне

    Каждая строка содержит:
    - ID привязки
    - Канал (telegram/max)
    - Chat ID
    - User ID
    - Username
    - Дату привязки
    - Дату последнего действия
    - Признак is_owner (если true — кнопка «Удалить» заблокирована)
    """

    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def get(self, request):
        # Получаем все активные привязки ботов к этой компании
        bindings = BotUserBinding.objects.filter(
            supplier=request.user,
            status=EntityStatus.ACTIVE,
        ).order_by("-created_at")

        # Считаем статистику по каналам
        telegram_count = sum(1 for b in bindings if b.channel == SourceChannel.TELEGRAM)
        max_count = sum(1 for b in bindings if b.channel == SourceChannel.MAX)

        # Формируем данные для таблицы, добавляя признак is_owner
        connections_data = []
        for binding in bindings:
            data = SupplierBotConnectionSerializer(binding).data
            data["is_owner"] = _is_bot_binding_owner(binding, request.user)
            connections_data.append(data)

        return Response(
            {
                "total_count": len(bindings),
                "telegram_count": telegram_count,
                "max_count": max_count,
                "connections": connections_data,
            }
        )


@extend_schema_view(
    delete=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Удалить агента из бота «Поставщик»",
        description=(
            "Полностью удаляет привязку агента к боту (hard delete). "
            "Нельзя удалить подключение владельца компании — вернётся 403."
        ),
        responses={
            204: OpenApiResponse(description="Агент успешно удалён из таблицы."),
            403: OpenApiResponse(description="Нельзя удалить владельца компании."),
            404: OpenApiResponse(description="Привязка не найдена или уже удалена."),
        },
    ),
)
class SupplierCabinetBotConnectionDetailView(APIView):
    """
    DELETE /api/v1/suppliers/cabinet/bot-connections/{binding_id}

    Удаляет строку из таблицы BotUserBinding.
    Защита: если binding принадлежит владельцу компании — возвращает 403.
    """

    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def delete(self, request, binding_id):
        # Находим привязку — только активную и принадлежащую этой компании
        binding = get_object_or_404(
            BotUserBinding,
            id=binding_id,
            supplier=request.user,
            status=EntityStatus.ACTIVE,
        )

        # Защита: нельзя удалить владельца компании
        if _is_bot_binding_owner(binding, request.user):
            raise PermissionDenied("Нельзя удалить подключение владельца компании.")

        # Полное удаление записи из базы данных
        binding.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Сгенерировать ссылку для приглашения агента в бот «Поставщик»",
        description=(
            "Создаёт одноразовую ссылку-приглашение для отправки агенту. "
            "Агент переходит по ссылке и автоматически привязывается к боту компании. "
            "Поддерживаются каналы: telegram и max."
        ),
        request=SupplierBotLinkGenerateSerializer,
        responses={
            201: SupplierBotLinkResponseSerializer,
            400: OpenApiResponse(description="Невалидный канал (ожидается telegram или max)."),
        },
    ),
)
class SupplierCabinetBotLinkGenerateView(APIView):
    """
    POST /api/v1/suppliers/cabinet/bot-link/generate

    Тело запроса: {"channel": "telegram"} или {"channel": "max"}

    Ответ:
    - token: одноразовый токен
    - deep_link: готовая ссылка (например, https://t.me/AutosnabSupplierBot?start=TOKEN)
    - expires_at: срок действия ссылки
    - channel: telegram или max
    """

    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def post(self, request):
        serializer = SupplierBotLinkGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        channel = serializer.validated_data["channel"]

        # Используем существующий сервис create_supplier_bot_link_token
        # из api/v1/suppliers/services.py
        result = create_supplier_bot_link_token(
            supplier=request.user,
            channel=channel,
            created_by=None,
        )
        return Response(result, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Подтвердить успешный вход в Telegram Mini App после веб-логина",
        description=(
            "Вызывается фронтом сразу после того, как пользователь авторизовался в кабинете "
            "и (если у него несколько компаний) выбрал компанию, находясь внутри Telegram Mini "
            "App. supplier берётся из cabinet JWT (Authorization: Bearer <cabinet token>), а не "
            "из тела запроса — фронт не может привязать чужую компанию к чужому чату. Создаёт "
            "или обновляет BotUserBinding и отправляет в чат сообщение «Авторизация прошла "
            "успешно». Повторные вызовы в течение 60с после последнего успешного уведомления "
            "не дублируют сообщение."
        ),
        request=SupplierBotLoginConfirmSerializer,
        responses={
            200: SupplierBotLoginConfirmResponseSerializer,
            409: OpenApiResponse(description="Этот chat_id уже привязан к другой компании"),
        },
    ),
)
class SupplierCabinetBotLoginConfirmView(APIView):
    """
    POST /api/v1/suppliers/cabinet/bot-link/confirm-login

    2026-08-18: Ярослав (фронт) — после веб-логина сайт не сообщал боту,
    что пользователь вошёл, бот не мог написать "Авторизация прошла
    успешно". Существовавший bot-user-bindings/direct-register для этого
    не годится — требует HasServiceToken (DEFAULT_PERMISSION_CLASSES),
    фронт не должен держать сервисный секрет. Эта вьюха — cabinet-JWT
    вариант того же действия, supplier доверяется только из токена.
    """

    authentication_classes = [SupplierCabinetJWTAuthentication]
    permission_classes = [IsSupplierCabinet]

    def post(self, request):
        serializer = SupplierBotLoginConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        platform = serializer.validated_data["platform"]
        telegram_user_id = serializer.validated_data["telegram_user_id"]
        chat_id = serializer.validated_data["chat_id"]
        supplier = request.user

        # A cabinet token identifies a supplier; only an active membership
        # proves which core account should own subsequent bot operations.
        account_user = None
        account_user_id = request.auth.get("user_id") if request.auth else None
        if account_user_id is not None:
            account_user_id = s.UUIDField().run_validation(account_user_id)
            membership = (
                SupplierUser.objects.select_related("user")
                .filter(
                    supplier=supplier,
                    user_id=account_user_id,
                    user__status=EntityStatus.ACTIVE,
                    status=EntityStatus.ACTIVE,
                )
                .first()
            )
            if membership is None:
                raise PermissionDenied("Requires an active member of this supplier.")
            account_user = membership.user

        now = timezone.now()
        with transaction.atomic():
            existing = (
                BotUserBinding.objects.select_for_update()
                .filter(channel=platform, chat_id=chat_id, status=EntityStatus.ACTIVE)
                .first()
            )
            if existing and existing.supplier_id and existing.supplier_id != supplier.id:
                return Response(
                    {
                        "detail": "Этот Telegram-чат уже привязан к другой компании.",
                        "existing_supplier_id": str(existing.supplier_id),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            should_notify = True
            if existing:
                last_confirmed_raw = (existing.metadata or {}).get("last_login_confirmed_at")
                if last_confirmed_raw:
                    last_confirmed_at = parse_datetime(last_confirmed_raw)
                    if last_confirmed_at and (now - last_confirmed_at) < BOT_LOGIN_CONFIRM_DEDUP_WINDOW:
                        should_notify = False

            merged_metadata = {**(existing.metadata if existing else {}), "last_login_confirmed_at": now.isoformat()}
            binding, created = BotUserBinding.objects.update_or_create(
                channel=platform,
                chat_id=chat_id,
                defaults={
                    "supplier": supplier,
                    "external_user_id": telegram_user_id,
                    "status": EntityStatus.ACTIVE,
                    "metadata": merged_metadata,
                    "user": account_user,
                },
            )
            if account_user is not None:
                ensure_supplier_notification_destination(
                    user=account_user,
                    supplier=supplier,
                    binding=binding,
                )

        message_sent = False
        if should_notify:
            message_sent, send_error = send_supplier_bot_message(
                chat_id=chat_id,
                text=f"Авторизация прошла успешно ✓\n\nКомпания «{supplier.display_name}» подключена к боту.",
            )
            if not message_sent:
                logger.warning(
                    "bot_login_confirm.notify_failed chat_id=%s supplier_id=%s error=%s",
                    chat_id,
                    supplier.id,
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
            ).data,
            status=status.HTTP_200_OK,
        )
