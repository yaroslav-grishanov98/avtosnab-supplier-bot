"""iiko adapter over the ported client, exercised against the local fake iikoServer on 127.0.0.1.

Nothing here talks to a real iiko server: the fake runs in a thread on a free loopback port, and the client
settings are swapped for loopback-friendly ones (plain http, no pause between requests).
"""

import base64
import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.test import SimpleTestCase, override_settings
from django.views.debug import ExceptionReporter

from accounting.adapters import get_adapter
from accounting.adapters import iiko as iiko_module
from accounting.adapters.iiko import (
    IIKO_CAPABILITIES,
    IikoAdapter,
    check_connection_access,
    map_error,
    operation_kind,
)
from accounting.dto import AdapterError, OperationKind, ProductDTO
from accounting.iiko_client import ErrorKind, IikoError, Settings
from accounting.iiko_client.config import OLAP_MAX_STORE_VALUES
from accounting.iiko_client.parsers import parse_balances
from accounting.models import AccountingConnection, Reliability
from accounting.models.connection import Provider
from accounting.test_support.fake_iiko_server import OLAP_PATH, FakeIikoServer

LOGIN = "iiko-adapter-tester"
# Exists only in the test: the connection seals SHA-1 of it, the adapter only ever sees the hash.
PASSWORD = "Adapter-Plain-Pw-7"
PASS_HASH = hashlib.sha1(PASSWORD.encode()).hexdigest()
SECRET_KEY = base64.b64encode(bytes([9]) * 32).decode("ascii")

AUTH = "/resto/api/auth"
LOGOUT = "/resto/api/logout"
SERVER_INFO = "/resto/get_server_info.jsp"
DEPARTMENTS = "/resto/api/corporation/departments"
STORES = "/resto/api/corporation/stores"
PRODUCTS = "/resto/api/v2/entities/products/list"
UNITS = "/resto/api/v2/entities/list"
GROUPS = "/resto/api/v2/entities/products/group/list"
BALANCES = "/resto/api/v2/reports/balance/stores"
OLAP_COLUMNS = "/resto/api/v2/reports/olap/columns"

AT = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
D1, D2, D3, D4 = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)


def error_report(call, expected) -> str:
    """HTML of a production error report (DEBUG off, frame locals included), as in test_models_connection.

    A plain try/except on purpose: assertRaises clears the traceback frames.
    """
    try:
        call()
    except expected as exc:
        return ExceptionReporter(None, type(exc), exc, exc.__traceback__).get_traceback_html()
    raise AssertionError(f"{expected.__name__} was not raised")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@override_settings(ACCOUNTING_SECRET_KEYS={1: SECRET_KEY}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1)
