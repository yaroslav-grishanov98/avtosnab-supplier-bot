"""Fake iikoServer on 127.0.0.1 for tests. Invented demo data only, nothing real.

Ported from the iiko_diagnostics prototype (``prototype/fake_iiko/server.py``). It mimics the documented
behaviour the client relies on:

  * GET /resto/api/auth?login=&pass=<sha1 hex> -> token (text/plain) plus ``Set-Cookie: key=<token>``;
    wrong credentials -> 401;
  * every other /resto/api/* call needs ``key`` (query) or cookie ``key`` -> else 401;
  * GET /resto/api/corporation/{departments,stores}: XML corporateItemDto; WITHOUT an ``Accept: application/xml``
    header it answers 200 with a JSON array of empty objects (the real server did exactly that);
  * GET /resto/api/v2/entities/products/list, /entities/products/group/list, /entities/list;
  * GET /resto/api/v2/reports/balance/stores?timestamp=&store=... (``store`` may repeat);
  * GET /resto/api/v2/reports/olap/columns?reportType=TRANSACTIONS -> ``OLAP_COLUMNS``;
  * POST /resto/api/v2/reports/olap (the one non-GET call) -> ``{"data": [...]}`` from ``olap_rows``;
  * GET /resto/api/logout?key=.

Special logins (with the configured hash): ``nolicense`` -> 403 licence text on auth; ``norights`` -> auth ok,
products/list answers 403 "Permission denied".

Unlike the prototype the state lives on a ``FakeIikoServer`` instance (no module globals), every request is
recorded, and tests can override any path with a status, body, headers and an artificial delay.
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DEMO_LOGIN = "demo"
# SHA-1 hex of the fake server's invented demo password; the plain value is not needed anywhere.
DEMO_PASS_HASH = "89e495e7941cf9e40e6980d14a16bf023ccd4c91"
NS = uuid.UUID("11111111-2222-3333-4444-555555555555")
OLAP_PATH = "/resto/api/v2/reports/olap"


def _id(name: str) -> str:
    return str(uuid.uuid5(NS, name))


UNITS = {"kg": "кг", "l": "л", "pcs": "шт", "port": "порц"}
GROUPS = [
    "Овощи, фрукты",
    "Молочные продукты",
    "Мясо, птица",
    "Рыба, морепродукты",
    "Бакалея",
    "Напитки",
    "Алкоголь",
    "Хозтовары",
]
BASES = {
    "Овощи, фрукты": ["картофель", "морковь", "лук репчатый", "помидоры", "огурцы", "яблоки", "лимоны", "капуста"],
    "Молочные продукты": ["молоко 3,2%", "сливки 33%", "сметана 20%", "творог", "масло сливочное", "кефир"],
    "Мясо, птица": ["говядина вырезка", "свинина шея", "курица филе", "индейка бедро", "фарш говяжий"],
    "Рыба, морепродукты": ["сёмга филе", "форель", "креветки очищ.", "кальмар тушка", "мидии"],
    "Бакалея": ["мука пшеничная", "сахар", "соль морская", "рис басмати", "масло подсолнечное", "кофе зерно"],
    "Напитки": ["вода без газа 0,5", "вода газ 0,5", "сок яблочный", "лимонад домашний"],
    "Алкоголь": ["вино белое сухое", "вино красное сухое", "джин", "пиво светлое"],
    "Хозтовары": ["салфетки", "перчатки нитриловые", "пакеты мусорные", "губки"],
}
STORES = {
    "Ресторан «Центр»": ["Склад Кухня", "Склад Бар", "Склад Хозтовары"],
    "Кафе «Север»": ["Склад Кухня (Север)", "Склад Бар (Север)"],
}
_FIELD = {"groupingAllowed": True, "aggregationAllowed": False, "filteringAllowed": True}
_AGGREGATE = {"groupingAllowed": False, "aggregationAllowed": True, "filteringAllowed": True}
# GET /api/v2/reports/olap/columns?reportType=TRANSACTIONS: the fields the OLAP body builder looks for.
OLAP_COLUMNS = {
    "Account.Name": _FIELD,
    "Store": _FIELD,
    "Product.Num": _FIELD,
    "Product.Name": _FIELD,
    "Product.MeasureUnit": _FIELD,
    "TransactionType": _FIELD,
    "DateTime.DateTyped": _FIELD,
    "Amount": _AGGREGATE,
    "Amount.In": _AGGREGATE,
    "Amount.Out": _AGGREGATE,
}


class DemoData:
    """Deterministic invented catalogue: departments, stores, units, groups, products and balances."""

    def __init__(self, seed: int = 7) -> None:
        rnd = random.Random(seed)
        unit_id = {k: _id("unit:" + k) for k in UNITS}
        self.units = [
            {"id": unit_id[k], "rootType": "MeasureUnit", "deleted": False, "code": k.upper(), "name": v}
            for k, v in UNITS.items()
        ]
        self.groups = [
            {"id": _id("group:" + g), "name": g, "num": f"G{i:02d}", "parent": None, "deleted": False}
            for i, g in enumerate(GROUPS, 1)
        ]
        corporation = {"id": _id("corp"), "name": "Демо-корпорация", "type": "CORPORATION", "parentId": ""}
        jur = {"id": _id("jur"), "name": "ООО «Демо»", "type": "JURPERSON", "parentId": corporation["id"]}
        self.departments = [corporation, jur]
        self.stores: list[dict] = []
        for dept_name, store_names in STORES.items():
            dept = {"id": _id("dept:" + dept_name), "name": dept_name, "type": "DEPARTMENT", "parentId": jur["id"]}
            self.departments.append(dept)
            for store_name in store_names:
                self.stores.append(
                    {
                        "id": _id(f"store:{dept_name}:{store_name}"),
                        "name": store_name,
                        "type": "STORE",
                        "parentId": dept["id"],
                        "code": "",
                    }
                )

        self.products: list[dict] = []
        n = 0
        for group in GROUPS:
            packs = ("", " промо") if group in ("Напитки", "Алкоголь", "Хозтовары") else ("", " 1 кг", " 5 кг")
            unit = "pcs" if group in ("Напитки", "Хозтовары") else ("l" if group == "Алкоголь" else "kg")
            for base in BASES[group]:
                for pack in packs:
                    n += 1
                    self.products.append(
                        {
                            "id": _id(f"product:{n}"),
                            "num": f"{n:05d}",
                            "code": str(n),
                            "name": f"{base}{pack}".strip(),
                            "type": "GOODS",
                            "mainUnit": unit_id[unit],
                            "parent": _id("group:" + group),
                            "deleted": False,
                        }
                    )
        for extra, kind in (("Салат Цезарь", "DISH"), ("Бульон куриный (заготовка)", "PREPARED")):
            n += 1
            self.products.append(
                {
                    "id": _id(f"product:{n}"),
                    "num": f"{n:05d}",
                    "code": str(n),
                    "name": extra,
                    "type": kind,
                    "mainUnit": unit_id["port"],
                    "parent": None,
                    "deleted": False,
                }
            )

        goods = [p for p in self.products if p["type"] == "GOODS"]
        unit_name = {u["id"]: u["name"] for u in self.units}
        # Like the real server, a balance report has no rows for zero balances; a few are negative.
        self.balances: dict[str, list[dict]] = {}
        for store in self.stores:
            rows = []
            for product in rnd.sample(goods, k=int(len(goods) * 0.7)):
                piece = unit_name[product["mainUnit"]] == "шт"
                amount = float(rnd.randint(1, 60)) if piece else round(rnd.uniform(0.05, 45), 3)
                if rnd.random() < 0.06:
                    amount = -abs(amount) / 4
                unit_cost = round(rnd.uniform(20, 1500), 2)
                rows.append(
                    {
                        "store": store["id"],
                        "product": product["id"],
                        "amount": amount,
                        "sum": round(amount * unit_cost, 2),
                    }
                )
            self.balances[store["id"]] = rows


def _corporate_xml(items: list[dict]) -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><corporateItemDtoes>']
    for item in items:
        parts.append("<corporateItemDto>")
        for key in ("id", "parentId", "code", "name", "type"):
            parts.append(f"<{key}>{item.get(key, '') or ''}</{key}>")
        parts.append("</corporateItemDto>")
    parts.append("</corporateItemDtoes>")
    return "".join(parts).encode("utf-8")


@dataclass
class RecordedRequest:
    method: str
    path: str
    raw_path: str  # path plus query string exactly as received
    params: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def param(self, name: str) -> str | None:
        values = self.params.get(name)
        return values[0] if values else None


@dataclass
class Override:
    status: int = 200
    body: bytes | str | Callable[[RecordedRequest], str] = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "text/plain;charset=UTF-8"
    delay: float = 0.0


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    fake: FakeIikoServer

    def handle_error(self, request, client_address):
        # A client that timed out closes the socket before a delayed answer is written; that is expected here.
        return


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeIiko/0.2"
    server: _Server

    def log_message(self, format, *args):
        # Keep quiet: the default prints the request line, i.e. query strings with login, hash and key.
        pass

    # -- plumbing ------------------------------------------------------------
    def _send(
        self,
        status: int,
        body: bytes | str,
        ctype: str = "text/plain;charset=UTF-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False), "application/json;charset=UTF-8")

    def _record(self) -> RecordedRequest:
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        recorded = RecordedRequest(
            method=self.command,
            path=url.path,
            raw_path=self.path,
            params=parse_qs(url.query, keep_blank_values=True),
            headers={k: v for k, v in self.headers.items()},
            body=body,
        )
        self.server.fake._remember(recorded)
        return recorded

    def _cookie_key(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "key":
                return value
        return ""

    def _handle(self) -> None:
        fake = self.server.fake
        req = self._record()
        override = fake.overrides.get(req.path)
        if override is not None:
            if override.delay:
                time.sleep(override.delay)
            body = override.body(req) if callable(override.body) else override.body
            return self._send(override.status, body, override.content_type, override.headers)
        if req.method == "GET":
            return self._route_get(fake, req)
        if req.method == "POST" and req.path == OLAP_PATH:
            return self._route_olap(fake, req)
        return self._send(405, "Method Not Allowed")

    # Every method is recorded, so tests can prove that nothing but GET (and the OLAP POST) was sent.
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

    # -- routes --------------------------------------------------------------
    def _route_get(self, fake: FakeIikoServer, req: RecordedRequest) -> None:
        path = req.path
        if path == "/resto/get_server_info.jsp":
            return self._send(200, "<r><version>9.4.8049.0</version><edition>demo</edition></r>", "text/xml")
        if path == "/resto/api/auth":
            login, pass_hash = req.param("login") or "", req.param("pass") or ""
            if login == "nolicense":
                return self._send(403, "License enhancement is required: no connections available for module 28008806")
            if login in (fake.login, "norights") and pass_hash == fake.pass_hash:
                token = fake._issue_token("norights" if login == "norights" else "ok")
                return self._send(200, token, headers={"Set-Cookie": f"key={token}; Path=/resto; HttpOnly"})
            return self._send(401, "Wrong credentials")
        if not path.startswith("/resto/api/"):
            return self._send(404, f"HTTP 404 Not Found {path}")

        key = req.param("key") or self._cookie_key()
        tag = fake.tokens.get(key)
        if tag is None:
            return self._send(401, "Token is expired or invalid")
        if path == "/resto/api/logout":
            fake.tokens.pop(key, None)
            return self._send(200, key)
        data = fake.data
        wants_xml = "xml" in (self.headers.get("Accept") or "")
        if path in ("/resto/api/corporation/departments", "/resto/api/corporation/stores"):
            items = data.departments if path.endswith("departments") else data.stores
            if wants_xml:
                return self._send(200, _corporate_xml(items), "application/xml")
            return self._json([{} for _ in items])
        if path == "/resto/api/v2/entities/products/list":
            if tag == "norights":
                return self._send(403, "Permission denied")
            return self._json(data.products)
        if path == "/resto/api/v2/entities/products/group/list":
            return self._json(data.groups)
        if path == "/resto/api/v2/entities/list":
            return self._json(data.units if "MeasureUnit" in req.params.get("rootType", []) else [])
        if path == "/resto/api/v2/reports/balance/stores":
            if not req.param("timestamp"):
                return self._send(400, "Wrong date format: ''")
            wanted = req.params.get("store") or list(data.balances)
            if fake.balance_failing_stores & set(wanted) or (
                fake.balance_max_stores is not None and len(wanted) > fake.balance_max_stores
            ):
                return self._send(500, "Internal server error")
            return self._json([row for store in wanted for row in data.balances.get(store, [])])
        if path == "/resto/api/v2/reports/olap/columns":
            return self._json(OLAP_COLUMNS if req.param("reportType") == "TRANSACTIONS" else {})
        return self._send(404, f"HTTP 404 Not Found {path}")

    def _route_olap(self, fake: FakeIikoServer, req: RecordedRequest) -> None:
        key = req.param("key") or self._cookie_key()
        if fake.tokens.get(key) is None:
            return self._send(401, "Token is expired or invalid")
        try:
            body = json.loads(req.body or b"{}")
        except ValueError:
            return self._send(400, "Bad JSON")
        filters = body.get("filters") or {}
        dates = [spec for name, spec in filters.items() if name.startswith("DateTime.")]
        day = str(dates[0].get("from") or "")[:10] if dates else ""
        if day in fake.olap_delays:
            time.sleep(fake.olap_delays[day])
        if day in fake.olap_failures:
            return self._send(fake.olap_failures[day], "OLAP report failed")
        values = [
            v for name, spec in filters.items() if not name.startswith("DateTime.") for v in spec.get("values") or []
        ]
        rows = [row for value in values for row in fake.olap_rows.get((value, day), [])]
        return self._json({"data": rows, "summary": []})


class FakeIikoServer:
    """A fake iikoServer bound to 127.0.0.1 on a free port, served from a daemon thread."""

    def __init__(self, *, login: str = DEMO_LOGIN, pass_hash: str = DEMO_PASS_HASH, host: str = "127.0.0.1") -> None:
        self.login = login
        self.pass_hash = pass_hash
        self.host = host
        self.data = DemoData()
        self.tokens: dict[str, str] = {}  # token -> "ok" | "norights"
        self.issued_tokens: list[str] = []
        self.requests: list[RecordedRequest] = []
        self.overrides: dict[str, Override] = {}
        # Balance report knobs: a request naming any of these stores, or more than this many stores, gets a 500.
        self.balance_failing_stores: set[str] = set()
        self.balance_max_stores: int | None = None
        # OLAP report knobs, keyed by the store filter value and the day ("YYYY-MM-DD") of the date filter.
        self.olap_rows: dict[tuple[str, str], list[dict]] = {}
        self.olap_failures: dict[str, int] = {}  # day -> HTTP status
        self.olap_delays: dict[str, float] = {}  # day -> seconds before answering
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> str:
        server = _Server((self.host, 0), _Handler)
        server.fake = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeIikoServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "server is not started"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/resto/"

    # -- test controls ---------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self.tokens.clear()
            self.issued_tokens.clear()
            self.requests.clear()
            self.overrides.clear()
            self.balance_failing_stores = set()
            self.balance_max_stores = None
            self.olap_rows = {}
            self.olap_failures = {}
            self.olap_delays = {}

    def set_override(self, path: str, **kwargs) -> None:
        """Answer ``path`` (e.g. "/resto/api/x") with a fixed response; see ``Override`` for the fields."""
        self.overrides[path] = Override(**kwargs)

    def calls(self, path: str | None = None, method: str | None = None) -> list[RecordedRequest]:
        with self._lock:
            items = list(self.requests)
        return [r for r in items if (path is None or r.path == path) and (method is None or r.method == method)]

    # -- used by the handler ---------------------------------------------------
    def _remember(self, request: RecordedRequest) -> None:
        with self._lock:
            self.requests.append(request)

    def _issue_token(self, tag: str) -> str:
        token = str(uuid.uuid4())
        with self._lock:
            self.tokens[token] = tag
            self.issued_tokens.append(token)
        return token
