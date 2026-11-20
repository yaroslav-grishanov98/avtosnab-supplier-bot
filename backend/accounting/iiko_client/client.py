"""Read-only, rate-limited HTTP client for iikoServer (ported from iiko_diagnostics).

Guarantees enforced here:
  * only GET/HEAD requests go through ``request`` (documented auth/logout are GET as well), with one narrow
    exception: ``post_olap_report`` sends a validated OLAP TRANSACTIONS report (see ``olap.py``), at most
    ``Settings.max_olap_posts`` times per session and never retried;
  * one request at a time; TLS always verified; no proxies or netrc from the environment; redirects are not
    followed (a redirect could carry the session key to another host);
  * a fresh ``httpx.Client`` per session (``open``/``close``): cookies the server set (iiko sets ``key``) are
    cleared on close and never reach the next session;
  * a pause between requests; at most one retry, and only when the request certainly did not reach the server
    (connect error / connect timeout) or after a short ``Retry-After``. Read/write timeouts are never re-sent
    (the server may already have done the work), and the auth call is never retried after any timeout: a lost
    answer may hold a token that occupies a licence slot, and a retry would take a second one;
    401/403/404 are never retried;
  * a cap of HTTP requests per session (``Settings.max_requests``, hard ceiling 100) and of response size
    (a truncated body raises ``RESPONSE_TOO_LARGE`` in ``raise_for_status`` instead of being parsed);
  * callers may pass only the ``Accept`` header; requests need an open client (``with`` or ``open()``);
  * login, hash and token are masked in logs and errors while the session is open (``scoped_secrets``).
"""

from __future__ import annotations

import logging
import socket
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .exceptions import NOT_FOUND_HINTS, ErrorKind, IikoError, kind_for_status
from .olap import OLAP_REPORT_PATH, validate_olap_body
from .redaction import SecretScope, mask_text, redact_url, scoped_secrets, sensitive_variables

logger = logging.getLogger("accounting.iiko_client.http")

ALLOWED_METHODS = frozenset({"GET", "HEAD"})
ALLOWED_CALLER_HEADERS = frozenset({"accept"})
AUTH_PURPOSE = "auth"
NOT_OPEN_MESSAGE = "IikoHttpClient is not open: use `with IikoHttpClient(...)` or call open() first"
MAX_RETRY_AFTER_SECONDS = 30.0
USER_AGENT = "autosnab-accounting/1.0 (read-only)"

Params = Mapping[str, Any] | Sequence[tuple[str, Any]] | None

_TRANSPORT_MESSAGES = {
    ErrorKind.TIMEOUT: "Сервер iiko не ответил за отведённое время",
    ErrorKind.DNS_ERROR: "Не удалось найти сервер iiko по имени (DNS)",
    ErrorKind.TLS_ERROR: "Ошибка защищённого соединения (TLS) с сервером iiko",
    ErrorKind.CONNECTION_ERROR: "Не удалось подключиться к серверу iiko",
}


@dataclass
class RequestRecord:
    """One recorded HTTP exchange; the URL is already stripped of secrets."""

    method: str
    url: str
    status_code: int | None
    content_type: str | None
    duration_ms: float
    error_kind: str | None = None
    authenticated: bool = False
    response_bytes: int = 0
    truncated: bool = False
    purpose: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HttpResult:
    status_code: int
    content_type: str | None
    body: bytes = field(repr=False)
    truncated: bool
    record: RequestRecord
    retry_after: float | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def error_kind(self) -> ErrorKind | None:
        return kind_for_status(self.status_code)

    @property
    def is_html(self) -> bool:
        head = self.body.lstrip(b"\xef\xbb\xbf \t\r\n")[:20].lower()
        return "html" in (self.content_type or "").lower() or head.startswith((b"<!doctype", b"<html"))

    @property
    def server_message(self) -> str | None:
        """Short error text of a non-HTML error body (iiko sends text/plain), secrets masked."""
        if self.status_code < 400 or self.is_html or not self.body:
            return None
        return mask_text(" ".join(self.text.split())[:200]) or None

    def raise_for_status(self, path: str | None = None) -> None:
        """Raise the classified ``IikoError`` for an error status (with ``retry_after`` for 429) or a body
        cut at ``max_response_bytes``. Call it before parsing: a truncated body must never be parsed."""
        kind = self.error_kind
        shown = path or urlsplit(self.record.url).path
        if kind is None and self.truncated:
            raise IikoError(
                ErrorKind.RESPONSE_TOO_LARGE,
                f"HTTP {self.status_code} на {shown}: ответ iiko превысил допустимый размер "
                f"({len(self.body)} байт), данные не разобраны",
                self.status_code,
                endpoint=self.record.url,
            )
        if kind is None:
            return
        raise IikoError(
            kind,
            describe_http_error(self, shown),
            self.status_code,
            endpoint=self.record.url,
            hints=NOT_FOUND_HINTS if self.status_code == 404 else (),
            retry_after=self.retry_after,
        )


