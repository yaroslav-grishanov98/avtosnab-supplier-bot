"""Venue cabinet business logic — mirrors api.v1.suppliers.services 1:1 for
the "access settings" surface (company list/create, cabinet session, profile
CRUD, team/invite/join/transfer). Product-display/catalog-browsing side is
deliberately out of scope here (unchanged, still lives in api.v1.catalog —
see docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md).

Two deliberate simplifications vs. the supplier mirror:
- No fuzzy INN/name/address duplicate-detection (api.v1.suppliers.dedup) on
  create — that machinery was built for a supplier-specific cold-registration
  pain point never reported for venues; only an exact-INN check guards here.
- No bot-issued one-time cabinet token exchange (CabinetSession/
  SupplierBotLinkToken equivalents) — that's a separate cross-service
  concern (autosnab_bots), out of scope for this mirror."""

import logging
import secrets
import smtplib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from api.v1.auth.services import EmailDeliveryError
from api.v1.catalog.services import reset_master_product_links_for_venue
from api.v1.venues.storage import persist_venue_join_request_document, persist_venue_logo
from core.audit import record_audit_log
from core.models import (
    AuditLog,
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    User,
    UserRole,
    Venue,
    VenueAccountTransfer,
    VenueAccountTransferStatus,
    VenueAddress,
    VenueBankAccount,
    VenueContact,
    VenueImage,
    VenueInviteLink,
    VenueJoinRequest,
    VenueJoinRequestStatus,
    VenueUser,
    VenueUserInvite,
    VenueUserInviteStatus,
    VenueUserRole,
)

logger = logging.getLogger("autosnab.venues.services")


def _create_venue_record(
    *,
    display_name,
    legal_name=None,
    short_name=None,
    entity_type=None,
    inn=None,
    kpp=None,
    ogrn=None,
    legal_address=None,
    okved=None,
    okpo=None,
    registration_date=None,
    general_director=None,
    logo_url=None,
    description=None,
    phone=None,
    email=None,
    website=None,
    addresses=None,
    contacts=None,
    bank_accounts=None,
    metadata=None,
):
    inn = (inn or "").strip()
    if inn and Venue.objects.filter(inn=inn, status=EntityStatus.ACTIVE).exists():
        raise ValidationError(
            {"detail": "Заведение с таким ИНН уже зарегистрировано.", "duplicate_match": "same_company"}
        )

    venue = Venue.objects.create(
        display_name=display_name,
        legal_name=legal_name or display_name,
        short_name=short_name or None,
        entity_type=entity_type or None,
        inn=inn or None,
        kpp=(kpp or "").strip() or None,
        ogrn=(ogrn or "").strip() or None,
        legal_address=legal_address or None,
        okved=okved or None,
        okpo=okpo or None,
        registration_date=registration_date or None,
        general_director=general_director or None,
        logo_url=logo_url or None,
        description=description or None,
        phone=phone or None,
        email=email or None,
        website=website or None,
        status=EntityStatus.ACTIVE,
        metadata=metadata or {},
    )
    _sync_venue_nested_collections(
        venue,
        addresses=addresses,
        contacts=contacts,
        bank_accounts=bank_accounts,
    )
    return venue


@transaction.atomic
def create_venue_by_owner(*, user, **fields):
    """Self-service: a registered venue_user creates and immediately owns a
    venue. Mirrors create_supplier_by_owner — can be called more than once,
    a person may own/manage several venues (GET /venues/mine lists all)."""
    User.objects.select_for_update().get(pk=user.pk)

    venue = _create_venue_record(**fields)
    VenueUser.objects.create(
        venue=venue,
        user=user,
        role=VenueUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    )
    return venue


def list_my_venues(*, user_id):
    """Mirrors GET /suppliers/mine — every Venue where the caller has an
    active VenueUser row, annotated with my_role."""
    memberships = (
        VenueUser.objects.filter(user_id=user_id, status=EntityStatus.ACTIVE)
        .select_related("venue")
        .order_by("-created_at")
    )
    venues = []
    for membership in memberships:
        venue = membership.venue
        venue.my_role = membership.role
        venues.append(venue)
    return venues


