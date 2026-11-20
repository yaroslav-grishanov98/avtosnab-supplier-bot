import uuid

from django.db import models


class EntityStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    ARCHIVED = "archived", "Archived"


class UserRole(models.TextChoices):
    ADMIN = "admin", "Admin"
    SUPPLIER = "supplier", "Supplier"
    VENUE_USER = "venue_user", "Venue user"
    SERVICE = "service", "Service"


class UnitKind(models.TextChoices):
    PIECE = "piece", "Piece"
    WEIGHT = "weight", "Weight"
    VOLUME = "volume", "Volume"
    PACKAGE = "package", "Package"


class ReceivingStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    IN_PROGRESS = "in_progress", "In progress"
    ACCEPTED = "accepted", "Accepted"
    ACCEPTED_WITH_DISCREPANCY = "accepted_with_discrepancy", "Accepted with discrepancy"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


class AuditLogEntityType(models.TextChoices):
    MASTER_PRODUCT = "master_product", "Мастер-товар"
    VENUE_PRODUCT_MATRIX_ITEM = "venue_product_matrix_item", "Товар компании"
    SUPPLIER_PRODUCT = "supplier_product", "Товар поставщика"
    SUPPLIER_PRODUCT_VENUE_PRICE = "supplier_product_venue_price", "Цена"
    VENUE = "venue", "Компания"
    # 2026-09-15 (raw ТЗ "ЛК Заведения" §29 "История"): "заявка" (§3.3), не
    # весь Order — "статусы разных заявок одного заказа независимы", и
    # история должна быть такой же. ORDER_ITEM — только для событий на
    # конкретной позиции (пока только отмена заведением, §21).
    SUPPLIER_ORDER_REQUEST = "supplier_order_request", "Заявка поставщику"
    ORDER_ITEM = "order_item", "Позиция заказа"


class AuditLogAction(models.TextChoices):
    DELETED = "deleted", "Удалено"
    ARCHIVED = "archived", "Архивировано"
    # 2026-09-29: publish-гейт для импорта товаров поставщика (unified-import-architecture
    # spec §9.8) — commit больше не публикует товар сам, отдельное действие publish пишет
    # этот action при переходе draft -> active.
    PUBLISHED = "published", "Опубликовано"
    # 2026-09-21 final-review fix wave (Fix 4) — generic "поле(я) сущности
    # изменены на месте" для сущностей, у которых нет отдельного статуса
    # для редактирования (в отличие от ARCHIVED/DELETED, статус сущности
    # после этого действия не меняется). Первое применение:
    # update_supplier_product_venue_price (api/v1/catalog/services.py).
    UPDATED = "updated", "Изменено"
    # 2026-09-15 (raw ТЗ §29) — заявка/позиция lifecycle events. Намеренно
    # НЕ включает "изменение цены/количества поставщиком" (§24 — реакция
    # заведения ещё не определена документом) и приёмку (§26 — тоже не
    # определена).
    SUBMITTED = "submitted", "Отправлено поставщику"
    VIEWED = "viewed", "Просмотрено поставщиком"
    CONFIRMED = "confirmed", "Подтверждено поставщиком"
    PARTIALLY_CONFIRMED = "partially_confirmed", "Принято частично"
    DECLINED = "declined", "Поставщик отказался"
    # Общий на "отменить одну позицию" (§21, entity_type=ORDER_ITEM) и
    # "отменить всю заявку" (§22, entity_type=SUPPLIER_ORDER_REQUEST) — само
    # действие одно и то же, разница только в масштабе (entity_type/entity_id).
    CANCELLED_BY_VENUE = "cancelled_by_venue", "Отменено заведением"
    COMPLETED = "completed", "Завершено"
    # 2026-09-23 (TASK-72): «Заменить на новый товар» при импорте в
    # справочник — name+unit существующей карточки перезаписаны, ID тот же.
    REPLACED = "replaced", "Заменено при импорте"


class AuditActorType(models.TextChoices):
    CORE_USER = "core_user", "Пользователь"
    SUPPLIER = "supplier", "Кабинет поставщика"
    VENUE = "venue", "Кабинет заведения"
    SERVICE = "service", "Сервисный токен"


class UUIDTimestampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class User(UUIDTimestampedModel):
    # 2026-08-06: раньше email был уникален глобально — один человек не мог
    # быть одновременно поставщиком и покупателем (venue_user), потому что
    # это буквально два разных аккаунта в этой модели. По явному решению
    # пользователя ("сервисы абсолютно разные, нужна возможность быть и тем
    # и другим") уникальность ослаблена до (email, role) — см. Meta.constraints
    # ниже. db_index заменяет автоматический unique-индекс, который снимается
    # вместе с unique=True.
    email = models.EmailField(db_index=True)
    phone = models.TextField(blank=True, null=True)
    full_name = models.TextField()
    # 2026-08-04: профиль поставщика — имя/фамилия отдельно от full_name
    # (full_name остаётся источником правды для всего существующего кода,
    # пересчитывается из first_name+last_name при сохранении профиля через
    # update_own_profile(), если хотя бы одно из полей передано в запросе).
    first_name = models.TextField(blank=True, null=True)
    last_name = models.TextField(blank=True, null=True)
    # Должность — свободный текст, значения вводит сам пользователь (не
    # фиксированный список — явное решение 2026-08-04, а не готовый classifier).
    position = models.TextField(blank=True, null=True)
    # IANA-имя (например "Europe/Moscow") — фронт определяет через
    # Intl.DateTimeFormat в браузере и предзаполняет, пользователь может
    # поправить вручную (решение 2026-08-04, в коде нет гео-IP/OAuth-источника
    # timezone). Валидируется на бэке через zoneinfo.available_timezones().
    timezone = models.CharField(max_length=64, blank=True, null=True)
    # Проставляется в _issue_jwt() — единая точка выдачи токена для всех
    # путей входа (email+пароль, VK/Yandex OAuth, подтверждение email,
    # сброс пароля), поэтому не нужно дублировать обновление в каждом из них.
    last_login_at = models.DateTimeField(blank=True, null=True)
    role = models.CharField(max_length=32, choices=UserRole.choices)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    # 2026-08-09: тот же случай, что у email выше — было unique=True глобально,
    # из-за чего один и тот же Yandex/VK-аккаунт мог "захватить" внешний id
    # только для одной роли: skolnik335@gmail.com с обеими ролями (supplier
    # и venue) через OAuth всегда попадал в тот аккаунт, что привязался
    # первым, — intent игнорировался на уровне поиска пользователя. Ослаблено
    # до (external_auth_id, role), см. Meta.constraints.
    external_auth_id = models.TextField(db_index=True, blank=True, null=True)
    # Hashed via django.contrib.auth.hashers; blank for users who only ever
    # authenticate through Telegram (BotUserBinding) or a future OAuth provider.
    password = models.TextField(blank=True, null=True)
    # Admin-assigned login for venue staff, who don't self-register and may
    # not have a real email on file. Suppliers log in with `email` instead,
    # so this stays blank for them.
    username = models.TextField(unique=True, blank=True, null=True)
    # Which restaurant/venue this account belongs to — only meaningful for
    # role=venue_user (admin accounts act across all venues, see
    # IsPlatformAdmin; supplier accounts are a wholly separate identity,
    # see suppliers.SupplierUser). One user = one venue by design decision
    # (2026-07-14) — not a join table like SupplierUser, so a person cannot
    # belong to multiple venues. Set via Django admin for now; no self-service
    # "create/join venue" flow exists yet.
    venue = models.ForeignKey("core.Venue", on_delete=models.SET_NULL, null=True, blank=True, related_name="users")

    class Meta:
        db_table = "users"
        indexes = [
            models.Index(fields=["status"], name="users_status_idx"),
        ]
        constraints = [
            # Один email — максимум один аккаунт на роль (не глобально один
            # аккаунт вообще), 2026-08-06. Позволяет тому же email быть
            # отдельным supplier-аккаунтом и отдельным venue_user-аккаунтом
            # одновременно — это два разных User-ряда с разными паролями.
            models.UniqueConstraint(fields=["email", "role"], name="users_email_role_uniq"),
            # 2026-08-09: тот же принцип для внешнего OAuth-id — см. комментарий
            # у поля external_auth_id выше.
            models.UniqueConstraint(fields=["external_auth_id", "role"], name="users_external_auth_id_role_uniq"),
        ]

    def __str__(self) -> str:
        return self.full_name


class EmailVerificationTokenStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    EXPIRED = "expired", "Expired"


class EmailVerificationToken(UUIDTimestampedModel):
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="email_verification_tokens")
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=EmailVerificationTokenStatus.choices,
        default=EmailVerificationTokenStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "email_verification_tokens"
        indexes = [
            models.Index(fields=["token"], name="evt_token_idx"),
        ]


class PasswordResetTokenStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    USED = "used", "Used"
    EXPIRED = "expired", "Expired"


class PasswordResetToken(UUIDTimestampedModel):
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="password_reset_tokens")
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=PasswordResetTokenStatus.choices,
        default=PasswordResetTokenStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "password_reset_tokens"
        indexes = [
            models.Index(fields=["token"], name="prt_token_idx"),
        ]


class Unit(UUIDTimestampedModel):
    code = models.TextField(unique=True)
    name = models.TextField()
    kind = models.CharField(max_length=16, choices=UnitKind.choices)
    ratio_to_base = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "units"
        indexes = [
            models.Index(fields=["kind", "status"], name="units_kind_status_idx"),
        ]

    def __str__(self) -> str:
        return self.code


class VenueAreaRole(models.TextChoices):
    KITCHEN = "kitchen", "Kitchen"
    BAR = "bar", "Bar"
    HALL = "hall", "Hall"
    MANAGER = "manager", "Manager"


class ExternalSystem(models.TextChoices):
    IIKO = "iiko", "iiko"
    AUTOSNAB = "autosnab", "Autosnab"
    GOOGLE_SHEETS = "google_sheets", "Google Sheets"
    MANUAL = "manual", "Manual"


class SuggestionSource(models.TextChoices):
    AI = "ai", "AI"
    MANUAL = "manual", "Manual"
    IMPORT = "import", "Import"
    SYSTEM = "system", "System"


class VenueEntityType(models.TextChoices):
    ORGANIZATION = "organization", "Организация"
    INDIVIDUAL_ENTREPRENEUR = "individual_entrepreneur", "ИП"
    INDIVIDUAL = "individual", "Физ. лицо"


