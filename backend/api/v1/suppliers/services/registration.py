"""Supplier creation and account transfer."""

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

from api.v1.auth.services import EmailDeliveryError
from api.v1.suppliers.dedup import (
    DIFFERENT,
    POSSIBLE_DUPLICATE,
    SAME_BRANCH,
    SAME_COMPANY,
    CompanyCanonical,
    dedup_decision,
)
from api.v1.validators import validate_inn
from core.models import (
    EntityStatus,
    User,
    UserRole,
)
from suppliers.models import (
    Supplier,
    SupplierAccountTransfer,
    SupplierAccountTransferStatus,
    SupplierUser,
    SupplierUserRole,
)

from ._shared import logger
from .cabinet import _sync_supplier_nested_collections


def _find_possible_duplicate(incoming):
    """Provider-agnostic anti-dup check (api/v1/suppliers/dedup.py) — scans
    active suppliers for the best match. Returns (Supplier, label, confidence)
    for the highest-confidence non-DIFFERENT match, or None.

    O(n) over active suppliers — fine at AutoSnab's current scale (see
    docs/wiki/logs/2026-07-23-supplier-dedup-algorithm.md); revisit with a
    pg_trgm-backed query if the supplier count grows into the thousands."""
    best = None
    for candidate in Supplier.objects.filter(status=EntityStatus.ACTIVE):
        existing = CompanyCanonical(
            inn=candidate.inn,
            kpp=candidate.kpp,
            ogrn=candidate.ogrn,
            full_name=candidate.legal_name,
            address=candidate.legal_address,
            status=None,
        )
        label, confidence = dedup_decision(existing, incoming)
        if label == DIFFERENT:
            continue
        if best is None or confidence > best[2]:
            best = (candidate, label, confidence)
    return best


@transaction.atomic
def _create_supplier_record(
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
    is_published=None,
    delivery_terms=None,
    delivery_currency=None,
    delivery_min_order_amount=None,
    delivery_cost=None,
    delivery_zone=None,
    fulfillment_method=None,
    shipping_point_name=None,
    shipping_point_address=None,
    standard_lead_time=None,
    pickup_available=False,
    pickup_conditions=None,
    pickup_address=None,
    pickup_currency=None,
    pickup_min_order_amount=None,
    addresses=None,
    contacts=None,
    bank_accounts=None,
    schedule_rules=None,
    metadata,
    confirm_not_duplicate=False,
):
    inn = (inn or "").strip()
    kpp = (kpp or "").strip()
    ogrn = (ogrn or "").strip()
    if inn:
        inn_error = validate_inn(inn)
        if inn_error:
            raise ValidationError({"inn": inn_error})

    incoming = CompanyCanonical(
        inn=inn or None,
        kpp=kpp or None,
        ogrn=ogrn or None,
        full_name=legal_name or display_name,
        address=legal_address or None,
        status=None,
    )
    match = _find_possible_duplicate(incoming)
    if match is not None:
        existing, label, confidence = match
        if label in (SAME_BRANCH, SAME_COMPANY):
            raise ValidationError(
                {
                    "detail": f"Компания с такими реквизитами уже зарегистрирована: «{existing.display_name}».",
                    "duplicate_supplier_id": str(existing.id),
                    "duplicate_match": label,
                }
            )
        if label == POSSIBLE_DUPLICATE and not confirm_not_duplicate:
            raise ValidationError(
                {
                    "detail": (
                        f"Похоже, такая компания уже есть: «{existing.display_name}» (совпадение по названию "
                        "и/или адресу). Если это другая компания, повторите запрос с confirm_not_duplicate=true."
                    ),
                    "duplicate_supplier_id": str(existing.id),
                    "duplicate_match": label,
                    "duplicate_confidence": confidence,
                }
            )

    supplier = Supplier.objects.create(
        display_name=display_name,
        legal_name=legal_name or display_name,
        short_name=short_name or None,
        entity_type=entity_type or None,
        inn=inn or None,
        kpp=kpp or None,
        ogrn=ogrn or None,
        legal_address=legal_address or None,
        okved=okved or None,
        okpo=okpo or None,
        registration_date=registration_date or None,
        general_director=general_director or None,
        logo_url=logo_url or None,
        description=description or None,
        delivery_terms=delivery_terms or None,
        delivery_currency=delivery_currency or "RUB",
        delivery_min_order_amount=delivery_min_order_amount,
        delivery_cost=delivery_cost,
        delivery_zone=delivery_zone or None,
        fulfillment_method=fulfillment_method or None,
        shipping_point_name=shipping_point_name or None,
        shipping_point_address=shipping_point_address or None,
        standard_lead_time=standard_lead_time or None,
        pickup_available=bool(pickup_available),
        pickup_conditions=pickup_conditions or None,
        pickup_address=pickup_address or None,
        pickup_currency=pickup_currency or "RUB",
        pickup_min_order_amount=pickup_min_order_amount,
        status=EntityStatus.ACTIVE,
        is_published=True if is_published is None else is_published,
        metadata=metadata,
    )
    _sync_supplier_nested_collections(
        supplier,
        addresses=addresses,
        contacts=contacts,
        bank_accounts=bank_accounts,
        schedule_rules=schedule_rules,
    )
    return supplier


