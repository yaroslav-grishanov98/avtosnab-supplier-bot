from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import EntityStatus, UUIDTimestampedModel
from core.search_expressions import SearchNameKey


class ProductAvailability(models.TextChoices):
    IN_STOCK = "in_stock", "В наличии"
    ON_ORDER = "on_order", "Под заказ"
    OUT_OF_STOCK = "out_of_stock", "Нет в наличии"


class SupplierUserRole(models.TextChoices):
    """2026-08-05: раньше было три уровня (owner/manager/operator) — по явному
    решению пользователя operator убран, owner и manager теперь имеют абсолютно
    одинаковые права везде. OWNER не даёт дополнительных прав — используется
    только для двух инвариантов в services.py: у компании должен быть хотя бы
    один активный owner (revoke_supplier_user/change_supplier_user_role), и не
    больше одного — ровно один (2026-08-06, _demote_other_active_owners):
    назначение нового owner'а (сменой роли, email-инвайтом или invite-link)
    понижает предыдущего до manager (2026-08-07: доступ сохраняется, теряется
    только owner-статус)."""

    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"


class SupplierAddressType(models.TextChoices):
    LEGAL = "legal", "Юридический"
    ACTUAL = "actual", "Фактический"
    HOME = "home", "Домашний"


class SupplierContactType(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Телефон"
    MESSENGER = "messenger", "Мессенджер"
    LINK = "link", "Ссылка на сайт или социальные сети"


class SupplierEntityType(models.TextChoices):
    ORGANIZATION = "organization", "Организация"
    INDIVIDUAL_ENTREPRENEUR = "individual_entrepreneur", "ИП"
    INDIVIDUAL = "individual", "Физ. лицо"


class SupplierFulfillmentMethod(models.TextChoices):
    SUPPLIER_DELIVERY = "supplier_delivery", "Доставка поставщиком"
    PICKUP = "pickup", "Самовывоз"
    CARRIER = "carrier", "Перевозчик"


class SupplierScheduleType(models.TextChoices):
    DELIVERY = "delivery", "Приём и доставка заказа"
    PICKUP = "pickup", "Приём и готовность заказа для самовывоза"


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Понедельник"
    TUESDAY = 1, "Вторник"
    WEDNESDAY = 2, "Среда"
    THURSDAY = 3, "Четверг"
    FRIDAY = 4, "Пятница"
    SATURDAY = 5, "Суббота"
    SUNDAY = 6, "Воскресенье"


class SourceChannel(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    MAX = "max", "Max"
    WEB = "web", "Web"
    API = "api", "API"
    GOOGLE_SHEETS = "google_sheets", "Google Sheets"
    MANUAL = "manual", "Manual"


class UploadStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSING = "processing", "Processing"
    PARSED = "parsed", "Parsed"
    APPLIED = "applied", "Applied"
    NEEDS_REVIEW = "needs_review", "Needs review"
    FAILED = "failed", "Failed"
    ARCHIVED = "archived", "Archived"


class UploadItemStatus(models.TextChoices):
    NEW = "new", "New"
    MATCHED = "matched", "Matched"
    UPDATED = "updated", "Updated"
    REJECTED = "rejected", "Rejected"
    NEEDS_REVIEW = "needs_review", "Needs review"


class PriceType(models.TextChoices):
    PRICE_LIST = "price_list", "Price List"
    INVOICE = "invoice", "Invoice"


class PriceStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUPERSEDED = "superseded", "Superseded"
    CANCELLED = "cancelled", "Cancelled"


class Supplier(UUIDTimestampedModel):
    legal_name = models.TextField()
    # "Полное наименование" (legal_name) — например, "Общество с ограниченной
    # ответственностью «Ромашка»". short_name — "Сокращённое наименование" из того же
    # блока реквизитов в макете, например "ООО «Ромашка»" — отдельное поле из ЕГРЮЛ/dadata
    # (name.short_with_opf), не то же самое, что display_name (публичное название бренда).
    short_name = models.TextField(blank=True, null=True)
    display_name = models.TextField()
    entity_type = models.CharField(max_length=24, choices=SupplierEntityType.choices, blank=True, null=True)
    inn = models.TextField(blank=True, null=True)
    kpp = models.TextField(blank=True, null=True)
    ogrn = models.TextField(blank=True, null=True)
    # Юридический адрес из ЕГРЮЛ/dadata — отдельно от delivery_zone (это про
    # логистику доставки, не про регистрационные данные). Используется в
    # дедупе (api/v1/suppliers/dedup.py) как fallback-сигнал, когда ИНН ещё
    # не известен.
    legal_address = models.TextField(blank=True, null=True)
    okved = models.TextField(blank=True, null=True)
    okpo = models.TextField(blank=True, null=True)
    registration_date = models.DateField(blank=True, null=True)
    general_director = models.TextField(blank=True, null=True)
    logo_url = models.URLField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    # Системный статус (создание/модерация/архивация) — SupplierCabinetJWTAuthentication
    # отклоняет вход в кабинет, если status != active (см. api/v1/authentication.py). Поэтому
    # публикация в каталоге НЕ переиспользует status — самообслуживаемое "снять с публикации" не
    # должно попутно лишать поставщика доступа к собственному кабинету. Для этого отдельное
    # is_published (2026-07-28, найдено на живой проверке: сначала попытались завязать на status,
    # но это моментально блокировало вход после self-service unpublish без пути назад).
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    is_published = models.BooleanField(default=True)
    delivery_terms = models.TextField(blank=True, null=True)
    # 2026-07-28: в макете "Минимальная сумма заказа"/"Валюта" показаны
    # отдельно в секции "Условия доставки курьером" и отдельно в "Условия
    # самовывоза" — по решению пользователя это два независимых значения,
    # не одно общее на компанию (у самовывоза минимума может не быть вовсе).
    delivery_min_order_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    delivery_currency = models.CharField(max_length=3, default="RUB")
    delivery_cost = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    delivery_zone = models.TextField(blank=True, null=True)
    fulfillment_method = models.CharField(
        max_length=20, choices=SupplierFulfillmentMethod.choices, blank=True, null=True
    )
    shipping_point_name = models.TextField(blank=True, null=True)
    shipping_point_address = models.TextField(blank=True, null=True)
    standard_lead_time = models.TextField(blank=True, null=True)
    pickup_available = models.BooleanField(default=False)
    pickup_conditions = models.TextField(blank=True, null=True)
    pickup_address = models.TextField(blank=True, null=True)
    pickup_min_order_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    pickup_currency = models.CharField(max_length=3, default="RUB")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "suppliers"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(inn__isnull=True) | ~models.Q(inn__regex=r"^\s*$"),
                name="suppliers_inn_nonempty",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="suppliers_status_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name


class SupplierAddress(UUIDTimestampedModel):
    """Типизированный адрес компании — юр./факт./домашний, структурированные
    поля из формы адреса в Figma-макете (2026-07-28). Компания может иметь
    несколько адресов одного и того же типа (несколько складов/точек и т.п.)."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="addresses")
    address_type = models.CharField(max_length=16, choices=SupplierAddressType.choices)
    # Полная строка адреса одним полем ("Красноярский край, г. Красноярск,
    # ул. Новосибирская, стр.4, оф. 32" в макете) — основной ввод/отображение.
    # country/region/... ниже — необязательная развёрнутая детализация
    # ("Раскрыть"/"Скрыть" в макете), не замена этого поля.
    address = models.TextField(blank=True, null=True)
    country = models.TextField(blank=True, null=True)
    region = models.TextField(blank=True, null=True)
    district = models.TextField(blank=True, null=True)
    locality = models.TextField(blank=True, null=True)
    street = models.TextField(blank=True, null=True)
    postal_code = models.TextField(blank=True, null=True)
    apartment_office = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "supplier_addresses"
        indexes = [
            models.Index(fields=["supplier"], name="supplier_addr_supplier_idx"),
        ]


class SupplierContact(UUIDTimestampedModel):
    """Дополнительный контакт компании (email/телефон/мессенджер) — компания
    может иметь несколько контактов каждого типа ("Добавить ещё" в макете).
    Supplier.email/phone остаются основным контактом, эта модель — для
    остальных."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="contacts")
    contact_type = models.CharField(max_length=16, choices=SupplierContactType.choices)
    messenger_type = models.TextField(blank=True, null=True)
    value = models.TextField()

    class Meta:
        db_table = "supplier_contacts"
        indexes = [
            models.Index(fields=["supplier"], name="supplier_contacts_supplier_idx"),
        ]


class SupplierBankAccount(UUIDTimestampedModel):
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="bank_accounts")
    bank_name = models.TextField()
    account_number = models.TextField()
    bik = models.TextField()
    correspondent_account = models.TextField(blank=True, null=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "supplier_bank_accounts"
        indexes = [
            models.Index(fields=["supplier"], name="supplier_bank_acc_supplier_idx"),
        ]


class SupplierImage(UUIDTimestampedModel):
    """2026-09-13: логотип поставщика расширен до галереи из нескольких фото
    (по прямой просьбе пользователя — "везде, включая оба лого"). Mirrors
    core.VenueImage. `Supplier.logo_url` остаётся для обратной совместимости
    — синхронизируется как URL самого раннего фото в галерее."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="images")
    image_url = models.URLField()

    class Meta:
        db_table = "supplier_images"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["supplier"], name="supplier_images_supplier_idx"),
        ]


class SupplierScheduleRule(UUIDTimestampedModel):
    """Строка недельного графика приёма/доставки или приёма/готовности для
    самовывоза — компания может иметь несколько правил (по дню недели).
    Хранится как данные; сама целесообразность детального графика vs.
    простого lead time — открытый бизнес-вопрос, см.
    docs/wiki/supplier-cabinet-figma-design-2026-07.md."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="schedule_rules")
    schedule_type = models.CharField(max_length=16, choices=SupplierScheduleType.choices)
    weekday = models.IntegerField(choices=Weekday.choices)
    accepts_from = models.TimeField()
    accepts_until = models.TimeField()
    fulfillment_weekday = models.IntegerField(choices=Weekday.choices, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    # 2026-07-29: правило можно временно выключить, не удаляя строку (например,
    # "по средам сейчас не принимаем заказы") — тот же принцип, что "enabled" у
    # каждого дня в легаси-кабинете (templates/cabinet.html, JSON metadata.schedule),
    # только теперь на реальной модели.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "supplier_schedule_rules"
        indexes = [
            models.Index(fields=["supplier", "schedule_type"], name="supplier_sched_supp_type_idx"),
        ]


class SupplierUser(UUIDTimestampedModel):
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="supplier_users")
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="supplier_users")
    role = models.CharField(max_length=16, choices=SupplierUserRole.choices)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "supplier_users"
        constraints = [
            models.UniqueConstraint(fields=["supplier", "user"], name="supplier_users_supplier_user_uniq"),
        ]
        indexes = [
            models.Index(fields=["user"], name="supplier_users_user_id_idx"),
            models.Index(fields=["supplier", "status"], name="su_supplier_status_idx"),
        ]


class SupplierAccountTransferStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class SupplierAccountTransfer(UUIDTimestampedModel):
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="account_transfers")
    initiated_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="initiated_account_transfers",
    )
    target_email = models.EmailField()
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=SupplierAccountTransferStatus.choices,
        default=SupplierAccountTransferStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(blank=True, null=True)
    claimed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="claimed_account_transfers",
    )

    class Meta:
        db_table = "supplier_account_transfers"
        indexes = [
            models.Index(fields=["token"], name="sat_token_idx"),
            models.Index(fields=["supplier", "status"], name="sat_supplier_status_idx"),
        ]


class SupplierUserInviteStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class SupplierUserInvite(UUIDTimestampedModel):
    """Adds a second (or third...) active SupplierUser to an existing company.
    role=manager never touches the existing owner. role=owner (2026-08-06) DOES
    demote the existing owner to manager on accept (2026-08-07: keeps their
    access, only the owner status changes hands) — same single-owner invariant
    as SupplierAccountTransfer, see _demote_other_active_owners() in
    services.py."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="user_invites")
    invited_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="sent_supplier_user_invites",
    )
    target_email = models.EmailField()
    token = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=16, choices=SupplierUserRole.choices, default=SupplierUserRole.MANAGER)
    status = models.CharField(
        max_length=16,
        choices=SupplierUserInviteStatus.choices,
        default=SupplierUserInviteStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(blank=True, null=True)
    claimed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="claimed_supplier_user_invites",
    )

    class Meta:
        db_table = "supplier_user_invites"
        indexes = [
            models.Index(fields=["token"], name="sui_token_idx"),
            models.Index(fields=["supplier", "status"], name="sui_supplier_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier} -> {self.target_email} ({self.status})"


class SupplierInviteLink(UUIDTimestampedModel):
    """2026-08-04: "Пригласите по ссылке" — Витя прислал референс из Bitrix24
    (не наш макет), где приглашение работает не по конкретному email
    (SupplierUserInvite), а одной расшариваемой многоразовой ссылкой с
    настройками (роль, срок действия, вкл/выкл). Один активный ряд на
    поставщика — "regenerate" переиздаёт token у того же ряда, а не плодит
    историю ссылок, потому что UI показывает ровно одну ссылку за раз."""

    supplier = models.OneToOneField("suppliers.Supplier", on_delete=models.CASCADE, related_name="invite_link")
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_supplier_invite_links",
    )
    token = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=16, choices=SupplierUserRole.choices, default=SupplierUserRole.MANAGER)
    # active/inactive — переиспользует EntityStatus, а не отдельный enum,
    # чтобы "выключить ссылку" выглядело так же, как везде в проекте.
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    expires_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "supplier_invite_links"
        indexes = [
            models.Index(fields=["token"], name="sil_token_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier} invite link ({self.status})"


class SupplierJoinRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class SupplierJoinRequest(UUIDTimestampedModel):
    """A logged-in core.User asks to join a company they already found (by
    INN) — the requester initiates, the company's own active owner
    approves/rejects from their ЛК. Mirror image of SupplierUserInvite
    (owner-initiated): no token/email-claim step needed since the requester
    already has an account."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="join_requests")
    requested_by = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="supplier_join_requests")
    message = models.TextField(blank=True, null=True)
    # Контактные телефон/email конкретно для этой заявки — не обязательно совпадают с
    # requested_by.phone/.email на аккаунте (например, рабочий номер в этой компании,
    # 2026-07-29). documents — ссылки на подтверждающие файлы (скан доверенности и т.п.),
    # загружаются через POST .../join-requests тем же multipart-запросом.
    phone = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    documents = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=SupplierJoinRequestStatus.choices,
        default=SupplierJoinRequestStatus.PENDING,
    )
    decided_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="decided_supplier_join_requests",
    )
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "supplier_join_requests"
        indexes = [
            models.Index(fields=["supplier", "status"], name="join_requests_sup_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.requested_by} -> {self.supplier} ({self.status})"


class BotUserBinding(UUIDTimestampedModel):
    # Account linking happens immediately after web authentication, before a
    # first company necessarily exists. The company is selected later, after
    # it is created in either the cabinet or the bot.
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.CASCADE,
        related_name="bot_bindings",
        blank=True,
        null=True,
    )
    user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="bot_bindings",
    )
    channel = models.CharField(max_length=32, choices=SourceChannel.choices)
    chat_id = models.TextField()
    external_user_id = models.TextField(blank=True, null=True)
    username = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "bot_user_bindings"
        constraints = [
            models.UniqueConstraint(fields=["channel", "chat_id"], name="bot_bindings_channel_chat_uniq"),
        ]
        indexes = [
            models.Index(fields=["supplier", "status"], name="bot_bind_supplier_status_idx"),
            models.Index(fields=["channel", "external_user_id"], name="bot_bind_channel_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.channel}:{self.chat_id} -> {self.supplier}"


class BotPendingAction(UUIDTimestampedModel):
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="bot_pending_actions",
    )
    platform = models.CharField(max_length=32, choices=SourceChannel.choices)
    telegram_user_id = models.TextField(blank=True, null=True)
    chat_id = models.TextField()
    action_type = models.CharField(max_length=64)
    file_id = models.TextField(blank=True, null=True)
    file_name = models.TextField(blank=True, null=True)
    mime_type = models.TextField(blank=True, null=True)
    message_id = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=64)
    raw_payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "bot_pending_actions"
        indexes = [
            models.Index(fields=["platform", "chat_id", "status"], name="bot_pending_chat_status_idx"),
            models.Index(fields=["platform", "telegram_user_id"], name="bot_pending_user_idx"),
            models.Index(fields=["supplier", "status"], name="bot_pending_supplier_idx"),
            models.Index(fields=["expires_at"], name="bot_pending_expires_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.chat_id}:{self.action_type}:{self.status}"


class WorkflowEvent(UUIDTimestampedModel):
    event_type = models.CharField(max_length=128)
    user_id = models.TextField(blank=True, null=True)
    chat_id = models.TextField()
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="workflow_events",
    )
    upload_id = models.UUIDField(blank=True, null=True)
    state_before = models.CharField(max_length=64, blank=True, null=True)
    state_after = models.CharField(max_length=64, blank=True, null=True)
    payload_json = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "workflow_events"
        indexes = [
            models.Index(fields=["event_type", "-created_at"], name="workflow_events_type_idx"),
            models.Index(fields=["chat_id", "-created_at"], name="workflow_events_chat_idx"),
            models.Index(fields=["supplier", "-created_at"], name="workflow_events_supplier_idx"),
            models.Index(fields=["upload_id"], name="workflow_events_upload_idx"),
        ]


class BotUserState(UUIDTimestampedModel):
    platform = models.CharField(max_length=32, choices=SourceChannel.choices)
    telegram_user_id = models.TextField(blank=True, null=True)
    chat_id = models.TextField()
    state = models.CharField(max_length=64, default="idle")
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "bot_user_states"
        constraints = [
            models.UniqueConstraint(fields=["platform", "chat_id"], name="bot_user_states_platform_chat_uniq"),
        ]
        indexes = [
            models.Index(fields=["platform", "telegram_user_id"], name="bot_state_platform_user_idx"),
            models.Index(fields=["platform", "chat_id", "state"], name="bot_state_chat_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.chat_id}:{self.state}"


class SupplierPriceUpload(UUIDTimestampedModel):
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.RESTRICT, related_name="price_uploads")
    uploaded_by_user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="supplier_price_uploads",
    )
    source_channel = models.CharField(max_length=32, choices=SourceChannel.choices)
    source_file_name = models.TextField(blank=True, null=True)
    source_file_uri = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=UploadStatus.choices, default=UploadStatus.RECEIVED)
    raw_payload = models.JSONField(default=dict, blank=True)
    ai_payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "supplier_price_uploads"
        indexes = [
            models.Index(fields=["supplier", "-uploaded_at"], name="spu_supplier_uploaded_idx"),
            models.Index(fields=["status"], name="spu_status_idx"),
        ]