def describe_http_error(result: HttpResult, path: str) -> str:
    """Explain an HTTP error using iiko's documented error table (article "Описание ошибок").

    Causes are listed as documented possibilities; the client never asserts which one applies.
    """
    status, message = result.status_code, result.server_message
    tail = f" Ответ сервера: {message}" if message else ""
    low = (message or "").lower()
    if status == 404:
        if result.is_html:
            what = (
                "Тело ответа — HTML. По документации iiko это признак сетевой ошибки или ошибки настройки "
                "(опечатка в URL, ошибка прокси, «пустой» Tomcat без сервера iiko), а не ответа REST API."
            )
        else:
            what = "Тело ответа — текст ошибки iiko; по документации 404 = объект не найден или некорректный путь."
        causes = "Возможные причины (не установлены): " + "; ".join(NOT_FOUND_HINTS)
        return f"HTTP 404 на маршруте {path}. {what} {causes}{tail}"
    if status == 403:
        if "blocked within current license" in low:
            hint = "по тексту ответа — на сервере нет лицензии на этот модуль API"
        elif "no connections available" in low:
            hint = "по тексту ответа — исчерпано число лицензионных подключений к API (возможно, висит старая сессия)"
        elif "permission denied" in low:
            hint = "по тексту ответа — у пользователя нет прав, нужных этому методу"
        else:
            hint = "документированные причины: нет лицензии на модуль, исчерпаны подключения, нет прав пользователя"
        return f"HTTP 403 на {path}: {hint}.{tail}"
    if status == 401:
        return (
            f"HTTP 401 на {path}: не аутентифицирован. Документированные причины: нет параметра key, "
            "истёк таймаут сессии (обычно 1 час), сервер перезапускался или ещё запускается." + tail
        )
    if status == 429:
        wait = f" Повторить не раньше чем через {result.retry_after:g} с." if result.retry_after is not None else ""
        return f"HTTP 429 на {path}: сервер iiko просит снизить частоту запросов.{wait}{tail}"
    if 500 <= status <= 599:
        return f"HTTP {status} на {path}: внутренняя ошибка сервера iiko.{tail}"
    return f"HTTP {status} на {path}.{tail}"


def _exception_chain(exc: BaseException | None):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify_transport_error(exc: BaseException) -> ErrorKind:
    """Map a transport-level exception to DNS / TLS / TIMEOUT / CONNECTION."""
    if isinstance(exc, httpx.TimeoutException):
        return ErrorKind.TIMEOUT
    for item in _exception_chain(exc):
        if isinstance(item, ssl.SSLError):
            return ErrorKind.TLS_ERROR
        if isinstance(item, socket.gaierror):
            return ErrorKind.DNS_ERROR
        if isinstance(item, TimeoutError):
            return ErrorKind.TIMEOUT
    text = str(exc).lower()
    if "certificate" in text or "ssl" in text or "tls" in text:
        return ErrorKind.TLS_ERROR
    if "getaddrinfo" in text or "name or service not known" in text or "name resolution" in text:
        return ErrorKind.DNS_ERROR
    return ErrorKind.CONNECTION_ERROR


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None  # HTTP-date form is not honoured; treated as "unknown"


