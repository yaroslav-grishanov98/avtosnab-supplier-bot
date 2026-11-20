"""Secret masking for logs, URLs and error texts.

Unlike the prototype (a process-global ``register_secret`` set that only grew), secrets are registered for the
lifetime of one session through ``scoped_secrets``: while any scope holding a value is open, that value is
replaced with ``***`` in every log record created in the process and in every ``IikoError`` built here. When
the scope ends the value is forgotten. Values shared by overlapping scopes are reference-counted, so scopes may
end in any order (two sessions in one worker process).

Values of the query parameters iiko uses for credentials (``login``, ``pass``, ``key``) and cookie headers are
masked by name as well, which also covers the session token in the auth response's ``Set-Cookie`` that httpcore
logs at DEBUG before the client has read the token from the body.

Limits of the log hook: it masks only the formatted message (``record.msg`` % ``record.args``), not
``exc_text``, ``stack_info`` or attributes passed through ``extra=`` — callers must never log secrets via
``extra`` or in exceptions they log. While any scope is open, every log record in the process is formatted
early (at creation) so it can be masked; with no scope open the hook does nothing.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    # Hides the locals of decorated functions (and of every frame they call) in Django error reports. It
    # needs no configured settings at import time, so the client stays importable without a Django project.
    from django.views.decorators.debug import sensitive_variables
except ImportError:  # pragma: no cover - the client is used inside the Django project

    def sensitive_variables(*variables):
        return lambda func: func


MASK = "***"
SENSITIVE_PARAMS = frozenset({"pass", "password", "key", "token", "login", "access_token"})
_MIN_SECRET_LEN = 4  # shorter values would garble ordinary text; params are masked by name

_lock = threading.RLock()
_active: Counter[str] = Counter()

_PARAM_RE = re.compile(
    r"(?i)(?P<prefix>[?&;\s\"'(](?:" + "|".join(sorted(SENSITIVE_PARAMS)) + r")=)(?P<val>[^&\s\"';)]*)"
)
_COOKIE_RE = re.compile(r"(?i)(?P<prefix>\b(?:cookie|set-cookie|authorization)\s*[:=]\s*)(?P<val>[^\r\n]*)")
# httpcore logs headers as a list of byte tuples: (b'Set-Cookie', b'key=...; Path=/').
_HEADER_TUPLE_RE = re.compile(
    r"(?i)(?P<prefix>b?['\"](?:set-cookie|cookie|authorization)['\"]\s*,\s*b?['\"])(?P<val>[^'\"]*)"
)


def _snapshot() -> list[str]:
    with _lock:
        # longest first so a secret that contains another is masked fully
        return sorted(_active, key=len, reverse=True)


def mask_text(text: str) -> str:
    """Replace the secrets of open scopes and credential-like parameters/headers with ``***``."""
    if not text:
        return text
    for secret in _snapshot():
        if secret in text:
            text = text.replace(secret, MASK)
    text = _HEADER_TUPLE_RE.sub(lambda m: m.group("prefix") + MASK, text)
    text = _PARAM_RE.sub(lambda m: m.group("prefix") + MASK, text)
    text = _COOKIE_RE.sub(lambda m: m.group("prefix") + MASK, text)
    return text


def redact_url(url: str) -> str:
    """Return the URL with values of sensitive query parameters replaced (fragment dropped)."""
    try:
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        redacted = [(k, MASK if k.lower() in SENSITIVE_PARAMS else v) for k, v in pairs]
        query = urlencode(redacted, safe="*")
        return mask_text(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")))
    except ValueError:
        return mask_text(url)


def mask_obj(obj):
    """Recursively mask strings inside JSON-like structures."""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v) for v in obj]
    return obj


class SecretScope:
    """Handle of one open ``scoped_secrets`` block; ``add`` registers values learnt later (the token)."""

    def __init__(self) -> None:
        self._values: list[str] = []
        self._closed = False

    def __repr__(self) -> str:
        return f"SecretScope(values={len(self._values)}, closed={self._closed})"

    def add(self, *values: str | None) -> None:
        with _lock:
            if self._closed:
                return
            for value in values:
                if value and len(value) >= _MIN_SECRET_LEN:
                    _active[value] += 1
                    self._values.append(value)

    def close(self) -> None:
        with _lock:
            for value in self._values:
                _active[value] -= 1
                if _active[value] <= 0:
                    del _active[value]
            self._values.clear()
            self._closed = True


@contextmanager
def scoped_secrets(*values: str | None) -> Iterator[SecretScope]:
    """Mask ``values`` in logs and iiko errors until the block ends."""
    _install_log_redaction()
    scope = SecretScope()
    scope.add(*values)
    try:
        yield scope
    finally:
        scope.close()


def _install_log_redaction() -> None:
    """Wrap the log record factory once; the wrapper is a no-op while no scope is open."""
    with _lock:
        current = logging.getLogRecordFactory()
        if getattr(current, "_iiko_redaction", False):
            return

        def factory(*args, **kwargs):
            record = current(*args, **kwargs)
            if not _active:
                return record
            try:
                message = record.getMessage()
            except Exception:  # malformed format args must never break logging
                message = str(record.msg)
            masked = mask_text(message)
            if masked != message:
                record.msg = masked
                record.args = ()
            return record

        factory._iiko_redaction = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)
