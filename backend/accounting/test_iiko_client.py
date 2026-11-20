"""Read-only iiko client ported from iiko_diagnostics, exercised against the local fake iikoServer.

The fake runs on 127.0.0.1 (port 0) in a thread; nothing here talks to a real iiko server.
"""

import copy
import dataclasses
import hashlib
import inspect
import json
import logging
import pickle
import socket
import threading
from contextlib import ExitStack
from decimal import Decimal
from unittest import mock

import httpx
from django.test import SimpleTestCase, override_settings
from django.views.debug import ExceptionReporter

from accounting.iiko_client import auth as auth_module
from accounting.iiko_client.auth import IikoSession
from accounting.iiko_client.client import IikoHttpClient
from accounting.iiko_client.config import HARD_MAX_REQUESTS, OLAP_MAX_STORE_VALUES, Settings, settings_from_django
from accounting.iiko_client.endpoints import build_params, get_spec
from accounting.iiko_client.exceptions import ErrorKind, IikoError
from accounting.iiko_client.olap import (
    OLAP_REPORT_PATH,
    STORE_FILTER_FIELDS,
    build_transactions_body,
    validate_olap_body,
)
from accounting.iiko_client.parsers import parse_balances, parse_catalog
from accounting.iiko_client.redaction import mask_text, scoped_secrets
from accounting.test_support.fake_iiko_server import OLAP_PATH, FakeIikoServer

LOGIN = "iiko-api-tester"
# The plain password exists only here, to prove it never leaves the test: the client is given the hash.
PASSWORD = "Plain-Secret-Pw-42"
PASS_HASH = hashlib.sha1(PASSWORD.encode()).hexdigest()

STORES = "/resto/api/corporation/stores"
AUTH = "/resto/api/auth"
LOGOUT = "/resto/api/logout"
PRODUCTS = "/resto/api/v2/entities/products/list"

COLUMNS = {
    "Account.Name": {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True},
    "Product.Num": {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True},
    "Product.Name": {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True},
    "Product.MeasureUnit": {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True},
    "TransactionType": {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True},
    "Amount": {"groupingAllowed": False, "aggregationAllowed": True, "filteringAllowed": True},
    "Amount.In": {"groupingAllowed": False, "aggregationAllowed": True, "filteringAllowed": True},
    "Amount.Out": {"groupingAllowed": False, "aggregationAllowed": True, "filteringAllowed": True},
    "DateTime.DateTyped": {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True},
}
DAY_FROM = "2026-10-21T00:00:00.000"
DAY_TO = "2026-10-22T00:00:00.000"


def good_body() -> dict:
    body = build_transactions_body(
        COLUMNS, day_from=DAY_FROM, day_to=DAY_TO, store_field="Account.Name", store_value="Склад Бар"
    )
    assert body is not None
    return body


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IikoClientTestBase(SimpleTestCase):
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
        self.sleeps: list[float] = []
        self._clients: list[IikoHttpClient] = []

    def tearDown(self):
        for client in self._clients:
            client.close()

    def make_settings(self, **overrides) -> Settings:
        values = dict(
            base_url=self.server.base_url,
            login=LOGIN,
            pass_hash=PASS_HASH,
            timeout=2.0,
            request_delay=0.0,
            allow_http_loopback=True,
        )
        values.update(overrides)
        return Settings(**values)

    def make_client(self, settings: Settings | None = None) -> IikoHttpClient:
        client = IikoHttpClient(settings or self.make_settings(), sleep=self.sleeps.append)
        self._clients.append(client)
        return client

    def make_session(self, **overrides) -> tuple[IikoHttpClient, IikoSession]:
        settings = self.make_settings(**overrides)
        client = self.make_client(settings)
        return client, IikoSession(client, settings)


