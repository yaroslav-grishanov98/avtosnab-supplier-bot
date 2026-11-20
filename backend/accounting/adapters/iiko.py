"""iiko adapter: the provider-neutral ``AccountingAdapter`` over the ported read-only client (``iiko_client``).

One adapter instance is one iiko session (plan D7): ``IikoAdapter.for_connection`` decrypts the connection's
credentials, logs in on enter and always logs out on exit. Every client failure leaves the adapter as an
``AdapterError`` built by ``map_error``: a fixed Russian message without server text, URLs or secrets.

Request costs (docs/wiki/integrations/iiko-capability-matrix.md):
  * catalogue: departments, stores (XML), products, units, groups — one GET each, cached for the session;
  * stock: the stores catalogue (one small XML GET per session, to tell a deleted store from an empty one; if it
    cannot be read, ``read_stock`` raises AdapterError and asks no balances), then one balance GET per chunk of
    ``ACCOUNTING_STORES_PER_REQUEST`` stores — nothing else when the caller passes ``product_units`` (plan D9:
    login + stores + balances + logout); without the map the server version and the product/unit catalogue are
    fetched once per session as a fallback;
  * consumption: one narrow OLAP POST per (warehouse, day), capped by ``ACCOUNTING_OLAP_POSTS_PER_SESSION``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from itertools import batched
from typing import TYPE_CHECKING, Any

from django.conf import settings as django_settings
from django.utils import timezone

from accounting.dto import (
    AccessReport,
    AccessStep,
    AdapterError,
    Capabilities,
    ConsumptionEventDTO,
    ConsumptionResult,
    DepartmentDTO,
    ItemErrorDTO,
    OperationKind,
    ProductDTO,
    StockItemDTO,
    StockSnapshotDTO,
    WarehouseDTO,
)
from accounting.iiko_client import (
    ErrorKind,
    HttpResult,
    IikoError,
    IikoHttpClient,
    IikoSession,
    Settings,
    scoped_secrets,
    settings_from_django,
)
from accounting.iiko_client.endpoints import TIMESTAMP_FORMAT, build_params, get_spec
from accounting.iiko_client.olap import OLAP_REPORT_PATH, build_transactions_body
from accounting.iiko_client.parsers import (
    decimal_or_none,
    parse_balances,
    parse_body,
    parse_catalog,
    parse_records,
    parse_server_info,
)
from accounting.iiko_client.redaction import sensitive_variables
from accounting.models.connection import CredentialsNotSet
from accounting.models.inventory import Reliability
from accounting.secrets import SecretError

if TYPE_CHECKING:
    from accounting.models import AccountingConnection

logger = logging.getLogger("accounting.adapters.iiko")

IIKO_CAPABILITIES = Capabilities(
    read_products=True,
    read_warehouses=True,
    read_departments=True,
    read_stock=True,
    read_consumption=True,
    write_receipt=False,
    omits_zero_balances=True,
    source_version="",  # unknown until a session asks the server (IikoAdapter.capabilities)
    notes=(
        "Остатки читаются одним запросом на пачку складов; нулевые остатки iiko не возвращает. "
        "Расход — узкий отчёт OLAP TRANSACTIONS (POST, один склад, один день), проверен на одном складе-дне "
        "тестового сервера. Запись приходов не поддерживается."
    ),
)

# The OLAP store filter field and value form confirmed on the test server: "Store" with the store's name
# (capability matrix, 22.10.2026). Store.Id and several stores per filter were never tried, so not used.
OLAP_STORE_FIELD = "Store"
OLAP_PURPOSE = "consumption"

_RETRYABLE = frozenset(
    {ErrorKind.TIMEOUT, ErrorKind.CONNECTION_ERROR, ErrorKind.DNS_ERROR, ErrorKind.SERVER_ERROR, ErrorKind.RATE_LIMITED}
)
# Stock: the session is unusable or the server asks to slow down, so store-by-store retries would not help.
_ABORT_STOCK = frozenset({ErrorKind.AUTH_ERROR, ErrorKind.FORBIDDEN, ErrorKind.RATE_LIMITED})
# Stock: failures that do not depend on which stores are in the request — the server cannot be reached, the
# method or path is missing, the request is refused before sending, the budget is gone. Retrying store by
# store would only repeat them N times, so the whole chunk fails. SERVER_ERROR, TIMEOUT, RESPONSE_TOO_LARGE
# and SCHEMA_MISMATCH may come from one store or from the chunk size, so those fall back to one store per
# request (and the fallback ends after MAX_FALLBACK_TIMEOUTS timeouts in a row).
_NO_STOCK_FALLBACK = frozenset(
    {
        ErrorKind.REQUEST_LIMIT_EXCEEDED,
        ErrorKind.CONNECTION_ERROR,
        ErrorKind.DNS_ERROR,
        ErrorKind.TLS_ERROR,
        ErrorKind.CONFIG_ERROR,
        ErrorKind.ENDPOINT_NOT_FOUND,
        ErrorKind.API_NOT_SUPPORTED,
        ErrorKind.MISSING_PARAMETERS,
        ErrorKind.READ_ONLY_VIOLATION,
    }
)
MAX_FALLBACK_TIMEOUTS = 2
# Consumption: these stop sending more reports; the days already read are returned with
# ConsumptionResult.stopped. Other failures (500, bad body, too large) only leave their own day uncovered.
_STOP_CONSUMPTION = frozenset(
    {
        ErrorKind.AUTH_ERROR,
        ErrorKind.FORBIDDEN,
        ErrorKind.RATE_LIMITED,
        ErrorKind.REQUEST_LIMIT_EXCEEDED,
        ErrorKind.READ_ONLY_VIOLATION,
        ErrorKind.CONNECTION_ERROR,
        ErrorKind.DNS_ERROR,
        ErrorKind.TLS_ERROR,
        ErrorKind.TIMEOUT,
    }
)
# A stop is "come back later" only when a new session can succeed: after a 429 or with a fresh request budget.
_RETRYABLE_STOPS = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.REQUEST_LIMIT_EXCEEDED})
# The list-products request must not degrade on these: the session is out of budget or rate limited.
_GROUPS_RAISE = frozenset(
    {ErrorKind.AUTH_ERROR, ErrorKind.FORBIDDEN, ErrorKind.RATE_LIMITED, ErrorKind.REQUEST_LIMIT_EXCEEDED}
)
_DAY_COMPLETE, _DAY_PARTIAL, _DAY_FAILED = "complete", "partial", "failed"

_MESSAGES = {
    ErrorKind.AUTH_ERROR: "Сервер iiko не принял логин и пароль или сессия iiko истекла.",
    ErrorKind.FORBIDDEN: "Сервер iiko отказал в доступе: нет прав пользователя, лицензии API или свободного подключения.",
    ErrorKind.TIMEOUT: "Сервер iiko не ответил за отведённое время.",
    ErrorKind.CONNECTION_ERROR: "Не удалось подключиться к серверу iiko.",
    ErrorKind.DNS_ERROR: "Не удалось найти сервер iiko по указанному адресу.",
    ErrorKind.TLS_ERROR: "Ошибка защищённого соединения (HTTPS) с сервером iiko.",
    ErrorKind.SERVER_ERROR: "Внутренняя ошибка сервера iiko.",
    ErrorKind.RATE_LIMITED: "Сервер iiko просит снизить частоту запросов.",
    ErrorKind.RESPONSE_TOO_LARGE: "Ответ сервера iiko слишком большой, данные не разобраны.",
    ErrorKind.ENDPOINT_NOT_FOUND: "Метод API не найден на сервере iiko.",
    ErrorKind.AUTH_METHOD_UNCONFIRMED: "Метод входа API не найден на сервере iiko.",
    ErrorKind.API_NOT_SUPPORTED: "Сервер iiko не поддерживает нужный метод API.",
    ErrorKind.INVALID_RESPONSE: "Сервер iiko вернул ответ в неожиданном формате.",
    ErrorKind.SCHEMA_MISMATCH: "Сервер iiko вернул данные в неожиданной структуре.",
    ErrorKind.REQUEST_LIMIT_EXCEEDED: "Достигнут лимит запросов к iiko за одну сессию.",
    ErrorKind.MISSING_PARAMETERS: "Внутренняя ошибка интеграции с iiko: запрос не отправлен.",
    ErrorKind.READ_ONLY_VIOLATION: "Внутренняя ошибка интеграции с iiko: запрос не отправлен.",
}

_HINTS = {
    "auth_error": "Проверьте логин и пароль пользователя iiko; после смены пароля в iiko замените его в подключении.",
    "forbidden": (
        "Проверьте права пользователя iiko (для номенклатуры нужно право B_EN) и наличие свободной лицензии API: "
        "незакрытая сессия держит лицензию до истечения таймаута."
    ),
    "timeout": "Проверьте, что сервер iiko доступен с серверов АвтоСнаб (адрес, порт, VPN, разрешённые IP).",
    "connection_error": "Проверьте, что сервер iiko доступен с серверов АвтоСнаб (адрес, порт, VPN, разрешённые IP).",
    "dns_error": "Проверьте адрес сервера iiko: имя хоста не находится.",
    "tls_error": "Проверьте HTTPS-сертификат сервера iiko: проверку сертификата отключить нельзя.",
    "endpoint_not_found": "Проверьте адрес сервера: обычно он заканчивается на /resto.",
    "auth_method_unconfirmed": "Проверьте адрес сервера: обычно он заканчивается на /resto.",
    "config_error": "Проверьте адрес сервера: нужен https:// и путь до /resto.",
    "rate_limited": "Сервер iiko ограничил частоту запросов; повторите проверку позже.",
    "server_error": "На сервере iiko внутренняя ошибка; повторите позже.",
    "credentials_missing": "Введите логин и пароль пользователя iiko в подключении.",
    "credentials_unreadable": "Введите логин и пароль пользователя iiko в подключении заново.",
}
_DEFAULT_HINT = "Повторите проверку позже; если ошибка повторяется, обратитесь в поддержку АвтоСнаб."

# Only what the capability matrix suggests; the type itself always stays in transaction_type, and which
# types count as consumption is BE-1's choice (plan O3, ``ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES``).
_KINDS = {
    "INVOICE": OperationKind.RECEIPT,
    "SESSION_WRITEOFF": OperationKind.CONSUMPTION,
    "WRITEOFF": OperationKind.CONSUMPTION,
}
# Same code and text as FakeAdapter: a warehouse the source does not know (deleted or never there).
_WAREHOUSE_NOT_FOUND = AdapterError(
    code="warehouse_not_found", message="Склад не найден в учётной системе.", retryable=False
)


def map_error(exc: IikoError) -> AdapterError:
    """The client's classified error as an AdapterError: fixed Russian text, ``retry_after`` kept from 429."""
    kind = exc.kind
    if kind is ErrorKind.CONFIG_ERROR:
        message = exc.message  # written for the user and never echoes the address or credentials
    else:
        message = _MESSAGES.get(kind, "Ошибка при обращении к серверу iiko.")
    if kind is ErrorKind.RATE_LIMITED and exc.retry_after is not None:
        message += f" Повторите не раньше чем через {exc.retry_after:g} с."
    return AdapterError(
        code=kind.value.lower(),
        message=message,
        retryable=kind in _RETRYABLE,
        retry_after=exc.retry_after,
    )


