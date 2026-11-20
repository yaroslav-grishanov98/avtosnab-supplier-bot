import hashlib
import json
from typing import NamedTuple
from urllib.parse import urlsplit

from django.core.validators import MinValueValidator
from django.db import models
from django.views.decorators.debug import sensitive_variables

from accounting.secrets import SealedSecret, SecretError, get_secret_box
from core.models import UUIDTimestampedModel


class Provider(models.TextChoices):
    IIKO = "iiko", "iiko"
    STOREHOUSE = "storehouse", "Storehouse"


class ConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    NEEDS_ATTENTION = "needs_attention", "Needs attention"
    DISABLED = "disabled", "Disabled"


class LinkStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"


class ConnectionEventAction(models.TextChoices):
    CREATED = "created", "Created"
    CREDENTIALS_REPLACED = "credentials_replaced", "Credentials replaced"
    DISCONNECTED = "disconnected", "Disconnected"
    DEPARTMENT_ASSIGNED = "department_assigned", "Department assigned"
    STATUS_CHANGED = "status_changed", "Status changed"


# A link in one of these statuses occupies the (venue, provider) slot.
LIVE_LINK_STATUSES = (LinkStatus.PENDING, LinkStatus.ACTIVE)


class CredentialsNotSet(SecretError):
    """The connection holds no credentials (never set or already cleared).

    Subclasses SecretError so callers that only care "credentials unusable"
    can catch one type; the message carries no secret data.
    """


class IikoCredentials(NamedTuple):
    login: str
    pass_hash: str

    def __repr__(self) -> str:
        # Tuples print their items; credentials must never reach logs or tracebacks.
        return "IikoCredentials(<hidden>)"

    __str__ = __repr__


class AccountingConnectionQuerySet(models.QuerySet):
    def for_venue(self, venue_id):
        """Connections that have a pending or active link to the venue."""
        # One filter() call keeps both conditions on the same joined link row.
        return self.filter(venue_links__venue_id=venue_id, venue_links__status__in=LIVE_LINK_STATUSES)