@transaction.atomic
def create_supplier_by_staff(
    *,
    created_by_user,
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
    is_published=None,
    delivery_terms=None,
    delivery_currency=None,
    delivery_min_order_amount=None,
    delivery_cost=None,
    delivery_zone=None,
    fulfillment_method=None,
    shipping_point_name=None,
    shipping_point_address=None,
    standard_lead_time=None,
    pickup_available=False,
    pickup_conditions=None,
    pickup_address=None,
    pickup_currency=None,
    pickup_min_order_amount=None,
    addresses=None,
    contacts=None,
    bank_accounts=None,
    schedule_rules=None,
    confirm_not_duplicate=False,
):
    """Admin onboards a company before the real supplier exists on the
    platform. They become the temporary owner — claim_supplier_account_transfer()
    later steps them down when the real supplier claims the account."""
    supplier = _create_supplier_record(
        display_name=display_name,
        legal_name=legal_name,
        short_name=short_name,
        entity_type=entity_type,
        inn=inn,
        kpp=kpp,
        ogrn=ogrn,
        legal_address=legal_address,
        okved=okved,
        okpo=okpo,
        registration_date=registration_date,
        general_director=general_director,
        logo_url=logo_url,
        description=description,
        is_published=is_published,
        delivery_terms=delivery_terms,
        delivery_currency=delivery_currency,
        delivery_min_order_amount=delivery_min_order_amount,
        delivery_cost=delivery_cost,
        delivery_zone=delivery_zone,
        fulfillment_method=fulfillment_method,
        shipping_point_name=shipping_point_name,
        shipping_point_address=shipping_point_address,
        standard_lead_time=standard_lead_time,
        pickup_available=pickup_available,
        pickup_conditions=pickup_conditions,
        pickup_address=pickup_address,
        pickup_currency=pickup_currency,
        pickup_min_order_amount=pickup_min_order_amount,
        addresses=addresses,
        contacts=contacts,
        bank_accounts=bank_accounts,
        schedule_rules=schedule_rules,
        metadata={"added_by_staff": True},
        confirm_not_duplicate=confirm_not_duplicate,
    )
    SupplierUser.objects.create(
        supplier=supplier,
        user=created_by_user,
        role=SupplierUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    )
    return supplier


@transaction.atomic
def create_supplier_by_owner(
    *,
    user,
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
    is_published=None,
    delivery_terms=None,
    delivery_currency=None,
    delivery_min_order_amount=None,
    delivery_cost=None,
    delivery_zone=None,
    fulfillment_method=None,
    shipping_point_name=None,
    shipping_point_address=None,
    standard_lead_time=None,
    pickup_available=False,
    pickup_conditions=None,
    pickup_address=None,
    pickup_currency=None,
    pickup_min_order_amount=None,
    addresses=None,
    contacts=None,
    bank_accounts=None,
    schedule_rules=None,
    confirm_not_duplicate=False,
):
    """Self-service: a registered supplier (role=supplier) creates and
    immediately owns a company — the "нет компании, некуда деваться после
    регистрации" gap. Unlike create_supplier_by_staff, ownership isn't
    temporary. Can be called more than once — a supplier may own/manage
    several companies at a time (2026-07-24 decision, closes the open
    multi-company question from docs/wiki/logs/2026-07-20-sync-call.md);
    GET /suppliers/mine lists all of them."""
    # Locking the user row serializes concurrent calls for the *same* user
    # (double-click, client retry on a flaky connection) — without it, two
    # near-simultaneous calls with the *same* INN can both pass the dedup
    # check below before either commits and both create a company with
    # that INN. Doesn't limit company count anymore, just keeps the dedup
    # check below race-safe for repeated submissions from one user.
    User.objects.select_for_update().get(pk=user.pk)

    supplier = _create_supplier_record(
        display_name=display_name,
        legal_name=legal_name,
        short_name=short_name,
        entity_type=entity_type,
        inn=inn,
        kpp=kpp,
        ogrn=ogrn,
        legal_address=legal_address,
        okved=okved,
        okpo=okpo,
        registration_date=registration_date,
        general_director=general_director,
        logo_url=logo_url,
        description=description,
        is_published=is_published,
        delivery_terms=delivery_terms,
        delivery_currency=delivery_currency,
        delivery_min_order_amount=delivery_min_order_amount,
        delivery_cost=delivery_cost,
        delivery_zone=delivery_zone,
        fulfillment_method=fulfillment_method,
        shipping_point_name=shipping_point_name,
        shipping_point_address=shipping_point_address,
        standard_lead_time=standard_lead_time,
        pickup_available=pickup_available,
        pickup_conditions=pickup_conditions,
        pickup_address=pickup_address,
        pickup_currency=pickup_currency,
        pickup_min_order_amount=pickup_min_order_amount,
        addresses=addresses,
        contacts=contacts,
        bank_accounts=bank_accounts,
        schedule_rules=schedule_rules,
        metadata={},
        confirm_not_duplicate=confirm_not_duplicate,
    )
    SupplierUser.objects.create(
        supplier=supplier,
        user=user,
        role=SupplierUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    )
    return supplier


