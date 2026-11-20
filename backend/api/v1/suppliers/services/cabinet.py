"""Supplier cabinet session, settings and bot link."""

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from api.v1.suppliers.storage import (
    persist_supplier_logo,
)
from core.models import (
    EntityStatus,
)
from suppliers.models import (
    CabinetSession,
    Supplier,
    SupplierAddress,
    SupplierBankAccount,
    SupplierBotLinkToken,
    SupplierContact,
    SupplierImage,
    SupplierPriceUpload,
    SupplierProduct,
    SupplierScheduleRule,
)


def issue_supplier_cabinet_tokens(supplier, user=None):
    """Issue a JWT pair identifying a supplier cabinet session.

    Uses a `supplier_id` claim (resolved by SupplierCabinetJWTAuthentication)
    instead of SimpleJWT's default `user_id` claim, since the subject is a
    Supplier, not a core.User.

    `user`, when known, is stamped as an additional `user_id` claim — not
    used to resolve request.user (still the Supplier), only read by
    IsSupplierCabinetOwner to check the acting core.User's role for actions
    as consequential as deleting the company (2026-08-01). Omitted when the
    session comes from the bot's one-time cabinet token, which has no linked
    core.User to stamp (BotUserBinding.user is never set today).
    """
    refresh = RefreshToken()
    refresh["supplier_id"] = str(supplier.pk)
    if user is not None:
        refresh["user_id"] = str(user.pk)
    access = refresh.access_token
    access["supplier_id"] = str(supplier.pk)
    if user is not None:
        access["user_id"] = str(user.pk)
    return {"access": str(access), "refresh": str(refresh)}


@transaction.atomic
def exchange_cabinet_session_token(*, token):
    """Exchange a bot-issued one-time cabinet token for a JWT pair.

    Replaces the old flow where this same token was exchanged for a Django
    session cookie (see api.v1.views.cabinet_view) — that only worked for
    the server-rendered cabinet page. A separate frontend needs a bearer
    token it can carry itself.
    """
    session = (
        # of=("self",): binding.supplier -- nullable FK (LEFT OUTER JOIN), а Postgres не даёт
        # FOR UPDATE на nullable-стороне join'а; блокировать нужна только сама одноразовая сессия.
        CabinetSession.objects.select_for_update(of=("self",))
        .select_related("binding__supplier")
        .filter(token=token, used=False, expires_at__gt=timezone.now())
        .first()
    )
    if not session:
        raise ValidationError({"detail": "Ссылка истекла или недействительна. Запросите новую в боте."})

    session.used = True
    session.save(update_fields=["used"])

    supplier = session.binding.supplier
    tokens = issue_supplier_cabinet_tokens(supplier, user=session.binding.user)
    return {
        **tokens,
        "supplier_id": str(supplier.pk),
        "supplier_name": supplier.display_name,
    }


def get_supplier_cabinet_snapshot(supplier):
    """Company info + upload history shown on the supplier cabinet page.

    Shared by the legacy server-rendered cabinet (api.v1.views._render_cabinet)
    and the SupplierCabinetView API so the two don't drift.
    """
    import zoneinfo

    msk = zoneinfo.ZoneInfo("Europe/Moscow")

    uploads = SupplierPriceUpload.objects.filter(supplier=supplier).order_by("-uploaded_at")[:20]
    uploads_data = []
    for u in uploads:
        norm = (u.raw_payload or {}).get("normalization", {}).get("result", {})
        uploads_data.append(
            {
                "id": str(u.pk),
                "file": u.source_file_name or "текст",
                "date": u.uploaded_at.astimezone(msk).strftime("%d.%m.%Y %H:%M"),
                "status": u.status,
                "items_total": norm.get("items_total", 0),
                "products_created": norm.get("products_created", 0),
                "prices_created": norm.get("prices_created", 0),
                "error_message": u.error_message or "",
            }
        )

    products_count = SupplierProduct.objects.filter(supplier=supplier, status="active").count()
    last_upload = (
        SupplierPriceUpload.objects.filter(supplier=supplier, status="applied").order_by("-uploaded_at").first()
    )
    last_upload_at = last_upload.uploaded_at.astimezone(msk).strftime("%d.%m.%Y %H:%M") if last_upload else None

    return {
        "supplier": supplier,
        "products_count": products_count,
        "last_upload_at": last_upload_at,
        "uploads": uploads_data,
    }


