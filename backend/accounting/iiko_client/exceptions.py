"""Error taxonomy for the iiko client (ported from iiko_diagnostics)."""

from __future__ import annotations

from enum import Enum

from .redaction import mask_text


class ErrorKind(str, Enum):
    DNS_ERROR = "DNS_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TLS_ERROR = "TLS_ERROR"
    TIMEOUT = "TIMEOUT"
    AUTH_ERROR = "AUTH_ERROR"
    FORBIDDEN = "FORBIDDEN"
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    API_NOT_SUPPORTED = "API_NOT_SUPPORTED"
    AUTH_METHOD_UNCONFIRMED = "AUTH_METHOD_UNCONFIRMED"
    SERVER_ERROR = "SERVER_ERROR"
    REQUEST_LIMIT_EXCEEDED = "REQUEST_LIMIT_EXCEEDED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    MISSING_PARAMETERS = "MISSING_PARAMETERS"
    READ_ONLY_VIOLATION = "READ_ONLY_VIOLATION"
    CONFIG_ERROR = "CONFIG_ERROR"


# Possible causes of an HTTP 404. These are hypotheses, not established facts.
NOT_FOUND_HINTS = (
    "маршрут указан неверно (опечатка или не тот префикс)",
    "метод отсутствует в данной версии iikoServer",
    "REST API не включён или недоступен на этом сервере/прокси",
    "неверный базовый путь (ожидается .../resto/)",
    "обратный прокси не пробрасывает этот путь",
)


class IikoError(Exception):
    """Classified iiko error. Message and endpoint are masked when the error is created.

    Masking at construction matters: the error is usually raised inside a session (where the session's
    login, hash and token are known to the redactor) but logged or shown after the session has ended.
    """

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        status_code: int | None = None,
        *,
        endpoint: str | None = None,
        hints: tuple[str, ...] = (),
        retry_after: float | None = None,
    ) -> None:
        message = mask_text(message)
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.endpoint = mask_text(endpoint) if endpoint else endpoint
        self.hints = hints
        self.retry_after = retry_after
        # False: the transport failed before the request reached the server (safe to retry).
        # None: unknown or not a transport error (after a read timeout the server may have processed it).
        self.request_sent: bool | None = None

    def __reduce__(self):
        # Default exception pickling replays self.args, which do not match __init__; Celery and multiprocessing
        # pickle errors, so rebuild from the already-masked fields and restore the instance dict.
        return (type(self), (self.kind, self.message, self.status_code), dict(self.__dict__))

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.message}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "status_code": self.status_code,
            "endpoint": self.endpoint,
            "hints": list(self.hints),
            "retry_after": self.retry_after,
            "request_sent": self.request_sent,
        }


def kind_for_status(status: int) -> ErrorKind | None:
    """Map an HTTP status to an error kind, or None if the status is not an error."""
    if status == 401:
        return ErrorKind.AUTH_ERROR
    if status == 403:
        return ErrorKind.FORBIDDEN
    if status == 404:
        return ErrorKind.ENDPOINT_NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (405, 501):
        return ErrorKind.API_NOT_SUPPORTED
    if 500 <= status <= 599:
        return ErrorKind.SERVER_ERROR
    if status >= 400 or 300 <= status < 400:
        return ErrorKind.INVALID_RESPONSE
    return None