class IikoAdapterTestBase(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = FakeIikoServer(login=LOGIN, pass_hash=PASS_HASH)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        super().tearDownClass()

    def setUp(self):
        self.server.reset()
        self.client_overrides: dict = {}
        patcher = mock.patch.object(iiko_module, "settings_from_django", side_effect=self.loopback_settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = self.server.data
        self.store_ids = [store["id"] for store in self.data.stores]
        # The adapter's log goes to a list instead of the console, and must never carry a secret.
        self.log = _ListHandler()
        adapter_logger = logging.getLogger("accounting.adapters.iiko")
        adapter_logger.addHandler(self.log)
        self.addCleanup(adapter_logger.removeHandler, self.log)
        propagate = mock.patch.object(adapter_logger, "propagate", False)
        propagate.start()
        self.addCleanup(propagate.stop)
        self.addCleanup(self.assert_log_holds_no_secrets)

    def assert_log_holds_no_secrets(self):
        text = "\n".join(self.log.messages)
        for secret in (LOGIN, PASS_HASH, *self.server.issued_tokens):
            self.assertNotIn(secret, text)

    def loopback_settings(self, *, base_url: str, login: str, pass_hash: str) -> Settings:
        """settings_from_django, but allowed to talk plain http to the fake and without pauses."""
        values = dict(
            base_url=base_url,
            login=login,
            pass_hash=pass_hash,
            timeout=2.0,
            request_delay=0.0,
            max_requests=django_settings.ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION,
            max_olap_posts=django_settings.ACCOUNTING_OLAP_POSTS_PER_SESSION,
            olap_max_store_values=OLAP_MAX_STORE_VALUES,
            allow_http_loopback=True,
        )
        values.update(self.client_overrides)
        return Settings(**values)

    def make_connection(self, login: str = LOGIN, password: str | None = PASSWORD) -> AccountingConnection:
        connection = AccountingConnection(provider=Provider.IIKO, base_url=self.server.base_url)
        if password is not None:
            connection.set_credentials(login, password)
        return connection

    def open(self, login: str = LOGIN, password: str | None = PASSWORD, **adapter_options):
        return IikoAdapter.for_connection(self.make_connection(login, password), **adapter_options)

    def unit_map(self) -> dict[str, str]:
        """What Task 10 builds from AccountingProduct: external product id -> main unit name."""
        units = {u["id"]: u["name"] for u in self.data.units}
        return {p["id"]: units[p["mainUnit"]] for p in self.data.products}

    def balance_calls(self):
        return self.server.calls(BALANCES)

    def empty_balances(self, store_id: str) -> None:
        saved = self.data.balances[store_id]
        self.data.balances[store_id] = []
        self.addCleanup(self.data.balances.__setitem__, store_id, saved)

    def expected_products(self, store_id: str) -> set[str]:
        return {row["product"] for row in self.data.balances.get(store_id, [])}


class IikoCatalogueTests(IikoAdapterTestBase):
    def test_list_departments_warehouses_products(self):
        with self.open() as adapter:
            departments = adapter.list_departments()
            warehouses = adapter.list_warehouses()
            products = adapter.list_products()

        self.assertEqual(
            [(d.external_id, d.name, d.department_type) for d in departments],
            [(d["id"], d["name"], d["type"]) for d in self.data.departments],
        )
        # A store names its department through parentId.
        self.assertEqual(
            [(w.external_id, w.name, w.external_department_id, w.store_type) for w in warehouses],
            [(s["id"], s["name"], s["parentId"], "STORE") for s in self.data.stores],
        )
        department_ids = {d.external_id for d in departments}
        self.assertTrue(all(w.external_department_id in department_ids for w in warehouses))

        units = {u["id"]: u["name"] for u in self.data.units}
        groups = {g["id"]: g["name"] for g in self.data.groups}
        expected = [
            ProductDTO(
                external_id=p["id"],
                name=p["name"],
                article=p["num"],
                group_name=groups.get(p["parent"], ""),
                product_type=p["type"],
                unit_external_id=p["mainUnit"],
                unit_name=units[p["mainUnit"]],
                is_deleted=False,
            )
            for p in self.data.products
        ]
        self.assertEqual(products, expected)
        self.assertTrue(any(p.group_name == "" for p in products))  # dishes outside any group

    def test_stores_catalogue_is_requested_as_xml(self):
        with self.open() as adapter:
            warehouses = adapter.list_warehouses()
            adapter.list_departments()

        for path in (STORES, DEPARTMENTS):
            (call,) = self.server.calls(path)
            headers = {name.lower(): value for name, value in call.headers.items()}
            self.assertEqual(headers["accept"], "application/xml")
        # Without the header the fake (like the real server) answers empty JSON objects; with it, real records.
        self.assertEqual(len(warehouses), len(self.data.stores))
        self.assertTrue(all(w.name for w in warehouses))

    def test_truncated_response_is_never_parsed(self):
        self.client_overrides["max_response_bytes"] = 2048
        with self.open() as adapter:
            with self.assertRaises(AdapterError) as caught:
                adapter.list_products()
            snapshots = list(adapter.read_stock(self.store_ids[:2], AT))

        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertIs(caught.exception.retryable, False)
        # The chunk and then each single store come back cut: never treated as a complete (or empty) snapshot.
        self.assertEqual([s.ok for s in snapshots], [False, False])
        self.assertEqual([s.items for s in snapshots], [[], []])
        self.assertEqual({s.errors[0].code for s in snapshots}, {"response_too_large"})


class IikoStockTests(IikoAdapterTestBase):
    @override_settings(TIME_ZONE="Europe/Moscow")
    def test_read_stock_one_snapshot_per_warehouse(self):
        first, empty, second = self.store_ids[0], self.store_ids[2], self.store_ids[3]
        self.empty_balances(empty)  # a known store without balance rows (all zero)
        with self.open() as adapter:
            snapshots = list(adapter.read_stock([first, empty, second], AT))

        self.assertEqual([s.external_warehouse_id for s in snapshots], [first, empty, second])
        for snapshot in snapshots:
            self.assertTrue(snapshot.ok)
            self.assertEqual(snapshot.errors, [])
            self.assertEqual(snapshot.taken_at, AT)
            self.assertEqual(snapshot.source_version, "9.4.8049.0")
            self.assertEqual(
                {item.external_product_id for item in snapshot.items},
                self.expected_products(snapshot.external_warehouse_id),
            )
        self.assertEqual(snapshots[1].items, [])  # zeros are filled in later from omits_zero_balances

        units = {u["id"]: u["name"] for u in self.data.units}
        unit_of = {p["id"]: units[p["mainUnit"]] for p in self.data.products}
        for item in snapshots[0].items:
            self.assertEqual(item.source_uom, unit_of[item.external_product_id])
            self.assertEqual(item.reliability, Reliability.OK)

        (call,) = self.balance_calls()
        self.assertEqual(call.params["store"], [first, empty, second])
        self.assertEqual(call.params["timestamp"], ["2026-10-22T12:00:00"])  # server-local (Moscow) time

    def test_failed_warehouse_yields_not_ok_snapshot_others_unaffected(self):
        first, broken, third = self.store_ids[:3]
        self.server.balance_failing_stores = {broken}
        with self.open() as adapter:
            snapshots = {s.external_warehouse_id: s for s in adapter.read_stock([first, broken, third], AT)}

        self.assertEqual(set(snapshots), {first, broken, third})
        for good in (first, third):
            self.assertTrue(snapshots[good].ok)
            self.assertEqual({i.external_product_id for i in snapshots[good].items}, self.expected_products(good))
        failed = snapshots[broken]
        self.assertFalse(failed.ok)
        self.assertEqual(failed.items, [])
        (error,) = failed.errors
        self.assertEqual((error.external_product_id, error.code), (None, "server_error"))
        self.assertIn("iiko", error.message)
        self.assertNotIn("Traceback", error.message)
        self.assertTrue(any("SERVER_ERROR" in message for message in self.log.messages))
        self.assertEqual(len(self.balance_calls()), 4)  # the chunk, then each of the three stores once

    def test_chunk_failure_falls_back_to_one_store_per_request(self):
        wanted = self.store_ids[:3]
        self.server.balance_max_stores = 1  # the chunk request fails, single-store requests work
        with self.open() as adapter:
            snapshots = list(adapter.read_stock(wanted, AT))

        self.assertEqual([s.external_warehouse_id for s in snapshots], wanted)
        self.assertTrue(all(s.ok for s in snapshots))
        self.assertEqual([c.params["store"] for c in self.balance_calls()], [wanted, *([s] for s in wanted)])
        # Same session for the chunk and the retries: one login, one logout.
        self.assertEqual((len(self.server.calls(AUTH)), len(self.server.calls(LOGOUT))), (1, 1))

    def test_zero_and_negative_balances_kept_as_decimal(self):
        store = self.store_ids[0]
        p_zero, p_negative, p_fraction, p_null = (p["id"] for p in self.data.products[:4])
        unknown = "00000000-0000-4000-8000-0000000000ff"
        rows = [
            {"store": store, "product": p_zero, "amount": 0, "sum": 0},
            {"store": store, "product": p_negative, "amount": -1.25, "sum": -10.5},
            {"store": store, "product": p_fraction, "amount": 3.333, "sum": 99.99},
            {"store": store, "product": p_null, "amount": None, "sum": None},
            {"store": store, "product": unknown, "amount": 2, "sum": 20},
        ]
        self.server.set_override(BALANCES, body=json.dumps(rows), content_type="application/json")
        with self.open() as adapter:
            (snapshot,) = adapter.read_stock([store], AT)

        items = {item.external_product_id: item for item in snapshot.items}
        self.assertEqual(set(items), {p_zero, p_negative, p_fraction, p_null, unknown})
        for product, expected in ((p_zero, "0"), (p_negative, "-1.25"), (p_fraction, "3.333"), (unknown, "2")):
            self.assertIsInstance(items[product].stock_qty, Decimal)
            self.assertEqual(items[product].stock_qty, Decimal(expected))
        self.assertIsNotNone(items[p_zero].stock_qty)  # a real zero is not "unknown"
        self.assertEqual((items[p_zero].reliability, items[p_zero].source_uom), (Reliability.OK, "кг"))
        # The server gave no number: None, never zero.
        self.assertIsNone(items[p_null].stock_qty)
        self.assertEqual(items[p_null].reliability, Reliability.SOURCE_ERROR)
        # A product the catalogue does not know has no unit to report.
        self.assertEqual((items[unknown].reliability, items[unknown].source_uom), (Reliability.UNIT_UNKNOWN, ""))
        self.assertTrue(snapshot.ok)
        self.assertEqual([(e.external_product_id, e.code) for e in snapshot.errors], [(p_null, "quantity_missing")])

    @override_settings(ACCOUNTING_STORES_PER_REQUEST=2)
    def test_read_stock_uses_one_request_per_chunk_of_stores(self):
        with self.open() as adapter:
            snapshots = list(adapter.read_stock(self.store_ids, AT))

        self.assertEqual([s.external_warehouse_id for s in snapshots], self.store_ids)
        self.assertTrue(all(s.ok for s in snapshots))
        self.assertEqual(
            [c.params["store"] for c in self.balance_calls()],
            [self.store_ids[0:2], self.store_ids[2:4], self.store_ids[4:5]],
        )
        # Without a unit map: auth, stores (to tell a deleted store from an empty one), version, units of the
        # products, one balance request per chunk, logout.
        self.assertEqual(
            [r.path for r in self.server.requests],
            [AUTH, STORES, SERVER_INFO, PRODUCTS, UNITS, BALANCES, BALANCES, BALANCES, LOGOUT],
        )

    def test_rate_limited_carries_retry_after(self):
        self.server.set_override(BALANCES, status=429, body="Too many requests", headers={"Retry-After": "120"})
        with self.open() as adapter:
            with self.assertRaises(AdapterError) as caught:
                list(adapter.read_stock(self.store_ids[:3], AT))

        error = caught.exception
        self.assertEqual((error.code, error.retryable, error.retry_after), ("rate_limited", True, 120.0))
        # No store-by-store retries against a server that asks to slow down; the session is still closed.
        self.assertEqual(len(self.balance_calls()), 1)
        self.assertEqual(len(self.server.calls(LOGOUT)), 1)

        mapped = map_error(IikoError(ErrorKind.RATE_LIMITED, "HTTP 429", 429, retry_after=7.5))
        self.assertEqual((mapped.code, mapped.retryable, mapped.retry_after), ("rate_limited", True, 7.5))
        for kind in (
            ErrorKind.TIMEOUT,
            ErrorKind.CONNECTION_ERROR,
            ErrorKind.DNS_ERROR,
            ErrorKind.SERVER_ERROR,
            ErrorKind.RATE_LIMITED,
        ):
            with self.subTest(kind=kind):
                self.assertIs(map_error(IikoError(kind, "x")).retryable, True)

    def test_auth_error_aborts_the_stock_read(self):
        self.server.set_override(BALANCES, status=401, body="Token is expired or invalid")
        with self.open() as adapter:
            with self.assertRaises(AdapterError) as caught:
                list(adapter.read_stock(self.store_ids[:3], AT))
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("auth_error", False))
        self.assertEqual(len(self.balance_calls()), 1)  # no per-store fallback for a dead session

    def test_unknown_warehouse_is_a_failed_snapshot(self):
        known = self.store_ids[0]
        gone = "00000000-0000-4000-8000-00000000e0e0"  # deleted in iiko or never there
        with self.open(product_units=self.unit_map()) as adapter:
            snapshots = {s.external_warehouse_id: s for s in adapter.read_stock([known, gone], AT)}

        self.assertTrue(snapshots[known].ok)
        # Not an empty ok snapshot: that would let the stock sync zero-fill the whole warehouse.
        failed = snapshots[gone]
        self.assertFalse(failed.ok)
        self.assertEqual(failed.items, [])
        self.assertEqual([(e.external_product_id, e.code) for e in failed.errors], [(None, "warehouse_not_found")])
        self.assertIn("Склад", failed.errors[0].message)
        self.assertEqual([c.params["store"] for c in self.balance_calls()], [[known]])  # never asked for

    def test_endpoint_not_found_on_a_chunk_is_not_retried_store_by_store(self):
        self.server.set_override(BALANCES, status=404, body="HTTP 404 Not Found")
        with self.open(product_units=self.unit_map()) as adapter:
            snapshots = list(adapter.read_stock(self.store_ids[:3], AT))
        self.assertEqual(len(self.balance_calls()), 1)
        self.assertEqual([s.ok for s in snapshots], [False, False, False])
        self.assertEqual({s.errors[0].code for s in snapshots}, {"endpoint_not_found"})

    def test_two_timeouts_in_a_row_end_the_fallback(self):
        wanted = self.store_ids[:5]
        answers = {
            (tuple(wanted),): IikoError(ErrorKind.SERVER_ERROR, "x"),
            ((wanted[0],),): IikoError(ErrorKind.TIMEOUT, "x"),
            ((wanted[1],),): ({wanted[1]: []}, 0),
            ((wanted[2],),): IikoError(ErrorKind.TIMEOUT, "x"),
            ((wanted[3],),): IikoError(ErrorKind.TIMEOUT, "x"),
        }

        def balances(chunk, timestamp):
            answer = answers[(tuple(chunk),)]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with self.open(product_units=self.unit_map()) as adapter:
            with mock.patch.object(adapter, "_balances", side_effect=balances) as patched:
                snapshots = list(adapter.read_stock(wanted, AT))

        # One timeout does not stop the fallback; the second one in a row does, without asking for the last store.
        self.assertEqual([call.args[0] for call in patched.call_args_list], [wanted, *([s] for s in wanted[:4])])
        self.assertEqual([s.external_warehouse_id for s in snapshots], wanted)
        self.assertEqual([s.ok for s in snapshots], [False, True, False, False, False])
        self.assertEqual(snapshots[4].errors[0].code, "timeout")


class IikoSessionTests(IikoAdapterTestBase):
    def test_auth_error_is_not_retryable(self):
        with self.assertRaises(AdapterError) as caught:
            with IikoAdapter.for_connection(self.make_connection(password="wrong-password")):
                self.fail("the body must not run")

        error = caught.exception
        self.assertEqual((error.code, error.retryable, error.retry_after), ("auth_error", False, None))
        self.assertNotIn(LOGIN, error.message)
        self.assertNotIn(PASS_HASH, error.message)
        self.assertEqual(len(self.server.calls(AUTH)), 1)  # never re-sent: it could lock the account
        self.assertEqual(self.server.calls(LOGOUT), [])  # no token, nothing to log out

        for kind in (ErrorKind.AUTH_ERROR, ErrorKind.FORBIDDEN):
            with self.subTest(kind=kind):
                mapped = map_error(IikoError(kind, "x", 403))
                self.assertEqual((mapped.code, mapped.retryable), (kind.value.lower(), False))
                self.assertTrue(mapped.message)

    def test_adapter_exits_with_logout_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.open() as adapter:
                adapter.list_departments()
                raise RuntimeError("boom")

        (logout,) = self.server.calls(LOGOUT)
        (token,) = self.server.issued_tokens
        self.assertEqual(logout.param("key"), token)
        self.assertNotIn(token, self.server.tokens)  # the licence slot is free again

    def test_using_the_adapter_after_the_block_is_a_programming_error(self):
        with self.open(product_units=self.unit_map()) as adapter:
            pending = adapter.read_stock(self.store_ids[:1], AT)  # a generator: nothing is sent yet
        with self.assertRaises(RuntimeError) as caught:
            list(pending)
        self.assertNotIn("логин", str(caught.exception))  # not disguised as a credentials problem
        self.assertEqual(self.balance_calls(), [])

    def test_login_failure_report_holds_no_credentials(self):
        connection = self.make_connection(password="wrong-password")
        wrong_hash = hashlib.sha1(b"wrong-password").hexdigest()
        report = error_report(lambda: IikoAdapter.for_connection(connection).__enter__(), AdapterError)
        self.assertIn("_open_session", report)  # the frame that held the credentials is in the report
        for secret in (LOGIN, wrong_hash):
            self.assertNotIn(secret, report)

    def test_missing_credentials_give_a_clear_error(self):
        with self.assertRaises(AdapterError) as caught:
            with IikoAdapter.for_connection(self.make_connection(password=None)):
                self.fail("the body must not run")
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("credentials_missing", False))
        self.assertEqual(self.server.requests, [])

    def test_get_adapter_gives_a_logged_in_iiko_adapter(self):
        with get_adapter(self.make_connection()) as adapter:
            self.assertIsInstance(adapter, IikoAdapter)
            self.assertEqual(adapter.provider, "iiko")
            self.assertEqual(len(adapter.list_warehouses()), len(self.data.stores))
        self.assertEqual(len(self.server.calls(LOGOUT)), 1)

    def test_capabilities_follow_the_capability_matrix(self):
        caps = IIKO_CAPABILITIES
        self.assertTrue(
            all((caps.read_departments, caps.read_warehouses, caps.read_products, caps.read_stock))
            and caps.read_consumption
        )
        self.assertIs(caps.omits_zero_balances, True)
        self.assertIs(caps.write_receipt, False)
        self.assertEqual(caps.source_version, "")  # unknown until a session asks the server
        self.assertTrue(caps.notes)

        with self.open() as adapter:
            first = adapter.capabilities()
            second = adapter.capabilities()
        self.assertEqual(first.source_version, "9.4.8049.0")
        self.assertEqual(first, second)
        self.assertEqual(len(self.server.calls(SERVER_INFO)), 1)  # asked once per session

    def test_check_access_lists_steps_without_secrets(self):
        with self.open() as adapter:
            report = adapter.check_access()

        self.assertTrue(report.ok)
        self.assertEqual(
            [(s.name, s.status) for s in report.steps],
            [
                ("login", "ok"),
                ("server_info", "ok"),
                ("departments", "ok"),
                ("stores", "ok"),
                ("products", "ok"),
                ("balances", "ok"),
            ],
        )
        self.assertEqual((report.error_code, report.hint), (None, None))
        (token,) = self.server.issued_tokens
        text = repr(report)
        for secret in (LOGIN, PASS_HASH, token):
            self.assertNotIn(secret, text)

    def test_check_access_failure_is_reported_not_raised(self):
        # "norights" logs in but may not read the product catalogue (403 Permission denied).
        with self.open(login="norights") as adapter:
            report = adapter.check_access()

        self.assertFalse(report.ok)
        self.assertEqual(report.error_code, "forbidden")
        self.assertIn("B_EN", report.hint)
        statuses = {s.name: s.status for s in report.steps}
        self.assertEqual((statuses["stores"], statuses["products"], statuses["balances"]), ("ok", "failed", "skipped"))

    def test_check_connection_access_reports_login_failure(self):
        report = check_connection_access(self.make_connection(password="wrong-password"))

        self.assertFalse(report.ok)
        self.assertEqual(report.error_code, "auth_error")
        self.assertEqual([(s.name, s.status) for s in report.steps], [("login", "failed")])
        self.assertIn("логин", report.hint)
        self.assertNotIn(PASS_HASH, repr(report))