def issue_venue_cabinet_tokens(venue, user=None):
    """Mirrors issue_supplier_cabinet_tokens — claim `venue_id` (resolved by
    VenueCabinetJWTAuthentication) instead of `supplier_id`."""
    refresh = RefreshToken()
    refresh["venue_id"] = str(venue.pk)
    if user is not None:
        refresh["user_id"] = str(user.pk)
    access = refresh.access_token
    access["venue_id"] = str(venue.pk)
    if user is not None:
        access["user_id"] = str(user.pk)
    return {"access": str(access), "refresh": str(refresh)}


def get_venue_cabinet_snapshot(venue):
    """Mirrors get_supplier_cabinet_snapshot, minus upload/product-count
    fields — product display is out of scope for this mirror."""
    return venue


@transaction.atomic
def update_venue_cabinet(*, venue, data):
    venue = Venue.objects.select_for_update().get(pk=venue.pk)
    allowed = (
        "display_name",
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
        "logo_url",
        "description",
        "phone",
        "email",
        "website",
    )
    for field in allowed:
        if field in data:
            setattr(venue, field, data[field])
    if "metadata" in data and isinstance(data["metadata"], dict):
        venue.metadata = {**(venue.metadata or {}), **data["metadata"]}
    venue.save()
    _sync_venue_nested_collections(
        venue,
        addresses=data.get("addresses"),
        contacts=data.get("contacts"),
        bank_accounts=data.get("bank_accounts"),
    )
    return venue


@transaction.atomic
def delete_venue_cabinet(*, venue, actor=None):
    """Mirrors delete_supplier_cabinet — soft delete (status=archived), not a
    row DELETE: core.Order/core.Receiving reference Venue via
    on_delete=RESTRICT. Membership survives archiving, same as Supplier."""
    venue.status = EntityStatus.ARCHIVED
    venue.save(update_fields=["status", "updated_at"])
    record_audit_log(
        actor=actor,
        action=AuditLogAction.ARCHIVED,
        entity_type=AuditLogEntityType.VENUE,
        entity_id=venue.id,
        summary=venue.display_name,
    )


def venue_audit_log(*, venue_id):
    return list(
        AuditLog.objects.filter(entity_type=AuditLogEntityType.VENUE, entity_id=venue_id).order_by("-created_at")
    )


@transaction.atomic
def restore_venue(*, venue_id):
    """Mirrors restore_supplier — goes through a core.User JWT +
    CanRestoreVenue, not the cabinet JWT (an archived venue's cabinet JWT is
    rejected outright by VenueCabinetJWTAuthentication)."""
    venue = get_object_or_404(Venue.objects.select_for_update(), pk=venue_id)
    if venue.status != EntityStatus.ARCHIVED:
        raise ValidationError({"detail": "Заведение не удалено — восстанавливать нечего."})
    venue.status = EntityStatus.ACTIVE
    venue.save(update_fields=["status", "updated_at"])
    return venue


@transaction.atomic
def upload_venue_logo(*, venue, logo_file):
    """2026-09-13: логотип расширен до галереи фото (по прямой просьбе
    пользователя — "везде, включая оба лого") — каждый вызов ДОБАВЛЯЕТ фото,
    не заменяет предыдущее. `Venue.logo_url` остаётся для обратной
    совместимости со всеми местами, которые уже читают его напрямую —
    синхронизируется как URL самого раннего (первого загруженного) фото в
    галерее, чтобы не оставлять его пустым, пока в галерее есть хоть одно
    фото."""
    image_url = persist_venue_logo(logo_file=logo_file, venue_id=venue.pk)
    image = VenueImage.objects.create(venue=venue, image_url=image_url)
    if not venue.logo_url:
        venue.logo_url = image_url
        venue.save(update_fields=["logo_url", "updated_at"])
    return image