class SupplierPriceUploadItem(UUIDTimestampedModel):
    upload = models.ForeignKey("suppliers.SupplierPriceUpload", on_delete=models.CASCADE, related_name="items")
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="upload_items",
    )
    row_number = models.IntegerField(blank=True, null=True)
    raw_name = models.TextField()
    raw_unit = models.TextField(blank=True, null=True)
    raw_price = models.TextField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    normalized_name = models.TextField(blank=True, null=True)
    parsed_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    parsed_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="parsed_upload_items",
    )
    status = models.CharField(max_length=16, choices=UploadItemStatus.choices, default=UploadItemStatus.NEW)
    review_comment = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "supplier_price_upload_items"
        indexes = [
            models.Index(fields=["upload", "status"], name="spui_upload_status_idx"),
        ]


class SupplierProduct(UUIDTimestampedModel):
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.RESTRICT, related_name="products")
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="supplier_products",
    )
    unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="supplier_products",
    )
    brand = models.ForeignKey(
        "catalog.Brand",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="supplier_products",
    )
    category = models.ForeignKey(
        "catalog.ProductCategory",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="supplier_products",
    )
    system_sku = models.CharField(max_length=20, unique=True, blank=True, null=True)
    supplier_sku = models.TextField(blank=True, null=True)
    supplier_name = models.TextField()
    normalized_name = models.TextField()
    image_url = models.URLField(blank=True, null=True)
    country_of_origin = models.TextField(blank=True, null=True)
    availability = models.CharField(
        max_length=16,
        choices=ProductAvailability.choices,
        default=ProductAvailability.IN_STOCK,
    )
    is_promo = models.BooleanField(default=False)
    is_discount = models.BooleanField(default=False)
    is_new = models.BooleanField(default=False)
    storage_conditions = models.TextField(blank=True, null=True)
    package = models.TextField(blank=True, null=True)
    # 2026-08-15: раньше отдельные weight_value/weight_unit и
    # volume_value/volume_unit — по прямой просьбе пользователя объединены в
    # одно поле "вес/объём нетто" (совпадает с макетом Павла — попап с 2
    # под-полями: число + единица, не 4 отдельных). На проде нашлось 21
    # активное/архивное позиция, где оба значения были осмысленно заполнены
    # одновременно (жидкие товары вроде молока/шампиньонов, где вес и объём не
    # совпадают из-за плотности) — при миграции данных для них выбран вес,
    # объём потерян (подтверждено пользователем явно). См.
    # docs/wiki/logs/2026-08-15-net-value-unit-merge.md.
    net_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    net_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="supplier_products_by_net",
    )
    min_order_qty = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    delivery_multiple = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    # 2026-09-28 (TASK-111, макет карточки товара в каталоге): «Состав упаковки» —
    # «8 шт. × 2 кг = 16 кг/кор.» хранится структурно: pack_items_count × pack_item_value
    # pack_item_unit, в таре pack_type (Unit вида package). Итог и строку собирает
    # api.v1.suppliers.package_composition. Не путать с `package` — тот свободный текст,
    # вырезаемый парсером из названия и входящий в ключ сопоставления импорта.
    pack_items_count = models.PositiveIntegerField(blank=True, null=True)
    pack_item_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    pack_item_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="supplier_products_by_pack_item",
    )
    pack_type = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="supplier_products_by_pack_type",
    )
    comment = models.TextField(blank=True, null=True)
    short_description = models.TextField(
        blank=True,
        null=True,
        help_text="Раздел 5 ТЗ «Карточка товара поставщика»: короткое описание для покупателя. "
        "Не заполняется автоматически дублированием названия/цены/характеристик.",
    )
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    last_upload = models.ForeignKey(
        "suppliers.SupplierPriceUpload",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="last_uploaded_products",
    )
    # 2026-08-10: denormalized snapshot of SupplierProductPrice, kept in sync by
    # _sync_product_price_snapshot() (services.py) on every price mutation. Exists
    # purely so ordering/filtering by price can use a plain indexed column instead
    # of a correlated Subquery — sorting by a per-row Subquery forces Postgres to
    # evaluate it for every SupplierProduct before it can sort, which doesn't scale
    # (see docs/wiki/current-status.md, 2026-08-10 lag report). Source of truth is
    # still SupplierProductPrice; these three fields are a read-optimization only.
    current_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    previous_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    price_changed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "supplier_products"
        constraints = [
            models.UniqueConstraint(fields=["supplier", "supplier_sku"], name="supplier_products_supplier_sku_uniq"),
            models.UniqueConstraint(
                fields=["supplier", "supplier_name"],
                name="supplier_products_supplier_raw_name_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["supplier", "status"], name="sp_supplier_status_idx"),
            models.Index(fields=["product"], name="supplier_products_product_idx"),
            models.Index(fields=["current_price"], name="sp_current_price_idx"),
            # Импорт товаров поставщика (api/v1/suppliers/product_import_matching.py) —
            # тот же паттерн, что и VenueProductMatrixItem.requested_name (catalog/models.py).
            GinIndex(fields=["supplier_name"], name="sp_supplier_name_trgm_gin", opclasses=["gin_trgm_ops"]),
            GinIndex(fields=["normalized_name"], name="sp_normalized_name_trgm_gin", opclasses=["gin_trgm_ops"]),
            GinIndex(OpClass(SearchNameKey("supplier_name"), name="gin_trgm_ops"), name="sp_supplier_search_key_gin"),
            GinIndex(
                OpClass(SearchNameKey("normalized_name"), name="gin_trgm_ops"), name="sp_normalized_search_key_gin"
            ),
            # Ветка точного совпадения lower(supplier_name) = lower(raw_name) в том же поиске дублей: без этого индекса
            # условие внутри OR не использует триграммный GIN и перебирает весь каталог поставщика на каждое
            # название (замер 2026-10-01: 2099 названий против каталога из 2099 -- 78 с, с индексом 7 с).
            models.Index(models.F("supplier"), Lower("supplier_name"), name="sp_supplier_lower_name_idx"),
        ]

    def __str__(self) -> str:
        return self.supplier_name


class SupplierProductImage(UUIDTimestampedModel):
    """2026-09-13: карточка товара расширена до галереи из нескольких фото
    (по прямой просьбе пользователя — "везде"). `SupplierProduct.image_url`
    остаётся для обратной совместимости — синхронизируется как URL фото на
    позиции 0 в галерее.

    2026-09-21 (§4 ТЗ «Карточка товара поставщика»): добавлено поле
    `position` — исходная реализация упорядочивала фото только по
    `created_at` (кто раньше загружен, тот и главный), без возможности
    сменить порядок; ТЗ прямо требует, чтобы пользователь мог назначить
    другое фото главным (reorder)."""

    supplier_product = models.ForeignKey("suppliers.SupplierProduct", on_delete=models.CASCADE, related_name="images")
    image_url = models.URLField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "supplier_product_images"
        ordering = ["position", "created_at"]
        indexes = [
            models.Index(fields=["supplier_product", "position"], name="spi_product_position_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_product_id}:{self.position}"


class SupplierProductPrice(UUIDTimestampedModel):
    # 2026-08-11: was RESTRICT -> CASCADE (Витя wanted DELETE to actually
    # delete). 2026-09-23: reverted back to RESTRICT (backlog#task-69, ТЗ
    # «Карточка товара поставщика» §32) — DELETE is archive again
    # (delete_supplier_product() now calls archive_supplier_product()), so
    # nothing should silently wipe price history through this FK anymore,
    # not even a raw ORM .delete() outside the service layer.
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.RESTRICT,
        related_name="prices",
    )
    upload_item = models.ForeignKey(
        "suppliers.SupplierPriceUploadItem",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="prices",
    )
    price_type = models.CharField(max_length=16, choices=PriceType.choices, default=PriceType.PRICE_LIST)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    source_document = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=PriceStatus.choices, default=PriceStatus.ACTIVE)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(blank=True, null=True)
    # 2026-08-10: Figma "История изменения цены" — причина/комментарий к конкретному
    # изменению цены (например "поставщик предоставил скидку при заказе от 50 кг"),
    # отдельно от source_document (то — название/номер исходного документа-прайса).
    comment = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "supplier_product_prices"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price__gte=0),
                name="supplier_product_prices_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=models.F("valid_from")),
                name="supplier_product_prices_period",
            ),
        ]
        indexes = [
            models.Index(
                fields=["supplier_product", "price_type", "-valid_from"],
                name="spp_current_idx",
                condition=models.Q(status=PriceStatus.ACTIVE, valid_to__isnull=True),
            ),
            models.Index(
                fields=["supplier_product", "price_type", "-valid_from", "-valid_to"],
                name="spp_history_idx",
            ),
        ]


class OwnershipRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class OwnershipRequestRole(models.TextChoices):
    OWNER = "owner", "Owner"
    EMPLOYEE = "employee", "Employee"
    SUPPORT = "support", "Support dispute"
    ADD_COMPANY = "add_company", "Add company request"


class SupplierOwnershipRequest(UUIDTimestampedModel):
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="ownership_requests")
    full_name = models.TextField()
    email = models.EmailField()
    phone = models.TextField(blank=True, null=True)
    inn = models.TextField()
    role_requested = models.CharField(
        max_length=16, choices=OwnershipRequestRole.choices, default=OwnershipRequestRole.OWNER
    )
    message = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=16, choices=OwnershipRequestStatus.choices, default=OwnershipRequestStatus.PENDING
    )
    admin_note = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "supplier_ownership_requests"
        indexes = [
            models.Index(fields=["supplier", "status"], name="sor_supplier_status_idx"),
            models.Index(fields=["email"], name="sor_email_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} → {self.supplier} ({self.status})"


class CabinetSession(UUIDTimestampedModel):
    token = models.CharField(max_length=64, unique=True)
    binding = models.ForeignKey("suppliers.BotUserBinding", on_delete=models.CASCADE, related_name="cabinet_sessions")
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "cabinet_sessions"
        indexes = [
            models.Index(fields=["token"], name="cabinet_sessions_token_idx"),
        ]


class SupplierBotLinkToken(UUIDTimestampedModel):
    """
    Одноразовый токен для кнопки «Присоединиться к боту» в веб-ЛК.

    Кнопка генерирует токен, открывает ссылку вида:
    - Telegram: https://t.me/<bot>?start=<token>
    - Max: https://max.ru/bot/<bot>?start=<token>

    Бот по /start обменивает токен на BotUserBinding (см. BotUserBindingRegisterView).

    ДОБАВЛЕНО (2026-08-03): поле channel — различает Telegram и Max ботов.
    Раньше всегда был только Telegram, теперь поставщик выбирает канал в модальном окне.
    """

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="bot_link_tokens")
    created_by = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, blank=True, null=True, related_name="created_bot_link_tokens"
    )
    token = models.CharField(max_length=64, unique=True)
    # ДОБАВЛЕНО: канал бота — telegram или max
    channel = models.CharField(
        max_length=32,
        choices=SourceChannel.choices,
        default=SourceChannel.TELEGRAM,
        help_text="Канал бота: telegram или max.",
    )
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "supplier_bot_link_tokens"
        indexes = [
            models.Index(fields=["token"], name="bot_link_tokens_token_idx"),
            # ДОБАВЛЕНО: индекс для поиска по каналу
            models.Index(fields=["supplier", "channel"], name="blt_sup_chan_idx"),
        ]


