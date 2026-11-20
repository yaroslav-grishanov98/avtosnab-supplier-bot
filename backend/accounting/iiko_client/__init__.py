"""Read-only iikoServer client, ported from the iiko_diagnostics prototype.

Credentials are the login and the SHA-1 hex of the password (never the password itself); one ``IikoSession``
is one httpx client, logged out and closed on exit. ``adapters/iiko.py`` is the only intended consumer.
"""

from .auth import IikoSession
from .client import HttpResult, IikoHttpClient
from .config import Settings, settings_from_django
from .exceptions import ErrorKind, IikoError
from .parsers import parse_balances, parse_body, parse_catalog
from .redaction import scoped_secrets

__all__ = [
    "ErrorKind",
    "HttpResult",
    "IikoError",
    "IikoHttpClient",
    "IikoSession",
    "Settings",
    "parse_balances",
    "parse_body",
    "parse_catalog",
    "scoped_secrets",
    "settings_from_django",
]
