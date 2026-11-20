"""Session handling for iikoServer (ported from iiko_diagnostics).

Mechanism, per the official iikoServer API documentation (article "Авторизация"):

  * ``GET api/auth?login=<login>&pass=<SHA-1 hex of the password>`` -> token string;
  * the token is passed as the ``key`` parameter (or cookie ``key``) of every request;
  * ``GET api/logout?key=<token>`` ends the session; authorisation occupies one licence slot until logout.

The client only ever holds the SHA-1 hex digest (``Settings.pass_hash``); there is no code path that sees or
hashes a plain password. A session is one httpx client: leaving the ``with`` block logs out (even when the
body raised), clears the cookies, closes the client and ends secret masking for this session's values.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from .client import AUTH_PURPOSE, HttpResult, IikoHttpClient, Params, describe_http_error
from .config import Settings
from .exceptions import NOT_FOUND_HINTS, ErrorKind, IikoError
from .redaction import sensitive_variables

logger = logging.getLogger("accounting.iiko_client.auth")

AUTH_PATH = "api/auth"
LOGOUT_PATH = "api/logout"
SESSION_NOT_OPEN_MESSAGE = "IikoSession is not open: call it inside `with IikoSession(...)`, never after the block"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-_.=+/]{8,256}$")


class IikoSession:
    """One authorised session, reused for all calls and closed exactly once."""

    def __init__(self, client: IikoHttpClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._token: str | None = None
        self.login_count = 0
        self.logout_done = False
        self.logout_status: int | None = None
        self._closed = False

    def __repr__(self) -> str:
        return f"IikoSession(open={self.is_open})"

    @property
    def is_open(self) -> bool:
        return self._token is not None and not self.logout_done and not self._closed

    # -- login / logout --------------------------------------------------
    # sensitive_variables(): frames of these methods and of everything they call (client, httpx) hold the
    # login, the hash or the token in their locals; Django error reports must show none of them.
    @sensitive_variables()
    def login(self) -> None:
        if self._token is not None and not self._closed:
            return  # session is reused, never re-created
        if self.logout_done or self._closed:
            raise IikoError(ErrorKind.AUTH_ERROR, "Сессия iiko уже завершена; для новой работы откройте новую")
        if not self._settings.has_credentials:
            raise IikoError(ErrorKind.CONFIG_ERROR, "Не заданы логин или пароль iiko")
        self._client.open()
        try:
            result = self._client.get(
                AUTH_PATH,
                {"login": self._settings.login, "pass": self._settings.pass_hash},
                purpose=AUTH_PURPOSE,  # the client never retries this call after a timeout
            )
            self.login_count += 1
            token = self._validate_login(result)
        except BaseException:
            # No token means nothing to log out; drop the httpx client and this session's masking now.
            self._client.close()
            raise
        self._client.protect(token)
        self._token = token
        logger.info("iiko: авторизация выполнена, сессия открыта")

    @staticmethod
    def _validate_login(result: HttpResult) -> str:
        status = result.status_code
        endpoint = result.record.url
        if status == 200:
            token = result.text.strip().strip('"')
            if _TOKEN_RE.match(token):
                return token
            # Fixed text only: the body may be the token in an unexpected shape. Not retried — a retry could
            # take a second licence slot while the first session stays open.
            logger.warning(
                "iiko: авторизация вернула HTTP 200 без распознаваемого токена; на сервере может остаться "
                "открытая сессия (занятый слот лицензии до истечения таймаута), повтор не выполняется"
            )
            raise IikoError(
                ErrorKind.INVALID_RESPONSE,
                "Сервер iiko вернул 200, но тело не похоже на токен сессии (формат не распознан)",
                status,
                endpoint=endpoint,
            )
        if status == 404:
            raise IikoError(
                ErrorKind.AUTH_METHOD_UNCONFIRMED,
                "Маршрут авторизации api/auth (описан в официальной документации) на этом сервере "
                "не найден: механизм авторизации не подтверждён, авторизованные запросы остановлены. "
                + describe_http_error(result, AUTH_PATH),
                status,
                endpoint=endpoint,
                hints=NOT_FOUND_HINTS,
            )
        if status == 401:
            raise IikoError(
                ErrorKind.AUTH_ERROR,
                "Сервер iiko отклонил логин или пароль. Запрос не повторяется автоматически, чтобы не "
                "блокировать учётную запись. " + describe_http_error(result, AUTH_PATH),
                status,
                endpoint=endpoint,
            )
        if status == 403:
            raise IikoError(ErrorKind.FORBIDDEN, describe_http_error(result, AUTH_PATH), status, endpoint=endpoint)
        kind = result.error_kind or ErrorKind.INVALID_RESPONSE
        raise IikoError(
            kind,
            "Авторизация в iiko не выполнена. " + describe_http_error(result, AUTH_PATH),
            status,
            endpoint=endpoint,
            retry_after=result.retry_after,
        )

    @sensitive_variables()
    def logout(self) -> bool:
        """End the session on the server. Safe to call repeatedly; never raises."""
        if self._token is None or self.logout_done:
            return self.logout_done
        try:
            result = self._client.get(LOGOUT_PATH, {"key": self._token}, is_logout=True, purpose="logout")
            self.logout_status = result.status_code
            self.logout_done = result.status_code == 200
            if not self.logout_done:
                logger.warning(
                    "iiko: завершение сессии вернуло HTTP %s — проверьте занятую лицензию", result.status_code
                )
        except IikoError as exc:
            logger.warning("iiko: не удалось завершить сессию: %s", exc.kind.value)
        except Exception as exc:
            # Logout runs from __exit__: it must never replace the body's exception. Only the type is logged,
            # since an arbitrary exception message could carry the token.
            logger.warning("iiko: не удалось завершить сессию: %s", type(exc).__name__)
        return self.logout_done

    def close(self) -> None:
        """Log out, then drop the token and the session's httpx client (cookies cleared)."""
        try:
            self.logout()
        finally:
            self._token = None
            self._closed = True
            self._client.close()

    # -- authorised calls ------------------------------------------------
    def _require_open(self) -> None:
        if not self.is_open:
            # A programming error (a call before login or after the block), not iiko rejecting the credentials.
            raise RuntimeError(SESSION_NOT_OPEN_MESSAGE)

    @sensitive_variables()
    def get(
        self, path: str, params: Params = None, *, purpose: str = "", headers: Mapping[str, str] | None = None
    ) -> HttpResult:
        self._require_open()
        merged: list[tuple[str, Any]] = [("key", self._token)]
        if params:
            items = params.items() if isinstance(params, Mapping) else params
            merged.extend((k, v) for k, v in items if v is not None)
        return self._client.get(path, merged, authenticated=True, purpose=purpose, headers=headers)

    @sensitive_variables()
    def post_olap_report(self, body: Mapping[str, Any], *, purpose: str = "") -> HttpResult:
        """Send the one allowed non-GET request (see ``olap.py``); the client validates the body first."""
        self._require_open()
        return self._client.post_olap_report([("key", self._token)], body, purpose=purpose)

    def __enter__(self) -> IikoSession:
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