@transaction.atomic
def update_supplier_cabinet(*, supplier, data):
    # Explicit lock for the duration of the transaction. Note: supplier.save()
    # below already issues an UPDATE that Postgres row-locks until commit, so
    # two concurrent calls are *already* serialized in the current code —
    # confirmed empirically (a forced-interleaving test using a barrier right
    # before _sync_supplier_nested_collections deadlocked instead of racing,
    # because the second call blocks on save()'s lock before it can even
    # reach that point). This explicit select_for_update() documents the
    # invariant and keeps the guarantee intact even if a future refactor
    # reorders things (e.g. moves the nested-collection sync before save()).
    supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
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
        "is_published",
        "delivery_terms",
        "delivery_currency",
        "delivery_min_order_amount",
        "delivery_cost",
        "delivery_zone",
        "fulfillment_method",
        "shipping_point_name",
        "shipping_point_address",
        "standard_lead_time",
        "pickup_available",
        "pickup_conditions",
        "pickup_address",
        "pickup_currency",
        "pickup_min_order_amount",
    )
    for field in allowed:
        if field in data:
            setattr(supplier, field, data[field])
    if "metadata" in data and isinstance(data["metadata"], dict):
        supplier.metadata = {**(supplier.metadata or {}), **data["metadata"]}
    supplier.save()
    _sync_supplier_nested_collections(
        supplier,
        addresses=data.get("addresses"),
        contacts=data.get("contacts"),
        bank_accounts=data.get("bank_accounts"),
        schedule_rules=data.get("schedule_rules"),
    )
    return supplier


def delete_supplier_cabinet(*, supplier):
    """Self-service "удалить компанию" (2026-07-29). Мягкое удаление —
    status=archived, не DELETE строки: SupplierPriceUpload/SupplierProduct
    ссылаются на Supplier через on_delete=RESTRICT (реальный DELETE упал бы
    с ошибкой БД для любой компании с загруженным прайсом), а остальные
    связанные записи (адреса/контакты/банковские реквизиты/график/заявки)
    каскадно стёрлись бы без возможности восстановить.

    Эффект: товары/сама компания сразу пропадают из покупательского каталога
    (catalog_products_queryset/catalog_suppliers_queryset фильтруют по
    supplier__status=active — тот же фикс добавлен вместе с этой функцией,
    иначе "удаление" никак не отражалось бы на витрине), из dedup-проверки
    при создании новой компании (_find_possible_duplicate фильтрует по
    status=active) и из входа в кабинет (SupplierCabinetJWTAuthentication
    пускает только status=active). НЕ пропадает из /suppliers/mine — владелец
    по-прежнему видит свою (теперь archived) компанию в списке, просто не
    может в неё зайти — это осознанно: тихое исчезновение строки без
    подтверждения было бы хуже, чем видимый статус archived."""
    supplier.status = EntityStatus.ARCHIVED
    supplier.is_published = False
    supplier.save(update_fields=["status", "is_published", "updated_at"])


@transaction.atomic
def restore_supplier(*, supplier_id):
    """Undo delete_supplier_cabinet() (2026-07-29). Has to go through a
    core.User JWT + CanRestoreSupplier, not the cabinet JWT — once
    status != active, SupplierCabinetJWTAuthentication rejects that
    company's cabinet token outright, so a self-service "restore" endpoint
    can't live under /suppliers/cabinet/*. Membership survives archiving
    (delete only touches Supplier.status/is_published, never SupplierUser),
    which is what makes this possible at all."""
    supplier = get_object_or_404(Supplier.objects.select_for_update(), pk=supplier_id)
    if supplier.status != EntityStatus.ARCHIVED:
        raise ValidationError({"detail": "Компания не удалена — восстанавливать нечего."})
    supplier.status = EntityStatus.ACTIVE
    supplier.is_published = True
    supplier.save(update_fields=["status", "is_published", "updated_at"])
    return supplier