@transaction.atomic
def initiate_supplier_account_transfer(*, supplier_id, target_email, initiated_by_user_id=None):
    supplier = get_object_or_404(Supplier.objects.select_for_update(), pk=supplier_id)

    # Only one live claim link per supplier at a time — superseding an older
    # pending transfer avoids two people racing to claim the same account.
    SupplierAccountTransfer.objects.filter(
        supplier=supplier,
        status=SupplierAccountTransferStatus.PENDING,
    ).update(status=SupplierAccountTransferStatus.CANCELLED)

    ttl_hours = settings.ACCOUNT_TRANSFER_TTL_HOURS
    transfer = SupplierAccountTransfer.objects.create(
        supplier=supplier,
        initiated_by_id=initiated_by_user_id,
        target_email=target_email,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )

    claim_url = f"{settings.ACCOUNT_TRANSFER_CLAIM_BASE_URL.rstrip('/')}/{transfer.token}"
    try:
        send_mail(
            subject="Передача аккаунта в АвтоСнаб",
            message=(
                f"Вам передан аккаунт поставщика «{supplier.display_name}» в АвтоСнаб.\n\n"
                f"Перейдите по ссылке, чтобы задать пароль и войти:\n{claim_url}\n\n"
                f"Ссылка действует {ttl_hours} ч."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[target_email],
        )
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("account_transfer.send_mail_failed email=%s error=%s", target_email, error)
        raise EmailDeliveryError(
            "Не удалось отправить письмо с приглашением. Попробуйте ещё раз через минуту."
        ) from error

    return transfer


def claim_supplier_account_transfer(*, token, new_password):
    # Guard checks run outside any atomic block so that marking an expired
    # transfer as EXPIRED is persisted even though we then raise — otherwise
    # the raise would roll the status change back along with everything else.
    transfer = get_object_or_404(
        SupplierAccountTransfer.objects.select_related("supplier"),
        token=token,
    )

    if transfer.status == SupplierAccountTransferStatus.CLAIMED:
        raise ValidationError({"token": "Эта ссылка уже была использована."})
    if transfer.status == SupplierAccountTransferStatus.CANCELLED:
        raise ValidationError({"token": "Эта ссылка больше не действительна — была создана более новая."})
    if transfer.expires_at <= timezone.now():
        transfer.status = SupplierAccountTransferStatus.EXPIRED
        transfer.save(update_fields=["status", "updated_at"])
        raise ValidationError({"token": "Срок действия ссылки истёк."})

    with transaction.atomic():
        transfer = SupplierAccountTransfer.objects.select_for_update().select_related("supplier").get(pk=transfer.pk)
        if transfer.status != SupplierAccountTransferStatus.PENDING:
            raise ValidationError({"token": "Эта ссылка уже была использована."})

        # role в lookup, не только defaults (2026-08-06) — email уникален по
        # (email, role), не глобально: если с этим email уже есть venue_user-
        # аккаунт, get_or_create должен завести отдельный supplier-аккаунт,
        # а не наткнуться на чужую роль или создать дубль по email.
        user, created = User.objects.get_or_create(
            email=transfer.target_email,
            role=UserRole.SUPPLIER,
            defaults={"full_name": transfer.target_email},
        )
        # Only a brand-new account needs a password set here — an existing
        # account already has one (or logs in via OAuth), and silently
        # overwriting it on claim would lock the real owner out of their
        # own password.
        if created:
            user.password = make_password(new_password)
            user.save(update_fields=["password", "updated_at"])

        # The handover is exclusive: whoever owned this supplier's seat before
        # (e.g. the снабженец who filled it in first) steps down as the real
        # supplier takes over, rather than ending up as a second active owner.
        SupplierUser.objects.select_for_update().filter(
            supplier=transfer.supplier,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        ).exclude(user=user).update(status=EntityStatus.INACTIVE)

        SupplierUser.objects.update_or_create(
            supplier=transfer.supplier,
            user=user,
            defaults={"role": SupplierUserRole.OWNER, "status": EntityStatus.ACTIVE},
        )

        transfer.status = SupplierAccountTransferStatus.CLAIMED
        transfer.claimed_at = timezone.now()
        transfer.claimed_by = user
        transfer.save(update_fields=["status", "claimed_at", "claimed_by", "updated_at"])

    return {"supplier_id": transfer.supplier_id, "user_id": user.id, "email": user.email}
