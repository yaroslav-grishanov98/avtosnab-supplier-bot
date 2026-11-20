import secrets
import urllib.parse

from django.conf import settings
from django.core import signing
from django.http import HttpResponseRedirect
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
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.auth.serializers import (
    BootstrapAdminUserSerializer,
    ForgotPasswordSerializer,
    PasswordResetConfirmSerializer,
    ProfileUpdateSerializer,
    SupplierLoginSerializer,
    SupplierRegisterSerializer,
    VenueLoginSerializer,
    VenueRegisterSerializer,
)
from api.v1.auth.services import (
    EmailDeliveryError,
    bootstrap_admin_user,
    confirm_email_verification,
    confirm_password_reset,
    get_user_profile,
    initiate_password_reset,
    login_or_register_supplier_via_vk,
    login_or_register_supplier_via_yandex,
    login_or_register_venue_user_via_vk,
    login_or_register_venue_user_via_yandex,
    login_supplier,
    login_venue_user,
    register_supplier,
    register_venue_user,
    update_own_profile,
)
from api.v1.auth.vk_client import VKOAuthError
from api.v1.auth.vk_client import exchange_code_for_token as exchange_code_for_vk_token
from api.v1.auth.vk_client import fetch_user_info as fetch_vk_user_info
from api.v1.auth.vk_client import generate_pkce_pair as generate_vk_pkce_pair
from api.v1.auth.yandex_client import YandexOAuthError, exchange_code_for_token, fetch_user_info
from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.permissions import IsCoreUser, IsSupplierOrVenueUser

_YANDEX_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
_YANDEX_INTENTS = ("supplier", "venue")

_VK_AUTHORIZE_URL = "https://id.vk.ru/authorize"
_VK_INTENTS = ("supplier", "venue")
_VK_STATE_COOKIE_NAME = "vk_oauth_state"
_VK_STATE_SALT = "vk-oauth-state"
_VK_STATE_MAX_AGE_SECONDS = 600
_VK_STATE_COOKIE_PATH = "/api/v1/auth/vk"

# Anti-CSRF для OAuth-редиректа: nonce живёт только в httponly-куке между шагом
# login и callback (один и тот же браузер), intent восстанавливается из куки, а не
# из query state — иначе атакующий мог бы прислать жертве ссылку со своим валидным
# code/state и залогинить её браузер под своим Яндекс-аккаунтом (login CSRF).
_YANDEX_STATE_COOKIE_NAME = "yandex_oauth_state"
_YANDEX_STATE_SALT = "yandex-oauth-state"
_YANDEX_STATE_MAX_AGE_SECONDS = 600
_YANDEX_STATE_COOKIE_PATH = "/api/v1/auth/yandex"

_REGISTER_RESPONSE_SCHEMA = inline_serializer(
    name="SupplierRegisterResponse",
    fields={
        "user_id": s.UUIDField(help_text="ID созданного core.User."),
        "email": s.EmailField(help_text="Email, на который отправлено письмо-подтверждение."),
        "status": s.CharField(help_text="Статус аккаунта: всегда draft — до подтверждения email по ссылке."),
    },
)

_JWT_EMAIL_RESPONSE_SCHEMA = inline_serializer(
    name="AuthJWTEmailResponse",
    fields={
        "user_id": s.UUIDField(help_text="ID вошедшего пользователя."),
        "email": s.EmailField(help_text="Email вошедшего пользователя."),
        "access": s.CharField(help_text="JWT access-токен — подставлять в Authorization: Bearer <access>."),
        "refresh": s.CharField(help_text="JWT refresh-токен — для обновления access без повторного логина."),
    },
)

_JWT_USERNAME_RESPONSE_SCHEMA = inline_serializer(
    name="AuthJWTUsernameResponse",
    fields={
        "user_id": s.UUIDField(help_text="ID вошедшего пользователя."),
        "username": s.CharField(help_text="Логин вошедшего пользователя."),
        "access": s.CharField(help_text="JWT access-токен — подставлять в Authorization: Bearer <access>."),
        "refresh": s.CharField(help_text="JWT refresh-токен — для обновления access без повторного логина."),
    },
)