class IikoClientTests(IikoClientTestBase):
    def test_login_sends_pass_hash_only(self):
        client, session = self.make_session()
        with session:
            session.get("api/corporation/stores")

        (auth,) = self.server.calls(AUTH)
        self.assertEqual(auth.params, {"login": [LOGIN], "pass": [PASS_HASH]})
        for request in self.server.requests:
            raw = request.raw_path + json.dumps(request.headers) + request.body.decode("utf-8", "replace")
            self.assertNotIn(PASSWORD, raw)
        self.assertTrue(all(PASS_HASH not in record.url for record in client.records))

        # There is no way to hand the client a plain password: no field for it, no hashing helper,
        # and a value that is not a SHA-1 hex digest is refused before any request is made.
        self.assertNotIn("password", {f.name for f in dataclasses.fields(Settings)})
        with self.assertRaises(TypeError):
            Settings(base_url=self.server.base_url, login=LOGIN, password=PASSWORD)  # type: ignore[call-arg]
        self.assertFalse(hasattr(auth_module, "hash_password"))
        before = len(self.server.requests)
        with self.assertRaises(IikoError) as err:
            self.make_settings(pass_hash=PASSWORD)
        self.assertIs(err.exception.kind, ErrorKind.CONFIG_ERROR)
        self.assertNotIn(PASSWORD, str(err.exception))
        self.assertEqual(len(self.server.requests), before)

    def test_logout_called_when_work_raises(self):
        client, session = self.make_session()
        with self.assertRaises(RuntimeError):
            with session:
                session.get("api/corporation/stores")
                raise RuntimeError("boom")

        (logout,) = self.server.calls(LOGOUT)
        self.assertEqual(logout.param("key"), self.server.issued_tokens[-1])
        self.assertTrue(session.logout_done)
        self.assertFalse(session.is_open)
        self.assertFalse(client.is_open)  # the per-session httpx client is closed too
        self.assertEqual(self.server.tokens, {})  # the licence slot is released on the server

    def test_get_only_guard_rejects_post(self):
        client = self.make_client()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                with self.assertRaises(IikoError) as err:
                    client.request(method, "api/v2/entities/products/save")
                self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)
        self.assertEqual(self.server.requests, [])

        # Inside an open session the generic request() still refuses POST, even to the OLAP route.
        session_client, session = self.make_session()
        with session:
            with self.assertRaises(IikoError) as err:
                session_client.request("POST", OLAP_REPORT_PATH)
            self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)
        self.assertEqual([r.method for r in self.server.requests], ["GET", "GET"])  # auth + logout

    def test_post_is_possible_only_through_the_olap_whitelist(self):
        client, session = self.make_session()
        with session:
            result = session.post_olap_report(good_body(), purpose="test")
            token = self.server.issued_tokens[-1]
            bad = good_body()
            bad["reportType"] = "SALES"
            with self.assertRaises(IikoError) as err:
                session.post_olap_report(bad)
            self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)

        self.assertEqual(result.status_code, 200)
        non_get = [r for r in self.server.requests if r.method != "GET"]
        self.assertEqual(len(non_get), 1)  # the rejected body was never sent
        (post,) = non_get
        self.assertEqual((post.method, post.path), ("POST", OLAP_PATH))
        self.assertEqual(post.param("key"), token)
        self.assertEqual(json.loads(post.body)["reportType"], "TRANSACTIONS")
        post_record = next(r for r in client.records if r.method == "POST")
        self.assertNotIn(token, post_record.url)
        # The route is fixed: the whitelisted call takes no path, so it cannot be pointed anywhere else.
        self.assertNotIn("path", inspect.signature(IikoHttpClient.post_olap_report).parameters)
        self.assertNotIn("path", inspect.signature(IikoSession.post_olap_report).parameters)

    def test_olap_guard_rejects_balance_fields_long_periods_and_other_report_types(self):
        mutations = {
            "sales report": lambda b: b.update(reportType="SALES"),
            "stock report": lambda b: b.update(reportType="STOCK"),
            "start balance aggregate": lambda b: b["aggregateFields"].append("StartBalance.Amount"),
            "final balance grouping": lambda b: b["groupByRowFields"].append("FinalBalance.Money"),
            "balance filter": lambda b: b["filters"].update(
                {"FinalBalance.Amount": {"filterType": "IncludeValues", "values": ["1"]}}
            ),
            "no date filter": lambda b: b["filters"].pop("DateTime.DateTyped"),
            "four days": lambda b: b["filters"]["DateTime.DateTyped"].update(to="2026-10-25T00:00:00.000"),
            "one day and an hour": lambda b: b["filters"]["DateTime.DateTyped"].update(to="2026-10-22T01:00:00.000"),
            "empty period": lambda b: b["filters"]["DateTime.DateTyped"].update(to=DAY_FROM),
            "bad date": lambda b: b["filters"]["DateTime.DateTyped"].update(to="22.10.2026"),
            "two stores by default": lambda b: b["filters"]["Account.Name"].update(values=["Бар", "Кухня"]),
            "second filter": lambda b: b["filters"].update(
                {"Product.Name": {"filterType": "IncludeValues", "values": ["a"]}}
            ),
            "exclude filter": lambda b: b["filters"]["Account.Name"].update(filterType="ExcludeValues"),
            "unexpected key": lambda b: b.update(unexpected=True),
            "filters not an object": lambda b: b.update(filters=["x"]),
            "day_to before day_from": lambda b: b["filters"]["DateTime.DateTyped"].update(to="2026-10-20T00:00:00.000"),
            "lower-case start balance": lambda b: b["aggregateFields"].append("startbalance.amount"),
            "upper-case final balance": lambda b: b["groupByRowFields"].append("FINALBALANCE.Money"),
            # One day, one "store filter" — but on a field that is not a store, so it does not narrow the report.
            "non-store field as the store filter": lambda b: b["filters"].update(
                {"TransactionType": b["filters"].pop("Account.Name")}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                body = copy.deepcopy(good_body())
                mutate(body)
                with self.assertRaises(IikoError) as err:
                    validate_olap_body(body)
                self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)

        validate_olap_body(good_body())  # the builder never produces something the guard rejects
        self.assertEqual(STORE_FILTER_FIELDS, ("Account.Name", "Store", "Store.Name", "Store.Id"))
        for field_name in STORE_FILTER_FIELDS:
            with self.subTest(store_field=field_name):
                body = copy.deepcopy(good_body())
                body["filters"][field_name] = body["filters"].pop("Account.Name")
                validate_olap_body(body)
        columns = {**COLUMNS, "TransactionType": {**COLUMNS["TransactionType"], "filteringAllowed": True}}
        self.assertIsNone(
            build_transactions_body(
                columns, day_from=DAY_FROM, day_to=DAY_TO, store_field="TransactionType", store_value="x"
            )
        )
        several = copy.deepcopy(good_body())
        several["filters"]["Account.Name"]["values"] = ["Бар", "Кухня"]
        validate_olap_body(several, max_store_values=2)
        with self.assertRaises(IikoError):
            validate_olap_body(several, max_store_values=1)

        # The client applies the store limit from its settings and sends nothing on a violation; by default the
        # limit is one store, since a multi-store filter has not been verified on a real server.
        self.assertEqual(Settings(base_url="https://iiko.example.test/resto/").olap_max_store_values, 1)
        _, session = self.make_session()
        with session:
            with self.assertRaises(IikoError) as err:
                session.post_olap_report(several)
        self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)
        self.assertEqual(self.server.calls(OLAP_PATH), [])

    def test_olap_post_is_not_retried_after_a_timeout(self):
        self.server.set_override(OLAP_PATH, status=200, body="{}", delay=1.0)
        _, session = self.make_session(timeout=0.3)
        with session:
            with self.assertRaises(IikoError) as err:
                session.post_olap_report(good_body())
        self.assertIs(err.exception.kind, ErrorKind.TIMEOUT)
        self.assertIsNone(err.exception.__cause__)
        self.assertIsNone(err.exception.__context__)
        # A heavy report must not be sent twice, and it went out as the whitelisted POST.
        self.assertEqual([call.method for call in self.server.calls(OLAP_PATH)], ["POST"])
        self.assertEqual(len(self.server.calls(LOGOUT)), 1)

    def test_olap_post_limit_per_session(self):
        client, session = self.make_session(max_olap_posts=2)
        with session:
            session.post_olap_report(good_body())
            session.post_olap_report(good_body())
            with self.assertRaises(IikoError) as err:
                session.post_olap_report(good_body())
            self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)
        self.assertEqual(len(self.server.calls(OLAP_PATH)), 2)

        # The limit is per session: a new session (same client object, fresh httpx client) may post again.
        with IikoSession(client, client.settings) as again:
            again.post_olap_report(good_body())
        self.assertEqual(len(self.server.calls(OLAP_PATH)), 3)

    def test_no_cookie_or_token_shared_between_sessions(self):
        client, first = self.make_session()
        with first:
            first.get("api/corporation/stores")
        old_token = self.server.issued_tokens[-1]
        # The server did set the cookie and the first session's jar did send it back.
        (stores_call,) = self.server.calls(STORES)
        self.assertIn(f"key={old_token}", stores_call.headers.get("Cookie", ""))
        self.assertFalse(client.is_open)

        seen = len(self.server.requests)
        with IikoSession(client, client.settings) as second:  # same IikoHttpClient, new httpx client
            second.get("api/corporation/stores")
        other = self.make_client()
        with IikoSession(other, other.settings) as third:  # a different client object
            third.get("api/corporation/stores")

        later = self.server.requests[seen:]
        self.assertEqual(len(self.server.issued_tokens), 3)
        self.assertEqual(len(set(self.server.issued_tokens)), 3)
        for request in later:
            self.assertNotIn(old_token, request.raw_path)
            self.assertNotIn(old_token, request.headers.get("Cookie", ""))
        for auth in [r for r in later if r.path == AUTH]:
            self.assertNotIn("Cookie", auth.headers)

    def test_secrets_masked_in_logs_and_exceptions(self):
        def echo_secrets(request):
            return f"Permission denied for {LOGIN} pass={PASS_HASH} token {request.param('key')}"

        self.server.set_override(PRODUCTS, status=403, body=echo_secrets)
        errors: list[IikoError] = []
        client, session = self.make_session()
        with self.assertLogs(level="DEBUG") as logs:
            with session:
                session.get("api/corporation/stores", headers={"Accept": "application/xml"})
                result = session.get("api/v2/entities/products/list")
                with self.assertRaises(IikoError) as err:
                    result.raise_for_status("api/v2/entities/products/list")
                errors.append(err.exception)
                token = self.server.issued_tokens[-1]
                logging.getLogger("accounting.test").warning("leak attempt %s %s %s", LOGIN, PASS_HASH, token)
                # An error built by any caller inside the session is masked when created, not when shown.
                errors.append(IikoError(ErrorKind.INVALID_RESPONSE, f"raw {LOGIN} {PASS_HASH} {token}"))

            # A failed login echoing the credentials back is masked as well.
            self.server.set_override(AUTH, status=401, body=lambda r: f"Wrong credentials {r.param('login')}")
            _, failing = self.make_session()
            with self.assertRaises(IikoError) as err:
                with failing:
                    pass
            errors.append(err.exception)

        text = "\n".join(logs.output)
        self.assertTrue(any(":httpx:" in line for line in logs.output))  # transport logs were really captured
        self.assertTrue(any(":httpcore" in line for line in logs.output))
        # httpcore logs the auth response headers (Set-Cookie: key=<token>) before the token is known.
        self.assertTrue(any("Set-Cookie" in line for line in logs.output))
        for secret in (LOGIN, PASS_HASH, token, PASSWORD):
            self.assertNotIn(secret, text)
            for error in errors:
                for shown in (str(error), repr(error), error.message, error.endpoint or ""):
                    self.assertNotIn(secret, shown)
            for shown in (repr(session), repr(client), repr(client.settings), str(client.settings)):
                self.assertNotIn(secret, shown)
            self.assertTrue(all(secret not in record.url for record in client.records))
        self.assertIn("key=***", text)
        self.assertEqual(errors[0].kind, ErrorKind.FORBIDDEN)
        self.assertEqual(errors[-1].kind, ErrorKind.AUTH_ERROR)
        # Other session cookies in httpcore's header tuples are masked by header name, whatever the cookie.
        self.assertNotIn("abc123", mask_text("[(b'Set-Cookie', b'JSESSIONID=abc123; Path=/')]"))
        for error in errors:
            self.assertIsNone(error.__cause__)

    def test_secret_mask_is_scoped(self):
        logger = logging.getLogger("accounting.test.scope")
        _, session = self.make_session()
        with session:
            token = self.server.issued_tokens[-1]
            self.assertEqual(mask_text(f"{LOGIN} {PASS_HASH} {token}"), "*** *** ***")
            with self.assertLogs(logger, "WARNING") as inside:
                logger.warning("inside %s", token)
            self.assertNotIn(token, inside.output[0])

        self.assertEqual(mask_text(token), token)
        self.assertEqual(mask_text(LOGIN), LOGIN)
        with self.assertLogs(logger, "WARNING") as after:
            logger.warning("after %s", token)
        self.assertIn(token, after.output[0])

        # Scopes may end out of order (two sessions in one worker): each removes only its own values.
        with ExitStack() as outer:
            first = scoped_secrets("alpha-secret-1")
            second = scoped_secrets("beta-secret-2")
            first.__enter__()
            outer.enter_context(second)
            first.__exit__(None, None, None)
            self.assertEqual(mask_text("alpha-secret-1 beta-secret-2"), "alpha-secret-1 ***")
        self.assertEqual(mask_text("alpha-secret-1 beta-secret-2"), "alpha-secret-1 beta-secret-2")

    def test_error_kinds_map_status_codes(self):
        client = self.make_client()
        client.open()
        for status, kind in (
            (401, ErrorKind.AUTH_ERROR),
            (403, ErrorKind.FORBIDDEN),
            (404, ErrorKind.ENDPOINT_NOT_FOUND),
            (500, ErrorKind.SERVER_ERROR),
            (503, ErrorKind.SERVER_ERROR),
        ):
            with self.subTest(status=status):
                path = f"/resto/api/status{status}"
                self.server.set_override(path, status=status, body="error text")
                result = client.get(path.removeprefix("/resto/"))
                self.assertEqual(result.status_code, status)
                self.assertIs(result.error_kind, kind)
                with self.assertRaises(IikoError) as err:
                    result.raise_for_status()
                self.assertIs(err.exception.kind, kind)
                self.assertEqual(err.exception.status_code, status)
                self.assertEqual(len(self.server.calls(path)), 1)  # HTTP errors are never retried

        # 429 with a long Retry-After is not waited for; the value is carried on the result and the error.
        self.server.set_override("/resto/api/busy", status=429, headers={"Retry-After": "120"})
        result = client.get("api/busy")
        self.assertIs(result.error_kind, ErrorKind.RATE_LIMITED)
        self.assertEqual(result.retry_after, 120.0)
        with self.assertRaises(IikoError) as err:
            result.raise_for_status()
        self.assertIs(err.exception.kind, ErrorKind.RATE_LIMITED)
        self.assertEqual(err.exception.retry_after, 120.0)
        self.assertEqual(len(self.server.calls("/resto/api/busy")), 1)

        # A short Retry-After is honoured once (through the injected sleep), never in a loop.
        self.server.set_override("/resto/api/brief", status=429, headers={"Retry-After": "2"})
        with self.assertLogs("accounting.iiko_client.http", "WARNING"):
            self.assertIs(client.get("api/brief").error_kind, ErrorKind.RATE_LIMITED)
        self.assertEqual(len(self.server.calls("/resto/api/brief")), 2)
        self.assertIn(2.0, self.sleeps)

        # A read timeout means the request may have reached the server: TIMEOUT, not re-sent, nothing chained.
        self.server.set_override("/resto/api/slow", status=200, body="late", delay=1.0)
        slow = self.make_client(self.make_settings(timeout=0.3))
        slow.open()
        with self.assertRaises(IikoError) as err:
            slow.get("api/slow")
        self.assertIs(err.exception.kind, ErrorKind.TIMEOUT)
        self.assertIs(err.exception.request_sent, None)  # unknown whether the server got it
        self.assertIsNone(err.exception.__cause__)
        self.assertIsNone(err.exception.__context__)
        self.assertEqual(len(self.server.calls("/resto/api/slow")), 1)
        self.assertEqual(slow.request_count, 1)

        # A refused connection certainly never reached the server: retried once, as before.
        closed = self.make_client(self.make_settings(base_url=f"http://127.0.0.1:{free_port()}/resto/"))
        closed.open()
        with self.assertRaises(IikoError) as err, self.assertLogs("accounting.iiko_client.http", "WARNING"):
            closed.get("api/anything")
        self.assertIs(err.exception.kind, ErrorKind.CONNECTION_ERROR)
        self.assertIs(err.exception.request_sent, False)
        self.assertEqual(closed.request_count, 2)

    def test_request_budget_enforced(self):
        client, session = self.make_session(max_requests=5)
        with session:  # auth = request 1
            for _ in range(3):
                session.get("api/corporation/stores")
            with self.assertRaises(IikoError) as err:
                session.get("api/corporation/stores")  # the last request is reserved for logout
            self.assertIs(err.exception.kind, ErrorKind.REQUEST_LIMIT_EXCEEDED)
        self.assertEqual(len(self.server.calls(LOGOUT)), 1)
        self.assertEqual(client.request_count, 5)
        self.assertEqual(len(self.server.requests), 5)

        # The limit comes from the settings, with a hard ceiling of 100 (the prototype had 20).
        self.assertEqual(HARD_MAX_REQUESTS, 100)
        self.assertEqual(self.make_settings(max_requests=80).max_requests, 80)
        self.assertEqual(self.make_settings(max_requests=500).max_requests, 100)
        with self.assertRaises(IikoError) as err:
            self.make_settings(max_requests=2)
        self.assertIs(err.exception.kind, ErrorKind.CONFIG_ERROR)

    def test_license_error_surfaces_as_forbidden(self):
        client, session = self.make_session(login="nolicense")
        with self.assertRaises(IikoError) as err:
            with session:
                self.fail("the body must not run without a session")
        self.assertIs(err.exception.kind, ErrorKind.FORBIDDEN)
        self.assertEqual(err.exception.status_code, 403)
        self.assertIn("лицензионных подключений", err.exception.message)
        self.assertIn("no connections available", err.exception.message)
        self.assertEqual(len(self.server.calls(AUTH)), 1)  # never retried
        self.assertEqual(self.server.calls(LOGOUT), [])  # no token, nothing to log out
        self.assertFalse(session.is_open)
        self.assertFalse(client.is_open)


