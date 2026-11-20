"""Supplier team: members, invites, invite links and join requests."""

from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
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
)
from api.v1.permissions import (
    CanInitiateAccountTransfer,
    IsCoreUser,
    IsSupplierUser,
    is_current_supplier_owner_or_staff,
)
from api.v1.suppliers.serializers import (
    SupplierInviteLinkSerializer,
    SupplierInviteLinkUpdateSerializer,
    SupplierJoinRequestCreateSerializer,
    SupplierJoinRequestSerializer,
    SupplierUserInviteCreateSerializer,
    SupplierUserInviteSerializer,
    SupplierUserRoleChangeSerializer,
    SupplierUserSerializer,
)
from api.v1.suppliers.services import (
    accept_supplier_user_invite,
    change_supplier_user_role,
    create_supplier_join_request,
    decide_supplier_join_request,
    get_or_create_supplier_invite_link,
    get_supplier_invite_link_preview,
    get_supplier_user_invite_preview,
    invite_supplier_user,
    join_supplier_via_invite_link,
    list_my_supplier_join_requests,
    list_pending_supplier_user_invites,
    list_supplier_join_requests,
    list_supplier_team,
    regenerate_supplier_invite_link,
    revoke_supplier_user,
    update_supplier_invite_link,
)
from core.models import User
from suppliers.models import (
    SupplierUserRole,
)

_SUPPLIER_USER_INVITE_CREATE_SCHEMA = inline_serializer(
    name="SupplierUserInviteCreateResult",
    fields={
        "id": s.UUIDField(help_text="ID созданного приглашения."),
        "supplier_id": s.UUIDField(help_text="ID компании, в которую приглашают."),
        "target_email": s.EmailField(help_text="Email получателя, на который отправлена ссылка."),
        "role": s.CharField(help_text="Роль, которую получит приглашённый после принятия: owner / manager."),
        "status": s.CharField(help_text="Статус приглашения: pending / claimed / cancelled / expired."),
        "expires_at": s.DateTimeField(help_text="До какого момента ссылка действительна."),
    },
)


_SUPPLIER_USER_INVITE_PREVIEW_SCHEMA = inline_serializer(
    name="SupplierUserInvitePreviewResult",
    fields={
        "target_email": s.EmailField(help_text="На какой email адресован инвайт — accept примет именно его."),
        "supplier_name": s.CharField(help_text="Название компании, в которую приглашают."),
        "supplier_inn": s.CharField(
            allow_null=True, help_text="ИНН компании, в которую приглашают (2026-08-07) — может быть пустым."
        ),
        "role": s.CharField(help_text="Роль, которую получит принявший: owner / manager."),
        "status": s.CharField(help_text="Текущий статус инвайта: pending / claimed / cancelled / expired."),
        "is_expired": s.BooleanField(help_text="true, если expires_at уже в прошлом (даже если status ещё pending)."),
        "account_exists": s.BooleanField(
            help_text=(
                "true — с этим email уже есть supplier-аккаунт. Фронт использует это, чтобы до accept "
                "решить, куда вести неавторизованного юзера — на форму логина или на форму регистрации "
                "(2026-08-07: accept сам больше не создаёт/логинит аккаунт, только принимает уже "
                "авторизованного)."
            )
        ),
    },
)


_SUPPLIER_INVITE_LINK_PREVIEW_SCHEMA = inline_serializer(
    name="SupplierInviteLinkPreviewResult",
    fields={
        "supplier_name": s.CharField(help_text="Название компании, в которую зовёт ссылка."),
        "supplier_inn": s.CharField(
            allow_null=True, help_text="ИНН компании, в которую зовёт ссылка (2026-08-07) — может быть пустым."
        ),
        "role": s.CharField(help_text="Роль, которую получит присоединившийся: owner / manager."),
        "status": s.CharField(help_text="active — ссылка рабочая, inactive — отключена владельцем."),
        "is_expired": s.BooleanField(help_text="true, если expires_at уже в прошлом (даже если status active)."),
    },
)