_DETAIL_RESPONSE_SCHEMA = inline_serializer(
    name="AuthDetailResponse", fields={"detail": s.CharField(help_text="Человекочитаемое сообщение для пользователя.")}
)

_VENUE_ID_FIELD = s.UUIDField(
    allow_null=True, help_text="ID заведения (только для role=venue_user). null, если ещё не назначено."
)
_VENUE_NAME_FIELD = s.CharField(allow_null=True, help_text="Название заведения. null, если venue_id не задан.")

_JWT_USERNAME_VENUE_RESPONSE_SCHEMA = inline_serializer(
    name="AuthJWTUsernameVenueResponse",
    fields={
        "user_id": s.UUIDField(help_text="ID вошедшего пользователя."),
        "username": s.CharField(help_text="Логин вошедшего пользователя."),
        "venue_id": _VENUE_ID_FIELD,
        "venue_name": _VENUE_NAME_FIELD,
        "access": s.CharField(help_text="JWT access-токен — подставлять в Authorization: Bearer <access>."),
        "refresh": s.CharField(help_text="JWT refresh-токен — для обновления access без повторного логина."),
    },
)

_ME_RESPONSE_SCHEMA = inline_serializer(
    name="AuthMeResponse",
    fields={
        "user_id": s.UUIDField(help_text="ID пользователя."),
        "email": s.EmailField(help_text="Email пользователя."),
        "full_name": s.CharField(help_text="ФИО пользователя."),
        "first_name": s.CharField(allow_null=True, help_text="Имя. null, если не заполнено."),
        "last_name": s.CharField(allow_null=True, help_text="Фамилия. null, если не заполнено."),
        "position": s.CharField(allow_null=True, help_text="Должность — свободный текст. null, если не заполнено."),
        "phone": s.CharField(allow_null=True, help_text="Телефон. null, если не заполнен."),
        "timezone": s.CharField(
            allow_null=True, help_text="IANA-имя часового пояса (например Europe/Moscow). null, если не заполнено."
        ),
        "role": s.CharField(help_text="Роль: supplier / venue_user / admin."),
        "status": s.CharField(help_text="Статус аккаунта."),
        "created_at": s.DateTimeField(help_text="Время регистрации."),
        "updated_at": s.DateTimeField(help_text="Время последнего изменения профиля."),
        "last_login_at": s.DateTimeField(allow_null=True, help_text="Последний раз заходил в сервис. null — ни разу."),
        "venue_id": _VENUE_ID_FIELD,
        "venue_name": _VENUE_NAME_FIELD,
        "has_supplier_company": s.BooleanField(
            help_text=(
                "role=supplier: есть ли хотя бы одна активная компания (SupplierUser). Если false — "
                "не дёргать /suppliers/cabinet/session-from-login (даст 404), а предложить создать "
                "компанию через POST /suppliers/mine, не обязательно сразу. Для остальных ролей — false."
            )
        ),
        "telegram_user_id": s.CharField(allow_null=True, help_text="Системное поле — Telegram user id из бота."),
        "telegram_chat_id": s.CharField(allow_null=True, help_text="Системное поле — Telegram chat id из бота."),
        "telegram_linked": s.BooleanField(help_text="Есть ли активная привязка к Telegram-боту."),
        "max_user_id": s.CharField(allow_null=True, help_text="Системное поле — Max user id из бота."),
        "max_chat_id": s.CharField(allow_null=True, help_text="Системное поле — Max chat id из бота."),
        "max_linked": s.BooleanField(help_text="Есть ли активная привязка к Max-боту."),
    },
)


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Регистрация поставщика — email + пароль",
        description="Публичный endpoint. Отправляет письмо для подтверждения адреса.",
        request=SupplierRegisterSerializer,
        responses={
            201: _REGISTER_RESPONSE_SCHEMA,
            503: OpenApiResponse(description="Не удалось отправить письмо подтверждения — сбой SMTP, повторите позже"),
        },
    ),
)
class SupplierRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SupplierRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = register_supplier(**serializer.validated_data)
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {"user_id": user.id, "email": user.email, "status": user.status},
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Подтвердить email после регистрации",
        description="Публичный endpoint, вызывается со страницы по ссылке из письма. Возвращает JWT-пару.",
        request=None,
        responses={200: _JWT_EMAIL_RESPONSE_SCHEMA},
    ),
)
class EmailVerificationConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token):
        result = confirm_email_verification(token=token)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Вход поставщика — email + пароль",
        request=SupplierLoginSerializer,
        responses={200: _JWT_EMAIL_RESPONSE_SCHEMA},
    ),
)
class SupplierLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SupplierLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = login_supplier(**serializer.validated_data)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Первый admin — разовый бутстрап",
        description=(
            "Защищён SERVICE_API_TOKEN, не JWT. Создаёт ровно один admin аккаунт — как только "
            "такой аккаунт уже есть в системе, endpoint навсегда возвращает 400. Все следующие "
            "сотрудники заводятся через Django admin, не через этот endpoint."
        ),
        request=BootstrapAdminUserSerializer,
        responses={201: _JWT_USERNAME_RESPONSE_SCHEMA},
    ),
)
class BootstrapAdminUserView(APIView):
    def post(self, request):
        serializer = BootstrapAdminUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = bootstrap_admin_user(**serializer.validated_data)
        return Response(result, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Регистрация заведения/покупателя — email + пароль",
        description=(
            "Публичный endpoint, самостоятельная регистрация (по аналогии с Ozon-покупателем) — "
            "не заменяет admin-провижининг сотрудников (Django admin может создать аккаунт вручную, "
            "входит он всё равно по email через venue/login). Отправляет письмо для подтверждения адреса."
        ),
        request=VenueRegisterSerializer,
        responses={
            201: _REGISTER_RESPONSE_SCHEMA,
            503: OpenApiResponse(description="Не удалось отправить письмо подтверждения — сбой SMTP, повторите позже"),
        },
    ),
)
class VenueRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VenueRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = register_venue_user(**serializer.validated_data)
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {"user_id": user.id, "email": user.email, "status": user.status},
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Вход заведения/закупщика — email + пароль",
        description=(
            "Единственное поле для входа — email (у core.User он обязателен и уникален для всех ролей, "
            "включая admin-provisioned сотрудников/снабженцев). Работает и для аккаунтов, созданных "
            "администратором, и для самостоятельной регистрации через venue/register или Яндекс."
        ),
        request=VenueLoginSerializer,
        responses={200: _JWT_USERNAME_VENUE_RESPONSE_SCHEMA},
    ),
)
class VenueLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VenueLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = login_venue_user(**serializer.validated_data)
        return Response(result)


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Я забыл пароль — запросить ссылку восстановления",
        description="Ответ одинаковый независимо от того, зарегистрирован ли email — чтобы нельзя было перебором проверить существующие адреса.",
        request=ForgotPasswordSerializer,
        responses={200: _DETAIL_RESPONSE_SCHEMA},
    ),
)
class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        initiate_password_reset(**serializer.validated_data)
        return Response({"detail": "Если такой email зарегистрирован, мы отправили инструкции по восстановлению."})