class IikoClientHardeningTests(IikoClientTestBase):
    """Review fix round 1: retries that could leak licence slots, lifecycle, headers, truncation, pickling."""

    def test_timed_out_auth_is_never_retried(self):
        # On a read timeout the server may already have issued a token; a retry would issue a second one and
        # only that one would be logged out, leaving the first holding a licence slot for about an hour.
        self.server.set_override(AUTH, status=200, body="11111111-2222-3333-4444-555555555555", delay=1.0)
        client, session = self.make_session(timeout=0.3)
        with self.assertRaises(IikoError) as err:
            with session:
                self.fail("the body must not run without a session")
        self.assertIs(err.exception.kind, ErrorKind.TIMEOUT)
        self.assertEqual(len(self.server.calls(AUTH)), 1)
        self.assertEqual(self.server.calls(LOGOUT), [])
        self.assertFalse(client.is_open)

        # Not even a connect timeout is retried for auth (a real one cannot be produced deterministically
        # on a local socket, so an in-process transport raises it); other calls still retry it once.
        seen: list[str] = []

        def connect_timeout(request):
            seen.append(request.url.path)
            raise httpx.ConnectTimeout("connect timed out", request=request)

        settings = self.make_settings()
        mocked = IikoHttpClient(settings, transport=httpx.MockTransport(connect_timeout), sleep=self.sleeps.append)
        self._clients.append(mocked)
        with self.assertRaises(IikoError) as err:
            IikoSession(mocked, settings).login()
        self.assertIs(err.exception.kind, ErrorKind.TIMEOUT)
        self.assertIs(err.exception.request_sent, False)
        self.assertEqual(seen, ["/resto/api/auth"])
        with mocked, self.assertLogs("accounting.iiko_client.http", "WARNING"):
            with self.assertRaises(IikoError):
                mocked.get("api/corporation/stores")
        self.assertEqual(seen[1:], ["/resto/api/corporation/stores"] * 2)

    def test_login_with_unrecognised_token_warns_and_is_not_retried(self):
        self.server.set_override(AUTH, status=200, body="<html>not a token</html>")
        _, session = self.make_session()
        with self.assertLogs("accounting.iiko_client.auth", "WARNING") as logs:
            with self.assertRaises(IikoError) as err:
                session.login()
        self.assertIs(err.exception.kind, ErrorKind.INVALID_RESPONSE)
        self.assertEqual(len(self.server.calls(AUTH)), 1)
        text = "\n".join(logs.output)
        self.assertIn("может остаться", text)  # a server-side session may remain
        self.assertNotIn("not a token", text)

    def test_bare_call_on_a_client_that_is_not_open_is_a_programming_error(self):
        client = self.make_client()
        with self.assertRaises(RuntimeError) as err:
            client.get("api/corporation/stores")
        self.assertIn("open", str(err.exception))
        with self.assertRaises(RuntimeError):
            client.post_olap_report([("key", "x")], good_body())
        self.assertEqual(self.server.requests, [])
        self.assertFalse(client.is_open)
        self.assertEqual(mask_text(LOGIN), LOGIN)  # no masking scope was left open either
        with client:
            self.assertEqual(client.get("get_server_info.jsp").status_code, 200)
        self.assertFalse(client.is_open)

    def test_closed_session_is_a_programming_error(self):
        _, session = self.make_session()
        with session:
            pass
        before = len(self.server.requests)
        for call in (lambda: session.get("api/corporation/stores"), lambda: session.post_olap_report(good_body())):
            with self.assertRaises(RuntimeError) as err:
                call()
            self.assertIn("IikoSession", str(err.exception))  # not "wrong login or password"
        self.assertEqual(len(self.server.requests), before)
        never_opened = self.make_session()[1]
        with self.assertRaises(RuntimeError):
            never_opened.get("api/corporation/stores")

    def test_only_the_accept_header_is_passed_through(self):
        _, session = self.make_session()
        with session:
            for name in ("Cookie", "X-HTTP-Method-Override", "Authorization"):
                with self.subTest(header=name):
                    with self.assertRaises(IikoError) as err:
                        session.get("api/corporation/stores", headers={name: "x"})
                    self.assertIs(err.exception.kind, ErrorKind.READ_ONLY_VIOLATION)
            session.get("api/corporation/stores", headers={"accept": "application/xml"})
        (stores,) = self.server.calls(STORES)
        sent = {name.lower(): value for name, value in stores.headers.items()}
        self.assertEqual(sent.get("accept"), "application/xml")
        self.assertNotIn("x-http-method-override", sent)

    def test_logout_failure_never_replaces_the_original_exception(self):
        client, session = self.make_session()
        real_get = client.get

        def get(path, *args, **kwargs):
            if path == auth_module.LOGOUT_PATH:
                raise KeyError("logout exploded with a secret-looking message")
            return real_get(path, *args, **kwargs)

        with mock.patch.object(client, "get", side_effect=get):
            with self.assertLogs("accounting.iiko_client.auth", "WARNING") as logs:
                with self.assertRaises(ValueError):
                    with session:
                        raise ValueError("body failed")
        text = "\n".join(logs.output)
        self.assertIn("KeyError", text)
        self.assertNotIn("secret-looking", text)
        self.assertFalse(session.logout_done)
        self.assertFalse(client.is_open)

    def test_truncated_response_raises_before_parsing(self):
        _, session = self.make_session(max_response_bytes=1024)
        with session:
            result = session.get("api/v2/entities/products/list")
            token = self.server.issued_tokens[-1]
            self.assertTrue(result.truncated)
            with self.assertRaises(IikoError) as err:
                result.raise_for_status("api/v2/entities/products/list")
        self.assertIs(err.exception.kind, ErrorKind.RESPONSE_TOO_LARGE)
        self.assertIn("ответ iiko превысил допустимый размер", err.exception.message.lower())
        for secret in (LOGIN, PASS_HASH, token):
            self.assertNotIn(secret, str(err.exception))

    def test_iiko_error_round_trips_through_pickle_masked(self):
        with scoped_secrets("pickle-secret-value"):
            error = IikoError(
                ErrorKind.RATE_LIMITED,
                "busy pickle-secret-value",
                429,
                endpoint="http://h/resto/api/x?key=pickle-secret-value",
                hints=("a",),
                retry_after=12.0,
            )
            error.request_sent = False
        restored = pickle.loads(pickle.dumps(error))
        self.assertIsInstance(restored, IikoError)
        self.assertEqual(
            (restored.kind, restored.message, restored.status_code, restored.endpoint, restored.hints),
            (error.kind, error.message, 429, error.endpoint, ("a",)),
        )
        self.assertEqual((restored.retry_after, restored.request_sent), (12.0, False))
        self.assertEqual(str(restored), str(error))
        self.assertNotIn("pickle-secret-value", str(restored) + (restored.endpoint or ""))

    def test_parallel_sessions_keep_each_others_masking(self):
        for first_to_exit in ("A", "B"):
            with self.subTest(first_to_exit=first_to_exit):
                self._run_parallel_sessions(first_to_exit)

    def _run_parallel_sessions(self, first_to_exit: str) -> None:
        logins = {"A": LOGIN, "B": "norights"}
        opened = {name: threading.Event() for name in logins}
        leave = {name: threading.Event() for name in logins}
        left = {name: threading.Event() for name in logins}
        tokens: dict[str, str] = {}
        failures: list[BaseException] = []

        def worker(name: str) -> None:
            try:
                settings = self.make_settings(login=logins[name])
                with IikoSession(IikoHttpClient(settings), settings) as session:
                    session.get("api/corporation/stores")
                    tokens[name] = session._token
                    opened[name].set()
                    if not leave[name].wait(5):
                        raise AssertionError("not released")
            except BaseException as exc:  # reported to the main thread
                failures.append(exc)
                opened[name].set()
            finally:
                left[name].set()

        threads = {name: threading.Thread(target=worker, args=(name,), daemon=True) for name in logins}
        for name, thread in threads.items():
            thread.start()
            self.assertTrue(opened[name].wait(5))
        self.assertEqual(failures, [])
        both = f"{logins['A']} {tokens['A']} {logins['B']} {tokens['B']} {PASS_HASH}"
        self.assertEqual(mask_text(both), "*** *** *** *** ***")

        first, second = (first_to_exit, "B" if first_to_exit == "A" else "A")
        leave[first].set()
        self.assertTrue(left[first].wait(5))
        # The second session's login and token stay masked, and so does the hash both sessions share.
        self.assertEqual(mask_text(f"{logins[second]} {tokens[second]} {PASS_HASH}"), "*** *** ***")
        self.assertEqual(mask_text(f"{logins[first]} {tokens[first]}"), f"{logins[first]} {tokens[first]}")
        logger = logging.getLogger("accounting.test.parallel")
        with self.assertLogs(logger, "WARNING") as logs:
            logger.warning("still open %s", tokens[second])
        self.assertNotIn(tokens[second], logs.output[0])

        leave[second].set()
        for thread in threads.values():
            thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(mask_text(f"{tokens[second]} {PASS_HASH}"), f"{tokens[second]} {PASS_HASH}")


