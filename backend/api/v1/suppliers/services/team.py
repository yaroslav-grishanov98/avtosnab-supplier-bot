"""Supplier team: members, invites, invite links and join requests."""

import secrets
import smtplib
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.v1.auth.services import EmailDeliveryError
from api.v1.suppliers.storage import (
    persist_supplier_join_request_document,
)
from core.models import (
    EntityStatus,
    User,
    UserRole,
)
from suppliers.models import (
    Supplier,
    SupplierInviteLink,
    SupplierJoinRequest,
    SupplierJoinRequestStatus,
    SupplierUser,
    SupplierUserInvite,
    SupplierUserInviteStatus,
    SupplierUserRole,
)

from ._shared import logger


def _raise_if_last_owner_loses_owner_role(*, supplier_id, membership, new_role):
    """ "У компании должен быть хотя бы один активный owner" — тот же инвариант,
    что revoke_supplier_user/change_supplier_user_role уже защищали, но
    accept_supplier_user_invite/join_supplier_via_invite_link его не проверяли
    вообще (2026-08-07, реальный баг от Вити): их update_or_create() тихо
    перезаписывает role существующего членства, включая случай, когда
    принимающий сам уже единственный owner компании, а инвайт/ссылка настроены
    на role=manager — например owner сам открывает свою же manager-ссылку,
    "чтобы проверить, как она работает". Воспроизведено эмпирически на
    реальном Postgres: компания оставалась с нулём активных owner'ов."""
    if membership is None or membership.role != SupplierUserRole.OWNER or new_role == SupplierUserRole.OWNER:
        return
    other_active_owner_exists = (
        SupplierUser.objects.filter(supplier_id=supplier_id, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE)
        .exclude(pk=membership.pk)
        .exists()
    )
    if not other_active_owner_exists:
        raise ValidationError({"detail": "Нельзя понизить в роли последнего владельца компании."})


def _demote_other_active_owners(*, supplier_id, keep_user_id):
    """2026-08-07 (исправлено по явному указанию пользователя — "так не должно
    быть, он должен становиться менеджером"): владелец компании только один,
    но при передаче прав старый owner понижается до manager, а не теряет
    доступ к компании целиком. Раньше (2026-08-06) это место деактивировало
    членство полностью (status=inactive) — по ошибочному выводу из "доступ
    пропадает" в изначальной формулировке, раз owner/manager имеют
    одинаковые права. Пользователь прямо поправил: доступ остаётся, теряется
    только owner-статус. Вызывается везде, где кто-то может получить
    role=owner: смена роли, email-инвайт, invite-link.

    Локает Supplier целиком (не только строки SupplierUser) — эмпирически
    подтверждённая гонка (5/5 запусков на реальном Postgres, найдена ещё для
    прежней deactivate-версии этой функции, тот же локинг остаётся
    актуальным и для demote): без этого лока два одновременных "сделать
    owner'ом" для двух РАЗНЫХ пользователей (в основном actionable только
    для текущего owner/staff/сервис-токена после ограничения в
    permissions.py, но staff и owner могут дёрнуть это параллельно) — обе
    UPDATE-транзакции видят исходного owner'а активным в своём снапшоте и
    понижают его, но ни одна не видит нового owner'а другой транзакции (тот
    появляется только после её коммита), и в итоге остаются два owner'а
    одновременно. Блокировка строки Supplier (тот же приём, что уже
    используют invite_supplier_user/initiate_supplier_account_transfer)
    форсирует последовательное выполнение — второй вызов стартует свой
    запрос уже после коммита первого и корректно видит нового owner'а как
    кандидата на понижение."""
    get_object_or_404(Supplier.objects.select_for_update(), pk=supplier_id)
    SupplierUser.objects.select_for_update().filter(
        supplier_id=supplier_id,
        role=SupplierUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    ).exclude(user_id=keep_user_id).update(role=SupplierUserRole.MANAGER)


def list_supplier_team(*, supplier_id):
    """ "Кто сейчас имеет доступ к компании" (2026-08-01) — active SupplierUser
    rows, newest first. select_related avoids the N+1 the 2026-07-28 senior
    review already flagged once for a similar company-list endpoint."""
    get_object_or_404(Supplier, pk=supplier_id)
    return list(
        SupplierUser.objects.filter(supplier_id=supplier_id, status=EntityStatus.ACTIVE)
        .select_related("user")
        .order_by("-created_at")
    )