_SUPPLIER_USER_INVITE_ACCEPT_SCHEMA = inline_serializer(
    name="SupplierUserInviteAcceptResult",
    fields={
        "supplier_id": s.UUIDField(help_text="ID компании, доступ к которой подтверждён."),
        "user_id": s.UUIDField(help_text="ID пользователя (уже авторизованного), получившего доступ."),
        "email": s.EmailField(help_text="Email вошедшего пользователя."),
        "role": s.CharField(help_text="Роль в компании: owner / manager."),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Кто имеет доступ к компании",
        description=(
            'Список активных участников (SupplierUser) с ролями — аналог диалога "Поделиться '
            'доступом" в Google Docs. Требует сервис-токен, staff или активного участника (owner или '
            "manager) этой компании (тот же круг, что и у приглашений/заявок на вступление)."
        ),
        responses={200: SupplierUserSerializer(many=True)},
    ),
)
class SupplierUserListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id):
        team = list_supplier_team(supplier_id=supplier_id)
        return Response(SupplierUserSerializer(team, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Отозвать доступ участника",
        description=(
            "Убирает участника из компании (status=inactive, не удаление строки — повторное "
            "приглашение того же email реактивирует ту же запись). Нельзя отозвать доступ самому "
            "себе и нельзя отозвать последнего активного owner'а компании — 400 в обоих случаях "
            "(owner — только бухгалтерская метка для этого инварианта, права у owner и manager "
            "одинаковые). Требует сервис-токен, staff или активного участника (owner или manager) "
            "этой компании."
        ),
        request=None,
        responses={
            200: SupplierUserSerializer,
            400: OpenApiResponse(description="Отзыв доступа самому себе или последнему owner'у"),
            404: OpenApiResponse(description="Участник не найден или уже неактивен"),
        },
    ),
)
class SupplierUserRevokeView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id, user_id):
        requested_by_user_id = request.user.id if isinstance(request.user, User) else None
        membership = revoke_supplier_user(
            supplier_id=supplier_id,
            target_user_id=user_id,
            requested_by_user_id=requested_by_user_id,
        )
        return Response(SupplierUserSerializer(membership).data)