@extend_schema_view(
    post=extend_schema(
        tags=["Авторизация"],
        summary="Задать новый пароль по ссылке восстановления",
        description="Публичный endpoint, вызывается со страницы по ссылке из письма. Возвращает JWT-пару.",
        request=PasswordResetConfirmSerializer,
        responses={200: _JWT_EMAIL_RESPONSE_SCHEMA},
    ),
)
class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = confirm_password_reset(token=token, new_password=serializer.validated_data["new_password"])
        return Response(result)


def _oauth_error_message(error):
    if isinstance(error, ValidationError):
        return str(error.detail["detail"])
    return str(error)


@extend_schema_view(
    get=extend_schema(
        tags=["Авторизация"],
        summary="Войти через Яндекс — редирект на страницу авторизации Яндекса",
        description=(
            "Дополнительный способ входа, не заменяет email/пароль (поставщик) и username/пароль "
            "(заведение). `intent` определяет, для кого этот вход и куда попадёт callback. Ставит "
            "httponly-куку с подписанным nonce — callback сверяет её с `state`, чтобы отличить свой "
            "браузер от чужого (anti-CSRF)."
        ),
        parameters=[
            OpenApiParameter(
                "intent", OpenApiTypes.STR, description="'supplier' или 'venue' — кто логинится", required=True
            )
        ],
        responses={302: OpenApiResponse(description="Редирект на oauth.yandex.ru")},
    ),
)
class YandexOAuthLoginView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        intent = request.query_params.get("intent")
        if intent not in _YANDEX_INTENTS:
            return Response(
                {"detail": "intent должен быть 'supplier' или 'venue'."}, status=status.HTTP_400_BAD_REQUEST
            )

        nonce = secrets.token_urlsafe(24)
        signed_state = signing.dumps({"intent": intent, "nonce": nonce}, salt=_YANDEX_STATE_SALT)

        params = {
            "response_type": "code",
            "client_id": settings.YANDEX_OAUTH_CLIENT_ID,
            "redirect_uri": settings.YANDEX_OAUTH_REDIRECT_URI,
            "state": nonce,
        }
        response = HttpResponseRedirect(f"{_YANDEX_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")
        response.set_cookie(
            _YANDEX_STATE_COOKIE_NAME,
            signed_state,
            max_age=_YANDEX_STATE_MAX_AGE_SECONDS,
            httponly=True,
            # request.is_secure() всегда False за nginx (TLS терминируется до Django,
            # SECURE_PROXY_SSL_HEADER в проекте не настроен) — используем тот же
            # признак прод/дев, что и остальной проект (DJANGO_DEBUG).
            secure=not settings.DEBUG,
            samesite="Lax",
            path=_YANDEX_STATE_COOKIE_PATH,
        )
        return response