@transaction.atomic
def revoke_supplier_user(*, supplier_id, target_user_id, requested_by_user_id=None):
    """Remove an active team member's access — the "X" next to a person in
    Google Docs' share dialog (2026-08-01). Soft (status=inactive), not a row
    delete: invite_supplier_user/accept_supplier_user_invite already
    update_or_create on (supplier, user), so re-inviting the same person
    later reactivates this same row instead of hitting the unique
    constraint on a stale inactive one."""
    if str(target_user_id) == str(requested_by_user_id):
        raise ValidationError({"detail": "Нельзя отозвать доступ самому себе — используйте удаление компании."})

    membership = get_object_or_404(
        SupplierUser.objects.select_for_update(),
        supplier_id=supplier_id,
        user_id=target_user_id,
        status=EntityStatus.ACTIVE,
    )

    if membership.role == SupplierUserRole.OWNER:
        other_active_owner_exists = (
            SupplierUser.objects.filter(
                supplier_id=supplier_id, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
            )
            .exclude(pk=membership.pk)
            .exists()
        )
        if not other_active_owner_exists:
            raise ValidationError({"detail": "Нельзя отозвать доступ последнему владельцу компании."})

    membership.status = EntityStatus.INACTIVE
    membership.save(update_fields=["status", "updated_at"])
    return membership


@transaction.atomic
def change_supplier_user_role(*, supplier_id, target_user_id, new_role, requested_by_user_id=None):
    """Change an active team member's role (2026-08-03 — Figma-гайд для
    фронтенда указал на пробел: invite/join-approve всегда создают
    role=manager, сменить роль после было нечем). Same last-owner
    protection as revoke_supplier_user() — demoting the only active owner
    would leave the company without one, same as removing them entirely."""
    membership = get_object_or_404(
        SupplierUser.objects.select_for_update(),
        supplier_id=supplier_id,
        user_id=target_user_id,
        status=EntityStatus.ACTIVE,
    )

    _raise_if_last_owner_loses_owner_role(supplier_id=supplier_id, membership=membership, new_role=new_role)

    membership.role = new_role
    membership.save(update_fields=["role", "updated_at"])

    # У компании только один активный владелец (2026-08-06) — назначение
    # нового owner'а понижает предыдущего до manager (2026-08-07: доступ
    # остаётся, теряется только owner-статус).
    if new_role == SupplierUserRole.OWNER:
        _demote_other_active_owners(supplier_id=supplier_id, keep_user_id=target_user_id)

    return membership


def list_pending_supplier_user_invites(*, supplier_id):
    """ "Кто ещё не принял приглашение" (2026-08-03) — pending email-invites,
    отдельно от join-requests/pending (те — заявки от людей, которые сами
    нашли компанию по ИНН; invites — приглашения, отправленные владельцем)."""
    get_object_or_404(Supplier, pk=supplier_id)
    return list(
        SupplierUserInvite.objects.filter(supplier_id=supplier_id, status=SupplierUserInviteStatus.PENDING)
        .select_related("invited_by")
        .order_by("-created_at")
    )


