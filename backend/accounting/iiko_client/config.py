"""Client settings.

The prototype read ``IIKO_*`` variables from ``.env`` and hashed a plain password itself. Here the client gets
only the SHA-1 hex digest (``AccountingConnection.credentials().pass_hash``); a plain password has no field and
no code path. Limits default to the ``ACCOUNTING_IIKO_*`` Django settings through ``settings_from_django``;
the rest of the package has no Django imports so it stays testable on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .exceptions import ErrorKind, IikoError
from .redaction import sensitive_variables

HARD_MAX_REQUESTS = 100  # ceiling per session whatever the settings say (the one-off prototype had 20)
MIN_REQUESTS = 3  # auth + one call + logout
# Stores per OLAP store filter. A multi-store filter has never been tried on a real server (plan Task 11,
# Step 0); until it is verified there, only one store goes into a report. Raise it (and wire it to a setting)
# only after that check. ACCOUNTING_STORES_PER_REQUEST is the balance-report chunk size, not this limit.
OLAP_MAX_STORE_VALUES = 1
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SHA1_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


def _config_error(message: str) -> IikoError:
    return IikoError(ErrorKind.CONFIG_ERROR, message)


def normalize_base_url(raw: str, *, allow_http_loopback: bool = False) -> str:
    """``https://host[:port]/path/`` with a trailing slash; the URL is never echoed back in errors."""
    raw = (raw or "").strip()
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
    except ValueError:
        raise _config_error("Адрес сервера iiko указан неверно") from None
    if not hostname:
        raise _config_error("В адресе сервера iiko не указан хост")
    plain_loopback = parts.scheme == "http" and allow_http_loopback and hostname in LOOPBACK_HOSTS
    if parts.scheme != "https" and not plain_loopback:
        raise _config_error("Адрес сервера iiko должен начинаться с https:// (проверку TLS отключать нельзя)")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise _config_error("Адрес сервера iiko не должен содержать параметры, фрагмент или учётные данные")
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


@dataclass(frozen=True)
class Settings:
    base_url: str
    login: str = field(default="", repr=False)
    pass_hash: str = field(default="", repr=False)  # SHA-1 hex of the password, never the password itself
    timeout: float = 15.0
    request_delay: float = 0.5
    max_requests: int = 60
    max_response_bytes: int = 10_000_000
    max_olap_posts: int = 8
    olap_max_store_values: int = OLAP_MAX_STORE_VALUES
    # Only tests set this: lets the client talk plain http to the fake server on 127.0.0.1.
    allow_http_loopback: bool = False

    def __post_init__(self) -> None:
        set_ = object.__setattr__  # frozen dataclass: normalise in place once
        set_(self, "base_url", normalize_base_url(self.base_url, allow_http_loopback=self.allow_http_loopback))
        set_(self, "login", (self.login or "").strip())
        pass_hash = (self.pass_hash or "").strip().lower()
        if pass_hash and not _SHA1_HEX_RE.match(pass_hash):
            raise _config_error("Пароль iiko передаётся только как SHA-1 (40 шестнадцатеричных символов)")
        set_(self, "pass_hash", pass_hash)
        if self.max_requests < MIN_REQUESTS:
            raise _config_error(f"Лимит запросов за сессию должен быть не меньше {MIN_REQUESTS}")
        set_(self, "max_requests", min(int(self.max_requests), HARD_MAX_REQUESTS))
        if self.timeout <= 0 or self.request_delay < 0:
            raise _config_error("Таймаут должен быть больше нуля, пауза между запросами — не меньше нуля")
        if self.max_response_bytes < 1024 or self.max_olap_posts < 0 or self.olap_max_store_values < 1:
            raise _config_error("Неверные лимиты клиента iiko (размер ответа, число OLAP-запросов или складов)")

    def __str__(self) -> str:
        return repr(self)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.login and self.pass_hash)


@sensitive_variables()  # login and pass_hash are arguments: keep them out of Django error reports
def settings_from_django(*, base_url: str, login: str, pass_hash: str) -> Settings:
    """Settings with the limits from ``config/settings.py`` (``ACCOUNTING_IIKO_*``, OLAP posts per session)."""
    from django.conf import settings as django_settings

    return Settings(
        base_url=base_url,
        login=login,
        pass_hash=pass_hash,
        timeout=float(django_settings.ACCOUNTING_IIKO_TIMEOUT),
        request_delay=float(django_settings.ACCOUNTING_IIKO_REQUEST_DELAY),
        max_requests=int(django_settings.ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION),
        max_olap_posts=int(django_settings.ACCOUNTING_OLAP_POSTS_PER_SESSION),
        olap_max_store_values=OLAP_MAX_STORE_VALUES,
    )