class SupplierProductImportSessionStatus(models.TextChoices):
    OPEN = "open", "Открыта"
    COMMITTED = "committed", "Завершена"


class SupplierProductImportProcessingStatus(models.TextChoices):
    """TASK-132: фоновая обработка (тот же словарь статусов, что у MasterProductImportUploadStatus, плюс
    pending): pending -> processing -> ready | failed. pending -- сессия создана и поставлена в очередь,
    воркер ещё не взял её в работу."""

    PENDING = "pending", "В очереди"
    PROCESSING = "processing", "Обрабатывается"
    READY = "ready", "Готово"
    FAILED = "failed", "Ошибка"


class SupplierProductImportCommitStatus(models.TextChoices):
    """Применение импорта (commit) в фоне: none -> pending -> running -> none | failed. none -- применение не
    идёт (не запускалось, либо завершилось успешно: итог в commit_result). Отдельно от processing_status, чтобы не
    ломать проверки «сессия готова к правкам»."""

    NONE = "none", "Не выполняется"
    PENDING = "pending", "В очереди на применение"
    RUNNING = "running", "Применяется"
    FAILED = "failed", "Ошибка применения"


class SupplierProductImportAction(models.TextChoices):
    REVIEW = "review", "Требует проверки"
    NEW = "new", "Добавить как новый"
    REPLACE = "replace", "Заменить товар"
    SKIP = "skip", "Не добавлять"