@extend_schema_view(
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Сменить роль участника",
        description=(
            "Меняет роль активного участника (owner/manager — права одинаковые, owner нужен только "
            'для инварианта "у компании ровно один активный owner"). Нельзя понизить в роли '
            "последнего активного owner'а компании — 400. Назначение new_role=owner автоматически "
            "понижает предыдущего владельца до manager (2026-08-06, доступ сохраняется — 2026-08-07) — "
            "поэтому назначить нового владельца может только текущий владелец, staff или сервис-токен, "
            "не manager — 403 иначе (2026-08-06, находка ревью: иначе manager мог бы одним запросом "
            "лишить владельца статуса owner'а)."
        ),
        request=SupplierUserRoleChangeSerializer,
        responses={
            200: SupplierUserSerializer,
            400: OpenApiResponse(description="Понижение последнего активного owner'а"),
            403: OpenApiResponse(description="new_role=owner от кого-то, кроме текущего owner/staff"),
            404: OpenApiResponse(description="Участник не найден или уже неактивен"),
        },
    ),
)
class SupplierUserRoleChangeView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def patch(self, request, supplier_id, user_id):
        serializer = SupplierUserRoleChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_role = serializer.validated_data["role"]

        if new_role == SupplierUserRole.OWNER and not is_current_supplier_owner_or_staff(request, supplier_id):
            raise PermissionDenied("Назначить нового владельца компании может только текущий владелец.")

        requested_by_user_id = request.user.id if isinstance(request.user, User) else None
        membership = change_supplier_user_role(
            supplier_id=supplier_id,
            target_user_id=user_id,
            new_role=new_role,
            requested_by_user_id=requested_by_user_id,
        )
        return Response(SupplierUserSerializer(membership).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Список неподтверждённых email-приглашений",
        description=(
            "Приглашения, отправленные владельцем или менеджером и ещё не принятые (не путать с "
            "join-requests/pending — это заявки от людей, самостоятельно нашедших компанию по ИНН). "
            "Требует сервис-токен, staff или активного участника (owner или manager) этой компании."
        ),
        responses={200: SupplierUserInviteSerializer(many=True)},
    ),
)
class SupplierUserInvitePendingListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id):
        invites = list_pending_supplier_user_invites(supplier_id=supplier_id)
        return Response(SupplierUserInviteSerializer(invites, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Пригласить сотрудника в ЛК компании по email",
        description=(
            "Добавляет ещё одного активного пользователя компании с указанной ролью (по умолчанию "
            "manager). role=manager не трогает существующего владельца — доступно owner или manager. "
            "role=owner — компания может иметь только одного активного владельца (2026-08-06): принятие "
            "такого приглашения автоматически понижает текущего owner'а до manager, доступ сохраняется "
            "(2026-08-07) — поэтому создать приглашение с role=owner может только текущий владелец, "
            "staff или сервис-токен, не manager — 403 иначе (2026-08-06, находка ревью)."
        ),
        request=SupplierUserInviteCreateSerializer,
        responses={
            201: _SUPPLIER_USER_INVITE_CREATE_SCHEMA,
            403: OpenApiResponse(description="role=owner от кого-то, кроме текущего owner/staff"),
            503: OpenApiResponse(description="Не удалось отправить письмо — сбой SMTP, повторите позже"),
        },
    ),
)
class SupplierUserInviteCreateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id):
        serializer = SupplierUserInviteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.validated_data["role"]

        if role == SupplierUserRole.OWNER and not is_current_supplier_owner_or_staff(request, supplier_id):
            raise PermissionDenied("Пригласить нового владельца компании может только текущий владелец.")

        invited_by_user_id = request.user.id if isinstance(request.user, User) else None

        try:
            invite = invite_supplier_user(
                supplier_id=supplier_id,
                target_email=serializer.validated_data["target_email"],
                role=role,
                invited_by_user_id=invited_by_user_id,
            )
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {
                "id": invite.id,
                "supplier_id": invite.supplier_id,
                "target_email": invite.target_email,
                "role": invite.role,
                "status": invite.status,
                "expires_at": invite.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Превью приглашения — без побочных эффектов",
        description=(
            "Публичный endpoint, ничего не меняет (в отличие от accept — не переводит "
            "истёкший инвайт в EXPIRED). Нужен фронту, чтобы до формы задать пароль "
            "сравнить target_email с уже залогиненным юзером в браузере — если юзер "
            "залогинен другим email, можно предупредить, что примет инвайт именно указанный "
            "здесь target_email, а не текущая сессия."
        ),
        request=None,
        responses={200: _SUPPLIER_USER_INVITE_PREVIEW_SCHEMA},
    ),
)
class SupplierUserInvitePreviewView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token):
        result = get_supplier_user_invite_preview(token=token)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Принять приглашение в ЛК компании",
        description=(
            "2026-08-07 (по явному решению пользователя): требует core.User JWT, role=supplier — "
            "никаких email/паролей в теле. Если у вызывающего ещё нет аккаунта — сначала обычная "
            "регистрация (POST /auth/supplier/register + подтверждение email), потом переход по этой "
            "ссылке уже залогиненным. Email вошедшего должен совпадать с target_email инвайта "
            "(см. GET .../invites/{token} для превью) — иначе 400. Если у приглашения role=owner — "
            "компания может иметь только одного активного владельца (2026-08-06), принятие "
            "автоматически понижает предыдущего до manager, доступ сохраняется (2026-08-07)."
        ),
        request=None,
        responses={200: _SUPPLIER_USER_INVITE_ACCEPT_SCHEMA},
    ),
)
class SupplierUserInviteAcceptView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsSupplierUser]

    def post(self, request, token):
        result = accept_supplier_user_invite(token=token, user=request.user)
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Ссылка-приглашение компании (получить/создать)",
        description=(
            "Многоразовая альтернатива приглашению по email — одна ссылка на компанию, "
            "присоединиться может кто угодно, у кого она есть. Первый вызов создаёт ссылку, "
            "дальше всегда отдаёт тот же token. Требует сервис-токен, staff или активного участника "
            "(owner или manager)."
        ),
        responses={200: SupplierInviteLinkSerializer},
    ),
    patch=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Настройки ссылки-приглашения",
        description=(
            "Роль присоединившегося, срок действия, включена/выключена. Все поля опциональны. "
            "Установить role=owner может только текущий владелец, staff или сервис-токен, не manager "
            "— 403 иначе (2026-08-06, находка ревью: присоединение по такой ссылке деактивирует "
            "текущего owner'а, см. GET /suppliers/invite-link/{token}/join)."
        ),
        request=SupplierInviteLinkUpdateSerializer,
        responses={
            200: SupplierInviteLinkSerializer,
            403: OpenApiResponse(description="role=owner от кого-то, кроме текущего owner/staff"),
        },
    ),
)
class SupplierInviteLinkView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id):
        link = get_or_create_supplier_invite_link(supplier_id=supplier_id)
        return Response(SupplierInviteLinkSerializer(link).data)

    def patch(self, request, supplier_id):
        serializer = SupplierInviteLinkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("role") == SupplierUserRole.OWNER and not is_current_supplier_owner_or_staff(
            request, supplier_id
        ):
            raise PermissionDenied("Настроить ссылку на назначение владельцем может только текущий владелец.")

        link = update_supplier_invite_link(supplier_id=supplier_id, data=serializer.validated_data)
        return Response(SupplierInviteLinkSerializer(link).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Перевыпустить ссылку-приглашение",
        description="Старый token перестаёт работать, настройки (роль/срок/статус) не трогаются.",
        request=None,
        responses={200: SupplierInviteLinkSerializer},
    ),
)
class SupplierInviteLinkRegenerateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id):
        link = regenerate_supplier_invite_link(supplier_id=supplier_id)
        return Response(SupplierInviteLinkSerializer(link).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Превью ссылки-приглашения — без побочных эффектов",
        description=(
            "Публичный endpoint, ничего не меняет и не требует авторизации. Нужен фронту, чтобы "
            "показать «вас приглашают в компанию X с ролью Y» до клика «присоединиться» "
            "(POST .../join), не отправляя join вслепую."
        ),
        request=None,
        responses={200: _SUPPLIER_INVITE_LINK_PREVIEW_SCHEMA},
    ),
)
class SupplierInviteLinkPreviewView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token):
        result = get_supplier_invite_link_preview(token=token)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Присоединиться к компании по ссылке-приглашению",
        description=(
            "2026-08-07 (по явному решению пользователя): требует core.User JWT, role=supplier — "
            "никаких email/паролей в теле, ссылка ни к какому email не привязана. Если у вызывающего "
            "ещё нет аккаунта — сначала обычная регистрация (POST /auth/supplier/register + "
            "подтверждение email), потом переход по этой ссылке уже залогиненным. Если у ссылки "
            "role=owner — компания может иметь только одного активного владельца (2026-08-06), "
            "присоединение автоматически понижает предыдущего до manager, доступ сохраняется (2026-08-07)."
        ),
        request=None,
        responses={200: _SUPPLIER_USER_INVITE_ACCEPT_SCHEMA},
    ),
)
class SupplierInviteLinkJoinView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsSupplierUser]

    def post(self, request, token):
        result = join_supplier_via_invite_link(token=token, user=request.user)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Запросить присоединение к существующей компании",
        description=(
            "Авторизованный пользователь отправляет заявку на присоединение к уже существующей "
            "активной компании (найденной по ИНН через search-by-inn). Заявку одобряет/отклоняет "
            "любой активный участник этой компании (owner или manager) из своего ЛК. О новой заявке "
            "уведомляется только служебный адрес (SUPPLIER_JOIN_REQUEST_NOTIFICATION_EMAIL) — "
            "участникам компании email не приходит. "
            "Опционально: описание ситуации (`message`), контактные `phone`/`email` для этой заявки "
            "(может отличаться от данных аккаунта) и `documents` — подтверждающие файлы (multipart)."
        ),
        request=SupplierJoinRequestCreateSerializer,
        responses={201: SupplierJoinRequestSerializer},
    ),
)
class SupplierJoinRequestCreateView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]
    # JSON остаётся для обратной совместимости (заявка без документов, как раньше);
    # multipart — новый путь, когда нужно приложить файлы.
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request, supplier_id):
        serializer = SupplierJoinRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        join_request = create_supplier_join_request(
            supplier_id=supplier_id,
            requested_by_user_id=request.user.id,
            message=serializer.validated_data.get("message"),
            phone=serializer.validated_data.get("phone"),
            email=serializer.validated_data.get("email"),
            documents=serializer.validated_data.get("documents"),
        )
        return Response(SupplierJoinRequestSerializer(join_request).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Заявки на присоединение к моей компании",
        description=(
            "Заявки в статусе pending на присоединение к этой компании. Требует сервис-токен, "
            "staff или активного участника (owner или manager) этой компании."
        ),
        responses={200: SupplierJoinRequestSerializer(many=True)},
    ),
)
class SupplierJoinRequestListView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def get(self, request, supplier_id):
        pending_requests = list_supplier_join_requests(supplier_id=supplier_id)
        return Response(SupplierJoinRequestSerializer(pending_requests, many=True).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Одобрить заявку на присоединение",
        description=(
            "Добавляет заявителя как участника компании (role=manager), не трогая владельца — "
            "как приглашение по email, но по инициативе заявителя. Требует сервис-токен, staff "
            "или активного участника (owner или manager) этой компании."
        ),
        request=None,
        responses={200: SupplierJoinRequestSerializer},
    ),
)
class SupplierJoinRequestApproveView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id, request_id):
        decided_by_user_id = request.user.id if isinstance(request.user, User) else None
        join_request = decide_supplier_join_request(
            supplier_id=supplier_id,
            request_id=request_id,
            decided_by_user_id=decided_by_user_id,
            approve=True,
        )
        return Response(SupplierJoinRequestSerializer(join_request).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Отклонить заявку на присоединение",
        description=("Требует сервис-токен, staff или активного участника (owner или manager) этой компании."),
        request=None,
        responses={200: SupplierJoinRequestSerializer},
    ),
)
class SupplierJoinRequestRejectView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id, request_id):
        decided_by_user_id = request.user.id if isinstance(request.user, User) else None
        join_request = decide_supplier_join_request(
            supplier_id=supplier_id,
            request_id=request_id,
            decided_by_user_id=decided_by_user_id,
            approve=False,
        )
        return Response(SupplierJoinRequestSerializer(join_request).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Мои заявки на присоединение",
        description="Все заявки текущего пользователя на присоединение к чужим компаниям, любого статуса.",
        responses={200: SupplierJoinRequestSerializer(many=True)},
    ),
)
class SupplierJoinRequestMineView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        my_requests = list_my_supplier_join_requests(user_id=request.user.id)
        return Response(SupplierJoinRequestSerializer(my_requests, many=True).data)
