"""Deterministic in-memory adapter over the JSON fixtures in accounting/fixtures/iiko/.

Used by tests, the load simulation and frontend mocks. The data is fictional
(see the fixtures); nothing here talks to a network or reads the clock.
"""

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from accounting.dto import (
    AccessReport,
    AccessStep,
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
from accounting.models.inventory import Reliability

# Warehouse ids are matched like the sync matches them (strip + lower): iiko GUID case may differ between methods.
from accounting.sync.keys import id_key

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "iiko"
FAKE_SOURCE_VERSION = "fake-1.0"


def _load(name: str) -> Any:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_departments() -> list[DepartmentDTO]:
    return [DepartmentDTO(**row) for row in _load("departments.json")]


def load_warehouses() -> list[WarehouseDTO]:
    return [WarehouseDTO(**row) for row in _load("warehouses.json")]


def load_products() -> list[ProductDTO]:
    return [ProductDTO(**row) for row in _load("products.json")]


def load_stock() -> dict[str, list[StockItemDTO]]:
    """Balances per external warehouse id; only the fixture's warehouse has any."""
    data = _load("stock_wh1.json")
    items = [
        StockItemDTO(
            external_product_id=row["external_product_id"],
            stock_qty=None if row["stock_qty"] is None else Decimal(row["stock_qty"]),
            source_uom=row["source_uom"],
            reliability=Reliability(row["reliability"]),
        )
        for row in data["items"]
    ]
    return {data["external_warehouse_id"]: items}


def load_consumption_events() -> list[ConsumptionEventDTO]:
    return [
        ConsumptionEventDTO(
            external_product_id=row["external_product_id"],
            external_warehouse_id=row["external_warehouse_id"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            qty=Decimal(row["qty"]),
            source_uom=row["source_uom"],
            transaction_type=row["transaction_type"],
            kind=OperationKind(row["kind"]),
        )
        for row in _load("consumption_day.json")["events"]
    ]


class FakeAdapter:
    """Same input, same output: no randomness and no clock (snapshots carry the ``at`` they are given).

    ``latency`` seconds are slept once per provider call, to imitate a slow server.
    Every call returns fresh lists, so a caller cannot change what the next call sees.
    """

    provider = "fake"

    def __init__(
        self,
        departments: Sequence[DepartmentDTO] | None = None,
        warehouses: Sequence[WarehouseDTO] | None = None,
        products: Sequence[ProductDTO] | None = None,
        stock: Mapping[str, Sequence[StockItemDTO]] | None = None,
        latency: float = 0.0,
    ):
        self._departments = list(load_departments() if departments is None else departments)
        self._warehouses = list(load_warehouses() if warehouses is None else warehouses)
        self._products = list(load_products() if products is None else products)
        self._stock = {key: list(items) for key, items in (load_stock() if stock is None else stock).items()}
        self._consumption = load_consumption_events()
        self._latency = latency

    def _wait(self) -> None:
        if self._latency > 0:
            time.sleep(self._latency)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            read_products=True,
            read_warehouses=True,
            read_departments=True,
            read_stock=True,
            read_consumption=True,
            write_receipt=False,
            omits_zero_balances=True,
            source_version=FAKE_SOURCE_VERSION,
            notes="Тестовый адаптер с вымышленными данными.",
        )

    def check_access(self) -> AccessReport:
        self._wait()
        return AccessReport(
            ok=True,
            steps=[
                AccessStep(name="login", status="ok", message="Вход выполнен."),
                AccessStep(name="read_catalogs", status="ok", message="Справочники доступны."),
            ],
            error_code=None,
            hint=None,
        )

    def list_departments(self) -> list[DepartmentDTO]:
        self._wait()
        return list(self._departments)

    def list_warehouses(self) -> list[WarehouseDTO]:
        self._wait()
        return list(self._warehouses)

    def list_products(self) -> list[ProductDTO]:
        self._wait()
        return list(self._products)

    def read_stock(self, warehouse_ids: Sequence[str], at: datetime) -> Iterator[StockSnapshotDTO]:
        """One snapshot per requested warehouse, in request order.

        A known warehouse without balances gives an empty, ok snapshot (like iiko, which leaves
        zero balances out); an unknown one gives ok=False with a ``warehouse_not_found`` error.
        As in IikoAdapter, an item without a quantity adds a ``quantity_missing`` error (PARTIAL snapshot).
        Ids are compared like ``id_key`` (strip + lower); a snapshot carries the id exactly as it was asked for.
        """
        self._wait()
        known = {id_key(warehouse.external_id) for warehouse in self._warehouses}
        stock = {id_key(warehouse_id): items for warehouse_id, items in self._stock.items()}
        for warehouse_id in warehouse_ids:
            if id_key(warehouse_id) in known:
                items = list(stock.get(id_key(warehouse_id), []))
                yield StockSnapshotDTO(
                    external_warehouse_id=warehouse_id,
                    taken_at=at,
                    items=items,
                    errors=[
                        ItemErrorDTO(
                            external_product_id=item.external_product_id,
                            code="quantity_missing",
                            message="Учётная система не сообщила количество остатка по товару.",
                        )
                        for item in items
                        if item.stock_qty is None
                    ],
                    source_version=FAKE_SOURCE_VERSION,
                    ok=True,
                )
            else:
                yield StockSnapshotDTO(
                    external_warehouse_id=warehouse_id,
                    taken_at=at,
                    items=[],
                    errors=[
                        ItemErrorDTO(
                            external_product_id=None,
                            code="warehouse_not_found",
                            message="Склад не найден в учётной системе.",
                        )
                    ],
                    source_version=FAKE_SOURCE_VERSION,
                    ok=False,
                )

    def read_consumption(self, warehouse_ids: Sequence[str], day_from: date, day_to: date) -> ConsumptionResult:
        """Events of the requested warehouses and days from the fixture.

        Coverage is True for every (known warehouse, day) pair inside the requested range, whether or not the
        fixture has events for it: the fake reads whole days like a healthy provider, so a day without events
        means zero consumption. Pairs for unknown warehouses are False ("no data").
        """
        if day_to < day_from:
            raise ValueError("day_to must not be earlier than day_from")
        self._wait()
        known = {id_key(warehouse.external_id) for warehouse in self._warehouses}
        wanted = {id_key(warehouse_id) for warehouse_id in warehouse_ids}
        events = [
            event
            for event in self._consumption
            if id_key(event.external_warehouse_id) in wanted and day_from <= event.occurred_at.date() <= day_to
        ]
        days = [day_from + timedelta(days=offset) for offset in range((day_to - day_from).days + 1)]
        covered = {(warehouse_id, day): id_key(warehouse_id) in known for warehouse_id in warehouse_ids for day in days}
        return ConsumptionResult(events=events, covered=covered)
