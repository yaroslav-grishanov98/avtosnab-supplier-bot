import dataclasses
import json
import pickle
import sys
import types
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from accounting.adapters import AccountingAdapter, FakeAdapter, StoreHouseAdapter, get_adapter
from accounting.adapters.fake import FIXTURES_DIR, load_consumption_events, load_stock
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
from accounting.models import Reliability

AT = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
DAY = date(2026, 3, 10)
# Ids from accounting/fixtures/iiko/warehouses.json and products.json.
WH1 = "5a1f0c10-0000-4000-8000-0000000000a1"
WH2 = "5a1f0c10-0000-4000-8000-0000000000a2"
WH3 = "5a1f0c10-0000-4000-8000-0000000000a3"
FLOUR = "7e570000-0000-4000-8000-0000000000b1"
TOMATOES = "7e570000-0000-4000-8000-0000000000b3"
NAPKINS = "7e570000-0000-4000-8000-0000000000b4"


def snapshots(adapter, warehouse_ids, at=AT):
    return list(adapter.read_stock(warehouse_ids, at))


class DtoTests(SimpleTestCase):
    def test_write_receipt_defaults_false(self):
        capabilities = Capabilities(
            read_products=True, read_warehouses=True, read_departments=True, read_stock=True, read_consumption=True
        )
        self.assertIs(capabilities.write_receipt, False)
        self.assertIsNone(capabilities.omits_zero_balances)
        self.assertIs(Capabilities.unsupported().write_receipt, False)

    def test_unsupported_capabilities_have_every_flag_off(self):
        capabilities = Capabilities.unsupported()
        for name in (
            "read_products",
            "read_warehouses",
            "read_departments",
            "read_stock",
            "read_consumption",
            "write_receipt",
        ):
            self.assertIs(getattr(capabilities, name), False, name)
        self.assertIsNone(capabilities.omits_zero_balances)

    def test_stock_item_distinguishes_zero_and_none(self):
        zero = StockItemDTO("p1", Decimal("0"), "кг", Reliability.OK)
        unknown = StockItemDTO("p1", None, "кг", Reliability.SOURCE_ERROR)
        self.assertEqual(zero.stock_qty, Decimal("0"))
        self.assertIsNotNone(zero.stock_qty)
        self.assertIsNone(unknown.stock_qty)
        self.assertNotEqual(zero, unknown)
        self.assertNotEqual(zero.stock_qty, unknown.stock_qty)
        self.assertEqual(zero.reliability, Reliability.OK)
        self.assertEqual(unknown.reliability, Reliability.SOURCE_ERROR)
        self.assertEqual(StockItemDTO("p2", Decimal("1"), "", Reliability.UNIT_UNKNOWN).reliability, "unit_unknown")

    def test_capabilities_json_roundtrip(self):
        original = Capabilities(
            read_products=True,
            read_warehouses=True,
            read_departments=False,
            read_stock=True,
            read_consumption=False,
            write_receipt=False,
            omits_zero_balances=True,
            source_version="7.8.1",
            notes="Только чтение.",
        )
        stored = json.loads(json.dumps(original.to_json()))
        self.assertEqual(Capabilities.from_json(stored), original)
        for capabilities in (Capabilities.unsupported(), dataclasses.replace(original, omits_zero_balances=False)):
            self.assertEqual(Capabilities.from_json(json.loads(json.dumps(capabilities.to_json()))), capabilities)

    def test_capabilities_from_json_tolerates_old_or_broken_payloads(self):
        self.assertEqual(Capabilities.from_json({}), Capabilities.unsupported())
        self.assertEqual(Capabilities.from_json(None), Capabilities.unsupported())
        self.assertEqual(Capabilities.from_json("garbage"), Capabilities.unsupported())
        # Only a real JSON true enables a capability: a string "false" (or 1) must not turn it on.
        strings = Capabilities.from_json({"read_stock": "false", "write_receipt": "true", "read_products": 1})
        self.assertEqual((strings.read_stock, strings.write_receipt, strings.read_products), (False, False, False))
        partial = Capabilities.from_json({"read_stock": True, "removed_in_future": 1})
        self.assertIs(partial.read_stock, True)
        self.assertIs(partial.read_products, False)
        self.assertIsNone(partial.omits_zero_balances)
        self.assertEqual(partial.source_version, "")

    def test_dtos_are_frozen(self):
        dtos = [
            DepartmentDTO("d", "n", "DEPARTMENT"),
            WarehouseDTO("w", "n", None, "STORE"),
            ProductDTO("p", "n", "1", "g", "GOODS", "u", "кг", False),
            StockItemDTO("p", Decimal("1"), "кг", Reliability.OK),
            ItemErrorDTO(None, "c", "m"),
            StockSnapshotDTO("w", AT, [], [], "v", True),
            ConsumptionEventDTO("p", "w", AT, Decimal("1"), "кг", "WRITEOFF", OperationKind.CONSUMPTION),
            ConsumptionResult([], {}),
            Capabilities.unsupported(),
            AccessStep("login", "ok", ""),
            AccessReport(True, [], None, None),
        ]
        for dto in dtos:
            with self.subTest(dto=type(dto).__name__):
                self.assertEqual(dataclasses.replace(dto), dto)  # copying works, mutation does not
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(dto, dataclasses.fields(dto)[0].name, "changed")

    def test_operation_kinds(self):
        self.assertEqual(
            {kind.name for kind in OperationKind},
            {"CONSUMPTION", "TRANSFER", "RECEIPT", "ADJUSTMENT", "UNKNOWN"},
        )

    def test_access_step_rejects_unknown_status(self):
        for status in ("ok", "failed", "skipped"):
            self.assertEqual(AccessStep("login", status, "").status, status)
        with self.assertRaises(ValueError):
            AccessStep("login", "maybe", "")

    def test_adapter_error_carries_fields_and_prints_only_message(self):
        error = AdapterError(code="rate_limited", message="Слишком много запросов.", retryable=True, retry_after=2.5)
        self.assertEqual(
            (error.code, error.message, error.retryable, error.retry_after),
            ("rate_limited", "Слишком много запросов.", True, 2.5),
        )
        self.assertEqual(str(error), "Слишком много запросов.")
        plain = AdapterError(code="x", message="y")
        self.assertIs(plain.retryable, False)
        self.assertIsNone(plain.retry_after)
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual((restored.code, restored.retryable, restored.retry_after), ("rate_limited", True, 2.5))


