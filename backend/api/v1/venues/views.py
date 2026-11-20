"""Venue cabinet views — mirrors the "access settings" subset of
api.v1.suppliers.views (company list/create, cabinet session, profile CRUD,
team/invite/join/transfer). Product-display/catalog-browsing side is out of
scope (unchanged, still lives in api.v1.catalog). See
docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md.

Deliberately not mirrored (see api/v1/venues/services.py docstring for why):
- Staff-only VenueCreateView / create_venue_by_staff — Django admin already
  covers staff-driven venue creation (core.admin.VenueAdmin).
- INN/address dadata lookup (supplier-specific integration).
- Bot-issued one-time cabinet token exchange (cross-service, autosnab_bots)."""

from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.auth.services import EmailDeliveryError
from api.v1.authentication import (
    CoreUserJWTAuthentication,
    OptionalCoreUserJWTAuthentication,
    VenueCabinetJWTAuthentication,
)
from api.v1.dadata_client import AddressLookupError, INNLookupError, lookup_company_by_inn, suggest_addresses
from api.v1.permissions import (
    CanInitiateVenueAccountTransfer,
    CanRestoreVenue,
    IsCoreUser,
    IsPlatformAdminOrVenueUser,
    IsVenueCabinet,
    IsVenueCabinetActiveMember,
    IsVenueMember,
    IsVenueUser,
    is_current_venue_owner_or_staff,
    is_venue_cabinet_active_member,
)
from api.v1.validators import validate_inn
from api.v1.venues.serializers import (
    AuditLogEntrySerializer,
    VenueAccountTransferClaimSerializer,
    VenueAccountTransferInitiateSerializer,
    VenueAddressLookupQuerySerializer,
    VenueAddressSerializer,
    VenueAddressSuggestionSerializer,
    VenueBankAccountSerializer,
    VenueCabinetSessionFromLoginRequestSerializer,
    VenueCabinetSessionSerializer,
    VenueCabinetUpdateSerializer,
    VenueContactSerializer,
    VenueCreateSerializer,
    VenueDetailErrorSerializer,
    VenueDetailSerializer,
    VenueImageSerializer,
    VenueInnLookupQuerySerializer,
    VenueInnLookupResponseSerializer,
    VenueInviteLinkPreviewResponseSerializer,
    VenueInviteLinkSerializer,
    VenueInviteLinkUpdateSerializer,
    VenueJoinRequestCreateSerializer,
    VenueJoinRequestSerializer,
    VenueLogoUploadSerializer,
    VenueSearchByInnQuerySerializer,
    VenueSearchByInnResponseSerializer,
    VenueUserInviteAcceptResponseSerializer,
    VenueUserInviteCreateSerializer,
    VenueUserInvitePreviewResponseSerializer,
    VenueUserInviteSerializer,
    VenueUserRoleChangeSerializer,
    VenueUserSerializer,
)
from api.v1.venues.services import (
    accept_venue_user_invite,
    change_venue_user_role,
    claim_venue_account_transfer,
    create_venue_address,
    create_venue_bank_account,
    create_venue_by_owner,
    create_venue_contact,
    create_venue_join_request,
    decide_venue_join_request,
    delete_venue_address,
    delete_venue_bank_account,
    delete_venue_cabinet,
    delete_venue_contact,
    delete_venue_image,
    get_or_create_venue_invite_link,
    get_venue_cabinet_snapshot,
    get_venue_invite_link_preview,
    get_venue_user_invite_preview,
    initiate_venue_account_transfer,
    invite_venue_user,
    issue_venue_cabinet_tokens,
    join_venue_via_invite_link,
    list_my_venue_join_requests,
    list_my_venues,
    list_pending_venue_user_invites,
    list_venue_join_requests,
    list_venue_team,
    regenerate_venue_invite_link,
    restore_venue,
    revoke_venue_user,
    update_venue_address,
    update_venue_bank_account,
    update_venue_cabinet,
    update_venue_contact,
    update_venue_invite_link,
    upload_venue_logo,
    venue_audit_log,
)
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole


@extend_schema_view(
    get=extend_schema(
        operation_id="venue_retrieve",
        tags=["Кабинет заведения"],
        summary="Карточка заведения",
        description=(
            "2026-09-28, ТЗ панели «Неразобранные»: popup карточки покупателя по клику на название заведения "
            "(`venue_id` из строки позиции). Тот же объект, что элемент GET /venues/mine; `my_role` — роль "
            "вызывающего в этом заведении (null для сервис-токена/admin). Только участник заведения, "
            "сервис-токен или admin."
        ),
        responses={
            200: VenueDetailSerializer,
            401: OpenApiResponse(
                response=VenueDetailErrorSerializer,
                description='Нет JWT/сервис-токена, либо JWT невалиден/просрочен: {"detail": "..."}.',
            ),
            403: OpenApiResponse(
                response=VenueDetailErrorSerializer,
                description='Не участник этого заведения: {"detail": "Requires a service token, staff account, or '
                'membership of this venue."}.',
            ),
            404: OpenApiResponse(
                response=VenueDetailErrorSerializer,
                description='Заведения с таким id нет: {"detail": "No Venue matches the given query."}.',
            ),
        },
    ),
)
class VenueDetailView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        venue = get_object_or_404(
            Venue.objects.prefetch_related("images", "addresses", "contacts", "bank_accounts"), pk=venue_id
        )
        if isinstance(request.user, User):
            venue.my_role = (
                VenueUser.objects.filter(venue=venue, user=request.user, status=EntityStatus.ACTIVE)
                .values_list("role", flat=True)
                .first()
            )
        return Response(VenueDetailSerializer(venue).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Мои заведения",
        description=(
            "Список заведений, где текущий core.User — активный участник (VenueUser), от последнего "
            "созданного к первому. Переключение между ними — через venue_id в "
            "POST /venues/cabinet/session-from-login. Пустой список, не 404, если заведений ещё нет."
        ),
        responses={200: VenueDetailSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Добавить ещё одно своё заведение — self-service",
        description=(
            "Требует JWT с role=venue_user. Сам пользователь заводит заведение и сразу становится его "
            "владельцем (VenueUser, role=owner). Можно вызывать многократно."
        ),
        request=VenueCreateSerializer,
        responses={
            201: VenueDetailSerializer,
            400: OpenApiResponse(description="Заведение с таким ИНН уже зарегистрировано"),
            403: OpenApiResponse(description="JWT не с role=venue_user"),
        },
    ),
)
class VenueMineView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        venues = list_my_venues(user_id=request.user.id)
        return Response(VenueDetailSerializer(venues, many=True).data)

    def post(self, request):
        if request.user.role != UserRole.VENUE_USER:
            return Response(
                {"detail": "Самостоятельно создать заведение может только аккаунт заведения."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = VenueCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        venue = create_venue_by_owner(user=request.user, **serializer.validated_data)
        venue.my_role = VenueUserRole.OWNER
        return Response(VenueDetailSerializer(venue).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Передать ЛК-аккаунт заведения на другой email",
        description=(
            "Отправляет письмо со ссылкой на claim-форму; предыдущая незавершённая передача для этого "
            "заведения отменяется. Требует сервис-токен, staff или активного участника (owner или "
            "manager) этого заведения."
        ),
        request=VenueAccountTransferInitiateSerializer,
        responses={
            201: VenueAccountTransferInitiateSerializer,
            503: OpenApiResponse(description="Не удалось отправить письмо — сбой SMTP, повторите позже"),
        },
    ),
)
class VenueAccountTransferInitiateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def post(self, request, venue_id):
        serializer = VenueAccountTransferInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        initiated_by_user_id = serializer.validated_data.get("initiated_by_user_id")
        if isinstance(request.user, User):
            initiated_by_user_id = request.user.id

        try:
            transfer = initiate_venue_account_transfer(
                venue_id=venue_id,
                target_email=serializer.validated_data["target_email"],
                initiated_by_user_id=initiated_by_user_id,
            )
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {
                "id": transfer.id,
                "venue_id": transfer.venue_id,
                "target_email": transfer.target_email,
                "status": transfer.status,
                "expires_at": transfer.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Восстановить удалённое (archived) заведение",
        description=(
            "Отменяет DELETE /venues/cabinet/me. Не через cabinet-JWT (заблокирован, пока status != "
            "active) — обычный JWT core.User + активное членство (owner или manager) или staff."
        ),
        request=None,
        responses={200: VenueDetailSerializer, 400: OpenApiResponse(description="Заведение не удалено")},
    ),
)
class VenueRestoreView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [CanRestoreVenue]

    def post(self, request, venue_id):
        venue = restore_venue(venue_id=venue_id)
        return Response(VenueDetailSerializer(venue).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Принять переданный ЛК-аккаунт — задать пароль",
        description="Публичный endpoint: вызывается со страницы, на которую ведёт ссылка из письма.",
        request=VenueAccountTransferClaimSerializer,
        responses={200: OpenApiResponse(description="venue_id/user_id/email нового владельца")},
    ),
)
class VenueAccountTransferClaimView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token):
        serializer = VenueAccountTransferClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = claim_venue_account_transfer(token=token, new_password=serializer.validated_data["new_password"])
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Кто имеет доступ к заведению",
        description="Список активных участников (VenueUser) с ролями.",
        responses={200: VenueUserSerializer(many=True)},
    ),
)
class VenueUserListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def get(self, request, venue_id):
        team = list_venue_team(venue_id=venue_id)
        return Response(VenueUserSerializer(team, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="История удаления/архивации заведения",
        responses={
            200: AuditLogEntrySerializer(many=True),
            401: OpenApiResponse(description="Нет JWT/сервис-токена, либо JWT невалиден/просрочен."),
            403: OpenApiResponse(description="JWT валиден, но не сервис-токен/admin/участник этого заведения."),
            404: OpenApiResponse(description='Заведения с таким id нет: {"detail": "Not found."}'),
        },
    ),
)
class VenueAuditLogView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        get_object_or_404(Venue, pk=venue_id)
        return Response(AuditLogEntrySerializer(venue_audit_log(venue_id=venue_id), many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Отозвать доступ участника",
        request=None,
        responses={
            200: VenueUserSerializer,
            400: OpenApiResponse(description="Отзыв доступа самому себе или последнему owner'у"),
            404: OpenApiResponse(description="Участник не найден или уже неактивен"),
        },
    ),
)
class VenueUserRevokeView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def post(self, request, venue_id, user_id):
        requested_by_user_id = request.user.id if isinstance(request.user, User) else None
        membership = revoke_venue_user(
            venue_id=venue_id, target_user_id=user_id, requested_by_user_id=requested_by_user_id
        )
        return Response(VenueUserSerializer(membership).data)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет заведения"],
        summary="Сменить роль участника",
        request=VenueUserRoleChangeSerializer,
        responses={
            200: VenueUserSerializer,
            400: OpenApiResponse(description="Понижение последнего активного owner'а"),
            403: OpenApiResponse(description="new_role=owner от кого-то, кроме текущего owner/staff"),
            404: OpenApiResponse(description="Участник не найден или уже неактивен"),
        },
    ),
)
class VenueUserRoleChangeView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def patch(self, request, venue_id, user_id):
        serializer = VenueUserRoleChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_role = serializer.validated_data["role"]

        if new_role == VenueUserRole.OWNER and not is_current_venue_owner_or_staff(request, venue_id):
            raise PermissionDenied("Назначить нового владельца заведения может только текущий владелец.")

        requested_by_user_id = request.user.id if isinstance(request.user, User) else None
        membership = change_venue_user_role(
            venue_id=venue_id,
            target_user_id=user_id,
            new_role=new_role,
            requested_by_user_id=requested_by_user_id,
        )
        return Response(VenueUserSerializer(membership).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Список неподтверждённых email-приглашений",
        responses={200: VenueUserInviteSerializer(many=True)},
    ),
)
class VenueUserInvitePendingListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def get(self, request, venue_id):
        invites = list_pending_venue_user_invites(venue_id=venue_id)
        return Response(VenueUserInviteSerializer(invites, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Пригласить сотрудника в ЛК заведения по email",
        request=VenueUserInviteCreateSerializer,
        responses={
            201: VenueUserInviteSerializer,
            403: OpenApiResponse(description="role=owner от кого-то, кроме текущего owner/staff"),
            503: OpenApiResponse(description="Не удалось отправить письмо — сбой SMTP, повторите позже"),
        },
    ),
)
class VenueUserInviteCreateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def post(self, request, venue_id):
        serializer = VenueUserInviteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.validated_data["role"]

        if role == VenueUserRole.OWNER and not is_current_venue_owner_or_staff(request, venue_id):
            raise PermissionDenied("Пригласить нового владельца заведения может только текущий владелец.")

        invited_by_user_id = request.user.id if isinstance(request.user, User) else None

        try:
            invite = invite_venue_user(
                venue_id=venue_id,
                target_email=serializer.validated_data["target_email"],
                role=role,
                invited_by_user_id=invited_by_user_id,
            )
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(VenueUserInviteSerializer(invite).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Превью приглашения — без побочных эффектов",
        description="Не требует авторизации, не меняет статус инвайта (в отличие от accept).",
        request=None,
        responses={
            200: VenueUserInvitePreviewResponseSerializer,
            404: OpenApiResponse(description='Токена нет в базе: {"detail": "Not found."}'),
        },
    ),
)
class VenueUserInvitePreviewView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token):
        result = get_venue_user_invite_preview(token=token)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Принять приглашение в ЛК заведения",
        description="Требует core.User JWT, role=venue_user. Email вошедшего должен совпадать с target_email.",
        request=None,
        responses={
            200: VenueUserInviteAcceptResponseSerializer,
            400: OpenApiResponse(
                description=(
                    'Один из: уже принято {"token": "Эта ссылка уже была использована."}; отменено '
                    '{"token": "Эта ссылка больше не действительна — была создана более новая."}; истёк срок '
                    '{"token": "Срок действия ссылки истёк."}; email не совпадает с target_email {"detail": '
                    '"Это приглашение адресовано другому email. Выйдите из аккаунта и попробуйте снова."}; '
                    'гонка на последнем owner {"detail": "Нельзя понизить в роли последнего владельца заведения."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT или JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description='JWT валиден, но role != venue_user: {"detail": "Requires a venue account."}'
            ),
            404: OpenApiResponse(description='Токена нет в базе: {"detail": "Not found."}'),
        },
    ),
)
class VenueUserInviteAcceptView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueUser]

    def post(self, request, token):
        result = accept_venue_user_invite(token=token, user=request.user)
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Ссылка-приглашение заведения (получить/создать)",
        responses={200: VenueInviteLinkSerializer},
    ),
    patch=extend_schema(
        tags=["Кабинет заведения"],
        summary="Настройки ссылки-приглашения",
        request=VenueInviteLinkUpdateSerializer,
        responses={
            200: VenueInviteLinkSerializer,
            403: OpenApiResponse(description="role=owner от кого-то, кроме текущего owner/staff"),
        },
    ),
)
class VenueInviteLinkView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def get(self, request, venue_id):
        link = get_or_create_venue_invite_link(venue_id=venue_id)
        return Response(VenueInviteLinkSerializer(link).data)

    def patch(self, request, venue_id):
        serializer = VenueInviteLinkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("role") == VenueUserRole.OWNER and not is_current_venue_owner_or_staff(
            request, venue_id
        ):
            raise PermissionDenied("Настроить ссылку на назначение владельцем может только текущий владелец.")

        link = update_venue_invite_link(venue_id=venue_id, data=serializer.validated_data)
        return Response(VenueInviteLinkSerializer(link).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Перевыпустить ссылку-приглашение",
        request=None,
        responses={200: VenueInviteLinkSerializer},
    ),
)
class VenueInviteLinkRegenerateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def post(self, request, venue_id):
        link = regenerate_venue_invite_link(venue_id=venue_id)
        return Response(VenueInviteLinkSerializer(link).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Превью ссылки-приглашения — без побочных эффектов",
        description="Не требует авторизации, не меняет ничего в базе.",
        request=None,
        responses={
            200: VenueInviteLinkPreviewResponseSerializer,
            404: OpenApiResponse(description='Токена нет в базе: {"detail": "Not found."}'),
        },
    ),
)
class VenueInviteLinkPreviewView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token):
        result = get_venue_invite_link_preview(token=token)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Присоединиться к заведению по ссылке-приглашению",
        description="Требует core.User JWT, role=venue_user.",
        request=None,
        responses={
            200: VenueUserInviteAcceptResponseSerializer,
            400: OpenApiResponse(
                description=(
                    'Один из: ссылка отключена {"token": "Эта ссылка отключена."}; истёк срок '
                    '{"token": "Срок действия ссылки истёк."}; заведение неактивно {"detail": "Заведение '
                    'недоступно."}; гонка на последнем owner {"detail": "Нельзя понизить в роли последнего '
                    'владельца заведения."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT или JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description='JWT валиден, но role != venue_user: {"detail": "Requires a venue account."}'
            ),
            404: OpenApiResponse(description='Токена нет в базе: {"detail": "Not found."}'),
        },
    ),
)
class VenueInviteLinkJoinView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsVenueUser]

    def post(self, request, token):
        result = join_venue_via_invite_link(token=token, user=request.user)
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Проверить компанию по ИНН (dadata)",
        description=(
            "Сначала локальная checksum-проверка ИНН, затем запрос в dadata за реквизитами "
            "(название, адрес, статус в реестре, ОКВЭД/ОКПО/дата регистрации/ФИО директора) — для "
            "автозаполнения формы карточки заведения. Не блокирует создание/редактирование заведения — "
            "только обогащение/проверка. Требует JWT admin или JWT venue_user. Mirrors "
            "/suppliers/inn-lookup — тот же dadata_client, см. api/v1/dadata_client.py."
        ),
        parameters=[OpenApiParameter("inn", OpenApiTypes.STR, description="ИНН, 10 или 12 цифр", required=True)],
        responses={
            200: VenueInnLookupResponseSerializer,
            400: OpenApiResponse(
                description=(
                    'Query-параметр inn отсутствует: {"inn": ["This field is required."]}, либо ИНН не '
                    'прошёл checksum-проверку: {"detail": "..."}'
                )
            ),
            401: OpenApiResponse(description="Нет JWT или JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description='JWT валиден, но role не admin/venue_user: {"detail": "Requires an admin or venue account."}'
            ),
            404: OpenApiResponse(description='Компания с таким ИНН не найдена в dadata: {"detail": "..."}'),
            503: OpenApiResponse(description='dadata не настроена или недоступна: {"detail": "..."}'),
        },
    ),
)
class VenueInnLookupView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsPlatformAdminOrVenueUser]

    def get(self, request):
        query_ser = VenueInnLookupQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        inn = query_ser.validated_data["inn"]

        inn_error = validate_inn(inn)
        if inn_error:
            return Response({"detail": inn_error}, status=status.HTTP_400_BAD_REQUEST)

        try:
            data = lookup_company_by_inn(inn)
        except INNLookupError as error:
            message = str(error)
            http_status = status.HTTP_404_NOT_FOUND if "не найдена" in message else status.HTTP_503_SERVICE_UNAVAILABLE
            return Response({"detail": message}, status=http_status)

        return Response(VenueInnLookupResponseSerializer(data).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Поиск по адресу (dadata) — карточка заведения",
        description=(
            "Тот же принцип, что у /venues/inn-lookup — обогащение через dadata, только это "
            "live-автокомплит (dadata сама ранжирует и ограничивает выдачу), а не поиск одной точной "
            "записи по ИНН: пустой список — нормальный ответ, пока запрос слишком короткий/неоднозначный, "
            "не 404. Требует JWT admin или JWT venue_user. Mirrors /suppliers/address-lookup."
        ),
        parameters=[
            OpenApiParameter("query", OpenApiTypes.STR, description="Часть адреса, как в поле ввода.", required=True)
        ],
        responses={
            200: VenueAddressSuggestionSerializer(many=True),
            400: OpenApiResponse(
                description='Query-параметр query отсутствует: {"query": ["This field is required."]}'
            ),
            401: OpenApiResponse(description="Нет JWT или JWT невалиден/просрочен."),
            403: OpenApiResponse(
                description='JWT валиден, но role не admin/venue_user: {"detail": "Requires an admin or venue account."}'
            ),
            503: OpenApiResponse(description='dadata не настроена или недоступна: {"detail": "..."}'),
        },
    ),
)
class VenueAddressLookupView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsPlatformAdminOrVenueUser]

    def get(self, request):
        query_ser = VenueAddressLookupQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        query = query_ser.validated_data["query"]

        try:
            suggestions = suggest_addresses(query)
        except AddressLookupError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(VenueAddressSuggestionSerializer(suggestions, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Найти заведение по ИНН, чтобы присоединиться",
        description="Ищет уже зарегистрированное активное заведение по ИНН. 404, если не найдено.",
        responses={200: VenueSearchByInnResponseSerializer},
    ),
)
class VenueSearchByInnView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query_ser = VenueSearchByInnQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        inn = query_ser.validated_data["inn"]

        inn_error = validate_inn(inn)
        if inn_error:
            return Response({"detail": inn_error}, status=status.HTTP_400_BAD_REQUEST)

        venue = Venue.objects.filter(inn=inn, status=EntityStatus.ACTIVE).first()
        if venue is None:
            return Response({"detail": "Заведение с таким ИНН не найдено."}, status=status.HTTP_404_NOT_FOUND)

        return Response(VenueSearchByInnResponseSerializer(venue).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Запросить присоединение к существующему заведению",
        description=(
            "Авторизованный пользователь отправляет заявку на присоединение к уже существующему "
            "активному заведению (найденному по ИНН через search-by-inn). Заявку одобряет/отклоняет "
            "любой активный участник заведения (owner или manager). Уведомляется только служебный "
            "адрес (VENUE_JOIN_REQUEST_NOTIFICATION_EMAIL)."
        ),
        request=VenueJoinRequestCreateSerializer,
        responses={201: VenueJoinRequestSerializer},
    ),
)
class VenueJoinRequestCreateView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request, venue_id):
        serializer = VenueJoinRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        join_request = create_venue_join_request(
            venue_id=venue_id,
            requested_by_user_id=request.user.id,
            message=serializer.validated_data.get("message"),
            phone=serializer.validated_data.get("phone"),
            email=serializer.validated_data.get("email"),
            documents=serializer.validated_data.get("documents"),
        )
        return Response(VenueJoinRequestSerializer(join_request).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Заявки на присоединение к моему заведению",
        responses={200: VenueJoinRequestSerializer(many=True)},
    ),
)
class VenueJoinRequestListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def get(self, request, venue_id):
        pending_requests = list_venue_join_requests(venue_id=venue_id)
        return Response(VenueJoinRequestSerializer(pending_requests, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Одобрить заявку на присоединение",
        request=None,
        responses={200: VenueJoinRequestSerializer},
    ),
)
class VenueJoinRequestApproveView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def post(self, request, venue_id, request_id):
        decided_by_user_id = request.user.id if isinstance(request.user, User) else None
        join_request = decide_venue_join_request(
            venue_id=venue_id, request_id=request_id, decided_by_user_id=decided_by_user_id, approve=True
        )
        return Response(VenueJoinRequestSerializer(join_request).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Отклонить заявку на присоединение",
        request=None,
        responses={200: VenueJoinRequestSerializer},
    ),
)
class VenueJoinRequestRejectView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateVenueAccountTransfer]

    def post(self, request, venue_id, request_id):
        decided_by_user_id = request.user.id if isinstance(request.user, User) else None
        join_request = decide_venue_join_request(
            venue_id=venue_id, request_id=request_id, decided_by_user_id=decided_by_user_id, approve=False
        )
        return Response(VenueJoinRequestSerializer(join_request).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Мои заявки на присоединение",
        responses={200: VenueJoinRequestSerializer(many=True)},
    ),
)
class VenueJoinRequestMineView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        my_requests = list_my_venue_join_requests(user_id=request.user.id)
        return Response(VenueJoinRequestSerializer(my_requests, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Обменять JWT входа на JWT кабинета заведения",
        description=(
            "Требует активное членство (VenueUser) хотя бы в одном заведении, иначе 404. Если "
            "заведений несколько (GET /venues/mine) — передайте venue_id явно; без него берётся "
            "последнее созданное."
        ),
        request=VenueCabinetSessionFromLoginRequestSerializer,
        responses={
            200: VenueCabinetSessionSerializer,
            404: OpenApiResponse(
                description="У пользователя нет привязанного активного заведения, либо указанный venue_id ему не принадлежит"
            ),
        },
    ),
)
class VenueCabinetSessionFromLoginView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def post(self, request):
        serializer = VenueCabinetSessionFromLoginRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_venue_id = serializer.validated_data.get("venue_id")

        venue_ids = VenueUser.objects.filter(user=request.user, status=EntityStatus.ACTIVE).values_list(
            "venue_id", flat=True
        )
        queryset = Venue.objects.filter(id__in=venue_ids)
        if requested_venue_id is not None:
            venue = queryset.filter(id=requested_venue_id).first()
            if venue is None:
                return Response(
                    {"detail": "Это заведение не привязано к вашему аккаунту, либо не существует."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            venue = queryset.order_by("-created_at").first()
            if venue is None:
                return Response({"detail": "У вас пока нет привязанного заведения."}, status=status.HTTP_404_NOT_FOUND)

        tokens = issue_venue_cabinet_tokens(venue, user=request.user)
        result = {**tokens, "venue_id": str(venue.pk), "venue_name": venue.display_name}
        return Response(VenueCabinetSessionSerializer(result).data)


# Mirrors suppliers' _IDENTIFIED_MEMBER_ONLY_CABINET_FIELDS (2026-08-01) —
# registry fields and bank_accounts change legal identity or where money
# goes, require an identified active member (web-login cabinet session).
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
        tags=["Кабинет заведения"],
        summary="Данные кабинета текущего заведения",
        responses={200: VenueDetailSerializer},
    ),
    patch=extend_schema(
        tags=["Кабинет заведения"],
        summary="Обновить карточку текущего заведения",
        description=(
            "Реестровые поля (legal_name/short_name/entity_type/inn/kpp/ogrn/legal_address/okved/okpo/"
            "registration_date/general_director) и bank_accounts требуют идентифицированной активной "
            "сессии (cabinet/session-from-login), не bot-сессии."
        ),
        request=VenueCabinetUpdateSerializer,
        responses={
            200: VenueDetailSerializer,
            403: OpenApiResponse(description="Поле изменено не идентифицированным активным участником"),
        },
    ),
    delete=extend_schema(
        tags=["Кабинет заведения"],
        summary="Удалить своё заведение",
        description="Мягкое удаление — переводит заведение в статус archived. Отменяется через .../restore.",
        responses={
            204: OpenApiResponse(description="Заведение переведено в archived"),
            403: OpenApiResponse(
                description="Требуется идентифицированный активный участник, вошедший через веб-кабинет"
            ),
        },
    ),
)
class VenueCabinetView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsVenueCabinetActiveMember()]
        return super().get_permissions()

    def get(self, request):
        snapshot = get_venue_cabinet_snapshot(request.user)
        return Response(VenueDetailSerializer(snapshot).data)

    def patch(self, request):
        serializer = VenueCabinetUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        attempted_identified_only_fields = _IDENTIFIED_MEMBER_ONLY_CABINET_FIELDS & set(serializer.validated_data)
        if attempted_identified_only_fields and not is_venue_cabinet_active_member(request):
            raise PermissionDenied(
                "Требуется идентифицированный активный участник заведения, вошедший через веб-кабинет, "
                "чтобы менять: " + ", ".join(sorted(attempted_identified_only_fields))
            )
        # 2026-08-15: update_venue_cabinet() re-fetches its own Venue instance
        # internally (select_for_update) rather than mutating request.user in
        # place — use its return value, not the stale request.user, or the
        # PATCH response echoes pre-update field values even though the DB
        # write succeeded (same latent bug found and fixed on the supplier
        # side, see SupplierCabinetView.patch).
        venue = update_venue_cabinet(venue=request.user, data=serializer.validated_data)
        snapshot = get_venue_cabinet_snapshot(venue)
        return Response(VenueDetailSerializer(snapshot).data)

    def delete(self, request):
        delete_venue_cabinet(venue=request.user, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Список фото логотипа моего заведения",
        description="2026-09-13: логотип — галерея из нескольких фото, не одно изображение. "
        "`Venue.logo_url` (в остальном API) синхронизирован с самым ранним (первым загруженным) "
        "фото из этой галереи.",
        responses={200: VenueImageSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Добавить фото в галерею логотипа моего заведения",
        description="2026-09-13: каждый вызов ДОБАВЛЯЕТ фото в галерею, не заменяет предыдущее "
        "(раньше — заменял). Первое загруженное фото становится значением `Venue.logo_url`.",
        request=VenueLogoUploadSerializer,
        responses={201: VenueImageSerializer},
    ),
)
class VenueCabinetLogoUploadView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        return Response(VenueImageSerializer(request.user.images.all(), many=True).data)

    def post(self, request):
        serializer = VenueLogoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = upload_venue_logo(venue=request.user, logo_file=serializer.validated_data["logo"])
        return Response(VenueImageSerializer(image).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(
        tags=["Кабинет заведения"],
        summary="Удалить фото из галереи логотипа моего заведения",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class VenueCabinetLogoDetailView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def delete(self, request, image_id):
        delete_venue_image(venue=request.user, image_id=image_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Список адресов моего заведения",
        responses={200: VenueAddressSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Добавить адрес моего заведения",
        request=VenueAddressSerializer,
        responses={201: VenueAddressSerializer},
    ),
)
class VenueCabinetAddressListView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def get(self, request):
        return Response(VenueAddressSerializer(request.user.addresses.all(), many=True).data)

    def post(self, request):
        serializer = VenueAddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        address = create_venue_address(venue=request.user, data=serializer.validated_data)
        return Response(VenueAddressSerializer(address).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет заведения"],
        summary="Изменить адрес моего заведения",
        request=VenueAddressSerializer,
        responses={200: VenueAddressSerializer},
    ),
    delete=extend_schema(
        tags=["Кабинет заведения"],
        summary="Удалить адрес моего заведения",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class VenueCabinetAddressDetailView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def patch(self, request, id):
        serializer = VenueAddressSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        address = update_venue_address(venue=request.user, address_id=id, data=serializer.validated_data)
        return Response(VenueAddressSerializer(address).data)

    def delete(self, request, id):
        delete_venue_address(venue=request.user, address_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Список доп. контактов моего заведения",
        responses={200: VenueContactSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Добавить контакт моего заведения",
        request=VenueContactSerializer,
        responses={201: VenueContactSerializer},
    ),
)
class VenueCabinetContactListView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def get(self, request):
        return Response(VenueContactSerializer(request.user.contacts.all(), many=True).data)

    def post(self, request):
        serializer = VenueContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        contact = create_venue_contact(venue=request.user, data=serializer.validated_data)
        return Response(VenueContactSerializer(contact).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет заведения"],
        summary="Изменить контакт моего заведения",
        request=VenueContactSerializer,
        responses={200: VenueContactSerializer},
    ),
    delete=extend_schema(
        tags=["Кабинет заведения"],
        summary="Удалить контакт моего заведения",
        responses={204: OpenApiResponse(description="Удалено")},
    ),
)
class VenueCabinetContactDetailView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def patch(self, request, id):
        serializer = VenueContactSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        contact = update_venue_contact(venue=request.user, contact_id=id, data=serializer.validated_data)
        return Response(VenueContactSerializer(contact).data)

    def delete(self, request, id):
        delete_venue_contact(venue=request.user, contact_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет заведения"],
        summary="Список банковских реквизитов моего заведения",
        responses={200: VenueBankAccountSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет заведения"],
        summary="Добавить банковский счёт моего заведения",
        description="Требует идентифицированного активного участника, вошедшего через веб-кабинет.",
        request=VenueBankAccountSerializer,
        responses={
            201: VenueBankAccountSerializer,
            403: OpenApiResponse(description="Требуется идентифицированный активный участник заведения"),
        },
    ),
)
class VenueCabinetBankAccountListView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinet]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsVenueCabinetActiveMember()]
        return super().get_permissions()

    def get(self, request):
        return Response(VenueBankAccountSerializer(request.user.bank_accounts.all(), many=True).data)

    def post(self, request):
        serializer = VenueBankAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = create_venue_bank_account(venue=request.user, data=serializer.validated_data)
        return Response(VenueBankAccountSerializer(account).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет заведения"],
        summary="Изменить банковский счёт моего заведения",
        description="Требует идентифицированного активного участника, вошедшего через веб-кабинет.",
        request=VenueBankAccountSerializer,
        responses={
            200: VenueBankAccountSerializer,
            403: OpenApiResponse(description="Требуется идентифицированный активный участник заведения"),
        },
    ),
    delete=extend_schema(
        tags=["Кабинет заведения"],
        summary="Удалить банковский счёт моего заведения",
        description="Требует идентифицированного активного участника, вошедшего через веб-кабинет.",
        responses={
            204: OpenApiResponse(description="Удалено"),
            403: OpenApiResponse(description="Требуется идентифицированный активный участник заведения"),
        },
    ),
)
class VenueCabinetBankAccountDetailView(APIView):
    authentication_classes = [VenueCabinetJWTAuthentication]
    permission_classes = [IsVenueCabinetActiveMember]

    def patch(self, request, id):
        serializer = VenueBankAccountSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        account = update_venue_bank_account(venue=request.user, bank_account_id=id, data=serializer.validated_data)
        return Response(VenueBankAccountSerializer(account).data)

    def delete(self, request, id):
        delete_venue_bank_account(venue=request.user, bank_account_id=id)
        return Response(status=status.HTTP_204_NO_CONTENT)