@override_settings(TIME_ZONE="Europe/Moscow")
class IikoConsumptionTests(IikoAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.kitchen, self.north = self.data.stores[0], self.data.stores[3]
        self.flour, self.milk = self.data.products[0], self.data.products[1]

    def row(self, store: dict, product: dict, transaction_type: str, quantity, unit: str = "кг") -> dict:
        """A warehouse-account row as the test server shows it (Step 0, 23.10): ``quantity`` leaving the warehouse
        is a NEGATIVE Amount with the same value in Amount.Out; a negative ``quantity`` is an inflow."""
        return {
            "Account.Name": store["name"],
            "Product.Num": product["num"],
            "Product.Name": product["name"],
            "Product.MeasureUnit": unit,
            "TransactionType": transaction_type,
            "Amount": -quantity,
            "Amount.In": max(-quantity, 0),
            "Amount.Out": max(quantity, 0),
        }

    def counter_row(self, product: dict, transaction_type: str, account: str, money) -> dict:
        """The other side of the same posting: a money amount on an income/expense account, no In/Out."""
        return {
            "Account.Name": account,
            "Product.Num": product["num"],
            "Product.Name": product["name"],
            "Product.MeasureUnit": "",
            "TransactionType": transaction_type,
            "Amount": money,
            "Amount.In": 0,
            "Amount.Out": 0,
        }

    def olap_bodies(self) -> list[dict]:
        return [json.loads(call.body) for call in self.server.calls(OLAP_PATH, method="POST")]

    def test_read_consumption_sends_one_post_per_warehouse_and_day(self):
        kitchen, north = self.kitchen, self.north
        self.server.olap_rows = {
            (kitchen["name"], "2026-10-20"): [
                self.row(kitchen, self.flour, "SESSION_WRITEOFF", 1.5),
                self.row(kitchen, self.milk, "INVOICE", 10, unit="л"),
                self.row(kitchen, self.flour, "SALES_REVENUE", 2),
            ],
            (north["name"], "2026-10-21"): [self.row(north, self.milk, "WRITEOFF", -0.25, unit="л")],
        }
        with self.open() as adapter:
            result = adapter.read_consumption([kitchen["id"], north["id"]], D1, D2)

        bodies = self.olap_bodies()
        self.assertEqual(
            [(b["filters"]["Store"]["values"], b["filters"]["DateTime.DateTyped"]["from"]) for b in bodies],
            [
                ([kitchen["name"]], "2026-10-20T00:00:00.000"),
                ([north["name"]], "2026-10-20T00:00:00.000"),
                ([kitchen["name"]], "2026-10-21T00:00:00.000"),
                ([north["name"]], "2026-10-21T00:00:00.000"),
            ],
        )
        for body in bodies:
            self.assertEqual(body["reportType"], "TRANSACTIONS")
            self.assertEqual(set(body["filters"]), {"DateTime.DateTyped", "Store"})  # one day, one store
            self.assertEqual(body["filters"]["Store"]["filterType"], "IncludeValues")
            self.assertEqual(len(body["filters"]["Store"]["values"]), 1)
            day_filter = body["filters"]["DateTime.DateTyped"]
            next_day = date.fromisoformat(day_filter["from"][:10]) + timedelta(days=1)
            self.assertEqual(day_filter["to"], f"{next_day.isoformat()}T00:00:00.000")
        self.assertEqual(len(self.server.calls(OLAP_COLUMNS)), 1)

        self.assertEqual(
            result.covered,
            {(kitchen["id"], D1): True, (north["id"], D1): True, (kitchen["id"], D2): True, (north["id"], D2): True},
        )
        moscow = ZoneInfo("Europe/Moscow")
        events = {
            (
                e.external_product_id,
                e.external_warehouse_id,
                e.occurred_at,
                e.qty,
                e.source_uom,
                e.transaction_type,
                e.kind,
            )
            for e in result.events
        }
        self.assertEqual(
            events,
            {
                (self.flour["id"], kitchen["id"], datetime(2026, 10, 20, tzinfo=moscow), Decimal("1.5"), "кг",
                 "SESSION_WRITEOFF", OperationKind.CONSUMPTION),
                (self.milk["id"], kitchen["id"], datetime(2026, 10, 20, tzinfo=moscow), Decimal("10"), "л",
                 "INVOICE", OperationKind.RECEIPT),
                (self.flour["id"], kitchen["id"], datetime(2026, 10, 20, tzinfo=moscow), Decimal("2"), "кг",
                 "SALES_REVENUE", OperationKind.UNKNOWN),
                (self.milk["id"], north["id"], datetime(2026, 10, 21, tzinfo=moscow), Decimal("-0.25"), "л",
                 "WRITEOFF", OperationKind.CONSUMPTION),
            },
        )  # fmt: skip
        self.assertTrue(all(isinstance(e.qty, Decimal) for e in result.events))

    def test_transaction_types_keep_their_name_and_get_a_kind(self):
        self.assertEqual(operation_kind("INVOICE"), OperationKind.RECEIPT)
        for consumption in ("SESSION_WRITEOFF", "WRITEOFF"):
            self.assertEqual(operation_kind(consumption), OperationKind.CONSUMPTION)
        for other in ("SALES_REVENUE", "DISCOUNT", "DISASSEMBLE", "SOMETHING_NEW"):
            self.assertEqual(operation_kind(other), OperationKind.UNKNOWN)

    @override_settings(ACCOUNTING_OLAP_POSTS_PER_SESSION=2)
    def test_olap_post_cap_leaves_remaining_pairs_uncovered(self):
        store_id = self.kitchen["id"]
        with self.open() as adapter:
            result = adapter.read_consumption([store_id], D1, D3)

        self.assertEqual(len(self.olap_bodies()), 2)
        self.assertEqual(result.covered, {(store_id, D1): True, (store_id, D2): True, (store_id, D3): False})
        # Stopping early is told apart from a failed day: the rest can be read in the next session.
        self.assertEqual((result.stopped.code, result.stopped.retryable), ("olap_post_limit", True))

    @override_settings(ACCOUNTING_OLAP_POSTS_PER_SESSION=2)
    def test_cap_reached_exactly_on_the_last_pair_is_not_a_stop(self):
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D2)
        self.assertTrue(all(result.covered.values()))
        self.assertIsNone(result.stopped)

    def test_rate_limited_olap_stops_with_retry_after(self):
        self.server.set_override(OLAP_PATH, status=429, body="Too many requests", headers={"Retry-After": "60"})
        store_id = self.kitchen["id"]
        with self.open() as adapter:
            result = adapter.read_consumption([store_id], D1, D3)

        self.assertEqual(len(self.server.calls(OLAP_PATH, method="POST")), 1)  # no more reports after a 429
        self.assertFalse(any(result.covered.values()))
        stopped = result.stopped
        self.assertEqual((stopped.code, stopped.retryable, stopped.retry_after), ("rate_limited", True, 60.0))

    def test_request_budget_running_out_stops_the_read(self):
        # auth, OLAP columns, stores, products, units = 5; one POST fits, the next would eat the logout reserve.
        self.client_overrides["max_requests"] = 7
        store_id = self.kitchen["id"]
        with self.open() as adapter:
            result = adapter.read_consumption([store_id], D1, D3)

        self.assertEqual(result.covered, {(store_id, D1): True, (store_id, D2): False, (store_id, D3): False})
        self.assertEqual((result.stopped.code, result.stopped.retryable), ("request_limit_exceeded", True))
        self.assertEqual(len(self.server.calls(LOGOUT)), 1)

    def test_only_the_warehouse_side_of_a_posting_counts(self):
        # Step 0 (23.10): every posting is two rows; the quantity is on the warehouse account, the counter
        # account carries money. Summing both sides (or reading the money as a quantity) would be wrong.
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [
                self.row(self.kitchen, self.flour, "WRITEOFF", 1),
                self.counter_row(self.flour, "WRITEOFF", "Порча", 187.4),
                self.row(self.kitchen, self.milk, "SESSION_WRITEOFF", 2),
                self.counter_row(self.milk, "SESSION_WRITEOFF", "Расход продуктов", 827.25),
            ]
        }
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)

        self.assertEqual(result.covered, {(self.kitchen["id"], D1): True})
        self.assertEqual(result.partial, frozenset())
        self.assertIsNone(result.stopped)
        self.assertEqual(
            {(e.external_product_id, e.transaction_type, e.qty) for e in result.events},
            {(self.flour["id"], "WRITEOFF", Decimal("1")), (self.milk["id"], "SESSION_WRITEOFF", Decimal("2"))},
        )

    def test_quantity_is_the_negated_amount_and_an_inflow_is_negative(self):
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [
                self.row(self.kitchen, self.flour, "SESSION_WRITEOFF", 0.125),
                self.row(self.kitchen, self.milk, "INVOICE", -10, unit="л"),
            ]
        }
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)

        by_type = {e.transaction_type: e.qty for e in result.events}
        self.assertEqual(by_type, {"SESSION_WRITEOFF": Decimal("0.125"), "INVOICE": Decimal("-10")})

    def test_warehouse_account_is_matched_ignoring_case_and_spaces(self):
        rows = [self.row(self.kitchen, self.flour, "WRITEOFF", 1)]
        rows[0]["Account.Name"] = "  " + self.kitchen["name"].upper() + " "
        self.server.olap_rows = {(self.kitchen["name"], "2026-10-20"): rows}
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)
        self.assertEqual([e.qty for e in result.events], [Decimal("1")])

    def test_a_day_without_any_warehouse_row_is_still_a_covered_zero(self):
        # Only counter-account rows came back: the warehouse itself consumed nothing that day.
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [self.counter_row(self.flour, "SALES_REVENUE", "Выручка", -50)]
        }
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)
        self.assertEqual((result.events, result.covered), ([], {(self.kitchen["id"], D1): True}))

    def test_missing_olap_body_is_an_explicit_error(self):
        real_builder = iiko_module.build_transactions_body
        answers = iter([True, False])  # the capability probe gets a body, the first day does not

        def builder(*args, **kwargs):
            return real_builder(*args, **kwargs) if next(answers) else None

        with mock.patch.object(iiko_module, "build_transactions_body", side_effect=builder):
            with self.open() as adapter:
                with self.assertRaises(AdapterError) as caught:
                    adapter.read_consumption([self.kitchen["id"]], D1, D1)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("schema_mismatch", False))
        self.assertEqual(self.olap_bodies(), [])

    def test_failed_day_is_uncovered_and_other_days_still_read(self):
        store_id = self.kitchen["id"]
        self.server.olap_failures = {"2026-10-21": 500}
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-22"): [self.row(self.kitchen, self.flour, "WRITEOFF", 1)]
        }
        with self.open() as adapter:
            result = adapter.read_consumption([store_id], D1, D3)

        self.assertEqual(result.covered, {(store_id, D1): True, (store_id, D2): False, (store_id, D3): True})
        # Each day was asked exactly once: the 500 was not re-sent.
        days = [body["filters"]["DateTime.DateTyped"]["from"][:10] for body in self.olap_bodies()]
        self.assertEqual(days, ["2026-10-20", "2026-10-21", "2026-10-22"])
        self.assertEqual([(e.transaction_type, e.qty) for e in result.events], [("WRITEOFF", Decimal("1"))])
        self.assertIsNone(result.stopped)  # a failed day is not a stop: the read went on

    def test_timeout_stops_the_read_and_keeps_the_days_already_read(self):
        store_id = self.kitchen["id"]
        self.client_overrides["timeout"] = 0.5
        self.server.olap_delays = {"2026-10-21": 1.5}  # longer than the client timeout
        with self.open() as adapter:
            result = adapter.read_consumption([store_id], D1, D3)

        self.assertEqual(result.covered, {(store_id, D1): True, (store_id, D2): False, (store_id, D3): False})
        days = [body["filters"]["DateTime.DateTyped"]["from"][:10] for body in self.olap_bodies()]
        self.assertEqual(days, ["2026-10-20", "2026-10-21"])  # not re-sent, and no further reports
        self.assertEqual((result.stopped.code, result.stopped.retryable), ("timeout", True))

    def test_transport_failures_stop_the_read(self):
        store_id = self.kitchen["id"]
        for kind, retryable in (
            (ErrorKind.CONNECTION_ERROR, True),
            (ErrorKind.DNS_ERROR, True),
            (ErrorKind.TLS_ERROR, False),
        ):
            with self.subTest(kind=kind), self.open() as adapter:
                real_post = adapter._session.post_olap_report
                calls = []

                def post(body, *, purpose, kind=kind, real_post=real_post, calls=calls):
                    calls.append(body)
                    if len(calls) == 1:
                        return real_post(body, purpose=purpose)
                    raise IikoError(kind, "x")

                with mock.patch.object(adapter._session, "post_olap_report", side_effect=post):
                    result = adapter.read_consumption([store_id], D1, D3)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result.covered, {(store_id, D1): True, (store_id, D2): False, (store_id, D3): False})
                self.assertEqual((result.stopped.code, result.stopped.retryable), (kind.value.lower(), retryable))

    def test_forbidden_olap_stops_the_read(self):
        self.server.olap_failures = {"2026-10-20": 403}
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D2)
        self.assertFalse(any(result.covered.values()))
        self.assertEqual((result.stopped.code, result.stopped.retryable), ("forbidden", False))
        self.assertEqual(len(self.olap_bodies()), 1)

    def test_auth_error_mid_read_keeps_the_days_already_read(self):
        store_id = self.kitchen["id"]
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [self.row(self.kitchen, self.flour, "WRITEOFF", 1)]
        }
        self.server.olap_failures = {"2026-10-21": 401}
        with self.open() as adapter:
            result = adapter.read_consumption([store_id], D1, D3)

        self.assertEqual(result.covered, {(store_id, D1): True, (store_id, D2): False, (store_id, D3): False})
        self.assertEqual([e.qty for e in result.events], [Decimal("1")])
        self.assertEqual((result.stopped.code, result.stopped.retryable), ("auth_error", False))
        self.assertEqual(len(self.olap_bodies()), 2)

    def test_rejected_olap_body_stops_without_a_retry_flag(self):
        rejected = IikoError(ErrorKind.READ_ONLY_VIOLATION, "Запрос OLAP не разрешён")
        with mock.patch("accounting.iiko_client.client.validate_olap_body", side_effect=rejected):
            with self.open() as adapter:
                result = adapter.read_consumption([self.kitchen["id"]], D1, D2)
        self.assertEqual(self.olap_bodies(), [])
        self.assertFalse(any(result.covered.values()))
        # A guard refusal is a bug, not "come back later": only 429 and the request budget are retryable stops.
        self.assertEqual((result.stopped.code, result.stopped.retryable), ("read_only_violation", False))

    def test_ambiguous_or_unknown_store_is_not_requested(self):
        twin_a, twin_b = "00000000-0000-4000-8000-0000000000a1", "00000000-0000-4000-8000-0000000000a2"
        xml = "".join(
            f"<corporateItemDto><id>{store_id}</id><parentId>d</parentId><name>Склад Общий</name>"
            "<type>STORE</type></corporateItemDto>"
            for store_id in (twin_a, twin_b)
        )
        self.server.set_override(
            STORES, body=f"<corporateItemDtoes>{xml}</corporateItemDtoes>", content_type="application/xml"
        )
        with self.open() as adapter:
            result = adapter.read_consumption([twin_a, "not-a-store"], D1, D1)

        # The store filter takes a name; two stores share it, so a report could not be split between them.
        self.assertEqual(result.covered, {(twin_a, D1): False, ("not-a-store", D1): False})
        self.assertEqual(self.olap_bodies(), [])
        log = "\n".join(self.log.messages)
        self.assertIn(twin_a, log)
        self.assertIn("not-a-store", log)

    def test_unresolved_product_makes_the_day_partial_not_failed(self):
        unknown = {"num": "99999", "name": "нет в каталоге"}
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [
                self.row(self.kitchen, self.flour, "WRITEOFF", 1),
                self.row(self.kitchen, unknown, "WRITEOFF", 3),
            ]
        }
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)

        # Read as a whole but not every row could be stored: "partial" (known incomplete), not "failed" or "not asked".
        self.assertEqual(result.covered, {(self.kitchen["id"], D1): False})
        self.assertEqual(result.partial, frozenset({(self.kitchen["id"], D1)}))
        self.assertEqual([e.external_product_id for e in result.events], [self.flour["id"]])

    def test_unresolved_product_of_an_excluded_type_does_not_make_the_day_partial(self):
        # Receipts, revenue and discounts are not stored anyway (ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES).
        unknown = {"num": "99999", "name": "нет в каталоге"}
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [
                self.row(self.kitchen, self.flour, "WRITEOFF", 1),
                self.row(self.kitchen, unknown, "INVOICE", -5),
            ]
        }
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)
        self.assertEqual((result.covered, result.partial), ({(self.kitchen["id"], D1): True}, frozenset()))

    def test_failed_and_unreached_pairs_are_not_partial(self):
        self.server.olap_failures = {"2026-10-21": 500}
        with self.open() as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D2)
        self.assertEqual(result.covered, {(self.kitchen["id"], D1): True, (self.kitchen["id"], D2): False})
        self.assertEqual(result.partial, frozenset())

    def test_an_injected_product_catalogue_avoids_the_product_downloads(self):
        catalogue = {self.flour["num"]: (self.flour["id"], "кг"), self.milk["num"]: (self.milk["id"], "л")}
        self.server.olap_rows = {
            (self.kitchen["name"], "2026-10-20"): [self.row(self.kitchen, self.flour, "WRITEOFF", 2)]
        }
        with self.open(product_catalog=catalogue) as adapter:
            result = adapter.read_consumption([self.kitchen["id"]], D1, D1)

        self.assertEqual([(e.external_product_id, e.qty) for e in result.events], [(self.flour["id"], Decimal("2"))])
        self.assertEqual(self.server.calls(PRODUCTS), [])
        self.assertEqual(self.server.calls(UNITS), [])

    def test_reversed_range_is_rejected(self):
        with self.open() as adapter:
            with self.assertRaises(ValueError):
                adapter.read_consumption([self.kitchen["id"]], D2, D1)
        self.assertEqual(self.olap_bodies(), [])


