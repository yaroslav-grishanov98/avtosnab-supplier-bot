import logging
import secrets
import smtplib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import IntegrityError, connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import (
    EmailVerificationToken,
    EmailVerificationTokenStatus,
    EntityStatus,
    PasswordResetToken,
    PasswordResetTokenStatus,
    User,
    UserRole,
)
from suppliers.models import BotUserBinding, SourceChannel, SupplierUser

logger = logging.getLogger("autosnab.auth.services")


class EmailDeliveryError(Exception):
    """send_mail() failed (SMTP/network) — an infra hiccup, not a user input
    error. Callers should surface this as 503, not let it bubble up as an
    unhandled 500 with a full debug traceback."""


def _issue_jwt(user):
    # Single choke point for every login path (email+password, VK/Yandex
    # OAuth, email verification confirm, password reset confirm) — updating
    # last_login_at here means every one of them counts as a visit without
    # duplicating the write at each call site (2026-08-04). update_fields
    # deliberately excludes "updated_at" (auto_now) — a login isn't a
    # profile edit, and Django only persists auto_now fields that are
    # listed in update_fields, so leaving it out keeps it untouched.
    user.last_login_at = timezone.now()
    user.save(update_fields=["last_login_at"])
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def _generic_login_error():
    return ValidationError({"detail": "Неверный email/логин или пароль."})


def _venue_fields(user):
    # venue — nullable FK (2026-07-14, one user = one venue by design), только
    # для role=venue_user; admin act across all venues (see IsPlatformAdmin)
    # и не привязаны к одному venue.
    return {
        "venue_id": user.venue_id,
        "venue_name": user.venue.display_name if user.venue_id else None,
    }


def _supplier_fields(user):
    # Без этого фронту после логина/регистрации неоткуда узнать "есть ли
    # компания", кроме как дёрнуть suppliers/cabinet/session-from-login,
    # который жёстко отдаёт 404 без неё — реальная причина, почему свежий
    # supplier "не мог зайти" (2026-07-24). has_supplier_company=False —
    # не ошибка, просто сигнал показать "зарегистрируйте компанию" не
    # блокируя вход, а не сразу дёргать session-from-login.
    return {
        "has_supplier_company": SupplierUser.objects.filter(user=user, status=EntityStatus.ACTIVE).exists(),
    }


def _bot_channel_fields(user, *, channel, prefix):
    # 2026-08-04: "системные поля Telegram/Max, заполняются автоматически,
    # когда юзер проявил себя в боте" — читает BotUserBinding.user, который
    # раньше никогда не проставлялся (см. BotUserBindingRegisterView, теперь
    # проставляет link_token.created_by и при создании, и при перепривязке).
    # Один и тот же механизм для обоих каналов — Max отличается только
    # значением channel, отдельного кода под него не потребовалось.
    binding = (
        BotUserBinding.objects.filter(user=user, channel=channel, status=EntityStatus.ACTIVE)
        .order_by("-updated_at")
        .first()
    )
    return {
        f"{prefix}_user_id": binding.external_user_id if binding else None,
        f"{prefix}_chat_id": binding.chat_id if binding else None,
        f"{prefix}_linked": binding is not None,
    }


def get_user_profile(user):
    """Полный профиль текущего пользователя — GET /api/v1/auth/me. Фронт
    вызывает это сразу после логина/OAuth-колбэка, чтобы узнать venue_id
    (нужен для /catalog/venues/{venue_id}/... — корзина/избранное/заказы)
    и has_supplier_company (нужен для /suppliers/cabinet/session-from-login)."""
    return {
        "user_id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "position": user.position,
        "phone": user.phone,
        "timezone": user.timezone,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "last_login_at": user.last_login_at,
        **_venue_fields(user),
        **_supplier_fields(user),
        **_bot_channel_fields(user, channel=SourceChannel.TELEGRAM, prefix="telegram"),
        **_bot_channel_fields(user, channel=SourceChannel.MAX, prefix="max"),
    }