@transaction.atomic
def upload_supplier_logo(*, supplier, logo_file):
    """Файл загружается и сохраняется сразу — фронту не нужен отдельный
    PATCH cabinet/me с logo_url следом (2026-07-28: до этого logo_url был
    только текстовым полем — вставить ссылку на уже где-то захостенную
    картинку, реальной загрузки файла не было, Витя сообщил, что это не
    работает).

    2026-09-13: логотип расширен до галереи фото (по прямой просьбе
    пользователя — "везде, включая оба лого") — каждый вызов ДОБАВЛЯЕТ фото,
    не заменяет предыдущее. `Supplier.logo_url` остаётся для обратной
    совместимости — синхронизируется как URL самого раннего фото в галерее."""
    image_url = persist_supplier_logo(logo_file=logo_file, supplier_id=supplier.pk)
    image = SupplierImage.objects.create(supplier=supplier, image_url=image_url)
    if not supplier.logo_url:
        supplier.logo_url = image_url
        supplier.save(update_fields=["logo_url", "updated_at"])
    return image


@transaction.atomic
def delete_supplier_image(*, supplier, image_id):
    image = _get_owned_or_404(SupplierImage.objects.all(), supplier=supplier, item_id=image_id)
    was_logo = supplier.logo_url == image.image_url
    image.delete()
    if was_logo:
        next_image = supplier.images.order_by("created_at", "id").first()
        supplier.logo_url = next_image.image_url if next_image else None
        supplier.save(update_fields=["logo_url", "updated_at"])


def _get_owned_or_404(queryset, *, supplier, item_id):
    return get_object_or_404(queryset, pk=item_id, supplier=supplier)


def _sync_supplier_nested_collections(
    supplier, *, addresses=None, contacts=None, bank_accounts=None, schedule_rules=None
):
    """Bulk (re)write the 4 nested collections in one shot — the Figma form
    is a single screen with one "Сохранить" button covering the whole card
    (company + addresses + contacts + bank accounts + schedule), not a wizard
    with a save-per-section step. Frontend can still manage items one at a
    time via the granular /cabinet/{addresses,contacts,...} endpoints; this
    is the bulk path for "save the whole form at once". Only touches a
    collection if its list is provided (not None) — replaces it entirely."""
    if addresses is not None:
        supplier.addresses.all().delete()
        SupplierAddress.objects.bulk_create([SupplierAddress(supplier=supplier, **item) for item in addresses])
    if contacts is not None:
        supplier.contacts.all().delete()
        SupplierContact.objects.bulk_create([SupplierContact(supplier=supplier, **item) for item in contacts])
    if bank_accounts is not None:
        supplier.bank_accounts.all().delete()
        # is_primary эксклюзивен — если в пачке несколько True, оставляем первый.
        objs = []
        primary_claimed = False
        for item in bank_accounts:
            item = dict(item)
            if item.get("is_primary"):
                if primary_claimed:
                    item["is_primary"] = False
                else:
                    primary_claimed = True
            objs.append(SupplierBankAccount(supplier=supplier, **item))
        SupplierBankAccount.objects.bulk_create(objs)
    if schedule_rules is not None:
        supplier.schedule_rules.all().delete()
        SupplierScheduleRule.objects.bulk_create(
            [SupplierScheduleRule(supplier=supplier, **item) for item in schedule_rules]
        )


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


def create_supplier_address(*, supplier, data):
    return SupplierAddress.objects.create(supplier=supplier, **{k: data.get(k) for k in _ADDRESS_FIELDS if k in data})


def update_supplier_address(*, supplier, address_id, data):
    address = _get_owned_or_404(SupplierAddress.objects.all(), supplier=supplier, item_id=address_id)
    for field in _ADDRESS_FIELDS:
        if field in data:
            setattr(address, field, data[field])
    address.save()
    return address


def delete_supplier_address(*, supplier, address_id):
    address = _get_owned_or_404(SupplierAddress.objects.all(), supplier=supplier, item_id=address_id)
    address.delete()


_CONTACT_FIELDS = ("contact_type", "messenger_type", "value")


def create_supplier_contact(*, supplier, data):
    return SupplierContact.objects.create(supplier=supplier, **{k: data.get(k) for k in _CONTACT_FIELDS if k in data})


def update_supplier_contact(*, supplier, contact_id, data):
    contact = _get_owned_or_404(SupplierContact.objects.all(), supplier=supplier, item_id=contact_id)
    for field in _CONTACT_FIELDS:
        if field in data:
            setattr(contact, field, data[field])
    contact.save()
    return contact