class SupplierProductImportSession(UUIDTimestampedModel):
    """ТЗ «Импорт товаров поставщика» — одна загрузка файла = одна сессия.
    Строки живут в SupplierProductImportItem. Ничего не пишет в
    SupplierProduct до commit_supplier_product_import_session (COMMIT, §20)."""

    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.CASCADE, related_name="product_import_sessions")
    recovery_attempts = models.PositiveSmallIntegerField(default=0)
    parser_version = models.CharField(max_length=64, blank=True, default="")
    source_file_name = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=SupplierProductImportSessionStatus.choices,
        default=SupplierProductImportSessionStatus.OPEN,
    )
    # TASK-132: у сессий, созданных до фоновой обработки, и у малых файлов (синхронный путь) — ready.
    processing_status = models.CharField(
        max_length=16,
        choices=SupplierProductImportProcessingStatus.choices,
        default=SupplierProductImportProcessingStatus.READY,
    )
    rows_total = models.PositiveIntegerField(default=0)
    rows_done = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    commit_status = models.CharField(
        max_length=16,
        choices=SupplierProductImportCommitStatus.choices,
        default=SupplierProductImportCommitStatus.NONE,
    )
    # Итог последнего применения (те же ключи, что возвращает commit_supplier_product_import_session) и текст
    # ошибки, если commit_status=failed.
    commit_result = models.JSONField(blank=True, null=True)
    commit_error = models.TextField(blank=True, null=True)
    # Предупреждения о качестве таблицы (file_imports.quality): [{"code", "message", ...}]. Только информируют,
    # импорт не блокируют.
    warnings = models.JSONField(default=list, blank=True)
    # TASK-133: sha256 загруженного файла -- повторная загрузка того же файла тем же поставщиком, пока сессия не
    # завершена, возвращает её же. У сессий, созданных до этого поля, NULL (идемпотентность на них не действует).
    content_sha256 = models.CharField(max_length=64, blank=True, null=True)
    # Исходный файл импорта (хранение 30 дней, file_imports.ImportFile); NULL у сессий до появления хранения и
    # при выключенном IMPORT_FILE_STORAGE_ENABLED.
    import_file = models.ForeignKey(
        "file_imports.ImportFile", on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )

    class Meta:
        db_table = "supplier_product_import_sessions"
        indexes = [
            models.Index(fields=["supplier", "status"], name="spis_supplier_status_idx"),
            models.Index(fields=["supplier", "content_sha256"], name="spis_supplier_sha256_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_id}:{self.id}"


class SupplierProductImportItem(UUIDTimestampedModel):
    """Одна строка файла. `name`/`country_of_origin`/`unit`/`net_value`/
    `net_unit` редактируемые (ТЗ §2) — правка сбрасывает соответствующий
    ключ field_sources на "user". `raw_name` — исходное название из файла,
    никогда не меняется (нужно для повторного rematch, ТЗ §14)."""

    session = models.ForeignKey(
        "suppliers.SupplierProductImportSession", on_delete=models.CASCADE, related_name="items"
    )
    row_number = models.PositiveIntegerField()
    raw_name = models.TextField()
    name = models.TextField()
    country_of_origin = models.TextField(blank=True, null=True)
    unit = models.ForeignKey("core.Unit", on_delete=models.RESTRICT, blank=True, null=True, related_name="+")
    price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    delivery_multiple = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    net_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    net_unit = models.ForeignKey("core.Unit", on_delete=models.RESTRICT, blank=True, null=True, related_name="+")
    min_order_qty = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    availability = models.CharField(max_length=16, choices=ProductAvailability.choices, blank=True, null=True)
    supplier_sku = models.TextField(blank=True, null=True)
    # «Состав упаковки» («8 шт. × 2 кг = 16 кг/кор.») — те же четыре поля, что у SupplierProduct;
    # заполняются целиком (count/value/unit вместе), тара необязательна. Не входит в обязательные для commit.
    pack_items_count = models.PositiveIntegerField(blank=True, null=True)
    pack_item_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    pack_item_unit = models.ForeignKey("core.Unit", on_delete=models.RESTRICT, blank=True, null=True, related_name="+")
    pack_type = models.ForeignKey("core.Unit", on_delete=models.RESTRICT, blank=True, null=True, related_name="+")
    action = models.CharField(
        max_length=16,
        choices=SupplierProductImportAction.choices,
        default=SupplierProductImportAction.REVIEW,
    )
    target_supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct", on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )
    target_expected_updated_at = models.DateTimeField(blank=True, null=True)
    validation_errors = models.JSONField(default=list, blank=True)
    imported_at = models.DateTimeField(blank=True, null=True)
    # {"unit": "llm", "net_value": "llm", "net_unit": "llm", "country_of_origin": "user", ...}
    # Только те поля, что не пришли обычным file-значением: отсутствие ключа = source "file".
    field_sources = models.JSONField(default=dict, blank=True)
    # Снимок кандидатов на момент последнего MATCH (ТЗ §6): [{"id", "name", "unit_id",
    # "unit_name", "score"}, ...], до 5, по убыванию score. Пересчитывается заново при
    # rematch (§14) — не инкрементальный кэш, полная замена.
    match_candidates = models.JSONField(default=list, blank=True)
    parsed_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "supplier_product_import_items"
        ordering = ["row_number"]
        indexes = [
            models.Index(fields=["session", "action"], name="spii_session_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.session_id}:{self.row_number}"