def update_own_profile(*, user, data):
    """PATCH /api/v1/auth/me, role=supplier или role=venue_user (2026-08-04:
    заполняемые поля профиля — имя/фамилия/должность/телефон/email/часовой
    пояс; должность — свободный текст, значения вводит сам пользователь, не
    фиксированный список; часовой пояс — фронт определяет через браузер и
    предзаполняет, но поле остаётся редактируемым, в коде нет гео-IP/OAuth
    источника timezone). full_name пересчитывается из first_name+last_name
    только если хотя бы одно из них реально пришло в запросе и результат
    непустой — иначе частичное обновление (например только position) не
    затирает существующее full_name, на которое завязан остальной код
    (заказы, письма и т.п.). Ничего здесь не специфично для supplier — до
    2026-08-19 ручка была искусственно закрыта для venue_user (см.
    IsSupplierOrVenueUser)."""
    update_fields = []

    if "first_name" in data:
        user.first_name = data["first_name"]
        update_fields.append("first_name")
    if "last_name" in data:
        user.last_name = data["last_name"]
        update_fields.append("last_name")
    if "position" in data:
        user.position = data["position"]
        update_fields.append("position")
    if "phone" in data:
        user.phone = data["phone"]
        update_fields.append("phone")
    if "timezone" in data:
        user.timezone = data["timezone"]
        update_fields.append("timezone")
    if "email" in data and data["email"] != user.email:
        # Скоуп по role (2026-08-06) — тот же email под ДРУГОЙ ролью (например,
        # уже есть venue_user с таким email) больше не конфликт, это два
        # отдельных аккаунта. Конфликт — только другой supplier с этим email.
        if User.objects.filter(email=data["email"], role=user.role).exclude(pk=user.pk).exists():
            raise ValidationError({"email": "Этот email уже используется другим аккаунтом."})
        user.email = data["email"]
        update_fields.append("email")

    if "first_name" in data or "last_name" in data:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        if full_name:
            user.full_name = full_name
            update_fields.append("full_name")

    if update_fields:
        user.save(update_fields=[*update_fields, "updated_at"])
    return user