def delete_supplier_contact(*, supplier, contact_id):
    contact = _get_owned_or_404(SupplierContact.objects.all(), supplier=supplier, item_id=contact_id)
    contact.delete()


_BANK_ACCOUNT_FIELDS = ("bank_name", "account_number", "bik", "correspondent_account", "is_primary")


@transaction.atomic
def create_supplier_bank_account(*, supplier, data):
    # is_primary — эксклюзивный флаг: если новый счёт заявлен основным, снимаем
    # флаг с остальных счетов этого поставщика, иначе можно было бы получить
    # несколько "основных" счетов одновременно.
    if data.get("is_primary"):
        SupplierBankAccount.objects.filter(supplier=supplier, is_primary=True).update(is_primary=False)
    return SupplierBankAccount.objects.create(
        supplier=supplier, **{k: data.get(k) for k in _BANK_ACCOUNT_FIELDS if k in data}
    )


@transaction.atomic
def update_supplier_bank_account(*, supplier, bank_account_id, data):
    account = _get_owned_or_404(
        SupplierBankAccount.objects.select_for_update(), supplier=supplier, item_id=bank_account_id
    )
    if data.get("is_primary"):
        SupplierBankAccount.objects.filter(supplier=supplier, is_primary=True).exclude(pk=account.pk).update(
            is_primary=False
        )
    for field in _BANK_ACCOUNT_FIELDS:
        if field in data:
            setattr(account, field, data[field])
    account.save()
    return account


def delete_supplier_bank_account(*, supplier, bank_account_id):
    account = _get_owned_or_404(SupplierBankAccount.objects.all(), supplier=supplier, item_id=bank_account_id)
    account.delete()


_SCHEDULE_RULE_FIELDS = (
    "schedule_type",
    "weekday",
    "accepts_from",
    "accepts_until",
    "fulfillment_weekday",
    "notes",
    "is_active",
)


def create_supplier_schedule_rule(*, supplier, data):
    return SupplierScheduleRule.objects.create(
        supplier=supplier, **{k: data.get(k) for k in _SCHEDULE_RULE_FIELDS if k in data}
    )


def update_supplier_schedule_rule(*, supplier, rule_id, data):
    rule = _get_owned_or_404(SupplierScheduleRule.objects.all(), supplier=supplier, item_id=rule_id)
    for field in _SCHEDULE_RULE_FIELDS:
        if field in data:
            setattr(rule, field, data[field])
    rule.save()
    return rule


def delete_supplier_schedule_rule(*, supplier, rule_id):
    rule = _get_owned_or_404(SupplierScheduleRule.objects.all(), supplier=supplier, item_id=rule_id)
    rule.delete()


def create_supplier_bot_link_token(*, supplier, channel="telegram", created_by=None):
    """
    Создаёт одноразовый токен и deep-link для подключения к боту «Поставщик».

    Параметры:
        supplier: объект Supplier (компания-поставщик)
        channel: канал бота — "telegram" или "max" (по умолчанию telegram)
        created_by: пользователь, создавший токен (опционально)

    Возвращает:
        token: одноразовый токен (часть deep_link после ?start=)
        deep_link: готовая ссылка для отправки агенту
        expires_at: дата и время истечения токена
        channel: канал, для которого создана ссылка
    """
    ttl_minutes = settings.SUPPLIER_BOT_LINK_TTL_MINUTES

    link_token = SupplierBotLinkToken.objects.create(
        supplier=supplier,
        created_by=created_by,
        channel=channel,  # <-- ДОБАВЛЕНО: сохраняем канал
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
    )

    # Формируем deep_link в зависимости от канала
    if channel == "telegram":
        bot_username = getattr(settings, "TELEGRAM_BOT_USERNAME", "AutosnabSupplierBot")
        deep_link = f"https://t.me/{bot_username}?start={link_token.token}"
    elif channel == "max":
        bot_username = getattr(settings, "MAX_BOT_USERNAME", "AutosnabSupplierBot")
        deep_link = f"https://max.ru/bot/{bot_username}?start={link_token.token}"
    else:
        # На случай, если передан неизвестный канал
        deep_link = f"https://bot.example.com/start?token={link_token.token}"

    return {
        "token": link_token.token,
        "deep_link": deep_link,
        "expires_at": link_token.expires_at,
        "channel": channel,  # <-- ДОБАВЛЕНО: возвращаем канал
    }