@extend_schema_view(
    get=extend_schema(
        tags=["Авторизация"],
        summary="Callback от Яндекса — обменивает код на JWT и редиректит на фронт",
        description=(
            "Публичный endpoint, redirect_uri Яндекс OAuth-приложения. Не вызывается напрямую фронтом — "
            "фронт получает результат через редирект на YANDEX_OAUTH_FRONTEND_CALLBACK_URL с access/refresh "
            "в query (или error при сбое)."
        ),
        parameters=[
            OpenApiParameter("code", OpenApiTypes.STR, description="Код авторизации от Яндекса"),
            OpenApiParameter(
                "state",
                OpenApiTypes.STR,
                description="Одноразовый nonce, выданный на шаге login (сверяется с httponly-кукой, не самодостаточен).",
            ),
        ],
        responses={302: OpenApiResponse(description="Редирект на фронтенд с access/refresh либо error в query")},
    ),
)
class YandexOAuthCallbackView(APIView):
    permission_classes = [AllowAny]

    def _resolve_intent(self, request):
        received_nonce = request.query_params.get("state")
        signed_cookie = request.COOKIES.get(_YANDEX_STATE_COOKIE_NAME)
        if not received_nonce or not signed_cookie:
            return None
        try:
            payload = signing.loads(signed_cookie, salt=_YANDEX_STATE_SALT, max_age=_YANDEX_STATE_MAX_AGE_SECONDS)
        except signing.BadSignature:
            return None
        if not secrets.compare_digest(payload.get("nonce", ""), received_nonce):
            return None
        return payload.get("intent")

    def get(self, request):
        intent = self._resolve_intent(request)
        code = request.query_params.get("code")
        frontend_url = settings.YANDEX_OAUTH_FRONTEND_CALLBACK_URL.rstrip("/")

        if not code or intent not in _YANDEX_INTENTS:
            query = urllib.parse.urlencode({"error": "invalid_request", "intent": intent or ""})
            response = HttpResponseRedirect(f"{frontend_url}?{query}")
            response.delete_cookie(_YANDEX_STATE_COOKIE_NAME, path=_YANDEX_STATE_COOKIE_PATH)
            return response

        try:
            access_token = exchange_code_for_token(code)
            user_info = fetch_user_info(access_token)
            if intent == "supplier":
                result = login_or_register_supplier_via_yandex(
                    yandex_id=user_info["yandex_id"],
                    email=user_info["email"],
                    full_name=user_info["full_name"],
                )
            else:
                result = login_or_register_venue_user_via_yandex(
                    yandex_id=user_info["yandex_id"],
                    email=user_info["email"],
                    full_name=user_info["full_name"],
                )
        except (YandexOAuthError, ValidationError) as error:
            query = urllib.parse.urlencode({"error": _oauth_error_message(error), "intent": intent})
            response = HttpResponseRedirect(f"{frontend_url}?{query}")
            response.delete_cookie(_YANDEX_STATE_COOKIE_NAME, path=_YANDEX_STATE_COOKIE_PATH)
            return response

        query = urllib.parse.urlencode({"access": result["access"], "refresh": result["refresh"], "intent": intent})
        response = HttpResponseRedirect(f"{frontend_url}?{query}")
        response.delete_cookie(_YANDEX_STATE_COOKIE_NAME, path=_YANDEX_STATE_COOKIE_PATH)
        return response