@transaction.atomic
def invite_supplier_user(*, supplier_id, target_email, role=SupplierUserRole.MANAGER, invited_by_user_id=None):
    supplier = get_object_or_404(Supplier.objects.select_for_update(), pk=supplier_id)

    # One live invite per (supplier, email) at a time — same reasoning as
    # account-transfer: avoid two links racing to claim the same seat.
    SupplierUserInvite.objects.filter(
        supplier=supplier,
        target_email=target_email,
        status=SupplierUserInviteStatus.PENDING,
    ).update(status=SupplierUserInviteStatus.CANCELLED)

    ttl_hours = settings.SUPPLIER_INVITE_TTL_HOURS
    invite = SupplierUserInvite.objects.create(
        supplier=supplier,
        invited_by_id=invited_by_user_id,
        target_email=target_email,
        role=role,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )

    accept_url = f"{settings.SUPPLIER_INVITE_BASE_URL.rstrip('/')}/{invite.token}"
    try:
        send_mail(
            subject="Приглашение в личный кабинет АвтоСнаб",
            message=(
                f"Вас пригласили в личный кабинет компании «{supplier.display_name}» в АвтоСнаб.\n\n"
                f"Перейдите по ссылке, чтобы задать пароль и войти:\n{accept_url}\n\n"
                f"Ссылка действует {ttl_hours} ч."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[target_email],
        )
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("supplier_invite.send_mail_failed email=%s error=%s", target_email, error)
        raise EmailDeliveryError(
            "Не удалось отправить письмо с приглашением. Попробуйте ещё раз через минуту."
        ) from error

    return invite


def get_supplier_user_invite_preview(*, token):
    """Read-only превью до accept — не меняет статус инвайта (в отличие от accept,
    который переводит EXPIRED при истёкшем сроке). Нужен фронту, чтобы до формы
    сравнить target_email с уже залогиненным юзером в браузере (2026-08-05).

    account_exists (2026-08-06): не залогинен, но target_email уже зарегистрирован —
    accept всё равно проигнорирует введённый пароль (см. accept_supplier_user_invite),
    так что просить придумать и подтвердить пароль бессмысленно и вводит в
    заблуждение — фронт должен вместо формы показать "у вас уже есть аккаунт,
    войдите текущим паролем". Не утечка данных: получатель ссылки и так знает
    свой собственный email."""
    invite = get_object_or_404(
        SupplierUserInvite.objects.select_related("supplier"),
        token=token,
    )
    return {
        "target_email": invite.target_email,
        "supplier_name": invite.supplier.display_name,
        "supplier_inn": invite.supplier.inn,
        "role": invite.role,
        "status": invite.status,
        "is_expired": invite.expires_at <= timezone.now(),
        # role=supplier (2026-08-06) — этот инвайт всегда про supplier-аккаунт,
        # существование ОТДЕЛЬНОГО venue_user-аккаунта с тем же email тут ни
        # при чём: accept всё равно создаёт/использует именно supplier-строку.
        "account_exists": User.objects.filter(email__iexact=invite.target_email, role=UserRole.SUPPLIER).exists(),
    }


def accept_supplier_user_invite(*, token, user):
    """2026-08-07 (явное решение пользователя): принять приглашение можно
    только уже авторизованным core.User JWT (role=supplier) — никаких
    email/паролей в теле этой ручки. У юзера без аккаунта путь такой:
    сначала обычная регистрация (POST /auth/supplier/register +
    подтверждение email), потом — уже залогиненным — переход по ссылке
    приглашения. Раньше здесь были ещё 2 состояния (создание аккаунта и
    логин существующего прямо в этой ручке, добавлены и допиливались
    2026-08-06) — убраны полностью вместе со всей парольной логикой и
    гонкой при создании аккаунта (см. её же историю в services.py git log),
    т.к. создание аккаунта больше не происходит здесь вообще."""
    # Guard checks outside any atomic block — see claim_supplier_account_transfer
    # for why: an EXPIRED status write must survive the subsequent raise.
    invite = get_object_or_404(
        SupplierUserInvite.objects.select_related("supplier"),
        token=token,
    )

    if invite.status == SupplierUserInviteStatus.CLAIMED:
        raise ValidationError({"token": "Эта ссылка уже была использована."})
    if invite.status == SupplierUserInviteStatus.CANCELLED:
        raise ValidationError({"token": "Эта ссылка больше не действительна — была создана более новая."})
    if invite.expires_at <= timezone.now():
        invite.status = SupplierUserInviteStatus.EXPIRED
        invite.save(update_fields=["status", "updated_at"])
        raise ValidationError({"token": "Срок действия ссылки истёк."})

    if user.email.strip().lower() != invite.target_email.strip().lower():
        raise ValidationError(
            {"detail": "Это приглашение адресовано другому email. Выйдите из аккаунта и попробуйте снова."}
        )

    with transaction.atomic():
        invite = SupplierUserInvite.objects.select_for_update().select_related("supplier").get(pk=invite.pk)
        if invite.status != SupplierUserInviteStatus.PENDING:
            raise ValidationError({"token": "Эта ссылка уже была использована."})

        # Unlike account-transfer's claim, this ADDS a user without touching
        # the existing owner or any other active SupplierUser — UNLESS the
        # invite grants role=owner, in which case the single-owner invariant
        # (2026-08-06) demotes whoever was owner before to manager (2026-08-07:
        # keeps their access, only the owner status changes hands).
        #
        # Guard (2026-08-07, реальный баг от Вити): update_or_create ниже
        # тихо перезапишет role уже существующего членства — если принимающий
        # уже единственный active owner, а у инвайта role=manager (например,
        # owner сам себе прислал ссылку), компания осталась бы без owner'а.
        existing_membership = (
            SupplierUser.objects.select_for_update()
            .filter(supplier=invite.supplier, user=user, status=EntityStatus.ACTIVE)
            .first()
        )
        _raise_if_last_owner_loses_owner_role(
            supplier_id=invite.supplier_id, membership=existing_membership, new_role=invite.role
        )

        SupplierUser.objects.update_or_create(
            supplier=invite.supplier,
            user=user,
            defaults={"role": invite.role, "status": EntityStatus.ACTIVE},
        )
        if invite.role == SupplierUserRole.OWNER:
            _demote_other_active_owners(supplier_id=invite.supplier_id, keep_user_id=user.id)

        invite.status = SupplierUserInviteStatus.CLAIMED
        invite.claimed_at = timezone.now()
        invite.claimed_by = user
        invite.save(update_fields=["status", "claimed_at", "claimed_by", "updated_at"])

    return {
        "supplier_id": invite.supplier_id,
        "user_id": user.id,
        "email": user.email,
        "role": invite.role,
    }


def get_supplier_invite_link_preview(*, token):
    """2026-08-06: та же мотивация, что у get_supplier_user_invite_preview —
    фронту нужно показать "вас приглашают в компанию X с ролью Y" до клика
    "присоединиться" (POST .../join), не отправляя join вслепую. Read-only,
    без побочных эффектов (в отличие от email-инвайта, статус ссылки не
    переводится в expired при истёкшем сроке — у SupplierInviteLink вообще
    нет отдельного EXPIRED, expires_at проверяется на лету в join)."""
    link = get_object_or_404(
        SupplierInviteLink.objects.select_related("supplier"),
        token=token,
    )
    return {
        "supplier_name": link.supplier.display_name,
        "supplier_inn": link.supplier.inn,
        "role": link.role,
        "status": link.status,
        "is_expired": bool(link.expires_at and link.expires_at <= timezone.now()),
    }


def get_or_create_supplier_invite_link(*, supplier_id):
    """2026-08-04: "Пригласите по ссылке" — одна многоразовая ссылка на
    компанию (в отличие от invite_supplier_user, которая одноразовая и
    привязана к конкретному email). Первый запрос создаёт её, дальше всегда
    возвращает тот же ряд — UI показывает ссылку сразу, без отдельной
    кнопки "создать"."""
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    link, _ = SupplierInviteLink.objects.get_or_create(
        supplier=supplier,
        defaults={"token": secrets.token_urlsafe(32)},
    )
    return link


@transaction.atomic
def update_supplier_invite_link(*, supplier_id, data):
    """ "Настройки ссылки" — какую роль получает присоединившийся, срок
    действия, включена ли ссылка вообще."""
    link = get_object_or_404(SupplierInviteLink.objects.select_for_update(), supplier_id=supplier_id)
    update_fields = []

    if "role" in data:
        link.role = data["role"]
        update_fields.append("role")
    if "expires_at" in data:
        link.expires_at = data["expires_at"]
        update_fields.append("expires_at")
    if "status" in data:
        link.status = data["status"]
        update_fields.append("status")

    if update_fields:
        link.save(update_fields=[*update_fields, "updated_at"])
    return link


@transaction.atomic
def regenerate_supplier_invite_link(*, supplier_id):
    """Инвалидирует старый token (например, если ссылка утекла не туда) без
    трогания роли/срока действия/статуса — те же настройки, новый секрет."""
    link = get_object_or_404(SupplierInviteLink.objects.select_for_update(), supplier_id=supplier_id)
    link.token = secrets.token_urlsafe(32)
    link.save(update_fields=["token", "updated_at"])
    return link


@transaction.atomic
def join_supplier_via_invite_link(*, token, user):
    """2026-08-07 (явное решение пользователя): присоединиться по ссылке
    можно только уже авторизованным core.User JWT (role=supplier) — никаких
    email/паролей в теле, ссылка ни к какому email не привязана вообще, так
    что и сверять не с чем. У юзера без аккаунта путь — сначала обычная
    регистрация, потом переход по ссылке залогиненным. Раньше (2026-08-04..06)
    была ещё анонимная ветка email+пароль с созданием/логином аккаунта прямо
    здесь — убрана полностью вместе с гонкой при создании нового аккаунта
    (см. историю этой функции в git log — savepoint/retry фикс, который был
    актуален только для той ветки). Ссылка не помечается "использована" —
    многоразовая по дизайну, можно звать сколько угодно раз, пока владелец
    её не отключит/не истёк срок."""
    link = get_object_or_404(SupplierInviteLink.objects.select_related("supplier"), token=token)

    if link.status != EntityStatus.ACTIVE:
        raise ValidationError({"token": "Эта ссылка отключена."})
    if link.expires_at and link.expires_at <= timezone.now():
        raise ValidationError({"token": "Срок действия ссылки истёк."})
    if link.supplier.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Компания недоступна."})

    # Guard (2026-08-07, реальный баг от Вити): "если юзер сам себе ссылку
    # менеджера отправит под ролью владельца, в компании не будет владельца"
    # — update_or_create ниже тихо перезапишет role уже существующего
    # членства; если присоединяющийся уже единственный active owner, а
    # ссылка настроена на role=manager, компания осталась бы без owner'а.
    # Воспроизведено эмпирически на реальном Postgres до фикса.
    existing_membership = (
        SupplierUser.objects.select_for_update()
        .filter(supplier=link.supplier, user=user, status=EntityStatus.ACTIVE)
        .first()
    )
    _raise_if_last_owner_loses_owner_role(
        supplier_id=link.supplier_id, membership=existing_membership, new_role=link.role
    )

    SupplierUser.objects.update_or_create(
        supplier=link.supplier,
        user=user,
        defaults={"role": link.role, "status": EntityStatus.ACTIVE},
    )
    if link.role == SupplierUserRole.OWNER:
        _demote_other_active_owners(supplier_id=link.supplier_id, keep_user_id=user.id)

    return {"supplier_id": link.supplier_id, "user_id": user.id, "email": user.email, "role": link.role}


@transaction.atomic
def create_supplier_join_request(
    *, supplier_id, requested_by_user_id, message=None, phone=None, email=None, documents=None
):
    """Requester-initiated mirror of invite_supplier_user: a user who
    already has an account asks to join a company they found by INN,
    instead of the owner inviting them by email. phone/email here are
    per-request contact details (may differ from the requester's account),
    documents are uploaded files (e.g. a scan of a power of attorney) —
    persisted and stored as URLs (2026-07-29)."""
    supplier = get_object_or_404(
        Supplier.objects.select_for_update(),
        pk=supplier_id,
        status=EntityStatus.ACTIVE,
    )

    if SupplierUser.objects.filter(
        supplier=supplier, user_id=requested_by_user_id, status=EntityStatus.ACTIVE
    ).exists():
        raise ValidationError({"detail": "Вы уже состоите в этой компании."})

    if SupplierJoinRequest.objects.filter(
        supplier=supplier,
        requested_by_id=requested_by_user_id,
        status=SupplierJoinRequestStatus.PENDING,
    ).exists():
        raise ValidationError({"detail": "Заявка на присоединение к этой компании уже отправлена и ожидает решения."})

    document_urls = [
        persist_supplier_join_request_document(document_file=document_file, supplier_id=supplier.pk)
        for document_file in (documents or [])
    ]

    join_request = SupplierJoinRequest.objects.create(
        supplier=supplier,
        requested_by_id=requested_by_user_id,
        message=message or None,
        phone=phone or None,
        email=email or None,
        documents=document_urls,
    )

    # 2026-08-13: раньше уведомлялись все активные участники компании
    # (owner/manager) — по прямой просьбе теперь уходит только на фиксированный
    # адрес поддержки, никому больше.
    requester = User.objects.get(pk=requested_by_user_id)
    send_mail(
        subject="Новая заявка на присоединение — АвтоСнаб",
        message=(
            f"Пользователь {requester.full_name} ({requester.email}) хочет присоединиться к "
            f"компании «{supplier.display_name}» (ИНН {supplier.inn}) в личном кабинете АвтоСнаб.\n\n"
            f"ID компании: {supplier.id}\nID заявки: {join_request.id}"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.SUPPLIER_JOIN_REQUEST_NOTIFICATION_EMAIL],
    )

    return join_request


def list_supplier_join_requests(*, supplier_id):
    return (
        SupplierJoinRequest.objects.filter(supplier_id=supplier_id, status=SupplierJoinRequestStatus.PENDING)
        .select_related("requested_by", "supplier")
        .order_by("created_at")
    )


def list_my_supplier_join_requests(*, user_id):
    return (
        SupplierJoinRequest.objects.filter(requested_by_id=user_id)
        .select_related("requested_by", "supplier")
        .order_by("-created_at")
    )


@transaction.atomic
def decide_supplier_join_request(*, supplier_id, request_id, decided_by_user_id, approve):
    join_request = get_object_or_404(
        SupplierJoinRequest.objects.select_for_update(),
        pk=request_id,
        supplier_id=supplier_id,
    )
    if join_request.status != SupplierJoinRequestStatus.PENDING:
        raise ValidationError({"detail": "Эта заявка уже была рассмотрена."})

    join_request.status = SupplierJoinRequestStatus.APPROVED if approve else SupplierJoinRequestStatus.REJECTED
    join_request.decided_by_id = decided_by_user_id
    join_request.decided_at = timezone.now()
    join_request.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])

    if approve:
        # Same "add without touching the owner" semantics as accept_supplier_user_invite.
        SupplierUser.objects.update_or_create(
            supplier_id=supplier_id,
            user_id=join_request.requested_by_id,
            defaults={"role": SupplierUserRole.MANAGER, "status": EntityStatus.ACTIVE},
        )

    return join_request