@transaction.atomic
def delete_venue_image(*, venue, image_id):
    image = _get_owned_or_404(VenueImage.objects.all(), venue=venue, item_id=image_id)
    was_logo = venue.logo_url == image.image_url
    image.delete()
    if was_logo:
        next_image = venue.images.order_by("created_at", "id").first()
        venue.logo_url = next_image.image_url if next_image else None
        venue.save(update_fields=["logo_url", "updated_at"])


def _get_owned_or_404(queryset, *, venue, item_id):
    return get_object_or_404(queryset, pk=item_id, venue=venue)


def _sync_venue_nested_collections(venue, *, addresses=None, contacts=None, bank_accounts=None):
    """Mirrors _sync_supplier_nested_collections — bulk (re)write, only
    touches a collection if its list is provided (not None)."""
    if addresses is not None:
        venue.addresses.all().delete()
        VenueAddress.objects.bulk_create([VenueAddress(venue=venue, **item) for item in addresses])
    if contacts is not None:
        venue.contacts.all().delete()
        VenueContact.objects.bulk_create([VenueContact(venue=venue, **item) for item in contacts])
    if bank_accounts is not None:
        venue.bank_accounts.all().delete()
        objs = []
        primary_claimed = False
        for item in bank_accounts:
            item = dict(item)
            if item.get("is_primary"):
                if primary_claimed:
                    item["is_primary"] = False
                else:
                    primary_claimed = True
            objs.append(VenueBankAccount(venue=venue, **item))
        VenueBankAccount.objects.bulk_create(objs)


_ADDRESS_FIELDS = (
    "address_type",
    "address",
    "country",
    "region",
    "district",
    "locality",
    "street",
    "postal_code",
    "apartment_office",
)


def create_venue_address(*, venue, data):
    return VenueAddress.objects.create(venue=venue, **{k: data.get(k) for k in _ADDRESS_FIELDS if k in data})


def update_venue_address(*, venue, address_id, data):
    address = _get_owned_or_404(VenueAddress.objects.all(), venue=venue, item_id=address_id)
    for field in _ADDRESS_FIELDS:
        if field in data:
            setattr(address, field, data[field])
    address.save()
    return address


def delete_venue_address(*, venue, address_id):
    address = _get_owned_or_404(VenueAddress.objects.all(), venue=venue, item_id=address_id)
    address.delete()


_CONTACT_FIELDS = ("contact_type", "messenger_type", "value")


def create_venue_contact(*, venue, data):
    return VenueContact.objects.create(venue=venue, **{k: data.get(k) for k in _CONTACT_FIELDS if k in data})


def update_venue_contact(*, venue, contact_id, data):
    contact = _get_owned_or_404(VenueContact.objects.all(), venue=venue, item_id=contact_id)
    for field in _CONTACT_FIELDS:
        if field in data:
            setattr(contact, field, data[field])
    contact.save()
    return contact


def delete_venue_contact(*, venue, contact_id):
    contact = _get_owned_or_404(VenueContact.objects.all(), venue=venue, item_id=contact_id)
    contact.delete()


_BANK_ACCOUNT_FIELDS = ("bank_name", "account_number", "bik", "correspondent_account", "is_primary")


@transaction.atomic
def create_venue_bank_account(*, venue, data):
    if data.get("is_primary"):
        VenueBankAccount.objects.filter(venue=venue, is_primary=True).update(is_primary=False)
    return VenueBankAccount.objects.create(venue=venue, **{k: data.get(k) for k in _BANK_ACCOUNT_FIELDS if k in data})


@transaction.atomic
def update_venue_bank_account(*, venue, bank_account_id, data):
    account = _get_owned_or_404(VenueBankAccount.objects.select_for_update(), venue=venue, item_id=bank_account_id)
    if data.get("is_primary"):
        VenueBankAccount.objects.filter(venue=venue, is_primary=True).exclude(pk=account.pk).update(is_primary=False)
    for field in _BANK_ACCOUNT_FIELDS:
        if field in data:
            setattr(account, field, data[field])
    account.save()
    return account


def delete_venue_bank_account(*, venue, bank_account_id):
    account = _get_owned_or_404(VenueBankAccount.objects.all(), venue=venue, item_id=bank_account_id)
    account.delete()