class StoreHouseAdapterTests(SimpleTestCase):
    def test_storehouse_adapter_documents_everything_unsupported(self):
        adapter = StoreHouseAdapter()
        self.assertEqual(adapter.provider, "storehouse")
        self.assertEqual(adapter.capabilities(), Capabilities.unsupported())
        operations = {
            "check_access": lambda: adapter.check_access(),
            "list_departments": lambda: adapter.list_departments(),
            "list_warehouses": lambda: adapter.list_warehouses(),
            "list_products": lambda: adapter.list_products(),
            "read_stock": lambda: list(adapter.read_stock(["w1"], AT)),
            "read_consumption": lambda: adapter.read_consumption(["w1"], DAY, DAY),
        }
        for name, call in operations.items():
            with self.subTest(operation=name):
                with self.assertRaises(AdapterError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "api_not_supported")
                self.assertIs(caught.exception.retryable, False)
                self.assertIsNone(caught.exception.retry_after)
                self.assertTrue(caught.exception.message)


class FakeAdapterTests(SimpleTestCase):
    def test_fake_adapter_is_deterministic(self):
        def everything(adapter):
            return (
                adapter.capabilities(),
                adapter.check_access(),
                adapter.list_departments(),
                adapter.list_warehouses(),
                adapter.list_products(),
                snapshots(adapter, [WH1, WH2, WH3]),
                adapter.read_consumption([WH1, WH2], DAY, DAY),
            )

        first, second = FakeAdapter(), FakeAdapter()
        self.assertEqual(everything(first), everything(second))
        self.assertEqual(everything(first), everything(first))

    def test_fake_adapter_implements_the_protocol(self):
        adapter: AccountingAdapter = FakeAdapter()
        self.assertTrue(adapter.provider)
        for name in (
            "capabilities",
            "check_access",
            "list_departments",
            "list_warehouses",
            "list_products",
            "read_stock",
            "read_consumption",
        ):
            self.assertTrue(callable(getattr(adapter, name)), name)
        for method in (FakeAdapter, StoreHouseAdapter):
            self.assertEqual(
                {name for name in AccountingAdapter.__dict__ if not name.startswith("_")} - set(dir(method)),
                set(),
            )

    def test_capabilities_and_access_report(self):
        adapter = FakeAdapter()
        capabilities = adapter.capabilities()
        for name in ("read_products", "read_warehouses", "read_departments", "read_stock", "read_consumption"):
            self.assertIs(getattr(capabilities, name), True, name)
        self.assertIs(capabilities.write_receipt, False)
        self.assertIs(capabilities.omits_zero_balances, True)
        report = adapter.check_access()
        self.assertIs(report.ok, True)
        self.assertIsNone(report.error_code)
        self.assertIsNone(report.hint)
        self.assertTrue(report.steps)
        self.assertTrue(all(step.status == "ok" for step in report.steps))

    def test_catalogs_come_from_fixtures(self):
        adapter = FakeAdapter()
        departments = adapter.list_departments()
        warehouses = adapter.list_warehouses()
        products = adapter.list_products()
        self.assertEqual(len(departments), 2)
        self.assertEqual([w.external_id for w in warehouses], [WH1, WH2, WH3])
        self.assertEqual(warehouses[0].external_department_id, departments[0].external_id)
        self.assertIsNone(warehouses[2].external_department_id)
        self.assertEqual(len(products), 6)
        self.assertEqual(sum(p.is_deleted for p in products), 1)
        self.assertTrue(all(isinstance(p, ProductDTO) for p in products))

    def test_fixture_ids_are_fictional_uuids(self):
        adapter = FakeAdapter()
        ids = (
            [d.external_id for d in adapter.list_departments()]
            + [w.external_id for w in adapter.list_warehouses()]
            + [p.external_id for p in adapter.list_products()]
            + [p.unit_external_id for p in adapter.list_products() if p.unit_external_id]
            # Every fixture file, not only the catalogues: stock and consumption carry ids too.
            + list(load_stock())
            + [item.external_product_id for items in load_stock().values() for item in items]
            + [e.external_warehouse_id for e in load_consumption_events()]
            + [e.external_product_id for e in load_consumption_events()]
        )
        for value in ids:
            self.assertEqual(str(uuid.UUID(value)), value)
        # The reserved marker prefixes keep these ids visibly apart from any real server data.
        self.assertTrue(all(v.startswith(("5a1f0c10-", "7e570000-", "d3a70000-", "00000000-")) for v in ids))
        self.assertEqual(
            sorted(path.name for path in FIXTURES_DIR.glob("*.json")),
            ["consumption_day.json", "departments.json", "products.json", "stock_wh1.json", "warehouses.json"],
        )

    def test_results_are_copies(self):
        adapter = FakeAdapter()
        adapter.list_products().clear()
        adapter.list_warehouses().clear()
        self.assertEqual(len(adapter.list_products()), 6)
        self.assertEqual(len(adapter.list_warehouses()), 3)

    def test_stock_snapshot_for_a_warehouse_with_balances(self):
        [snapshot] = snapshots(FakeAdapter(), [WH1])
        self.assertEqual(snapshot.external_warehouse_id, WH1)
        self.assertEqual(snapshot.taken_at, AT)
        self.assertIs(snapshot.ok, True)
        # Like IikoAdapter: a balance the source could not report is an item error, so the snapshot is PARTIAL.
        self.assertEqual([(e.external_product_id, e.code) for e in snapshot.errors], [(TOMATOES, "quantity_missing")])
        self.assertEqual(snapshot.source_version, FakeAdapter().capabilities().source_version)
        by_product = {item.external_product_id: item for item in snapshot.items}
        self.assertEqual(by_product[FLOUR].stock_qty, Decimal("12.500"))
        self.assertEqual(by_product[FLOUR].reliability, Reliability.OK)
        self.assertEqual(by_product[FLOUR].source_uom, "кг")
        self.assertIsNone(by_product[TOMATOES].stock_qty)
        self.assertEqual(by_product[TOMATOES].reliability, Reliability.SOURCE_ERROR)
        self.assertEqual(by_product[NAPKINS].reliability, Reliability.UNIT_UNKNOWN)
        self.assertTrue(any(item.stock_qty is not None and item.stock_qty < 0 for item in snapshot.items))
        # iiko omits zero balances, so the fixture does not contain a zero row either.
        self.assertFalse(any(item.stock_qty == 0 for item in snapshot.items))

    def test_stock_snapshots_follow_the_requested_order_and_cover_empty_warehouses(self):
        result = snapshots(FakeAdapter(), [WH2, WH1, WH3])
        self.assertEqual([s.external_warehouse_id for s in result], [WH2, WH1, WH3])
        self.assertEqual([s.ok for s in result], [True, True, True])
        self.assertEqual(result[0].items, [])
        self.assertEqual(result[2].items, [])
        self.assertTrue(result[1].items)

    def test_unknown_warehouse_is_a_failed_snapshot_not_an_exception(self):
        [snapshot] = snapshots(FakeAdapter(), ["no-such-warehouse"])
        self.assertIs(snapshot.ok, False)
        self.assertEqual(snapshot.items, [])
        self.assertEqual([e.code for e in snapshot.errors], ["warehouse_not_found"])
        self.assertIsNone(snapshot.errors[0].external_product_id)
        self.assertTrue(snapshot.errors[0].message)

    def test_constructor_overrides_the_fixtures(self):
        item = StockItemDTO("p-custom", Decimal("0"), "шт", Reliability.OK)
        warehouse = WarehouseDTO("w-custom", "Тестовый склад", None, "STORE")
        adapter = FakeAdapter(departments=[], warehouses=[warehouse], products=[], stock={"w-custom": [item]})
        self.assertEqual(adapter.list_departments(), [])
        self.assertEqual(adapter.list_warehouses(), [warehouse])
        self.assertEqual(adapter.list_products(), [])
        [snapshot] = snapshots(adapter, ["w-custom"])
        self.assertEqual(snapshot.items, [item])
        self.assertEqual(snapshots(adapter, [WH1])[0].ok, False)

    def test_latency_sleeps_per_call_and_not_at_all_by_default(self):
        with mock.patch("accounting.adapters.fake.time.sleep") as sleep:
            adapter = FakeAdapter()
            adapter.list_products()
            snapshots(adapter, [WH1])
            sleep.assert_not_called()
        with mock.patch("accounting.adapters.fake.time.sleep") as sleep:
            slow = FakeAdapter(latency=0.25)
            slow.check_access()
            slow.list_departments()
            slow.list_warehouses()
            slow.list_products()
            snapshots(slow, [WH1, WH2])
            slow.read_consumption([WH1], DAY, DAY)
            self.assertEqual(sleep.call_count, 6)
            sleep.assert_called_with(0.25)

    def test_consumption_comes_from_the_day_fixture(self):
        result = FakeAdapter().read_consumption([WH1], DAY, DAY)
        self.assertIsInstance(result, ConsumptionResult)
        self.assertEqual(result.covered, {(WH1, DAY): True})
        self.assertTrue(result.events)
        self.assertTrue(all(e.external_warehouse_id == WH1 and e.occurred_at.date() == DAY for e in result.events))
        types_and_kinds = {(e.transaction_type, e.kind) for e in result.events}
        self.assertIn(("WRITEOFF", OperationKind.CONSUMPTION), types_and_kinds)
        self.assertIn(("SESSION_WRITEOFF", OperationKind.CONSUMPTION), types_and_kinds)
        self.assertIn(("INVOICE", OperationKind.RECEIPT), types_and_kinds)
        self.assertIn(("TRANSFER", OperationKind.TRANSFER), types_and_kinds)
        self.assertIn(("SOME_NEW_TYPE", OperationKind.UNKNOWN), types_and_kinds)
        self.assertTrue(all(isinstance(e.qty, Decimal) for e in result.events))

    def test_consumption_coverage_spans_the_requested_range_for_known_warehouses_only(self):
        result = FakeAdapter().read_consumption([WH1, WH2, "no-such-warehouse"], date(2026, 3, 9), date(2026, 3, 11))
        days = [date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11)]
        self.assertEqual(
            result.covered,
            {(wh, day): wh != "no-such-warehouse" for wh in (WH1, WH2, "no-such-warehouse") for day in days},
        )
        # A covered day without events is a real zero; only 10 March has events.
        self.assertEqual({e.occurred_at.date() for e in result.events}, {DAY})
        self.assertEqual({e.external_warehouse_id for e in result.events}, {WH1, WH2})

    def test_consumption_outside_the_fixture_day_has_coverage_but_no_events(self):
        result = FakeAdapter().read_consumption([WH1], date(2026, 3, 1), date(2026, 3, 1))
        self.assertEqual(result.events, [])
        self.assertEqual(result.covered, {(WH1, date(2026, 3, 1)): True})

    def test_consumption_rejects_an_inverted_range(self):
        with self.assertRaises(ValueError):
            FakeAdapter().read_consumption([WH1], date(2026, 3, 11), date(2026, 3, 10))

    def test_warehouse_ids_are_matched_like_id_key(self):
        # Same normalisation as accounting.sync.keys.id_key: iiko GUID case may differ between methods.
        asked = f"  {WH1.upper()} "
        [snapshot] = snapshots(FakeAdapter(), [asked])
        self.assertIs(snapshot.ok, True)
        self.assertEqual(snapshot.external_warehouse_id, asked)  # answered under the id it was asked for
        self.assertEqual(
            {item.external_product_id for item in snapshot.items},
            {item.external_product_id for item in snapshots(FakeAdapter(), [WH1])[0].items},
        )

        result = FakeAdapter().read_consumption([asked], DAY, DAY)
        self.assertEqual(result.covered, {(asked, DAY): True})
        self.assertTrue(result.events)