class AccountingConnection(UUIDTimestampedModel):
    provider = models.CharField(max_length=16, choices=Provider.choices, help_text="Accounting system type.")
    base_url = models.URLField(
        max_length=500, help_text="Server address including the path, e.g. https://x.iiko.it/resto."
    )
    # Denormalised from base_url in save(): the per-host throttle and advisory
    # lock key on it, and indexed lookups on a plain column are cheaper than parsing.
    host = models.CharField(max_length=255, editable=False, help_text="Lower-case host name derived from base_url.")
    credentials_ciphertext = models.BinaryField(null=True, blank=True, editable=False)
    credentials_key_version = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    status = models.CharField(max_length=24, choices=ConnectionStatus.choices, default=ConnectionStatus.ACTIVE)
    error_code = models.CharField(
        max_length=64, blank=True, default="", help_text="Last blocking error code, empty if none."
    )
    stale_reason = models.CharField(
        max_length=64, blank=True, default="", help_text="Why synced data is considered stale."
    )
    server_version = models.CharField(
        max_length=64, blank=True, default="", help_text="Server version reported by the system."
    )
    capabilities = models.JSONField(default=dict, blank=True, help_text="Features detected on the server.")
    # Literal rather than settings.ACCOUNTING_STOCK_INTERVAL_MINUTES so the
    # migration does not depend on the environment.
    sync_interval_minutes = models.PositiveIntegerField(default=60, validators=[MinValueValidator(1)])
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_sync_at = models.DateTimeField(null=True, blank=True)
    catalog_synced_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accounting_connections_created",
    )

    objects = AccountingConnectionQuerySet.as_manager()

    class Meta:
        db_table = "accounting_connections"
        indexes = [
            models.Index(fields=["next_sync_at"], name="acc_conn_next_sync_idx"),
        ]
        constraints = [
            # A ciphertext without its key version (or vice versa) cannot be opened.
            models.CheckConstraint(
                condition=models.Q(credentials_ciphertext__isnull=True, credentials_key_version__isnull=True)
                | models.Q(credentials_ciphertext__isnull=False, credentials_key_version__isnull=False),
                name="accounting_connections_credentials_pair",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.host}"

    def save(self, *args, **kwargs):
        self.host = self._host_from_url(self.base_url)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "base_url" in update_fields:
            kwargs["update_fields"] = {*update_fields, "host"}
        super().save(*args, **kwargs)

    @staticmethod
    def _host_from_url(base_url: str) -> str:
        """Lower-case host of base_url; ValueError (without echoing the URL) if it is unusable."""
        value = base_url.strip()
        if "//" not in value:
            value = f"//{value}"
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            username, password = parts.username, parts.password
            _ = parts.port  # accessing the port validates it
        except ValueError:
            raise ValueError("Некорректный адрес сервера.") from None
        # The URL ends up in logs and API responses; credentials belong in set_credentials().
        if username is not None or password is not None:
            raise ValueError("Адрес сервера не должен содержать логин и пароль.")
        host = (hostname or "").lower().rstrip(".")
        if not host:
            raise ValueError("В адресе сервера не указано имя хоста.")
        return host

    @sensitive_variables()
    def set_credentials(self, login: str, password: str) -> None:
        """Seal the login and the SHA-1 hex of the password into the model fields.

        Like ``User.set_password`` this only changes the instance; the caller saves.
        The row id is the AAD, so the sealed value only opens on this row.
        """
        if not login or not password:
            raise ValueError("Логин и пароль не могут быть пустыми.")
        try:
            password_bytes = password.encode("utf-8")
        except UnicodeEncodeError:
            # Not chained: the original error text names the offending character.
            raise ValueError("Пароль содержит символы, которые нельзя закодировать.") from None
        # SHA-1 is not our choice: the iiko API authenticates with the SHA-1 hex digest of the password.
        pass_hash = hashlib.sha1(password_bytes, usedforsecurity=False).hexdigest()
        sealed = get_secret_box().seal(json.dumps({"login": login, "pass_hash": pass_hash}), aad=self.id)
        self.credentials_ciphertext = sealed.ciphertext
        self.credentials_key_version = sealed.key_version

    @sensitive_variables()
    def credentials(self) -> IikoCredentials:
        """Open the sealed credentials.

        Raises CredentialsNotSet when there are none and SecretError when they cannot be opened.
        """
        if self.credentials_ciphertext is None and self.credentials_key_version is None:
            raise CredentialsNotSet("Учётные данные подключения не заданы.")
        if self.credentials_ciphertext is None or self.credentials_key_version is None:
            raise SecretError("Не удалось расшифровать секрет.")
        sealed = SealedSecret(ciphertext=bytes(self.credentials_ciphertext), key_version=self.credentials_key_version)
        plaintext = get_secret_box().open(sealed, aad=self.id)
        try:
            payload = json.loads(plaintext)
            return IikoCredentials(login=payload["login"], pass_hash=payload["pass_hash"])
        except (ValueError, KeyError, TypeError):
            raise SecretError("Не удалось расшифровать секрет.") from None

    def clear_credentials(self) -> None:
        """Drop the sealed credentials from the instance; the caller saves."""
        self.credentials_ciphertext = None
        self.credentials_key_version = None


class AccountingVenueLink(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="venue_links")
    venue = models.ForeignKey("core.Venue", on_delete=models.PROTECT, related_name="accounting_links")
    provider = models.CharField(max_length=16, choices=Provider.choices, help_text="Same as the connection's provider.")
    status = models.CharField(max_length=16, choices=LinkStatus.choices, default=LinkStatus.PENDING)

    class Meta:
        db_table = "accounting_venue_links"
        constraints = [
            # Target for composite foreign keys that must stay within one (connection, venue) pair.
            models.UniqueConstraint(
                fields=["connection", "venue"], name="accounting_venue_links_connection_venue_uniq"
            ),
            models.UniqueConstraint(
                fields=["venue", "provider"],
                condition=models.Q(status__in=LIVE_LINK_STATUSES),
                name="accounting_venue_links_live_venue_provider_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.venue_id} -> {self.connection_id} ({self.status})"

    def save(self, *args, **kwargs):
        # The provider is always the connection's own, so the partial unique
        # (venue, provider) really guards one live connection per venue and
        # provider and a link can never claim another provider than its connection.
        self.provider = self.connection.provider
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "provider"}
        super().save(*args, **kwargs)


class AccountingDepartment(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="departments")
    external_id = models.CharField(max_length=64, help_text="Department id in the accounting system.")
    name = models.CharField(max_length=255)
    # Free text: the set of department types belongs to the external system.
    department_type = models.CharField(max_length=32, blank=True, default="")
    venue = models.ForeignKey(
        "core.Venue",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accounting_departments",
        help_text="Venue that owns this department; one venue may own several.",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounting_departments"
        constraints = [
            models.UniqueConstraint(fields=["connection", "external_id"], name="accounting_departments_conn_ext_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class AccountingConnectionEvent(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="events")
    # Django admins are auth.User, not core.User, so actor_user stays empty for them.
    actor_label = models.CharField(max_length=255, help_text="Django admin name or the user's email.")
    actor_user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accounting_connection_events",
    )
    action = models.CharField(max_length=32, choices=ConnectionEventAction.choices)
    details = models.JSONField(default=dict, blank=True, help_text="Event context. Never contains secrets.")

    class Meta:
        db_table = "accounting_connection_events"
        indexes = [
            models.Index(fields=["connection", "-created_at"], name="acc_event_conn_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action} by {self.actor_label}"