def _register_user_with_email_verification(*, email, password, full_name, role):
    # Scoped by (email, role) — 2026-08-06, тот же email может отдельно
    # существовать под другой ролью (supplier vs venue_user), это два разных
    # аккаунта, не конфликт. Регистрация проверяет и создаёт строго "свою" пару.
    existing = User.objects.filter(email=email, role=role).first()
    if existing and existing.status == EntityStatus.ACTIVE:
        raise ValidationError({"email": "Этот email уже зарегистрирован. Войдите в аккаунт."})

    # Re-registering before confirming just resets password/name and issues a
    # fresh link, rather than erroring on the still-unverified draft user.
    user, _ = User.objects.update_or_create(
        email=email,
        role=role,
        defaults={
            "full_name": full_name or email,
            "status": EntityStatus.DRAFT,
            "password": make_password(password),
        },
    )

    EmailVerificationToken.objects.filter(
        user=user,
        status=EmailVerificationTokenStatus.PENDING,
    ).update(status=EmailVerificationTokenStatus.EXPIRED)

    ttl_hours = settings.EMAIL_VERIFICATION_TTL_HOURS
    verification = EmailVerificationToken.objects.create(
        user=user,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )
    verify_url = f"{settings.EMAIL_VERIFICATION_BASE_URL.rstrip('/')}/{verification.token}"
    try:
        send_mail(
            subject="Подтверждение регистрации в АвтоСнаб",
            message=(
                f"Перейдите по ссылке, чтобы подтвердить адрес электронной почты:\n{verify_url}\n\n"
                f"Ссылка действует {ttl_hours} ч."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
        )
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("register.send_mail_failed email=%s error=%s", email, error)
        raise EmailDeliveryError(
            "Не удалось отправить письмо для подтверждения. Попробуйте зарегистрироваться ещё раз через минуту."
        ) from error
    return user


@transaction.atomic
def register_supplier(*, email, password, full_name=""):
    return _register_user_with_email_verification(
        email=email, password=password, full_name=full_name, role=UserRole.SUPPLIER
    )


@transaction.atomic
def register_venue_user(*, email, password, full_name=""):
    """Самостоятельная регистрация заведения/покупателя email+паролем — по
    аналогии с Ozon-покупателем. Не заменяет admin-провижининг (admin по-прежнему
    может создать аккаунт с username через Django admin — но входит он всё равно
    по email, см. login_venue_user)."""
    return _register_user_with_email_verification(
        email=email, password=password, full_name=full_name, role=UserRole.VENUE_USER
    )


def confirm_email_verification(*, token):
    # Guard checks run outside any atomic block so an EXPIRED status change
    # is persisted even though we then raise (see the same pattern/reasoning
    # in claim_supplier_account_transfer).
    verification = get_object_or_404(
        EmailVerificationToken.objects.select_related("user"),
        token=token,
    )
    if verification.status == EmailVerificationTokenStatus.CONFIRMED:
        raise ValidationError({"token": "Email уже подтверждён."})
    if verification.expires_at <= timezone.now():
        verification.status = EmailVerificationTokenStatus.EXPIRED
        verification.save(update_fields=["status", "updated_at"])
        raise ValidationError({"token": "Срок действия ссылки истёк."})

    with transaction.atomic():
        verification = EmailVerificationToken.objects.select_for_update().select_related("user").get(pk=verification.pk)
        if verification.status != EmailVerificationTokenStatus.PENDING:
            raise ValidationError({"token": "Ссылка уже недействительна."})

        user = verification.user
        user.status = EntityStatus.ACTIVE
        user.save(update_fields=["status", "updated_at"])

        verification.status = EmailVerificationTokenStatus.CONFIRMED
        verification.confirmed_at = timezone.now()
        verification.save(update_fields=["status", "confirmed_at", "updated_at"])

    return {"user_id": user.id, "email": user.email, **_issue_jwt(user)}


def login_supplier(*, email, password):
    user = User.objects.filter(email=email, role=UserRole.SUPPLIER).first()
    if user is None or not user.password or not check_password(password, user.password):
        raise _generic_login_error()
    if user.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Подтвердите email перед входом — проверьте почту."})
    return {"user_id": user.id, "email": user.email, **_issue_jwt(user)}


# Arbitrary fixed key for the Postgres advisory lock used below — any int64
# works, it just has to stay constant across calls/processes.
_BOOTSTRAP_ADMIN_LOCK_KEY = 892517403


@transaction.atomic
def bootstrap_admin_user(*, username, password, full_name, email, phone=""):
    # Advisory lock serializes concurrent calls — there's no existing row to
    # select_for_update() before the first admin exists, so without this, two
    # requests arriving together can both pass the "no admin yet" check below
    # before either commits, both create an admin, and defeat the "exactly
    # one" guarantee (confirmed empirically: 2 admins created under real
    # concurrency). Released automatically at transaction end.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_BOOTSTRAP_ADMIN_LOCK_KEY])

    # Self-disabling: only ever creates the *first* admin account. Once one
    # exists, every later hire goes through Django admin — this endpoint
    # stays reachable behind SERVICE_API_TOKEN but permanently 400s, so a
    # leaked service token can't be used to mint more staff accounts.
    if User.objects.filter(role=UserRole.ADMIN).exists():
        raise ValidationError({"detail": "Первый admin уже создан. Новых сотрудников заводите через Django admin."})

    user = User.objects.create(
        username=username,
        email=email,
        full_name=full_name,
        role=UserRole.ADMIN,
        status=EntityStatus.ACTIVE,
        password=make_password(password),
        phone=phone,
    )
    return {"user_id": user.id, "username": user.username, **_issue_jwt(user)}


def login_venue_user(*, email, password):
    # email — единственный способ входа (и для admin-provisioned аккаунтов
    # тоже: у core.User email обязателен и уникален, см. core/models.py).
    # Раньше принимался ещё и отдельный username, но два поля-синонима с
    # неочевидным приоритетом были источником путаницы у фронтенда (Swagger
    # UI подставляет "string" в оба текстовых поля, легко забыть стереть).
    user = User.objects.filter(
        email=email,
        role=UserRole.VENUE_USER,
    ).first()
    if user is None or not user.password or not check_password(password, user.password):
        raise _generic_login_error()
    if user.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Доступ отключён. Обратитесь к своему менеджеру."})
    return {
        "user_id": user.id,
        "username": user.username or user.email,
        **_venue_fields(user),
        **_issue_jwt(user),
    }


_PASSWORD_RESET_ROLE_LABELS = {
    UserRole.SUPPLIER: "поставщика",
    UserRole.VENUE_USER: "покупателя",
}


@transaction.atomic
def initiate_password_reset(*, email):
    """Silently no-ops for unregistered/inactive emails — the view always
    returns the same generic response so this can't be used to enumerate
    which addresses have an account.

    2026-08-06: тот же email теперь может отдельно принадлежать supplier- и
    venue_user-аккаунту (см. (email, role) unique constraint на core.User) —
    это два разных аккаунта с разными паролями, один reset-запрос должен
    сбросить пароль КАЖДОМУ совпавшему, не только первому попавшемуся
    (иначе один из двух аккаунтов остался бы без способа восстановления,
    а пользователь решил бы, что ссылка сработала). Каждый получает свой
    token и своё письмо с уточнением роли, чтобы не перепутать, какая
    ссылка от какого аккаунта. Возвращает token первого совпадения (как и
    раньше, для обратной совместимости с вызывающими, которым нужен только
    один аккаунт) — остальные всё равно создаются и уходят письмом."""
    users = list(
        User.objects.filter(email=email, role__in=[UserRole.SUPPLIER, UserRole.VENUE_USER], status=EntityStatus.ACTIVE)
    )
    if not users:
        return None

    ttl_hours = settings.PASSWORD_RESET_TTL_HOURS
    first_token = None
    for user in users:
        PasswordResetToken.objects.filter(
            user=user,
            status=PasswordResetTokenStatus.PENDING,
        ).update(status=PasswordResetTokenStatus.EXPIRED)

        reset_token = PasswordResetToken.objects.create(
            user=user,
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(hours=ttl_hours),
        )
        if first_token is None:
            first_token = reset_token

        role_label = _PASSWORD_RESET_ROLE_LABELS.get(user.role, "")
        reset_url = f"{settings.PASSWORD_RESET_BASE_URL.rstrip('/')}/{reset_token.token}"
        try:
            send_mail(
                subject=f"Восстановление пароля в АвтоСнаб ({role_label})"
                if role_label
                else "Восстановление пароля в АвтоСнаб",
                message=(
                    f"Перейдите по ссылке, чтобы задать новый пароль для аккаунта {role_label}:\n{reset_url}\n\n"
                    if role_label
                    else f"Перейдите по ссылке, чтобы задать новый пароль:\n{reset_url}\n\n"
                )
                + (
                    f"Ссылка действует {ttl_hours} ч. Если вы не запрашивали восстановление — "
                    "просто проигнорируйте это письмо."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
            )
        except (smtplib.SMTPException, OSError) as error:
            # Не пробрасываем дальше: view всегда отдаёт один и тот же generic-ответ
            # независимо от исхода — чтобы нельзя было перебором проверить существующие
            # адреса (см. docstring выше). Отличимый статус при сбое SMTP сломал бы это,
            # и не должен блокировать письмо второму аккаунту, если он тоже совпал.
            logger.warning("password_reset.send_mail_failed email=%s role=%s error=%s", email, user.role, error)

    return first_token


def confirm_password_reset(*, token, new_password):
    reset_token = get_object_or_404(
        PasswordResetToken.objects.select_related("user"),
        token=token,
    )
    if reset_token.status == PasswordResetTokenStatus.USED:
        raise ValidationError({"token": "Эта ссылка уже была использована."})
    if reset_token.expires_at <= timezone.now():
        reset_token.status = PasswordResetTokenStatus.EXPIRED
        reset_token.save(update_fields=["status", "updated_at"])
        raise ValidationError({"token": "Срок действия ссылки истёк."})

    with transaction.atomic():
        reset_token = PasswordResetToken.objects.select_for_update().select_related("user").get(pk=reset_token.pk)
        if reset_token.status != PasswordResetTokenStatus.PENDING:
            raise ValidationError({"token": "Эта ссылка уже была использована."})

        user = reset_token.user
        user.password = make_password(new_password)
        user.save(update_fields=["password", "updated_at"])

        reset_token.status = PasswordResetTokenStatus.USED
        reset_token.used_at = timezone.now()
        reset_token.save(update_fields=["status", "used_at", "updated_at"])

    return {"user_id": user.id, "email": user.email, **_issue_jwt(user)}


def _yandex_external_id(yandex_id):
    return f"yandex:{yandex_id}"


def _find_or_create_oauth_user(*, external_id, email, full_name, role):
    """Shared by the 4 login_or_register_*_via_{yandex,vk} functions. Two
    OAuth callbacks racing for the same brand-new external_id/email (double
    submit, duplicate redirect) can both pass the "no existing user" checks
    before either commits — confirmed empirically under real concurrency.
    Retrying the lookup on IntegrityError mirrors what Django's own
    get_or_create()/update_or_create() already do for a plain unique field.

    2026-08-09 (реальный баг от Вити): external_auth_id, полученный от
    Yandex/VK, зависит только от внешнего аккаунта, не от role — тот же
    Yandex-логин используется и для intent=supplier, и для intent=venue.
    Раньше первый lookup искал по external_auth_id без учёта role, поэтому
    тот, кто первым привязал внешний id (например supplier), навсегда
    "захватывал" его — вход с intent=venue тем же внешним аккаунтом тихо
    возвращал чужой (supplier) User, полностью игнорируя role. Оба lookup'а
    ниже теперь скопированы по role — тот же принцип, что и у email (см.
    users_external_auth_id_role_uniq в core.models.User.Meta.constraints).

    2026-10-22: the losing callback used to run a separate "email already taken" check before create(). It only
    fired in exactly this race (a same-role account with that email is linked by the lookup above), so the second
    callback got a 400 instead of the account the first one had just created. The race is now resolved in one
    place: create(), and on IntegrityError the same lookup again."""
    user = _lookup_oauth_user(external_id=external_id, email=email, role=role)
    if user is not None:
        return user
    try:
        with transaction.atomic():
            return User.objects.create(
                email=email,
                full_name=full_name,
                role=role,
                status=EntityStatus.ACTIVE,
                external_auth_id=external_id,
            )
    except IntegrityError:
        user = _lookup_oauth_user(external_id=external_id, email=email, role=role)
        if user is None:
            raise
        return user


def _lookup_oauth_user(*, external_id, email, role):
    """Account of this role by external id, else by email (then linked to the external id)."""
    user = User.objects.filter(external_auth_id=external_id, role=role).first()
    if user is None:
        user = User.objects.filter(email=email, role=role).first()
        if user is not None:
            user.external_auth_id = external_id
            user.save(update_fields=["external_auth_id", "updated_at"])
    return user


@transaction.atomic
def login_or_register_supplier_via_yandex(*, yandex_id, email, full_name):
    """Additional login path alongside register_supplier/login_supplier
    (email+password) — never replaces it. Yandex already verifies the email,
    so a fresh account goes ACTIVE immediately, skipping EmailVerificationToken."""
    external_id = _yandex_external_id(yandex_id)
    user = _find_or_create_oauth_user(external_id=external_id, email=email, full_name=full_name, role=UserRole.SUPPLIER)

    if user.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Подтвердите email перед входом — проверьте почту."})

    return {"user_id": user.id, "email": user.email, **_issue_jwt(user)}


@transaction.atomic
def login_or_register_venue_user_via_yandex(*, yandex_id, email, full_name):
    """Additional login path alongside login_venue_user (email+password) —
    never replaces it. Unlike login_venue_user, this can self-register a brand
    new venue account (by design decision — see plan/wiki), skipping the
    admin-provisioning step classic venue login still requires."""
    external_id = _yandex_external_id(yandex_id)
    user = _find_or_create_oauth_user(
        external_id=external_id, email=email, full_name=full_name, role=UserRole.VENUE_USER
    )

    if user.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Доступ отключён. Обратитесь к своему менеджеру."})

    return {"user_id": user.id, "email": user.email, **_venue_fields(user), **_issue_jwt(user)}


def _vk_external_id(vk_id):
    return f"vk:{vk_id}"


@transaction.atomic
def login_or_register_supplier_via_vk(*, vk_id, email, full_name):
    """Additional login path alongside register_supplier/login_supplier and
    login_or_register_supplier_via_yandex — never replaces them. VK ID already
    verifies the email (via oauth2/user_info), so a fresh account goes ACTIVE immediately,
    skipping EmailVerificationToken."""
    external_id = _vk_external_id(vk_id)
    user = _find_or_create_oauth_user(external_id=external_id, email=email, full_name=full_name, role=UserRole.SUPPLIER)

    if user.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Подтвердите email перед входом — проверьте почту."})

    return {"user_id": user.id, "email": user.email, **_issue_jwt(user)}


@transaction.atomic
def login_or_register_venue_user_via_vk(*, vk_id, email, full_name):
    """Additional login path alongside login_venue_user (email+password) and
    login_or_register_venue_user_via_yandex — never replaces them. Can self-register
    a brand new venue account, same as the Yandex path."""
    external_id = _vk_external_id(vk_id)
    user = _find_or_create_oauth_user(
        external_id=external_id, email=email, full_name=full_name, role=UserRole.VENUE_USER
    )

    if user.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Доступ отключён. Обратитесь к своему менеджеру."})

    return {"user_id": user.id, "email": user.email, **_venue_fields(user), **_issue_jwt(user)}