def access_hint(code: str) -> str:
    return _HINTS.get(code, _DEFAULT_HINT)


def operation_kind(transaction_type: str) -> OperationKind:
    return _KINDS.get(transaction_type, OperationKind.UNKNOWN)


@contextmanager
def _mapped_errors() -> Iterator[None]:
    try:
        yield
    except IikoError as exc:
        raise map_error(exc) from None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _key(value: Any) -> str:
    """Lookup key for ids (iiko ids are GUIDs whose case may differ between methods); never stored."""
    return (_text(value) or "").lower()


def _server_time(at: datetime) -> datetime:
    # iiko reads the timestamp as server-local time; the calendar day is settings.TIME_ZONE (plan O4).
    return at.astimezone(timezone.get_default_timezone()) if timezone.is_aware(at) else at


def _olap_day(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000"


class _StopReading(Exception):
    """read_consumption must stop sending reports; ``error`` goes to ConsumptionResult.stopped."""

    def __init__(self, error: AdapterError) -> None:
        super().__init__(error.code)
        self.error = error


def _stop_error(exc: IikoError) -> AdapterError:
    mapped = map_error(exc)
    if exc.kind not in _RETRYABLE_STOPS:
        return mapped  # e.g. a refused body or a dead session: as retryable as the error itself
    # After a 429 or an exhausted budget the remaining pairs were simply not asked; a new session can read them.
    return AdapterError(code=mapped.code, message=mapped.message, retryable=True, retry_after=mapped.retry_after)


def check_connection_access(connection: AccountingConnection) -> AccessReport:
    """Access check of an iiko connection that never raises: a failed login is a report too."""
    try:
        with IikoAdapter.for_connection(connection) as adapter:
            return adapter.check_access()
    except AdapterError as exc:
        return AccessReport(
            ok=False,
            steps=[AccessStep(name="login", status="failed", message=exc.message)],
            error_code=exc.code,
            hint=access_hint(exc.code),
        )


class IikoAdapter:
    """One logged-in iiko session; create it with ``for_connection``, never directly.

    Optional keywords (``for_connection`` and ``get_adapter(connection, **adapter_options)`` pass them on):

    * ``product_units``: external product id -> main unit name, as already stored by the catalogue sync. The
      stock sync (Task 10's runner) builds it from
      ``AccountingProduct.objects.filter(connection=...).values_list("external_id", "unit_name")``. With it,
      ``read_stock`` sends only the balance requests; a product missing from the map is UNIT_UNKNOWN with an
      empty ``source_uom``. Without it the product and unit catalogues are downloaded once per session.
    * ``source_version``: the server version already known (``AccountingConnection.server_version``); with it
      the session never asks ``get_server_info.jsp``. With a unit map and no version, snapshots carry "".
    * ``product_catalog``: article (``Product.Num``) -> (external product id, main unit name), only for articles no
      other product shares, as stored by the catalogue sync. ``read_consumption`` uses it instead of downloading
      the whole product catalogue in every session; rows whose article is missing from it make their day partial.
    """

    provider = "iiko"

    def __init__(
        self,
        session: IikoSession,
        client: IikoHttpClient,
        settings: Settings,
        *,
        product_units: Mapping[str, str] | None = None,
        source_version: str | None = None,
        product_catalog: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._settings = settings
        self._product_catalog = None if product_catalog is None else dict(product_catalog)
        self._server_version: str | None = source_version  # None: not known and not asked yet in this session
        self._product_units = (
            None
            if product_units is None
            else {_key(product_id): unit for product_id, raw in product_units.items() if (unit := _text(raw))}
        )
        self._cache: dict[str, Any] = {}
        self._olap_posts = 0

    def __repr__(self) -> str:
        return f"IikoAdapter(host={self._settings.host!r})"

    @classmethod
    @contextmanager
    def for_connection(
        cls,
        connection: AccountingConnection,
        *,
        product_units: Mapping[str, str] | None = None,
        source_version: str | None = None,
        product_catalog: Mapping[str, tuple[str, str]] | None = None,
    ) -> Iterator[IikoAdapter]:
        """Log in with the connection's credentials; leaving the block always logs out, even on an exception.

        ``product_units``, ``source_version`` and ``product_catalog`` are described on the class.
        """
        session, client, client_settings = cls._open_session(connection)
        try:
            yield cls(
                session,
                client,
                client_settings,
                product_units=product_units,
                source_version=source_version,
                product_catalog=product_catalog,
            )
        finally:
            session.close()  # logs out and never raises, so it cannot hide the block's own exception

    @staticmethod
    @sensitive_variables()  # a plain function, not the generator above: Django honours the decorator only here
    def _open_session(connection: AccountingConnection) -> tuple[IikoSession, IikoHttpClient, Settings]:
        """Decrypt the credentials, build the client settings and log in; AdapterError on any failure."""
        try:
            credentials = connection.credentials()
        except CredentialsNotSet:
            raise AdapterError(
                code="credentials_missing", message="У подключения iiko не заданы логин и пароль.", retryable=False
            ) from None
        except SecretError:
            raise AdapterError(
                code="credentials_unreadable",
                message="Не удалось расшифровать учётные данные подключения iiko.",
                retryable=False,
            ) from None
        # Masks login and hash already while the settings are checked; from login() on, the client keeps its own
        # masking scope for login, hash and token until the session is closed.
        with scoped_secrets(credentials.login, credentials.pass_hash):
            try:
                client_settings = settings_from_django(
                    base_url=connection.base_url, login=credentials.login, pass_hash=credentials.pass_hash
                )
            except IikoError as exc:
                raise map_error(exc) from None
            client = IikoHttpClient(client_settings)
            session = IikoSession(client, client_settings)
            try:
                session.login()
            except IikoError as exc:
                raise map_error(exc) from None  # login() has already closed the client
        return session, client, client_settings

    # -- low-level -------------------------------------------------------
    def _fetch(
        self, key: str, extra: dict[str, Any] | None = None, *, store_ids: Sequence[str] = (), purpose: str
    ) -> HttpResult:
        spec = get_spec(key)
        params: list[tuple[str, Any]] = list(build_params(spec, extra).items())
        params.extend(("store", store_id) for store_id in store_ids)
        result = self._session.get(spec.path, params, purpose=purpose, headers=dict(spec.headers) or None)
        result.raise_for_status(spec.path)  # before parsing: a truncated body must never be parsed
        return result

    def _cached(self, name: str, load: Callable[[], Any]) -> Any:
        if name not in self._cache:
            self._cache[name] = load()
        return self._cache[name]

    def _catalog(self, key: str) -> list[dict]:
        def load() -> list[dict]:
            result = self._fetch(key, purpose="catalog")
            return parse_catalog(result.content_type, result.body).records

        return self._cached(key, load)

    def _json_records(self, key: str) -> list[dict]:
        def load() -> list[dict]:
            result = self._fetch(key, purpose="catalog")
            return [
                {str(k).lower(): v for k, v in item.items()} for item in parse_records(result.content_type, result.body)
            ]

        return self._cached(key, load)

    def _names(self, key: str) -> dict[str, str]:
        """id (lookup key) -> name of a JSON reference list (units, product groups)."""
        return {_key(r.get("id")): name for r in self._json_records(key) if (name := _text(r.get("name")))}

    def _source_version(self) -> str:
        """Server version, asked once per session; "" when the server does not tell (it is optional)."""
        if self._server_version is None:
            self._server_version = ""
            spec = get_spec("server_info")
            try:
                result = self._client.get(spec.path, build_params(spec), purpose="server_info")
                result.raise_for_status(spec.path)
                self._server_version = parse_server_info(result.content_type, result.body).get("version", "")[:64]
            except IikoError as exc:
                logger.info("iiko: версию сервера узнать не удалось (%s)", exc.kind.value)
        return self._server_version

    # -- capabilities and access check -----------------------------------
    def capabilities(self) -> Capabilities:
        return replace(IIKO_CAPABILITIES, source_version=self._source_version())

    def check_access(self) -> AccessReport:
        """Login, server version, catalogues and one balance read, as steps without secrets.

        An iiko failure ends up in the report instead of being raised. ``StoreHouseAdapter.check_access``
        raises AdapterError instead, so callers going through ``get_adapter`` must still catch AdapterError.
        """
        steps = [AccessStep(name="login", status="ok", message="Вход в iiko выполнен.")]
        version = self._source_version()
        if version:
            steps.append(AccessStep(name="server_info", status="ok", message=f"Версия сервера iiko: {version}."))
        else:
            steps.append(
                AccessStep(name="server_info", status="skipped", message="Версию сервера iiko узнать не удалось.")
            )
        checks: tuple[tuple[str, str, Callable[[], int | None]], ...] = (
            ("departments", "Подразделения", lambda: len(self._catalog("departments"))),
            ("stores", "Склады", lambda: len(self._catalog("stores"))),
            ("products", "Номенклатура", lambda: len(self._json_records("products"))),
            ("balances", "Остатки", self._probe_balances),
        )
        for index, (name, label, run) in enumerate(checks):
            try:
                count = run()
            except IikoError as exc:
                error = map_error(exc)
                steps.append(AccessStep(name=name, status="failed", message=f"{label}: {error.message}"))
                steps.extend(
                    AccessStep(name=rest, status="skipped", message="Не проверялось: предыдущий шаг не пройден.")
                    for rest, _, _ in checks[index + 1 :]
                )
                return AccessReport(ok=False, steps=steps, error_code=error.code, hint=access_hint(error.code))
            if count is None:
                steps.append(AccessStep(name=name, status="skipped", message=f"{label}: нет складов для проверки."))
            else:
                steps.append(AccessStep(name=name, status="ok", message=f"{label}: получено записей — {count}."))
        return AccessReport(ok=True, steps=steps)

    def _probe_balances(self) -> int | None:
        stores = self._catalog("stores")
        store = next((s for s in stores if (s["type"] or "STORE") == "STORE"), None)
        if store is None:
            return None
        timestamp = _server_time(timezone.now()).strftime(TIMESTAMP_FORMAT)
        result = self._fetch("balances", {"timestamp": timestamp}, store_ids=[store["id"]], purpose="check_access")
        return len(parse_balances(result.content_type, result.body).rows)

    # -- catalogue -------------------------------------------------------
    def list_departments(self) -> list[DepartmentDTO]:
        with _mapped_errors():
            records = self._catalog("departments")
        return [DepartmentDTO(external_id=r["id"], name=r["name"], department_type=r["type"] or "") for r in records]

    def list_warehouses(self) -> list[WarehouseDTO]:
        with _mapped_errors():
            records = self._catalog("stores")
        return [
            WarehouseDTO(
                external_id=r["id"],
                name=r["name"],
                external_department_id=r["parent_id"] or None,  # the store's department (parentId)
                store_type=r["type"] or "",
            )
            for r in records
        ]

    def list_products(self) -> list[ProductDTO]:
        with _mapped_errors():
            products = self._json_records("products")
            units = self._names("measure_units")
            try:
                groups = self._names("product_groups")
            except IikoError as exc:
                if exc.kind in _GROUPS_RAISE:
                    raise
                # The group name only decorates the catalogue; the products themselves are still complete.
                logger.warning("iiko: группы номенклатуры не прочитаны (%s), названия групп пустые", exc.kind.value)
                groups = {}
        result = []
        for product in products:
            external_id, name = _text(product.get("id")), _text(product.get("name"))
            if external_id is None or name is None:
                continue
            unit_id = _text(product.get("mainunit")) or ""
            result.append(
                ProductDTO(
                    external_id=external_id,
                    name=name,
                    article=_text(product.get("num")) or "",
                    group_name=groups.get(_key(product.get("parent")), ""),
                    product_type=_text(product.get("type")) or "",
                    unit_external_id=unit_id,
                    unit_name=units.get(_key(unit_id), ""),
                    is_deleted=product.get("deleted") in (True, "true"),
                )
            )
        return result

    def _unit_by_product(self) -> dict[str, str]:
        """Product id (lookup key) -> main unit name; {} when the catalogue cannot be read.

        The units only label the balances: without them items are UNIT_UNKNOWN, which is better than no
        stock at all (a user without the B_EN right cannot read products). A dead session still aborts.
        """
        try:
            products = self._json_records("products")
            units = self._names("measure_units")
        except IikoError as exc:
            if exc.kind is ErrorKind.AUTH_ERROR:
                raise
            logger.warning("iiko: номенклатура для единиц остатков не прочитана (%s)", exc.kind.value)
            return {}
        return {
            _key(product.get("id")): unit for product in products if (unit := units.get(_key(product.get("mainunit"))))
        }

    # -- stock -----------------------------------------------------------
    def read_stock(self, warehouse_ids: Sequence[str], at: datetime) -> Iterator[StockSnapshotDTO]:
        """One snapshot per requested warehouse (duplicates once), in request order.

        One balance request per chunk of ``ACCOUNTING_STORES_PER_REQUEST`` stores; a warehouse without rows is
        an empty, ok snapshot (iiko leaves zero balances out). An empty answer is the same for a deleted store,
        so the stores list is read first (one small XML request per session) and a warehouse iiko does not
        know is ok=False with ``warehouse_not_found``, never asked for. A failed chunk is retried store by store
        in the same session (not for errors that do not depend on the stores, and only until two timeouts in a
        row), so one broken warehouse comes back ok=False and the others are unaffected. A dead session
        (AUTH_ERROR), missing rights (FORBIDDEN), HTTP 429 and an unreadable stores list abort the read with
        AdapterError. Rows of stores that were not asked for are dropped; if a requested store got no rows while
        such rows came, the answer is a SCHEMA_MISMATCH (re-read store by store), never an empty warehouse.
        """
        wanted = list(dict.fromkeys(warehouse_ids))
        if not wanted:
            return
        with _mapped_errors():
            known = {_key(store["id"]) for store in self._catalog("stores")}
        if self._product_units is not None:
            # The caller's stored catalogue: no version, products or units download (plan D9).
            units = self._product_units
            version = self._server_version or ""
        else:
            version = self._source_version()
            with _mapped_errors():
                units = self._unit_by_product()
        missing = [store_id for store_id in wanted if _key(store_id) not in known]
        if missing:
            logger.warning("iiko: склады %s не найдены в справочнике iiko, остатки не запрошены", ", ".join(missing))
        snapshots: dict[str, StockSnapshotDTO] = {
            store_id: self._failed_snapshot(store_id, at, version, _WAREHOUSE_NOT_FOUND) for store_id in missing
        }
        timestamp = _server_time(at).strftime(TIMESTAMP_FORMAT)
        size = max(1, int(django_settings.ACCOUNTING_STORES_PER_REQUEST))
        readable = [store_id for store_id in wanted if store_id not in snapshots]
        pending = iter(wanted)
        for chunk in batched(readable, size):
            for snapshot in self._read_chunk(list(chunk), at, timestamp, version, units):
                # Keep the request order: unknown warehouses come out in their place, next to the read ones.
                for store_id in pending:
                    if store_id == snapshot.external_warehouse_id:
                        break
                    yield snapshots[store_id]
                yield snapshot
        for store_id in pending:
            yield snapshots[store_id]

    @staticmethod
    def _failed_snapshot(store_id: str, at: datetime, version: str, error: AdapterError) -> StockSnapshotDTO:
        return StockSnapshotDTO(
            external_warehouse_id=store_id,
            taken_at=at,
            items=[],
            errors=[ItemErrorDTO(external_product_id=None, code=error.code, message=error.message)],
            source_version=version,
            ok=False,
        )

    def _read_chunk(
        self, chunk: list[str], at: datetime, timestamp: str, version: str, units: dict[str, str]
    ) -> Iterator[StockSnapshotDTO]:
        try:
            by_store, invalid_rows = self._balances(chunk, timestamp)
        except IikoError as exc:
            if exc.kind in _ABORT_STOCK:
                raise map_error(exc) from None
            failure = exc
        else:
            for store_id in chunk:
                yield self._snapshot(store_id, at, version, units, by_store[store_id], invalid_rows)
            return
        if len(chunk) == 1 or failure.kind in _NO_STOCK_FALLBACK:
            logger.warning("iiko: остатки %d склад(ов) не прочитаны (%s)", len(chunk), failure.kind.value)
            error = map_error(failure)
            for store_id in chunk:
                yield self._failed_snapshot(store_id, at, version, error)
            return
        logger.info(
            "iiko: остатки пачки из %d складов не прочитаны (%s), повтор по одному складу",
            len(chunk),
            failure.kind.value,
        )
        timeouts = 0
        for index, store_id in enumerate(chunk):
            try:
                by_store, invalid_rows = self._balances([store_id], timestamp)
            except IikoError as exc:
                if exc.kind in _ABORT_STOCK:
                    raise map_error(exc) from None
                logger.warning("iiko: остатки склада %s не прочитаны (%s)", store_id, exc.kind.value)
                error = map_error(exc)
                yield self._failed_snapshot(store_id, at, version, error)
                timeouts = timeouts + 1 if exc.kind is ErrorKind.TIMEOUT else 0
                if exc.kind in _NO_STOCK_FALLBACK or timeouts >= MAX_FALLBACK_TIMEOUTS:
                    # The server is unreachable, out of budget or too slow: do not wait for every other store.
                    rest = chunk[index + 1 :]
                    if rest:
                        logger.warning(
                            "iiko: повтор по одному складу остановлен (%s), ещё %d склад(ов) не прочитаны",
                            exc.kind.value,
                            len(rest),
                        )
                    for other in rest:
                        yield self._failed_snapshot(other, at, version, error)
                    return
                continue
            timeouts = 0
            yield self._snapshot(store_id, at, version, units, by_store[store_id], invalid_rows)

    def _balances(self, chunk: list[str], timestamp: str) -> tuple[dict[str, list[dict]], int]:
        """Balance rows of the chunk split by store, and the number of unreadable rows (single store only)."""
        result = self._fetch("balances", {"timestamp": timestamp}, store_ids=chunk, purpose="stock")
        parsed = parse_balances(result.content_type, result.body)
        single = chunk[0] if len(chunk) == 1 else None
        if parsed.invalid_rows and single is None:
            # Unreadable rows cannot be attributed to a store: re-read store by store instead.
            raise IikoError(ErrorKind.SCHEMA_MISMATCH, "Строки остатков не разобраны в ответе по нескольким складам")
        index = {_key(store_id): store_id for store_id in chunk}
        by_store: dict[str, list[dict]] = {store_id: [] for store_id in chunk}
        foreign = 0
        for row in parsed.rows:
            if row["store_id"] is None:
                if single is None:
                    raise IikoError(
                        ErrorKind.SCHEMA_MISMATCH, "Строка остатков без склада в ответе по нескольким складам"
                    )
                by_store[single].append(row)
            elif (store_id := index.get(_key(row["store_id"]))) is not None:
                by_store[store_id].append(row)
            else:
                foreign += 1  # a store that was not asked for
        if foreign:
            logger.info("iiko: в ответе остатков строк других складов: %d, пропущены", foreign)
            if any(not rows for rows in by_store.values()):
                # Rows that match no requested store while a requested store got none: most likely the store ids
                # come back in a format we cannot match. That must not look like an empty (all-zero) warehouse.
                raise IikoError(
                    ErrorKind.SCHEMA_MISMATCH, "Строки остатков относятся к складам, которые не запрашивались"
                )
        return by_store, parsed.invalid_rows

    @staticmethod
    def _snapshot(
        store_id: str, at: datetime, version: str, units: dict[str, str], rows: list[dict], invalid_rows: int
    ) -> StockSnapshotDTO:
        items: list[StockItemDTO] = []
        errors: list[ItemErrorDTO] = []
        for row in rows:
            product_id, quantity = row["product_id"], row["amount"]
            unit = units.get(_key(product_id), "")
            if quantity is None:
                reliability = Reliability.SOURCE_ERROR
                errors.append(
                    ItemErrorDTO(
                        external_product_id=product_id,
                        code="quantity_missing",
                        message="iiko не сообщил количество остатка по товару.",
                    )
                )
            elif not unit:
                reliability = Reliability.UNIT_UNKNOWN
            else:
                reliability = Reliability.OK
            items.append(
                StockItemDTO(
                    external_product_id=product_id, stock_qty=quantity, source_uom=unit, reliability=reliability
                )
            )
        if invalid_rows:
            errors.append(
                ItemErrorDTO(
                    external_product_id=None,
                    code="invalid_rows",
                    message=f"Строк остатков, которые не удалось разобрать: {invalid_rows}.",
                )
            )
        # ok: the warehouse was read as a whole; item errors only make the snapshot partial.
        return StockSnapshotDTO(
            external_warehouse_id=store_id, taken_at=at, items=items, errors=errors, source_version=version, ok=True
        )

    # -- consumption -----------------------------------------------------
    def read_consumption(self, warehouse_ids: Sequence[str], day_from: date, day_to: date) -> ConsumptionResult:
        """Per-product movements by transaction type, one OLAP report per (warehouse, day), oldest day first.

        Only the warehouse side of a posting counts (``Account.Name`` is the store's name, quantity = -Amount: positive
        means the stock left the warehouse); counter-account rows carry money and are skipped (Step 0, 23.10.2026).
        A pair is covered only when its whole report was read and every warehouse row of a stored type was mapped to
        a catalogue product. A pair read whole but with unmapped rows is *partial*: ``covered`` is False, it is in
        ``ConsumptionResult.partial`` and its mapped events are returned.
        A failed report (500, bad or too large body) leaves its pair uncovered and the next pairs are still read;
        the client never re-sends a report. The per-session POST cap, HTTP 429, the request budget, a timeout, a
        transport failure (connection, DNS, TLS), a dead session or missing OLAP rights stop the read: the days
        already read are returned, the rest stay uncovered and ``ConsumptionResult.stopped`` says why
        (``retryable`` is True for the cap, 429 with ``retry_after`` and the budget; otherwise it is the error's
        own), so callers re-ask only the uncovered pairs later. Failures before the first report (columns, stores,
        products) raise AdapterError: nothing was read yet.
        """
        if day_to < day_from:
            raise ValueError("day_to must not be earlier than day_from")
        wanted = list(dict.fromkeys(warehouse_ids))
        days = [day_from + timedelta(days=offset) for offset in range((day_to - day_from).days + 1)]
        covered = {(warehouse_id, day): False for day in days for warehouse_id in wanted}
        events: list[ConsumptionEventDTO] = []
        partial: set[tuple[str, date]] = set()
        if not wanted:
            return ConsumptionResult(events=events, covered=covered)
        with _mapped_errors():
            columns = self._olap_columns()
            store_names = self._unique_store_names()
            products = self._products_by_num()
        probe = build_transactions_body(
            columns, day_from=_olap_day(day_from), day_to=_olap_day(day_from + timedelta(days=1)),
            store_field=OLAP_STORE_FIELD, store_value="probe",
        )  # fmt: skip
        if probe is None or "Product.Num" not in probe["groupByRowFields"]:
            raise AdapterError(
                code="api_not_supported",
                message="Отчёт OLAP по проводкам на этом сервере iiko не содержит полей, нужных для расхода.",
                retryable=False,
            )
        targets = []
        for warehouse_id in wanted:
            name = store_names.get(_key(warehouse_id))
            if name is None:
                logger.warning(
                    "iiko: склад %s не найден в справочнике или его имя не уникально, расход не запрошен", warehouse_id
                )
            else:
                targets.append((warehouse_id, name))
        try:
            for day in days:
                for warehouse_id, store_name in targets:
                    if self._olap_posts >= self._settings.max_olap_posts:
                        logger.info("iiko: исчерпан лимит отчётов OLAP за сессию, остальные дни не прочитаны")
                        raise _StopReading(
                            AdapterError(
                                code="olap_post_limit",
                                message="Достигнут лимит отчётов iiko за сессию; остальные дни будут прочитаны позже.",
                                retryable=True,
                            )
                        )
                    day_events, how = self._read_day(warehouse_id, store_name, day, columns, products)
                    events.extend(day_events)
                    covered[(warehouse_id, day)] = how == _DAY_COMPLETE
                    if how == _DAY_PARTIAL:
                        partial.add((warehouse_id, day))
        except _StopReading as stop:
            return ConsumptionResult(events=events, covered=covered, stopped=stop.error, partial=frozenset(partial))
        return ConsumptionResult(events=events, covered=covered, partial=frozenset(partial))

    def _read_day(
        self,
        warehouse_id: str,
        store_name: str,
        day: date,
        columns: dict[str, Any],
        products: dict[str, tuple[str, str]],
    ) -> tuple[list[ConsumptionEventDTO], str]:
        """Events of one (warehouse, day) and how the day was read (complete, partial, failed); raises _StopReading
        to stop the read."""
        body = build_transactions_body(
            columns,
            day_from=_olap_day(day),
            day_to=_olap_day(day + timedelta(days=1)),
            store_field=OLAP_STORE_FIELD,
            store_value=store_name,
        )
        if body is None:
            # Cannot happen with the columns that already gave the probe body; refuse rather than send nothing.
            raise map_error(IikoError(ErrorKind.SCHEMA_MISMATCH, "Тело отчёта OLAP не построено"))
        self._olap_posts += 1
        try:
            result = self._session.post_olap_report(body, purpose=OLAP_PURPOSE)
            result.raise_for_status(OLAP_REPORT_PATH)
            rows = parse_records(result.content_type, result.body)
        except IikoError as exc:
            logger.warning("iiko: отчёт расхода за %s не прочитан (%s)", day.isoformat(), exc.kind.value)
            if exc.kind in _STOP_CONSUMPTION:
                raise _StopReading(_stop_error(exc)) from None
            return [], _DAY_FAILED
        day_start = datetime.combine(day, time.min, tzinfo=timezone.get_default_timezone())
        events: list[ConsumptionEventDTO] = []
        unmapped = 0
        store_key = _key(store_name)
        excluded = {str(kind).upper() for kind in django_settings.ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES}
        for row in rows:
            # Step 0 (23.10.2026): every posting is two rows. The quantity is on the warehouse account
            # (Account.Name is the store's name); the counter account (expense, revenue, supplier debt) carries
            # money. Only the warehouse side is read, so nothing is summed twice or read as a wrong unit.
            if _key(row.get("Account.Name")) != store_key:
                continue
            transaction_type = _text(row.get("TransactionType"))
            amount = decimal_or_none(row.get("Amount"))
            if transaction_type is None or amount is None:
                unmapped += 1
                continue
            num = _text(row.get("Product.Num"))
            product = products.get(num) if num else None
            if product is None:
                # Types that are not stored anyway (receipts, revenue, discounts) do not make the day incomplete.
                if transaction_type.upper() not in excluded:
                    unmapped += 1
                continue
            product_id, catalogue_unit = product
            events.append(
                ConsumptionEventDTO(
                    external_product_id=product_id,
                    external_warehouse_id=warehouse_id,
                    occurred_at=day_start,  # the report is a daily total, not single movements
                    qty=-amount,  # Amount is negative when stock leaves the warehouse; qty > 0 means consumed
                    source_uom=_text(row.get("Product.MeasureUnit")) or catalogue_unit,
                    transaction_type=transaction_type,  # kept exactly as iiko names it
                    kind=operation_kind(transaction_type),
                )
            )
        if unmapped:
            logger.warning(
                "iiko: строк расхода за %s без товара из каталога или количества: %d, день прочитан не полностью",
                day.isoformat(),
                unmapped,
            )
        return events, (_DAY_PARTIAL if unmapped else _DAY_COMPLETE)

    def _olap_columns(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            result = self._fetch("olap_columns", {"reportType": "TRANSACTIONS"}, purpose=OLAP_PURPOSE)
            _, data = parse_body(result.content_type, result.body)
            if not isinstance(data, dict):
                raise IikoError(ErrorKind.SCHEMA_MISMATCH, "Список полей OLAP-отчёта iiko не является объектом")
            return data

        return self._cached("olap_columns", load)

    def _unique_store_names(self) -> dict[str, str]:
        """Store id (lookup key) -> name, only for names no other store shares (the OLAP filter takes a name)."""
        stores = self._catalog("stores")
        counts: dict[str, int] = {}
        for store in stores:
            counts[store["name"].casefold()] = counts.get(store["name"].casefold(), 0) + 1
        return {_key(store["id"]): store["name"] for store in stores if counts[store["name"].casefold()] == 1}

    def _products_by_num(self) -> dict[str, tuple[str, str]]:
        """Article (Product.Num) -> (product id, main unit name), only for articles no other product shares.

        The OLAP report is grouped by article: Product.Id is not among the fields verified on the test server.
        """
        if self._product_catalog is not None:
            return self._product_catalog
        products = self._json_records("products")
        try:
            units = self._names("measure_units")
        except IikoError as exc:
            if exc.kind is ErrorKind.AUTH_ERROR:
                raise
            units = {}  # only the fallback unit for rows without Product.MeasureUnit
        counts: dict[str, int] = {}
        for product in products:
            if num := _text(product.get("num")):
                counts[num] = counts.get(num, 0) + 1
        result = {}
        for product in products:
            num, product_id = _text(product.get("num")), _text(product.get("id"))
            if num and product_id and counts[num] == 1:
                result[num] = (product_id, units.get(_key(product.get("mainunit")), ""))
        return result