@extend_schema_view(
    get=extend_schema(
        tags=["Авторизация"],
        summary="Войти через VK ID — редирект на страницу авторизации VK",
        description=(
            "Дополнительный способ входа, не заменяет email/пароль (поставщик) и username/пароль "
            "(заведение) — работает точно так же, как вход через Яндекс. `intent` определяет, для кого "
            "этот вход и куда попадёт callback. VK ID требует PKCE (OAuth 2.1) — code_verifier хранится "
            "в той же httponly-куке с подписанным nonce, что и intent (anti-CSRF, без внешнего кэша)."
        ),
        parameters=[
            OpenApiParameter(
                "intent", OpenApiTypes.STR, description="'supplier' или 'venue' — кто логинится", required=True
            )
        ],
        responses={302: OpenApiResponse(description="Редирект на id.vk.ru")},
    ),
)
class VKOAuthLoginView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        intent = request.query_params.get("intent")
        if intent not in _VK_INTENTS:
            return Response(
                {"detail": "intent должен быть 'supplier' или 'venue'."}, status=status.HTTP_400_BAD_REQUEST
            )

        nonce = secrets.token_urlsafe(24)
        code_verifier, code_challenge = generate_vk_pkce_pair()
        signed_state = signing.dumps(
            {"intent": intent, "nonce": nonce, "code_verifier": code_verifier}, salt=_VK_STATE_SALT
        )

        params = {
            "response_type": "code",
            "client_id": settings.VK_OAUTH_CLIENT_ID,
            "redirect_uri": settings.VK_OAUTH_REDIRECT_URI,
            "scope": "email",
            "state": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        response = HttpResponseRedirect(f"{_VK_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")
        response.set_cookie(
            _VK_STATE_COOKIE_NAME,
            signed_state,
            max_age=_VK_STATE_MAX_AGE_SECONDS,
            httponly=True,
            secure=not settings.DEBUG,
            samesite="Lax",
            path=_VK_STATE_COOKIE_PATH,
        )
        return response


@extend_schema_view(
    get=extend_schema(
        tags=["Авторизация"],
        summary="Callback от VK ID — обменивает код на JWT и редиректит на фронт",
        description=(
            "Публичный endpoint, redirect_uri VK ID-приложения. Не вызывается напрямую фронтом — "
            "фронт получает результат через редирект на VK_OAUTH_FRONTEND_CALLBACK_URL с access/refresh "
            "в query (или error при сбое)."
        ),
        parameters=[
            OpenApiParameter("code", OpenApiTypes.STR, description="Код авторизации от VK ID"),
            OpenApiParameter(
                "state",
                OpenApiTypes.STR,
                description="Одноразовый nonce, выданный на шаге login (сверяется с httponly-кукой, не самодостаточен).",
            ),
            OpenApiParameter(
                "device_id", OpenApiTypes.STR, description="device_id, который VK ID возвращает вместе с code"
            ),
        ],
        responses={302: OpenApiResponse(description="Редирект на фронтенд с access/refresh либо error в query")},
    ),
)
class VKOAuthCallbackView(APIView):
    permission_classes = [AllowAny]

    def _resolve_state(self, request):
        received_nonce = request.query_params.get("state")
        signed_cookie = request.COOKIES.get(_VK_STATE_COOKIE_NAME)
        if not received_nonce or not signed_cookie:
            return None
        try:
            payload = signing.loads(signed_cookie, salt=_VK_STATE_SALT, max_age=_VK_STATE_MAX_AGE_SECONDS)
        except signing.BadSignature:
            return None
        if not secrets.compare_digest(payload.get("nonce", ""), received_nonce):
            return None
        return payload

    def get(self, request):
        state_payload = self._resolve_state(request)
        intent = state_payload.get("intent") if state_payload else None
        code = request.query_params.get("code")
        device_id = request.query_params.get("device_id")
        frontend_url = settings.VK_OAUTH_FRONTEND_CALLBACK_URL.rstrip("/")

        if not code or intent not in _VK_INTENTS:
            query = urllib.parse.urlencode({"error": "invalid_request", "intent": intent or ""})
            response = HttpResponseRedirect(f"{frontend_url}?{query}")
            response.delete_cookie(_VK_STATE_COOKIE_NAME, path=_VK_STATE_COOKIE_PATH)
            return response

        try:
            token_response = exchange_code_for_vk_token(
                code, code_verifier=state_payload["code_verifier"], device_id=device_id
            )
            user_info = fetch_vk_user_info(token_response)
            if intent == "supplier":
                result = login_or_register_supplier_via_vk(
                    vk_id=user_info["vk_id"],
                    email=user_info["email"],
                    full_name=user_info["full_name"],
                )
            else:
                result = login_or_register_venue_user_via_vk(
                    vk_id=user_info["vk_id"],
                    email=user_info["email"],
                    full_name=user_info["full_name"],
                )
        except (VKOAuthError, ValidationError) as error:
            query = urllib.parse.urlencode({"error": _oauth_error_message(error), "intent": intent})
            response = HttpResponseRedirect(f"{frontend_url}?{query}")
            response.delete_cookie(_VK_STATE_COOKIE_NAME, path=_VK_STATE_COOKIE_PATH)
            return response

        query = urllib.parse.urlencode({"access": result["access"], "refresh": result["refresh"], "intent": intent})
        response = HttpResponseRedirect(f"{frontend_url}?{query}")
        response.delete_cookie(_VK_STATE_COOKIE_NAME, path=_VK_STATE_COOKIE_PATH)
        return response


@extend_schema_view(
    get=extend_schema(
        tags=["Авторизация"],
        summary="Мой профиль — кто я и к какому заведению отношусь",
        description=(
            "Требует JWT core.User (любая роль — supplier/venue_user/admin). Фронт вызывает "
            "сразу после логина/OAuth-колбэка, чтобы узнать `venue_id` — он нужен дальше для "
            "`GET/POST /catalog/venues/{venue_id}/...` (избранное, корзина, заказы). `venue_id`/`venue_name` — "
            "`null`, если пользователю ещё не назначено заведение (назначается пока только вручную через "
            "Django admin — самостоятельного создания/привязки заведения нет). Для role=supplier — "
            "`has_supplier_company`: если `false`, у пользователя пока нет ни одной компании — предложить "
            "создать через `POST /suppliers/mine`, не блокируя вход. `telegram_username`/`telegram_linked` — "
            "системные поля, заполняются автоматически после привязки Telegram-бота."
        ),
        responses={200: _ME_RESPONSE_SCHEMA},
    ),
    patch=extend_schema(
        tags=["Авторизация"],
        summary="Редактировать мой профиль (поставщики и сотрудники заведений)",
        description=(
            "Имя/фамилия/должность/телефон/email/часовой пояс — role=supplier или role=venue_user, "
            "остальным (admin) 403. Должность — свободный текст, без фиксированного списка. Часовой "
            "пояс — IANA-имя (например Europe/Moscow), фронт определяет через браузер и предзаполняет, "
            "поле остаётся редактируемым. full_name пересчитывается из first_name+last_name, если хотя "
            "бы одно из них передано в запросе."
        ),
        request=ProfileUpdateSerializer,
        responses={200: _ME_RESPONSE_SCHEMA},
    ),
)
class MeView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]

    def get_permissions(self):
        if self.request.method == "PATCH":
            return [IsCoreUser(), IsSupplierOrVenueUser()]
        return [IsCoreUser()]

    def get(self, request):
        return Response(get_user_profile(request.user))

    def patch(self, request):
        serializer = ProfileUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        update_own_profile(user=request.user, data=serializer.validated_data)
        return Response(get_user_profile(request.user))