class GetAdapterTests(SimpleTestCase):
    def test_storehouse_connection_gets_the_storehouse_adapter(self):
        with get_adapter(SimpleNamespace(provider="storehouse")) as adapter:
            self.assertIsInstance(adapter, StoreHouseAdapter)
        # Adapter options are provider-specific; StoreHouse ignores them.
        with get_adapter(SimpleNamespace(provider="storehouse"), product_units={"p": "кг"}) as adapter:
            self.assertIsInstance(adapter, StoreHouseAdapter)

    def test_iiko_adapter_that_is_not_available_yet_gives_a_clear_error(self):
        # None in sys.modules makes the import fail whether or not accounting.adapters.iiko exists.
        with mock.patch.dict(sys.modules, {"accounting.adapters.iiko": None}):
            with self.assertRaises(AdapterError) as caught, get_adapter(SimpleNamespace(provider="iiko")):
                self.fail("the body must not run")
        self.assertEqual(caught.exception.code, "adapter_not_available")
        self.assertIs(caught.exception.retryable, False)
        self.assertTrue(caught.exception.message)

    def test_iiko_connection_is_delegated_to_for_connection(self):
        events = []
        connection = SimpleNamespace(provider="iiko")

        class StubIikoAdapter:
            @staticmethod
            @contextmanager
            def for_connection(received, **options):
                events.append(("enter", received, options))
                try:
                    yield "adapter"
                finally:
                    events.append(("exit", received))

        module = types.ModuleType("accounting.adapters.iiko")
        module.IikoAdapter = StubIikoAdapter
        with mock.patch.dict(sys.modules, {"accounting.adapters.iiko": module}):
            with get_adapter(connection) as adapter:
                self.assertEqual(adapter, "adapter")
                self.assertEqual(events, [("enter", connection, {})])
            self.assertEqual(events, [("enter", connection, {}), ("exit", connection)])
            events.clear()
            with get_adapter(connection, product_units={"p": "кг"}, source_version="9.4"):
                pass
        self.assertEqual(
            events,
            [("enter", connection, {"product_units": {"p": "кг"}, "source_version": "9.4"}), ("exit", connection)],
        )

    def test_a_broken_dependency_of_the_iiko_module_is_not_hidden(self):
        class BrokenIikoModuleFinder:
            @staticmethod
            def find_spec(name, path=None, target=None):
                if name == "accounting.adapters.iiko":
                    raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")
                return None

        with (
            mock.patch.dict(sys.modules),
            mock.patch.object(sys, "meta_path", [BrokenIikoModuleFinder(), *sys.meta_path]),
        ):
            sys.modules.pop("accounting.adapters.iiko", None)
            with self.assertRaises(ModuleNotFoundError) as caught, get_adapter(SimpleNamespace(provider="iiko")):
                self.fail("the body must not run")
        self.assertEqual(caught.exception.name, "some_dependency")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(AdapterError) as caught, get_adapter(SimpleNamespace(provider="1c")):
            self.fail("the body must not run")
        self.assertEqual(caught.exception.code, "unknown_provider")
        self.assertIs(caught.exception.retryable, False)