class IikoStockHardeningTests(IikoAdapterTestBase):
    def test_injected_unit_map_avoids_catalogue_downloads(self):
        store = self.store_ids[0]
        units = self.unit_map()
        missing = self.data.balances[store][0]["product"]
        del units[missing]
        with self.open(product_units=units, source_version="9.4.1") as adapter:
            (snapshot,) = adapter.read_stock([store], AT)
            self.assertEqual(adapter.capabilities().source_version, "9.4.1")

        # Units come from the stored catalogue (Task 10 builds the map from AccountingProduct): no products or
        # units download; only the small stores list, which tells a deleted store from an empty one.
        self.assertEqual([r.path for r in self.server.requests], [AUTH, STORES, BALANCES, LOGOUT])
        self.assertTrue(snapshot.ok)
        self.assertEqual(snapshot.source_version, "9.4.1")
        items = {item.external_product_id: item for item in snapshot.items}
        self.assertEqual((items[missing].reliability, items[missing].source_uom), (Reliability.UNIT_UNKNOWN, ""))
        for product_id, item in items.items():
            if product_id != missing:
                self.assertEqual((item.reliability, item.source_uom), (Reliability.OK, units[product_id]))

    def test_injected_unit_map_without_version_does_not_ask_the_server(self):
        with self.open(product_units=self.unit_map()) as adapter:
            (snapshot,) = adapter.read_stock(self.store_ids[:1], AT)
        self.assertEqual(snapshot.source_version, "")
        self.assertEqual([r.path for r in self.server.requests], [AUTH, STORES, BALANCES, LOGOUT])

    def test_get_adapter_passes_adapter_options_through(self):
        with get_adapter(self.make_connection(), product_units=self.unit_map()) as adapter:
            list(adapter.read_stock(self.store_ids[:2], AT))
        self.assertEqual([r.path for r in self.server.requests], [AUTH, STORES, BALANCES, LOGOUT])

    def test_all_amounts_unusable_is_a_schema_mismatch(self):
        store = self.store_ids[0]
        rows = [{"store": store, "product": p["id"], "amount": None} for p in self.data.products[:3]]
        body = json.dumps(rows).encode()
        with self.assertRaises(IikoError) as caught:
            parse_balances("application/json", body)
        self.assertIs(caught.exception.kind, ErrorKind.SCHEMA_MISMATCH)
        # One unusable row among good ones is still kept as None (see the zero/negative test).
        mixed = json.dumps([*rows, {"store": store, "product": "x", "amount": 1}]).encode()
        self.assertEqual(len(parse_balances("application/json", mixed).rows), 4)

        self.server.set_override(BALANCES, body=body, content_type="application/json")
        with self.open(product_units=self.unit_map()) as adapter:
            (snapshot,) = adapter.read_stock([store], AT)
        # A changed schema must not overwrite every level with "unknown": the warehouse fails instead.
        self.assertFalse(snapshot.ok)
        self.assertEqual(snapshot.errors[0].code, "schema_mismatch")

    def test_connection_level_errors_fail_the_chunk_without_fallback(self):
        wanted = self.store_ids[:3]
        no_fallback = (
            ErrorKind.CONNECTION_ERROR,
            ErrorKind.DNS_ERROR,
            ErrorKind.TLS_ERROR,
            ErrorKind.CONFIG_ERROR,
            ErrorKind.ENDPOINT_NOT_FOUND,
            ErrorKind.API_NOT_SUPPORTED,
            ErrorKind.MISSING_PARAMETERS,
            ErrorKind.READ_ONLY_VIOLATION,
        )
        with_fallback = (
            ErrorKind.SERVER_ERROR,
            ErrorKind.TIMEOUT,
            ErrorKind.RESPONSE_TOO_LARGE,
            ErrorKind.SCHEMA_MISMATCH,
        )
        with self.open(product_units=self.unit_map()) as adapter:
            for kind in (*no_fallback, *with_fallback):
                with (
                    self.subTest(kind=kind),
                    mock.patch.object(adapter, "_balances", side_effect=IikoError(kind, "x")) as balances,
                ):
                    snapshots = list(adapter.read_stock(wanted, AT))
                    self.assertEqual([s.external_warehouse_id for s in snapshots], wanted)
                    self.assertFalse(any(s.ok for s in snapshots))
                    self.assertEqual({s.errors[0].code for s in snapshots}, {kind.value.lower()})
                    # TIMEOUT: the fallback ends after two timeouts in a row, the third store is not asked.
                    expected = 1 if kind in no_fallback else (3 if kind is ErrorKind.TIMEOUT else 1 + len(wanted))
                    self.assertEqual(balances.call_count, expected)

    def test_group_failure_degrades_to_empty_group_names(self):
        self.server.set_override(GROUPS, status=500, body="Internal error")
        with self.open() as adapter:
            products = adapter.list_products()
        self.assertEqual(len(products), len(self.data.products))
        self.assertEqual({p.group_name for p in products}, {""})
        self.assertTrue(all(p.unit_name for p in products))
        self.assertTrue(any("групп" in message for message in self.log.messages))

        for status, code in ((403, "forbidden"), (401, "auth_error"), (429, "rate_limited")):
            with self.subTest(status=status):
                self.server.set_override(GROUPS, status=status, body="Denied", headers={"Retry-After": "120"})
                with self.open() as adapter, self.assertRaises(AdapterError) as caught:
                    adapter.list_products()
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.retry_after, 120.0)

        # The request budget running out at the groups call is not a "groups failed": it is raised too.
        self.server.overrides.pop(GROUPS)
        self.client_overrides["max_requests"] = 4  # auth, products, units; groups would eat the logout reserve
        with self.open() as adapter, self.assertRaises(AdapterError) as caught:
            adapter.list_products()
        self.assertEqual(caught.exception.code, "request_limit_exceeded")

    def test_empty_warehouse_ids_make_no_request(self):
        with self.open() as adapter:
            self.assertEqual(list(adapter.read_stock([], AT)), [])
        self.assertEqual([r.path for r in self.server.requests], [AUTH, LOGOUT])

    def test_duplicate_ids_give_one_snapshot(self):
        first, second = self.store_ids[:2]
        with self.open(product_units=self.unit_map()) as adapter:
            snapshots = list(adapter.read_stock([first, first, second, first], AT))
        self.assertEqual([s.external_warehouse_id for s in snapshots], [first, second])
        self.assertEqual([c.params["store"] for c in self.balance_calls()], [[first, second]])

    def test_budget_running_out_mid_fallback_fails_the_rest_without_traffic(self):
        # auth, stores, the failing chunk and one single store fit; the last request is reserved for the logout.
        self.client_overrides["max_requests"] = 5
        self.server.balance_max_stores = 1
        wanted = self.store_ids[:3]
        with self.open(product_units=self.unit_map()) as adapter:
            snapshots = list(adapter.read_stock(wanted, AT))

        self.assertEqual([s.ok for s in snapshots], [True, False, False])
        self.assertEqual({s.errors[0].code for s in snapshots[1:]}, {"request_limit_exceeded"})
        self.assertEqual([r.path for r in self.server.requests], [AUTH, STORES, BALANCES, BALANCES, LOGOUT])

    def test_unreadable_stores_catalogue_aborts_without_asking_balances(self):
        broken = {
            "server error": {"status": 500, "body": "Internal error"},
            "broken xml": {"body": "<corporateItemDtoes><corporateItemDto><id>", "content_type": "application/xml"},
        }
        for label, override in broken.items():
            with self.subTest(label):
                self.server.reset()
                self.server.set_override(STORES, **override)
                with self.open(product_units=self.unit_map()) as adapter:
                    with self.assertRaises(AdapterError):
                        list(adapter.read_stock(self.store_ids[:2], AT))
                # Without the stores list a deleted store cannot be told from an empty one: nothing is read.
                self.assertEqual(self.balance_calls(), [])
                self.assertEqual(len(self.server.calls(LOGOUT)), 1)

    def test_rows_of_other_stores_with_none_for_a_requested_one_are_a_schema_mismatch(self):
        first, second = self.store_ids[:2]
        product = self.data.products[0]["id"]
        foreign = "00000000-0000-4000-8000-00000000f0f0"
        rows = [{"store": foreign, "product": product, "amount": 5, "sum": 50}]
        self.server.set_override(BALANCES, body=json.dumps(rows), content_type="application/json")
        with self.open(product_units=self.unit_map()) as adapter:
            snapshots = list(adapter.read_stock([first, second], AT))

        # An id format the adapter cannot match must not look like two empty warehouses (zero-fill would follow).
        self.assertEqual([s.ok for s in snapshots], [False, False])
        self.assertEqual({s.errors[0].code for s in snapshots}, {"schema_mismatch"})
        self.assertEqual([c.params["store"] for c in self.balance_calls()], [[first, second], [first], [second]])

    def test_rows_of_other_stores_are_ignored_when_every_requested_store_has_rows(self):
        first = self.store_ids[0]
        product = self.data.products[0]["id"]
        rows = [
            {"store": first, "product": product, "amount": 2, "sum": 20},
            {"store": "00000000-0000-4000-8000-00000000f0f0", "product": product, "amount": 5, "sum": 50},
        ]
        self.server.set_override(BALANCES, body=json.dumps(rows), content_type="application/json")
        with self.open(product_units=self.unit_map()) as adapter:
            (snapshot,) = adapter.read_stock([first], AT)

        self.assertTrue(snapshot.ok)
        self.assertEqual([(i.external_product_id, i.stock_qty) for i in snapshot.items], [(product, Decimal("2"))])