class IikoClientSupportTests(IikoClientTestBase):
    """Settings, factory, registry and parsers around the client."""

    def test_settings_require_https_unless_loopback_is_explicitly_allowed(self):
        self.assertEqual(
            Settings(base_url="https://iiko.example.test/resto").base_url, "https://iiko.example.test/resto/"
        )
        for url in (
            "http://iiko.example.test/resto/",
            "http://127.0.0.1:8080/resto/",  # loopback over http only with the explicit test flag
            "https://user:pw@iiko.example.test/resto/",
            "https://iiko.example.test/resto/?key=1",
            "ftp://iiko.example.test/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(IikoError) as err:
                    Settings(base_url=url)
                self.assertIs(err.exception.kind, ErrorKind.CONFIG_ERROR)
                self.assertNotIn("pw@", str(err.exception))
        with self.assertRaises(IikoError):
            Settings(base_url="http://iiko.example.test/resto/", allow_http_loopback=True)
        self.assertEqual(self.make_settings(pass_hash=PASS_HASH.upper()).pass_hash, PASS_HASH)

    @override_settings(
        ACCOUNTING_IIKO_TIMEOUT=7,
        ACCOUNTING_IIKO_REQUEST_DELAY=0.25,
        ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION=40,
        ACCOUNTING_OLAP_POSTS_PER_SESSION=3,
        ACCOUNTING_STORES_PER_REQUEST=5,
    )
    def test_settings_from_django_uses_accounting_limits(self):
        settings = settings_from_django(base_url="https://iiko.example.test/resto/", login=LOGIN, pass_hash=PASS_HASH)
        self.assertEqual(
            (
                settings.timeout,
                settings.request_delay,
                settings.max_requests,
                settings.max_olap_posts,
                settings.olap_max_store_values,
            ),
            # One store per OLAP filter whatever ACCOUNTING_STORES_PER_REQUEST says (that is for balances).
            (7.0, 0.25, 40, 3, OLAP_MAX_STORE_VALUES),
        )
        self.assertEqual(OLAP_MAX_STORE_VALUES, 1)
        self.assertFalse(settings.allow_http_loopback)
        with override_settings(ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION=1000):
            self.assertEqual(
                settings_from_django(base_url="https://a.example.test/", login=LOGIN, pass_hash=PASS_HASH).max_requests,
                100,
            )

    def test_catalogues_and_balances_parse_from_the_fake(self):
        stores_spec, balances_spec = get_spec("stores"), get_spec("balances")
        with self.assertRaises(IikoError) as err:
            build_params(balances_spec, {})
        self.assertIs(err.exception.kind, ErrorKind.MISSING_PARAMETERS)

        _, session = self.make_session()
        with session:
            stores = session.get(stores_spec.path, headers=dict(stores_spec.headers))
            catalog = parse_catalog(stores.content_type, stores.body)
            first_store = catalog.records[0]["id"]
            params = list(build_params(balances_spec, {"timestamp": "2026-10-22T00:00:00"}).items())
            params += [("store", record["id"]) for record in catalog.records[:2]]
            balances = session.get(balances_spec.path, params)
        self.assertEqual(catalog.fmt, "xml")
        self.assertEqual(len(catalog.records), 5)
        self.assertTrue(all(record["parent_id"] for record in catalog.records))
        parsed = parse_balances(balances.content_type, balances.body)
        self.assertTrue(parsed.rows)
        self.assertTrue(all(isinstance(row["amount"], Decimal) for row in parsed.rows))
        self.assertTrue(any(row["amount"] < 0 for row in parsed.rows))
        self.assertEqual({row["store_id"] for row in parsed.rows} - {r["id"] for r in catalog.records[:2]}, set())
        self.assertIn(first_store, {row["store_id"] for row in parsed.rows})
        (call,) = self.server.calls("/resto/api/v2/reports/balance/stores")
        self.assertEqual(len(call.params["store"]), 2)  # several stores in one request


TOKEN = "Report-Token-0123456789"


def error_report(call, expected) -> str:
    """HTML of a production error report (DEBUG off, frame locals included), as in test_models_connection.

    A plain try/except on purpose: assertRaises clears the traceback frames.
    """
    try:
        call()
    except expected as exc:
        return ExceptionReporter(None, type(exc), exc, exc.__traceback__).get_traceback_html()
    raise AssertionError(f"{expected.__name__} was not raised")


class IikoClientErrorReportTests(SimpleTestCase):
    """Django error reports (mail_admins) list frame locals; login, hash and token must not be among them.

    Uses httpx.MockTransport: no socket is opened at all.
    """

    def make_session(self, handler) -> IikoSession:
        settings = Settings(base_url="https://iiko.example.test/resto/", login=LOGIN, pass_hash=PASS_HASH)
        client = IikoHttpClient(settings, transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
        self.addCleanup(client.close)
        return IikoSession(client, settings)

    @staticmethod
    def token_then_refused(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/api/auth", "/api/logout")):
            return httpx.Response(200, text=TOKEN)
        raise httpx.ConnectError("refused", request=request)

    def test_positive_control_the_report_shows_plain_locals(self):
        def leaky():
            token = TOKEN  # noqa: F841 - the local the report is expected to show
            raise ValueError("boom")

        self.assertIn(TOKEN, error_report(leaky, ValueError))

    def test_failed_login_report_hides_login_and_hash(self):
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        report = error_report(self.make_session(refused).login, IikoError)
        self.assertIn("_send_once", report)  # the frames that held the auth URL are in the report
        self.assertNotIn(LOGIN, report)
        self.assertNotIn(PASS_HASH, report)

    def test_failed_request_report_hides_the_token(self):
        session = self.make_session(self.token_then_refused)
        with session:
            reports = [
                error_report(lambda: session.get("api/corporation/stores"), IikoError),
                error_report(lambda: session.post_olap_report(good_body()), IikoError),
            ]
        for report in reports:
            self.assertIn("_send_once", report)
            self.assertNotIn(TOKEN, report)
            self.assertNotIn(PASS_HASH, report)