class Venue(UUIDTimestampedModel):
    legal_name = models.TextField(blank=True, null=True)
    short_name = models.TextField(blank=True, null=True)
    display_name = models.TextField()
    iiko_org_id = models.TextField(blank=True, null=True)
    # 2026-08-15: по прямой просьбе пользователя — личный кабинет заведения
    # зеркалит кабинет поставщика (docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md):
    # реквизиты, лого/описание/контакты — те же общие поля профиля компании,
    # что у Supplier. Логистические поля (доставка/самовывоз/is_published)
    # сознательно НЕ мирроятся — заведение ничего не отгружает и не
    # публикует каталог, это специфика поставщика.
    entity_type = models.CharField(max_length=24, choices=VenueEntityType.choices, blank=True, null=True)
    inn = models.TextField(blank=True, null=True)
    kpp = models.TextField(blank=True, null=True)
    ogrn = models.TextField(blank=True, null=True)
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
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "venues"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(inn__isnull=True) | ~models.Q(inn__regex=r"^\s*$"),
                name="venues_inn_nonempty",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="venues_status_idx"),
            models.Index(fields=["iiko_org_id"], name="venues_iiko_org_id_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name


class VenueUserRole(models.TextChoices):
    """Mirrors suppliers.SupplierUserRole — owner/manager have identical
    permissions everywhere; OWNER only matters for the single-active-owner
    invariant (see _demote_other_active_venue_owners() in
    api/v1/venues/services.py)."""

    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"


class VenueAddressType(models.TextChoices):
    LEGAL = "legal", "Юридический"
    ACTUAL = "actual", "Фактический"
    HOME = "home", "Домашний"


class VenueContactType(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Телефон"
    MESSENGER = "messenger", "Мессенджер"
    LINK = "link", "Ссылка на сайт или социальные сети"


class VenueAddress(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierAddress."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="addresses")
    address_type = models.CharField(max_length=16, choices=VenueAddressType.choices)
    address = models.TextField(blank=True, null=True)
    country = models.TextField(blank=True, null=True)
    region = models.TextField(blank=True, null=True)
    district = models.TextField(blank=True, null=True)
    locality = models.TextField(blank=True, null=True)
    street = models.TextField(blank=True, null=True)
    postal_code = models.TextField(blank=True, null=True)
    apartment_office = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "venue_addresses"
        indexes = [
            models.Index(fields=["venue"], name="venue_addr_venue_idx"),
        ]


class VenueContact(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierContact."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="contacts")
    contact_type = models.CharField(max_length=16, choices=VenueContactType.choices)
    messenger_type = models.TextField(blank=True, null=True)
    value = models.TextField()

    class Meta:
        db_table = "venue_contacts"
        indexes = [
            models.Index(fields=["venue"], name="venue_contacts_venue_idx"),
        ]


class VenueBankAccount(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierBankAccount."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="bank_accounts")
    bank_name = models.TextField()
    account_number = models.TextField()
    bik = models.TextField()
    correspondent_account = models.TextField(blank=True, null=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "venue_bank_accounts"
        indexes = [
            models.Index(fields=["venue"], name="venue_bank_acc_venue_idx"),
        ]


class VenueImage(UUIDTimestampedModel):
    """2026-09-13: логотип заведения расширен до галереи из нескольких фото
    (по прямой просьбе пользователя — "везде, включая оба лого"). `Venue.
    logo_url` остаётся для обратной совместимости со всеми существующими
    местами, где он уже читается напрямую — синхронизируется на сервисном
    уровне (upload_venue_logo/delete_venue_image) как URL самого раннего
    (первого загруженного) фото в галерее, `null` если галерея пуста."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="images")
    image_url = models.URLField()

    class Meta:
        db_table = "venue_images"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["venue"], name="venue_images_venue_idx"),
        ]


class VenueUser(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierUser — deliberately separate from the
    existing single-nullable core.User.venue FK (which stays untouched,
    still powers the existing catalog/cart/order endpoints, see
    api/v1/permissions.py IsVenueMember). VenueUser is a many-to-many-style
    join table for the new cabinet feature set only, allowing one person to
    belong to multiple venues, mirroring SupplierUser."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="venue_users")
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="venue_memberships")
    role = models.CharField(max_length=16, choices=VenueUserRole.choices)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "venue_users"
        constraints = [
            models.UniqueConstraint(fields=["venue", "user"], name="venue_users_venue_user_uniq"),
        ]
        indexes = [
            models.Index(fields=["user"], name="venue_users_user_id_idx"),
            models.Index(fields=["venue", "status"], name="vu_venue_status_idx"),
        ]


class VenueAccountTransferStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class VenueAccountTransfer(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierAccountTransfer."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="account_transfers")
    initiated_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="initiated_venue_account_transfers",
    )
    target_email = models.EmailField()
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=VenueAccountTransferStatus.choices,
        default=VenueAccountTransferStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(blank=True, null=True)
    claimed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="claimed_venue_account_transfers",
    )

    class Meta:
        db_table = "venue_account_transfers"
        indexes = [
            models.Index(fields=["token"], name="vat_token_idx"),
            models.Index(fields=["venue", "status"], name="vat_venue_status_idx"),
        ]


class VenueUserInviteStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class VenueUserInvite(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierUserInvite."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="user_invites")
    invited_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="sent_venue_user_invites",
    )
    target_email = models.EmailField()
    token = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=16, choices=VenueUserRole.choices, default=VenueUserRole.MANAGER)
    status = models.CharField(
        max_length=16,
        choices=VenueUserInviteStatus.choices,
        default=VenueUserInviteStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(blank=True, null=True)
    claimed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="claimed_venue_user_invites",
    )

    class Meta:
        db_table = "venue_user_invites"
        indexes = [
            models.Index(fields=["token"], name="vui_token_idx"),
            models.Index(fields=["venue", "status"], name="vui_venue_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.venue} -> {self.target_email} ({self.status})"


class VenueInviteLink(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierInviteLink."""

    venue = models.OneToOneField("core.Venue", on_delete=models.CASCADE, related_name="invite_link")
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_venue_invite_links",
    )
    token = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=16, choices=VenueUserRole.choices, default=VenueUserRole.MANAGER)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    expires_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "venue_invite_links"
        indexes = [
            models.Index(fields=["token"], name="vil_token_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.venue} invite link ({self.status})"


class VenueJoinRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class VenueJoinRequest(UUIDTimestampedModel):
    """Mirrors suppliers.SupplierJoinRequest — a logged-in core.User asks to
    join a venue they already found (by ИНН)."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="join_requests")
    requested_by = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="venue_join_requests")
    message = models.TextField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    documents = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=VenueJoinRequestStatus.choices,
        default=VenueJoinRequestStatus.PENDING,
    )
    decided_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="decided_venue_join_requests",
    )
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "venue_join_requests"
        indexes = [
            models.Index(fields=["venue", "status"], name="vjr_venue_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.requested_by} -> {self.venue} ({self.status})"


class VenueSubdivision(UUIDTimestampedModel):
    """2026-08-26 sync call: "просто список из названий" — flat, per-venue list
    (кухня/бар/зал), no nested structure or attached product lists in v1.
    Assigned per OrderItem (see OrderItem.subdivision below), not per Order —
    the order-history mockup shows different subdivisions on different rows
    of the same order."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="subdivisions")
    name = models.TextField()
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "venue_subdivisions"
        constraints = [
            models.UniqueConstraint(fields=["venue", "name"], name="venue_subdivisions_venue_name_uniq"),
        ]
        indexes = [
            models.Index(fields=["venue", "status"], name="vs_venue_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.venue} / {self.name}"


class OffSystemSupplier(UUIDTimestampedModel):
    """2026-08-26 sync call: поставщики вне системы (~40% по 2026-08-21 call) —
    no account, no SupplierUser, order-history mockup badges them "Не в
    системе". Per-venue registry (each venue's own manually-called contacts),
    not a shared/global list — no signal on the call for cross-venue sharing.
    Orders against these suppliers carry no price/unit and have their status
    set manually by the venue user (see set_off_system_order_item_status in
    api/v1/catalog/services.py) — no accept/decline handshake is possible
    without an account."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="off_system_suppliers")
    display_name = models.TextField()
    phone = models.TextField(blank=True, null=True)
    comment = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)
    # 2026-09-25 (Виктор, задача #Лк1): макет корзины показывает "Мин. заказ"
    # на группе off-system поставщика тем же образом, что у обычного
    # Supplier.delivery_min_order_amount/delivery_currency — вручную
    # проставляется снабженцем при создании контакта (нет прайса/API, чтобы
    # получить это откуда-то ещё). null по умолчанию — как и раньше, группа
    # без указанного минимума.
    delivery_min_order_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    delivery_currency = models.CharField(max_length=3, default="RUB")

    class Meta:
        db_table = "off_system_suppliers"
        constraints = [
            models.UniqueConstraint(fields=["venue", "display_name"], name="off_system_suppliers_venue_name_uniq"),
        ]
        indexes = [
            models.Index(fields=["venue", "status"], name="oss_venue_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.venue}, off-system)"


class VenueCart(UUIDTimestampedModel):
    """2026-08-28: разделение корзины и заявки по прямому требованию Вити
    ("концептуально cart и order это разные бизнес сущности... это
    архитектурное нарушение") — раньше GET /venues/{id}/cart отдавал
    Order.status=draft, из-за чего в ответе были бессмысленные для корзины
    поля (order_number у ещё не отправленной заявки, confirmed_* всегда
    null).

    2026-09-08 (Витя: "по прежнему требует venue_id в ручке
    catalog/venues/{venue_id}/cart", закрывает "venue_id нужен только для
    отправки корзины"): singleton теперь на ПОЛЬЗОВАТЕЛЯ (`user`), не на
    заведение — один аккаунт с несколькими заведениями ведёт одну общую
    корзину и выбирает заведение только в момент submit.

    2026-09-09 (Витя, тот же тред: "можешь плиз это вынести вообще из
    корзины и принимать параметрами в /cart/submit" — про
    subdivision_id/delivery_address_id/contacts): раз venue появляется
    только в момент submit, у venue-owned полей на самой корзине больше нет
    смысла существовать как отдельное состояние — subdivision/
    delivery_address (были здесь) и контакты (были в VenueCartContact,
    удалена) стали параметрами POST /cart/submit (см.
    api.v1.catalog.services.submit_venue_cart).

    2026-09-18, пользователь: "ты еще корзину не привязывал к компании?
    нужно получать корзину по ид заведения" → уточнено и подтверждено:
    "Привязать по-настоящему (отдельная корзина на каждое заведение)" —
    разворот решения 2026-09-08. `venue` не удалялся при том переходе,
    просто стал nullable/SET_NULL и перестал читаться/писаться — теперь
    снова обязателен и вместе с `user` образует реальный ключ
    уникальности (один пользователь — по одной корзине на каждое
    управляемое им заведение, а не одна общая на все). Закрывает и
    сопутствующий гэп: товары разных заведений больше не могут молча
    смешаться в одной корзине (было зафиксировано как известный
    критичный гэп в сверке ТЗ «ЛК Заведения», 2026-09-14, №2)."""

    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="carts")
    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="carts")
    # Ввод пользователя до отправки — при submit копируются в созданный
    # Order как исторический факт заявки (Order.comment/requested_delivery_date
    # остаются read-only снимком на конкретный отправленный заказ).
    comment = models.TextField(blank=True, null=True)
    requested_delivery_date = models.DateField(blank=True, null=True)

    class Meta:
        db_table = "venue_carts"
        constraints = [
            models.UniqueConstraint(fields=["venue", "user"], name="venue_carts_venue_user_uniq"),
        ]

    def __str__(self) -> str:
        return f"Cart({self.user}, {self.venue})"


class VenueCartItem(UUIDTimestampedModel):
    cart = models.ForeignKey("core.VenueCart", on_delete=models.CASCADE, related_name="items")
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="cart_items",
    )
    supplier_product_deleted = models.BooleanField(
        default=False,
    )
    off_system_supplier = models.ForeignKey(
        "core.OffSystemSupplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="cart_items",
    )
    subdivision = models.ForeignKey(
        "core.VenueSubdivision",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="cart_items",
    )
    requested_name = models.TextField()
    requested_qty = models.DecimalField(max_digits=14, decimal_places=4)
    requested_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="cart_items",
    )
    comment = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "venue_cart_items"
        # 2026-09-08 (Витя, баг "список прыгает при быстром вводе количества"):
        # порядка не было вообще — Postgres отдавал строки в heap-порядке, а он
        # меняется при КАЖДОМ UPDATE (MVCC: обновлённая строка физически
        # дописывается в конец heap). Из-за этого правка qty у одной позиции
        # переставляла местами совершенно другие, нетронутые позиции. Это не
        # было осознанным "последняя изменённая — наверх", это был
        # неопределённый порядок. Теперь — стабильный порядок добавления;
        # id вторым ключом, т.к. bulk_create может проставить одинаковый
        # created_at нескольким строкам сразу.
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(requested_qty__gt=0),
                name="venue_cart_items_requested_qty_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["cart"], name="vci_cart_idx"),
        ]


class OrderStatus(models.TextChoices):
    AWAITING_DISPATCH = "awaiting_dispatch", "Awaiting dispatch"
    # 2026-08-28: "draft" убран — Order теперь создаётся только в момент
    # submit (см. VenueCart выше), никогда не существует пустым/черновым.
    SUBMITTED = "submitted", "Submitted"
    CONFIRMED = "confirmed", "Confirmed"
    # 2026-09-15: PARTIALLY_RECEIVED/RECEIVED убраны — их никогда не
    # выставлял ни один сервис/вьюха (только submitted/confirmed/cancelled
    # через _advance_order_status_if_fully_resolved, completed вручную
    # через VenueOrderCompleteView), физическая приёмка идёт через
    # отдельную модель Receiving/ReceivingStatus, не завязанную на
    # Order.status. Пользователь заметил расхождение между Swagger-enum'ом
    # и реальным поведением — раз значения недостижимы, лучше убрать, чем
    # документировать несуществующие состояния (choices не создаёт DB
    # constraint, миграция нужна только для метаданных Django).
    #
    # 2026-08-26 sync call: 5th status seen on the order-history mockup —
    # manually set by the order's creator once the whole delivery is done
    # ("статус завершена — пока вручную ставится создателем заявки", per
    # the 2026-08-21 call recap). Order-level only, deliberately not mirrored
    # on OrderItemStatus — the existing Receiving model already covers a more
    # granular receiving flow and isn't touched here.
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class OrderItemStatus(models.TextChoices):
    # 2026-08-28: "draft" убран вместе с OrderStatus.DRAFT — см. VenueCartItem
    # для позиций до отправки.
    SUBMITTED = "submitted", "Submitted"
    CONFIRMED = "confirmed", "Confirmed"
    REPLACED = "replaced", "Replaced"
    # 2026-09-15: PARTIALLY_RECEIVED/RECEIVED убраны — см. OrderStatus выше,
    # тот же анти-паттерн (никогда не выставлялись реальным кодом).
    CANCELLED = "cancelled", "Cancelled"
    # 2026-09-15 (raw ТЗ "ЛК Заведения" §20-23): a DISTINCT value from
    # CANCELLED on purpose — CANCELLED means the supplier declined
    # (decline_supplier_order_items / accept with confirmed_qty=0), this one
    # means the venue cancelled before the supplier ever responded. §23:
    # "не смешивать" — keeping them structurally separate (not just a
    # comment/actor field on the same value) makes it impossible for
    # change_order_item_supplier ("Найти замену") or the "Неразобранные"
    # list (venue_unresolved_order_items_queryset) to accidentally treat
    # one as the other: a venue-cancelled item is never eligible for
    # reroute (§21 — the venue no longer wants it) and never surfaces in
    # Неразобранные (§23 — "не попадают в Неразобранные").
    CANCELLED_BY_VENUE = "cancelled_by_venue", "Cancelled by venue"


class OrderItemAppliedPriceType(models.TextChoices):
    BASE = "base", "Базовая"
    INDIVIDUAL = "individual", "Индивидуальная"


class Order(UUIDTimestampedModel):
    approval_status = models.CharField(max_length=16, default="not_required")
    revision = models.PositiveIntegerField(default=1)
    business_updated_at = models.DateTimeField(null=True)
    venue = models.ForeignKey("core.Venue", on_delete=models.RESTRICT, related_name="orders")
    created_by_user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_orders",
    )
    order_number = models.TextField(unique=True)
    status = models.CharField(max_length=32, choices=OrderStatus.choices, default=OrderStatus.SUBMITTED)
    requested_delivery_date = models.DateField(blank=True, null=True)
    submitted_at = models.DateTimeField(blank=True, null=True)
    comment = models.TextField(blank=True, null=True)
    # 2026-08-30: снимок-на-момент-отправки, тот же паттерн, что comment/
    # requested_delivery_date. 2026-09-09: приходят прямо параметрами
    # POST /cart/submit (не были состоянием самой VenueCart и до этого).
    subdivision = models.ForeignKey(
        "core.VenueSubdivision", on_delete=models.SET_NULL, blank=True, null=True, related_name="orders"
    )
    delivery_address = models.ForeignKey(
        "core.VenueAddress", on_delete=models.SET_NULL, blank=True, null=True, related_name="orders"
    )

    class Meta:
        db_table = "orders"
        indexes = [
            models.Index(fields=["venue", "-created_at"], name="orders_venue_created_idx"),
            models.Index(fields=["status"], name="orders_status_idx"),
        ]

    def __str__(self) -> str:
        return self.order_number


class OrderItem(UUIDTimestampedModel):
    applied_currency = models.CharField(max_length=3, null=True, blank=True)
    order = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="items")
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_items",
    )
    supplier_product_deleted = models.BooleanField(
        default=False,
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_items",
    )
    requested_name = models.TextField()
    requested_qty = models.DecimalField(max_digits=14, decimal_places=4)
    requested_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="requested_order_items",
    )
    confirmed_qty = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    confirmed_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    # Дата поставки, которую указывает поставщик при принятии заказа (2026-08-21 sync call) —
    # отдельно от Order.requested_delivery_date (пожелание заведения на этапе оформления).
    confirmed_delivery_date = models.DateField(blank=True, null=True)
    # 2026-09-21, по просьбе пользователя: "нету даты когда поставщик обработал
    # заявку (изменил статус)" — null, пока позиция submitted. Проставляется
    # ровно в accept_supplier_order_items/decline_supplier_order_items (сам
    # ответ поставщика), намеренно НЕ переиспользует updated_at — тот
    # перезаписывается ещё и change_order_item_supplier (реroute помечает
    # исходную позицию REPLACED) и cancel_order_item (отмена ЗАВЕДЕНИЕМ, не
    # поставщиком), из-за чего потерял бы исходный момент решения поставщика.
    supplier_responded_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=32, choices=OrderItemStatus.choices, default=OrderItemStatus.SUBMITTED)
    comment = models.TextField(blank=True, null=True)
    # 2026-09-15 (raw ТЗ §20: "Причина отмены: необязательна") — set only by
    # cancel_order_item/cancel_supplier_order_request (status becomes
    # CANCELLED_BY_VENUE), never by the supplier decline path.
    cancellation_reason = models.TextField(blank=True, null=True)
    # 2026-08-26 sync call — both added for the order-history mockup, which
    # shows a "Подразделение" column varying per row (not per whole Order)
    # and an "Не в системе" badge per supplier group.
    subdivision = models.ForeignKey(
        "core.VenueSubdivision",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_items",
    )
    off_system_supplier = models.ForeignKey(
        "core.OffSystemSupplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_items",
    )
    # 2026-09-25 (Виктор, задача #Лк1): "Отказать в поставке?" на макете несёт
    # необязательный комментарий — тот же UX, что у supplier_comment
    # реального поставщика (SupplierOrderRequest.supplier_comment,
    # 2026-09-22), но своё поле: off-system позиции не имеют
    # SupplierOrderRequest вообще (статус меняется поштучно через
    # set_off_system_order_item_status, не пачкой через accept/decline).
    # OrderItemSerializer.get_supplier_comment читает это поле для
    # off-system позиций тем же выходным полем supplier_comment, что и у
    # реального поставщика — фронту не нужно различать источник.
    off_system_status_comment = models.TextField(blank=True, null=True)
    # 2026-09-03: "Сменить поставщика" (Виктор/Павел board screenshot) — set
    # only on the new item created by change_order_item_supplier(), pointing
    # back at the declined/partially-confirmed item whose unfulfilled
    # remainder it re-routes to a different supplier. The old item's own
    # status flips to REPLACED; this FK is what lets the frontend show "was
    # reassigned from X" instead of the new item looking unrelated.
    replaces = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="replaced_by",
    )
    # 2026-09-28 (ТЗ «Неразобранные», кнопка «Убрать из списка»): закупщик
    # убирает позицию из «Неразобранных» навсегда, не решая её, — статус и
    # количества не меняются, только позиция больше не попадает в
    # venue_unresolved_order_items_queryset и счётчик. Не REPLACED и не
    # CANCELLED_BY_VENUE: заказ и ответ поставщика остаются как были.
    unresolved_dismissed_at = models.DateTimeField(blank=True, null=True)
    unresolved_dismissed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="dismissed_unresolved_order_items",
    )
    # 2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §39-45): price
    # snapshot taken at submit_venue_cart() time via resolve_supplier_product_price
    # — re-resolved right before submit (§38, a cart item can sit for hours/
    # days), never re-resolved afterwards. applied_individual_price stays
    # set even if that SupplierProductVenuePrice is later archived/replaced
    # (on_delete=SET_NULL only covers a hard delete, which the price service
    # never does) — it's how "Подтверждено по этой цене"
    # (supplier_product_venue_price_confirmed_qty) attributes confirmed_qty
    # back to the exact price version that was in effect at submit time,
    # per §42-45's example (не восстанавливать по датам/сумме, только по
    # этой ссылке). off_system_supplier/product-only items never resolve a
    # price, so both fields stay null for them.
    applied_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    applied_price_type = models.CharField(
        max_length=16, choices=OrderItemAppliedPriceType.choices, blank=True, null=True
    )
    applied_individual_price = models.ForeignKey(
        "catalog.SupplierProductVenuePrice",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_items",
    )
    # 2026-09-15 (raw ТЗ "ЛК Заведения" §3.4/§3.3): связь с собственной
    # Заявкой поставщику (SupplierOrderRequest, ниже) — null только для
    # позиций без обычного поставщика (off_system_supplier/свободный текст),
    # которые в эту сущность сознательно не включены (§30 "Поставщик не в
    # системе" — логика ещё не определена документом, существующий
    # OffSystemSupplier-флоу не трогается).
    supplier_request = models.ForeignKey(
        "core.SupplierOrderRequest",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="items",
    )

    class Meta:
        db_table = "order_items"
        # 2026-09-08: та же проблема, что у VenueCartItem (см. там) — приём/
        # отказ/смена поставщика делают UPDATE позиции, из-за чего история
        # заказа перетасовывалась после каждого действия поставщика.
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(requested_qty__gt=0),
                name="order_items_requested_qty_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["order"], name="order_items_order_idx"),
            models.Index(fields=["supplier_product"], name="oi_supplier_product_idx"),
            models.Index(fields=["off_system_supplier"], name="oi_off_system_supplier_idx"),
            models.Index(fields=["applied_individual_price"], name="oi_applied_ind_price_idx"),
            models.Index(fields=["supplier_request"], name="oi_supplier_request_idx"),
        ]


class SupplierOrderRequestStatus(models.TextChoices):
    """Raw ТЗ "ЛК Заведения - заказы, корзина и заявки поставщикам" §11 —
    ровно эти 5 значений, не больше (§12: старые технические стадии
    Авто/Вручную/Контроль не переносить сюда)."""

    SUBMITTED = "submitted", "Отправлено поставщику"
    CONFIRMED = "confirmed", "Подтверждена поставщиком"
    PARTIALLY_CONFIRMED = "partially_confirmed", "Принята частично"
    DECLINED = "declined", "Поставщик отказался"
    COMPLETED = "completed", "Завершена"


class SupplierOrderRequest(UUIDTimestampedModel):
    """Raw ТЗ §3.3 "Заявка поставщику" — дочерняя сущность Заказа заведения
    (Order), одна на пару (order, supplier). До 2026-09-15 такой сущности
    не было вообще: "заявка" была чисто вычисляемым фильтром `OrderItem` по
    `supplier_product__supplier_id` поверх общего `Order` — из-за этого не
    было куда положить независимый статус/номер/признак "Просмотрено"/
    историю каждого поставщика (см. docs/wiki/logs/2026-09-14-venue-
    cabinet-cart-orders-tz-gap-analysis.md, расхождение №1). `status` —
    пересчитывается сервисным слоем (accept/decline_supplier_order_items)
    из состояния своих `items`, никогда не выставляется руками напрямую.

    `viewed_at` — §13 "Просмотрено": null до первого открытия детали этой
    заявки поставщиком, дальше не меняется повторными открытиями.

    `supplier_comment` — 2026-09-22, по просьбе пользователя ("в запрос
    accept необходимо добавить поле комментарий"): один общий комментарий
    поставщика на весь вызов accept, не на отдельную позицию (по аналогии
    с confirmed_delivery_date). Не путать с обычным `comment` в ответе
    (Order.comment — комментарий ЗАВЕДЕНИЯ, задаётся при отправке заявки) —
    разные поля разных акторов, отсюда и другое имя."""

    order = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="supplier_requests")
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.RESTRICT, related_name="order_requests")
    number = models.TextField(unique=True)
    status = models.CharField(
        max_length=32, choices=SupplierOrderRequestStatus.choices, default=SupplierOrderRequestStatus.SUBMITTED
    )
    viewed_at = models.DateTimeField(blank=True, null=True)
    supplier_comment = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "supplier_order_requests"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["order", "supplier"], name="supplier_order_requests_order_supplier_uniq"),
        ]
        indexes = [
            models.Index(fields=["supplier", "-created_at"], name="sor_supplier_created_idx"),
            models.Index(fields=["order"], name="sor_order_idx"),
        ]

    def __str__(self) -> str:
        return self.number


class OrderContact(UUIDTimestampedModel):
    """2026-09-09: контакты для связи по заявке — раньше были отдельной
    сущностью на корзине (VenueCartContact, могли накапливаться до
    submit), теперь приходят прямо в теле POST /cart/submit (Витя: "вынести
    вообще из корзины и принимать параметрами в submit") и сохраняются
    только здесь, на самом заказе — снимок-на-момент-отправки, тот же
    паттерн, что у Order.comment/requested_delivery_date."""

    order = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="contacts")
    full_name = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "order_contacts"
        # См. VenueCartItem.Meta.ordering — та же причина.
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["order"], name="order_contacts_order_idx"),
        ]


class OrderTemplate(UUIDTimestampedModel):
    """2026-08-26 sync call screenshots: "Список шаблонов" / "Сохранить шаблон"
    on the cart screen — never discussed verbally on any call, reconstructed
    purely from the mockup. Saved as a snapshot of the venue's current cart
    (VenueCart, button sits next to a populated cart in the screenshot),
    applied later to add items into a (possibly new) cart."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="order_templates")
    name = models.TextField()
    created_by_user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_order_templates",
    )
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "order_templates"
        constraints = [
            models.UniqueConstraint(fields=["venue", "name"], name="order_templates_venue_name_uniq"),
        ]
        indexes = [
            models.Index(fields=["venue", "status"], name="ordt_venue_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.venue})"


class OrderTemplateItem(UUIDTimestampedModel):
    template = models.ForeignKey("core.OrderTemplate", on_delete=models.CASCADE, related_name="items")
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_template_items",
    )
    supplier_product_deleted = models.BooleanField(
        default=False,
    )
    off_system_supplier = models.ForeignKey(
        "core.OffSystemSupplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_template_items",
    )
    subdivision = models.ForeignKey(
        "core.VenueSubdivision",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="order_template_items",
    )
    requested_name = models.TextField()
    requested_qty = models.DecimalField(max_digits=14, decimal_places=4)
    requested_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="order_template_items",
    )
    comment = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "order_template_items"
        # См. VenueCartItem.Meta.ordering — та же причина. Здесь ещё и важно,
        # чтобы apply-шаблона клал позиции в корзину в том же порядке, в
        # котором они были сохранены.
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(requested_qty__gt=0),
                name="order_template_items_requested_qty_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["template"], name="oti_template_idx"),
        ]


class Receiving(UUIDTimestampedModel):
    venue = models.ForeignKey("core.Venue", on_delete=models.RESTRICT, related_name="receivings")
    order = models.ForeignKey(
        "core.Order",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="receivings",
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="receivings",
    )
    status = models.CharField(max_length=32, choices=ReceivingStatus.choices, default=ReceivingStatus.DRAFT)
    received_at = models.DateTimeField(blank=True, null=True)
    accepted_by_user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="accepted_receivings",
    )
    comment = models.TextField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "receivings"
        indexes = [
            models.Index(fields=["venue", "-created_at"], name="receivings_venue_created_idx"),
            models.Index(fields=["order"], name="receivings_order_idx"),
        ]


class ReceivingDocument(UUIDTimestampedModel):
    receiving = models.ForeignKey("core.Receiving", on_delete=models.CASCADE, related_name="documents")
    order_item = models.ForeignKey(
        "core.OrderItem",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="receiving_documents",
    )
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="receiving_documents",
    )
    supplier_product_deleted = models.BooleanField(
        default=False,
    )
    product_mapping = models.ForeignKey(
        "catalog.ProductMapping",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="receiving_documents",
    )
    document_number = models.TextField(blank=True, null=True)
    document_date = models.DateField(blank=True, null=True)
    received_name = models.TextField()
    received_qty = models.DecimalField(max_digits=14, decimal_places=4)
    received_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="receiving_documents",
    )
    received_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    discrepancy_reason = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=32, choices=ReceivingStatus.choices, default=ReceivingStatus.DRAFT)
    external_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "receiving_documents"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(received_qty__gte=0),
                name="receiving_documents_qty_nonnegative",
            ),
        ]
        indexes = [
            models.Index(fields=["receiving"], name="rec_docs_receiving_idx"),
            models.Index(fields=["order_item"], name="rec_docs_order_item_idx"),
        ]


class AuditLog(UUIDTimestampedModel):
    """2026-09-04 (TASK-35, "Дополнение к ТЗ: безопасность данных и
    удаление товаров"): who/when/what was deleted or archived, for every
    entity the doc names. entity_type/entity_id is a plain choices-string
    + UUID pair, not a GenericForeignKey — the entity set is fixed at
    exactly these five rows, and contenttypes is installed but unused
    anywhere else in this codebase. scope_id is a durable read-authorization
    key, populated only for entity types that can be hard deleted
    (MASTER_PRODUCT -> owner_id, SUPPLIER_PRODUCT -> supplier_id) — the
    other three entity types are only ever archived, so their read
    endpoints authorize off the still-live row instead. updated_at (from
    UUIDTimestampedModel) is unused; rows are write-once."""

    entity_type = models.CharField(max_length=32, choices=AuditLogEntityType.choices)
    entity_id = models.UUIDField()
    action = models.CharField(max_length=32, choices=AuditLogAction.choices)
    summary = models.TextField()
    scope_id = models.UUIDField(blank=True, null=True)
    actor_type = models.CharField(max_length=16, choices=AuditActorType.choices)
    actor_id = models.UUIDField(blank=True, null=True)
    actor_label = models.TextField(blank=True)

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(fields=["entity_type", "entity_id"], name="audit_log_entity_idx"),
            models.Index(fields=["scope_id"], name="audit_log_scope_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} {self.entity_type} {self.entity_id}"


from core.approval_models import (  # noqa: E402,F401
    CompanyApprovalApprover,
    CompanyApprovalSettings,
    OrderApprovalCommand,
    OrderApprovalCycle,
    OrderApprovalStep,
    OrderBusinessEvent,
    OrderRevision,
)