class IikoHttpClient:
    """Transport for one session at a time; ``open`` starts a fresh httpx client, ``close`` drops it."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._http: httpx.Client | None = None
        self._secrets: ExitStack | None = None
        self._scope: SecretScope | None = None
        self._last_request_at: float | None = None
        self.request_count = 0
        self._olap_posts = 0
        self.records: list[RequestRecord] = []

    def __repr__(self) -> str:
        return f"IikoHttpClient(host={self.settings.host!r}, open={self.is_open}, requests={self.request_count})"

    # -- lifecycle -------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._http is not None

    def open(self) -> None:
        """Start a session-scoped httpx client; budgets and masking are per session."""
        if self._http is not None:
            return
        stack = ExitStack()
        self._scope = stack.enter_context(scoped_secrets(self.settings.login, self.settings.pass_hash))
        self._secrets = stack
        self._http = httpx.Client(
            timeout=httpx.Timeout(self.settings.timeout),
            verify=True,  # never disabled
            trust_env=False,  # no proxy / netrc / CA overrides from the worker environment
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml;q=0.9, */*;q=0.5"},
            transport=self._transport,
        )
        self.request_count = 0
        self._olap_posts = 0
        self._last_request_at = None

    def protect(self, *values: str | None) -> None:
        """Mask more values (the session token) until ``close``."""
        if self._scope is not None:
            self._scope.add(*values)

    def close(self) -> None:
        http, self._http = self._http, None
        secrets, self._secrets = self._secrets, None
        self._scope = None
        try:
            if http is not None:
                http.cookies.clear()
                http.close()
        finally:
            if secrets is not None:
                secrets.close()

    def __enter__(self) -> IikoHttpClient:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers ---------------------------------------------------------
    def url_for(self, path: str) -> str:
        if "://" in path or path.startswith("//"):
            raise IikoError(ErrorKind.CONFIG_ERROR, "Путь запроса должен быть относительным к адресу сервера iiko")
        return self.settings.base_url + path.lstrip("/")

    @property
    def requests_left(self) -> int:
        return max(0, self.settings.max_requests - self.request_count)

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        wait = self.settings.request_delay - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    def _check_budget(self, *, is_logout: bool) -> None:
        # one request is always reserved so the session can be closed
        reserve = 0 if is_logout else 1
        if self.request_count + reserve >= self.settings.max_requests:
            raise IikoError(
                ErrorKind.REQUEST_LIMIT_EXCEEDED,
                f"Достигнут лимит {self.settings.max_requests} HTTP-запросов к iiko за сессию",
            )

    # -- main entry ------------------------------------------------------
    # sensitive_variables(): params, the built request and its URL carry login and hash (auth) or the token
    # (every other call); Django error reports must not list them. It also covers the httpx frames below.
    @sensitive_variables()
    def request(
        self,
        method: str,
        path: str,
        params: Params = None,
        *,
        authenticated: bool = False,
        is_logout: bool = False,
        purpose: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        method = method.upper()
        if method not in ALLOWED_METHODS:
            raise IikoError(
                ErrorKind.READ_ONLY_VIOLATION,
                f"Метод {method} запрещён: клиент iiko допускает только чтение (GET/HEAD)",
            )
        self._require_open()
        if headers and any(name.lower() not in ALLOWED_CALLER_HEADERS for name in headers):
            raise IikoError(ErrorKind.READ_ONLY_VIOLATION, "Клиенту iiko можно передать только заголовок Accept")
        url = self.url_for(path)
        retried = False
        while True:
            self._check_budget(is_logout=is_logout)
            self._pace()
            try:
                result = self._send_once(method, url, params, authenticated, purpose, headers)
            except IikoError as exc:
                if not retried and self._may_retry(exc, purpose):
                    retried = True
                    logger.warning("iiko: временная сетевая ошибка (%s), один повтор запроса", exc.kind.value)
                    continue
                raise
            if result.status_code == 429 and not retried:
                delay = result.retry_after
                if delay is not None and delay <= MAX_RETRY_AFTER_SECONDS:
                    retried = True
                    logger.warning("iiko: HTTP 429, повтор через %.1f c (Retry-After)", delay)
                    self._sleep(delay)
                    continue
            return result

    @staticmethod
    def _may_retry(exc: IikoError, purpose: str) -> bool:
        if exc.kind not in (ErrorKind.TIMEOUT, ErrorKind.CONNECTION_ERROR) or exc.request_sent is not False:
            return False  # the request may have reached the server: re-sending could repeat its effect
        # Even a connect timeout is not retried for auth: a lost token would keep a licence slot busy.
        return not (purpose == AUTH_PURPOSE and exc.kind is ErrorKind.TIMEOUT)

    def _require_open(self) -> None:
        if self._http is None:
            # A programming error, not an iiko failure: a lazily opened client would leave an httpx client and
            # a masking scope open after the call.
            raise RuntimeError(NOT_OPEN_MESSAGE)

    @sensitive_variables()
    def get(self, path: str, params: Params = None, **kwargs: Any) -> HttpResult:
        return self.request("GET", path, params, **kwargs)

    @sensitive_variables()
    def post_olap_report(self, params: Params, body: Mapping[str, Any], *, purpose: str = "") -> HttpResult:
        """The one allowed non-GET call: a validated OLAP TRANSACTIONS report. Never retried."""
        self._require_open()
        validate_olap_body(body, max_store_values=self.settings.olap_max_store_values)
        if self._olap_posts >= self.settings.max_olap_posts:
            raise IikoError(
                ErrorKind.READ_ONLY_VIOLATION,
                f"Допустимо не больше {self.settings.max_olap_posts} запросов OLAP за сессию",
            )
        url = self.url_for(OLAP_REPORT_PATH)
        self._check_budget(is_logout=False)
        self._pace()
        self._olap_posts += 1  # counted before sending: a heavy report must not be repeated after a timeout
        return self._send_once("POST", url, params, True, purpose, None, json_body=dict(body))

    @sensitive_variables()
    def _send_once(
        self,
        method: str,
        url: str,
        params: Params,
        authenticated: bool,
        purpose: str,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> HttpResult:
        # Defence in depth: the only non-GET request that may leave is the OLAP report with a JSON body.
        olap_post = method == "POST" and json_body is not None and url == self.url_for(OLAP_REPORT_PATH)
        if method not in ALLOWED_METHODS and not olap_post:
            raise IikoError(ErrorKind.READ_ONLY_VIOLATION, f"Метод {method} запрещён для клиента iiko")
        self._require_open()
        assert self._http is not None
        request = self._http.build_request(method, url, params=params, headers=headers, json=json_body)
        safe_url = redact_url(str(request.url))
        started = time.perf_counter()
        self.request_count += 1
        self._last_request_at = self._clock()
        logger.debug("iiko: %s %s", method, safe_url)
        failure: ErrorKind | None = None
        not_sent = False
        body = bytearray()
        truncated = False
        try:
            response = self._http.send(request, stream=True)
            try:
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.max_response_bytes:
                        truncated = True
                        del body[self.settings.max_response_bytes :]
                        break
                status = response.status_code
                content_type = response.headers.get("content-type")
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
            finally:
                response.close()
        except httpx.HTTPError as exc:
            failure = classify_transport_error(exc)
            # Only a failure while connecting proves the request never left; read/write errors do not.
            not_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        if failure is not None:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            self.records.append(
                RequestRecord(
                    method,
                    safe_url,
                    None,
                    None,
                    elapsed,
                    error_kind=failure.value,
                    authenticated=authenticated,
                    purpose=purpose,
                )
            )
            # Raised outside the except block and "from None": the httpx exception carries the full URL, and
            # the auth URL holds login and hash, so it must not ride along as __cause__ or __context__.
            error = IikoError(failure, f"{_TRANSPORT_MESSAGES[failure]} ({urlsplit(safe_url).path})", endpoint=safe_url)
            error.request_sent = False if not_sent else None
            raise error from None

        kind = kind_for_status(status)
        record = RequestRecord(
            method,
            safe_url,
            status,
            content_type,
            round((time.perf_counter() - started) * 1000, 1),
            error_kind=kind.value if kind else None,
            authenticated=authenticated,
            response_bytes=len(body),
            truncated=truncated,
            purpose=purpose,
        )
        self.records.append(record)
        logger.debug("iiko: -> HTTP %s, %d байт, %.0f мс", status, len(body), record.duration_ms)
        return HttpResult(status, content_type, bytes(body), truncated, record, retry_after)