@transaction.atomic
def initiate_venue_account_transfer(*, venue_id, target_email, initiated_by_user_id=None):
    venue = get_object_or_404(Venue.objects.select_for_update(), pk=venue_id)

    VenueAccountTransfer.objects.filter(
        venue=venue,
        status=VenueAccountTransferStatus.PENDING,
    ).update(status=VenueAccountTransferStatus.CANCELLED)

    ttl_hours = settings.VENUE_ACCOUNT_TRANSFER_TTL_HOURS
    transfer = VenueAccountTransfer.objects.create(
        venue=venue,
        initiated_by_id=initiated_by_user_id,
        target_email=target_email,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )

    claim_url = f"{settings.VENUE_ACCOUNT_TRANSFER_CLAIM_BASE_URL.rstrip('/')}/{transfer.token}"
    try:
        send_mail(
            subject="Передача аккаунта в АвтоСнаб",
            message=(
                f"Вам передан аккаунт заведения «{venue.display_name}» в АвтоСнаб.\n\n"
                f"Перейдите по ссылке, чтобы задать пароль и войти:\n{claim_url}\n\n"
                f"Ссылка действует {ttl_hours} ч."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[target_email],
        )
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("venue_account_transfer.send_mail_failed email=%s error=%s", target_email, error)
        raise EmailDeliveryError(
            "Не удалось отправить письмо с приглашением. Попробуйте ещё раз через минуту."
        ) from error

    return transfer


def claim_venue_account_transfer(*, token, new_password):
    transfer = get_object_or_404(
        VenueAccountTransfer.objects.select_related("venue"),
        token=token,
    )

    if transfer.status == VenueAccountTransferStatus.CLAIMED:
        raise ValidationError({"token": "Эта ссылка уже была использована."})
    if transfer.status == VenueAccountTransferStatus.CANCELLED:
        raise ValidationError({"token": "Эта ссылка больше не действительна — была создана более новая."})
    if transfer.expires_at <= timezone.now():
        transfer.status = VenueAccountTransferStatus.EXPIRED
        transfer.save(update_fields=["status", "updated_at"])
        raise ValidationError({"token": "Срок действия ссылки истёк."})

    with transaction.atomic():
        transfer = VenueAccountTransfer.objects.select_for_update().select_related("venue").get(pk=transfer.pk)
        if transfer.status != VenueAccountTransferStatus.PENDING:
            raise ValidationError({"token": "Эта ссылка уже была использована."})

        user, created = User.objects.get_or_create(
            email=transfer.target_email,
            role=UserRole.VENUE_USER,
            defaults={"full_name": transfer.target_email},
        )
        if created:
            user.password = make_password(new_password)
            user.save(update_fields=["password", "updated_at"])

        VenueUser.objects.select_for_update().filter(
            venue=transfer.venue,
            role=VenueUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        ).exclude(user=user).update(status=EntityStatus.INACTIVE)

        VenueUser.objects.update_or_create(
            venue=transfer.venue,
            user=user,
            defaults={"role": VenueUserRole.OWNER, "status": EntityStatus.ACTIVE},
        )

        reset_master_product_links_for_venue(venue_id=transfer.venue_id)

        transfer.status = VenueAccountTransferStatus.CLAIMED
        transfer.claimed_at = timezone.now()
        transfer.claimed_by = user
        transfer.save(update_fields=["status", "claimed_at", "claimed_by", "updated_at"])

    return {"venue_id": transfer.venue_id, "user_id": user.id, "email": user.email}


def _raise_if_last_owner_loses_owner_role(*, venue_id, membership, new_role):
    """Mirrors suppliers' _raise_if_last_owner_loses_owner_role."""
    if membership is None or membership.role != VenueUserRole.OWNER or new_role == VenueUserRole.OWNER:
        return
    other_active_owner_exists = (
        VenueUser.objects.filter(venue_id=venue_id, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        .exclude(pk=membership.pk)
        .exists()
    )
    if not other_active_owner_exists:
        raise ValidationError({"detail": "Нельзя понизить в роли последнего владельца заведения."})


def _demote_other_active_owners(*, venue_id, keep_user_id):
    """Mirrors suppliers' _demote_other_active_owners — locks Venue itself
    (not just VenueUser rows) to serialize concurrent "make X the owner"
    calls, same race prevention as the supplier side."""
    get_object_or_404(Venue.objects.select_for_update(), pk=venue_id)
    VenueUser.objects.select_for_update().filter(
        venue_id=venue_id,
        role=VenueUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    ).exclude(user_id=keep_user_id).update(role=VenueUserRole.MANAGER)


def list_venue_team(*, venue_id):
    get_object_or_404(Venue, pk=venue_id)
    return list(
        VenueUser.objects.filter(venue_id=venue_id, status=EntityStatus.ACTIVE)
        .select_related("user")
        .order_by("-created_at")
    )


@transaction.atomic
def revoke_venue_user(*, venue_id, target_user_id, requested_by_user_id=None):
    if str(target_user_id) == str(requested_by_user_id):
        raise ValidationError({"detail": "Нельзя отозвать доступ самому себе — используйте удаление заведения."})

    membership = get_object_or_404(
        VenueUser.objects.select_for_update(),
        venue_id=venue_id,
        user_id=target_user_id,
        status=EntityStatus.ACTIVE,
    )

    if membership.role == VenueUserRole.OWNER:
        other_active_owner_exists = (
            VenueUser.objects.filter(venue_id=venue_id, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
            .exclude(pk=membership.pk)
            .exists()
        )
        if not other_active_owner_exists:
            raise ValidationError({"detail": "Нельзя отозвать доступ последнему владельцу заведения."})

    membership.status = EntityStatus.INACTIVE
    membership.save(update_fields=["status", "updated_at"])
    return membership


@transaction.atomic
def change_venue_user_role(*, venue_id, target_user_id, new_role, requested_by_user_id=None):
    membership = get_object_or_404(
        VenueUser.objects.select_for_update(),
        venue_id=venue_id,
        user_id=target_user_id,
        status=EntityStatus.ACTIVE,
    )

    _raise_if_last_owner_loses_owner_role(venue_id=venue_id, membership=membership, new_role=new_role)

    membership.role = new_role
    membership.save(update_fields=["role", "updated_at"])

    if new_role == VenueUserRole.OWNER:
        _demote_other_active_owners(venue_id=venue_id, keep_user_id=target_user_id)

    return membership


def list_pending_venue_user_invites(*, venue_id):
    get_object_or_404(Venue, pk=venue_id)
    return list(
        VenueUserInvite.objects.filter(venue_id=venue_id, status=VenueUserInviteStatus.PENDING)
        .select_related("invited_by")
        .order_by("-created_at")
    )


@transaction.atomic
def invite_venue_user(*, venue_id, target_email, role=VenueUserRole.MANAGER, invited_by_user_id=None):
    venue = get_object_or_404(Venue.objects.select_for_update(), pk=venue_id)

    VenueUserInvite.objects.filter(
        venue=venue,
        target_email=target_email,
        status=VenueUserInviteStatus.PENDING,
    ).update(status=VenueUserInviteStatus.CANCELLED)

    ttl_hours = settings.VENUE_INVITE_TTL_HOURS
    invite = VenueUserInvite.objects.create(
        venue=venue,
        invited_by_id=invited_by_user_id,
        target_email=target_email,
        role=role,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )

    accept_url = f"{settings.VENUE_INVITE_BASE_URL.rstrip('/')}/{invite.token}"
    try:
        send_mail(
            subject="Приглашение в личный кабинет АвтоСнаб",
            message=(
                f"Вас пригласили в личный кабинет заведения «{venue.display_name}» в АвтоСнаб.\n\n"
                f"Перейдите по ссылке, чтобы задать пароль и войти:\n{accept_url}\n\n"
                f"Ссылка действует {ttl_hours} ч."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[target_email],
        )
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("venue_invite.send_mail_failed email=%s error=%s", target_email, error)
        raise EmailDeliveryError(
            "Не удалось отправить письмо с приглашением. Попробуйте ещё раз через минуту."
        ) from error

    return invite


def get_venue_user_invite_preview(*, token):
    invite = get_object_or_404(
        VenueUserInvite.objects.select_related("venue"),
        token=token,
    )
    return {
        "target_email": invite.target_email,
        "venue_name": invite.venue.display_name,
        "venue_inn": invite.venue.inn,
        "role": invite.role,
        "status": invite.status,
        "is_expired": invite.expires_at <= timezone.now(),
        "account_exists": User.objects.filter(email__iexact=invite.target_email, role=UserRole.VENUE_USER).exists(),
    }


def accept_venue_user_invite(*, token, user):
    invite = get_object_or_404(
        VenueUserInvite.objects.select_related("venue"),
        token=token,
    )

    if invite.status == VenueUserInviteStatus.CLAIMED:
        raise ValidationError({"token": "Эта ссылка уже была использована."})
    if invite.status == VenueUserInviteStatus.CANCELLED:
        raise ValidationError({"token": "Эта ссылка больше не действительна — была создана более новая."})
    if invite.expires_at <= timezone.now():
        invite.status = VenueUserInviteStatus.EXPIRED
        invite.save(update_fields=["status", "updated_at"])
        raise ValidationError({"token": "Срок действия ссылки истёк."})

    if user.email.strip().lower() != invite.target_email.strip().lower():
        raise ValidationError(
            {"detail": "Это приглашение адресовано другому email. Выйдите из аккаунта и попробуйте снова."}
        )

    with transaction.atomic():
        invite = VenueUserInvite.objects.select_for_update().select_related("venue").get(pk=invite.pk)
        if invite.status != VenueUserInviteStatus.PENDING:
            raise ValidationError({"token": "Эта ссылка уже была использована."})

        existing_membership = (
            VenueUser.objects.select_for_update()
            .filter(venue=invite.venue, user=user, status=EntityStatus.ACTIVE)
            .first()
        )
        _raise_if_last_owner_loses_owner_role(
            venue_id=invite.venue_id, membership=existing_membership, new_role=invite.role
        )

        VenueUser.objects.update_or_create(
            venue=invite.venue,
            user=user,
            defaults={"role": invite.role, "status": EntityStatus.ACTIVE},
        )
        if invite.role == VenueUserRole.OWNER:
            _demote_other_active_owners(venue_id=invite.venue_id, keep_user_id=user.id)

        invite.status = VenueUserInviteStatus.CLAIMED
        invite.claimed_at = timezone.now()
        invite.claimed_by = user
        invite.save(update_fields=["status", "claimed_at", "claimed_by", "updated_at"])

    return {
        "venue_id": invite.venue_id,
        "user_id": user.id,
        "email": user.email,
        "role": invite.role,
    }


def get_venue_invite_link_preview(*, token):
    link = get_object_or_404(
        VenueInviteLink.objects.select_related("venue"),
        token=token,
    )
    return {
        "venue_name": link.venue.display_name,
        "venue_inn": link.venue.inn,
        "role": link.role,
        "status": link.status,
        "is_expired": bool(link.expires_at and link.expires_at <= timezone.now()),
    }


def get_or_create_venue_invite_link(*, venue_id):
    venue = get_object_or_404(Venue, pk=venue_id)
    link, _ = VenueInviteLink.objects.get_or_create(
        venue=venue,
        defaults={"token": secrets.token_urlsafe(32)},
    )
    return link


@transaction.atomic
def update_venue_invite_link(*, venue_id, data):
    link = get_object_or_404(VenueInviteLink.objects.select_for_update(), venue_id=venue_id)
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
def regenerate_venue_invite_link(*, venue_id):
    link = get_object_or_404(VenueInviteLink.objects.select_for_update(), venue_id=venue_id)
    link.token = secrets.token_urlsafe(32)
    link.save(update_fields=["token", "updated_at"])
    return link


@transaction.atomic
def join_venue_via_invite_link(*, token, user):
    link = get_object_or_404(VenueInviteLink.objects.select_related("venue"), token=token)

    if link.status != EntityStatus.ACTIVE:
        raise ValidationError({"token": "Эта ссылка отключена."})
    if link.expires_at and link.expires_at <= timezone.now():
        raise ValidationError({"token": "Срок действия ссылки истёк."})
    if link.venue.status != EntityStatus.ACTIVE:
        raise ValidationError({"detail": "Заведение недоступно."})

    existing_membership = (
        VenueUser.objects.select_for_update().filter(venue=link.venue, user=user, status=EntityStatus.ACTIVE).first()
    )
    _raise_if_last_owner_loses_owner_role(venue_id=link.venue_id, membership=existing_membership, new_role=link.role)

    VenueUser.objects.update_or_create(
        venue=link.venue,
        user=user,
        defaults={"role": link.role, "status": EntityStatus.ACTIVE},
    )
    if link.role == VenueUserRole.OWNER:
        _demote_other_active_owners(venue_id=link.venue_id, keep_user_id=user.id)

    return {"venue_id": link.venue_id, "user_id": user.id, "email": user.email, "role": link.role}


@transaction.atomic
def create_venue_join_request(*, venue_id, requested_by_user_id, message=None, phone=None, email=None, documents=None):
    venue = get_object_or_404(
        Venue.objects.select_for_update(),
        pk=venue_id,
        status=EntityStatus.ACTIVE,
    )

    if VenueUser.objects.filter(venue=venue, user_id=requested_by_user_id, status=EntityStatus.ACTIVE).exists():
        raise ValidationError({"detail": "Вы уже состоите в этом заведении."})

    if VenueJoinRequest.objects.filter(
        venue=venue,
        requested_by_id=requested_by_user_id,
        status=VenueJoinRequestStatus.PENDING,
    ).exists():
        raise ValidationError({"detail": "Заявка на присоединение к этому заведению уже отправлена и ожидает решения."})

    document_urls = [
        persist_venue_join_request_document(document_file=document_file, venue_id=venue.pk)
        for document_file in (documents or [])
    ]

    join_request = VenueJoinRequest.objects.create(
        venue=venue,
        requested_by_id=requested_by_user_id,
        message=message or None,
        phone=phone or None,
        email=email or None,
        documents=document_urls,
    )

    requester = User.objects.get(pk=requested_by_user_id)
    send_mail(
        subject="Новая заявка на присоединение — АвтоСнаб",
        message=(
            f"Пользователь {requester.full_name} ({requester.email}) хочет присоединиться к "
            f"заведению «{venue.display_name}» (ИНН {venue.inn}) в личном кабинете АвтоСнаб.\n\n"
            f"ID заведения: {venue.id}\nID заявки: {join_request.id}"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.VENUE_JOIN_REQUEST_NOTIFICATION_EMAIL],
    )

    return join_request


def list_venue_join_requests(*, venue_id):
    return (
        VenueJoinRequest.objects.filter(venue_id=venue_id, status=VenueJoinRequestStatus.PENDING)
        .select_related("requested_by", "venue")
        .order_by("created_at")
    )


def list_my_venue_join_requests(*, user_id):
    return (
        VenueJoinRequest.objects.filter(requested_by_id=user_id)
        .select_related("requested_by", "venue")
        .order_by("-created_at")
    )


@transaction.atomic
def decide_venue_join_request(*, venue_id, request_id, decided_by_user_id, approve):
    join_request = get_object_or_404(
        VenueJoinRequest.objects.select_for_update(),
        pk=request_id,
        venue_id=venue_id,
    )
    if join_request.status != VenueJoinRequestStatus.PENDING:
        raise ValidationError({"detail": "Эта заявка уже была рассмотрена."})

    join_request.status = VenueJoinRequestStatus.APPROVED if approve else VenueJoinRequestStatus.REJECTED
    join_request.decided_by_id = decided_by_user_id
    join_request.decided_at = timezone.now()
    join_request.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])

    if approve:
        VenueUser.objects.update_or_create(
            venue_id=venue_id,
            user_id=join_request.requested_by_id,
            defaults={"role": VenueUserRole.MANAGER, "status": EntityStatus.ACTIVE},
        )

    return join_request
