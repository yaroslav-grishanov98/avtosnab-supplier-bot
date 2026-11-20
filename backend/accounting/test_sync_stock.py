from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.db import DataError
from django.db import connection as db_connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounting.adapters import FakeAdapter
from accounting.dto import AdapterError
from accounting.locks import sync_guard
from accounting.models import (
    AccountingProduct,
    AccountingStockLevel,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingWarehouse,
    ConnectionStatus,
    LinkStatus,
    Reliability,
    SnapshotStatus,
    SyncKind,
    SyncStatus,
)
from accounting.signals import stock_snapshot_ready
from accounting.sync import stock as stock_module
from accounting.sync.runner import busy_retry_delay, run_sync_job
from accounting.sync.stock import sync_stock
from accounting.test_support.sync import (
    adapter_factory,
    item,
    link,
    make_connection,
    make_job,
    make_venue,
    warehouse,
)

AT = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.get_default_timezone())


class RecordingStockAdapter(FakeAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stock_requests = []

    def read_stock(self, warehouse_ids, at):
        self.stock_requests.append((list(warehouse_ids), at))
        return super().read_stock(warehouse_ids, at)


class AbortAfterFirstAdapter(FakeAdapter):
    """Yields the first warehouse, then the source refuses to go on (e.g. HTTP 429)."""

    def read_stock(self, warehouse_ids, at):
        snapshots = super().read_stock(warehouse_ids, at)
        yield next(snapshots)
        raise AdapterError("rate_limited", "Сервер iiko просит снизить частоту запросов.", True, 30.0)


class BrokenAfterFirstAdapter(FakeAdapter):
    """Yields the first warehouse, then fails with a bug (not an AdapterError)."""

    def read_stock(self, warehouse_ids, at):
        snapshots = super().read_stock(warehouse_ids, at)
        yield next(snapshots)
        raise ValueError("adapter bug")


class SwappedCaseAdapter(FakeAdapter):
    """Answers with the warehouse id in another letter case than asked: iiko GUID case varies between methods."""

    def read_stock(self, warehouse_ids, at):
        for snapshot in super().read_stock(warehouse_ids, at):
            yield replace(snapshot, external_warehouse_id=snapshot.external_warehouse_id.swapcase())


class SilentQuantityAdapter(FakeAdapter):
    """Leaves out the quantity_missing errors the FakeAdapter adds: an adapter that forgot to report them."""

    def read_stock(self, warehouse_ids, at):
        for snapshot in super().read_stock(warehouse_ids, at):
            yield replace(snapshot, errors=[])


class Interrupt(BaseException):
    """Stands in for KeyboardInterrupt/SystemExit."""


class StockFixtureMixin:
    """Two venues of one connection, a warehouse each, four catalogue products."""

    def make_world(self, capabilities=None, server_version="7.9.1"):
        self.venue_a = make_venue("Заведение A")
        self.venue_b = make_venue("Заведение B")
        self.connection = make_connection(
            capabilities={"omits_zero_balances": True} if capabilities is None else capabilities,
            server_version=server_version,
        )
        link(self.connection, self.venue_a)
        link(self.connection, self.venue_b)
        self.wh1 = self.make_warehouse("wh-1", self.venue_a, "Кухня")
        self.wh2 = self.make_warehouse("wh-2", self.venue_b, "Бар")
        self.products = {
            ext: AccountingProduct.objects.create(
                connection=self.connection, external_id=ext, name=f"Товар {ext}", unit_name=unit
            )
            for ext, unit in (("p-1", "кг"), ("p-2", "л"), ("p-3", "кг"), ("p-4", ""))
        }

    def make_warehouse(self, external_id, venue, name, connection=None, **fields):
        return AccountingWarehouse.objects.create(
            connection=connection or self.connection, venue=venue, external_id=external_id, name=name, **fields
        )

    def adapter(self, stock, known=("wh-1", "wh-2"), cls=FakeAdapter):
        return cls(warehouses=[warehouse(ext, None) for ext in known], stock=stock, products=[], departments=[])

    def level(self, wh, product_id):
        return AccountingStockLevel.objects.filter(warehouse=wh, product__external_id=product_id).first()

    def seed_level(self, wh, product_id, qty, reliability=Reliability.OK):
        snapshot = AccountingStockSnapshot.objects.create(
            connection=wh.connection, venue=wh.venue, warehouse=wh, taken_at=AT, status=SnapshotStatus.COMPLETE
        )
        return AccountingStockLevel.objects.create(
            connection=wh.connection,
            venue=wh.venue,
            warehouse=wh,
            product=AccountingProduct.objects.get(connection=wh.connection, external_id=product_id),
            snapshot=snapshot,
            stock_qty=None if qty is None else Decimal(qty),
            source_uom="кг",
            taken_at=AT,
            reliability=reliability,
        )

    def run_job(self, adapter, kind=SyncKind.STOCK, calls=None):
        job = make_job(self.connection, kind=kind)
        with mock.patch("accounting.sync.runner.get_adapter", adapter_factory(adapter, calls)):
            outcome = run_sync_job(job.job_id)
        job.refresh_from_db()
        return job, outcome


class SyncStockTests(StockFixtureMixin, TestCase):
    def test_lost_guard_does_not_write_success(self):
        job = make_job(self.connection)
        with (
            mock.patch("accounting.sync.runner.get_adapter", adapter_factory(self.adapter({}))),
            mock.patch("accounting.locks.GuardResult.still_held", return_value=False),
        ):
            outcome = run_sync_job(job.pk)
        self.assertEqual(outcome.status, SyncStatus.FAILED)
        self.assertEqual(outcome.job.error_code, "worker_lost")
        self.assertFalse(outcome.ran)

    def setUp(self):
        self.make_world()

    def test_complete_snapshot_upserts_levels_and_writes_header(self):
        job = make_job(self.connection)
        stats = sync_stock(
            self.adapter({"wh-1": [item("p-1", "12.5"), item("p-2", "8", "л")]}), self.connection, job, at=AT
        )

        snapshot = AccountingStockSnapshot.objects.get(warehouse=self.wh1)
        self.assertEqual(snapshot.status, SnapshotStatus.COMPLETE)
        self.assertEqual(snapshot.job_id, job.job_id)
        self.assertEqual(snapshot.taken_at, AT)
        self.assertEqual(snapshot.item_count, 2)
        self.assertEqual(snapshot.error_count, 0)
        self.assertEqual(snapshot.venue_id, self.venue_a.id)
        self.assertEqual(snapshot.source_version, "fake-1.0")
        level = self.level(self.wh1, "p-1")
        self.assertEqual(level.stock_qty, Decimal("12.5"))
        self.assertEqual(level.snapshot_id, snapshot.id)
        self.assertEqual(level.venue_id, self.venue_a.id)
        self.assertEqual(level.taken_at, AT)
        self.assertEqual(self.level(self.wh1, "p-2").source_uom, "л")
        self.assertEqual(stats.complete, 2)  # wh-2 is known to the source and has no balances

        later = AT.replace(hour=10)
        sync_stock(
            self.adapter({"wh-1": [item("p-1", "3.25"), item("p-2", "8", "л")]}),
            self.connection,
            make_job(self.connection),
            at=later,
        )

        self.assertEqual(AccountingStockLevel.objects.filter(warehouse=self.wh1).count(), 2)
        level = self.level(self.wh1, "p-1")
        self.assertEqual(level.stock_qty, Decimal("3.25"))
        self.assertEqual(level.taken_at, later)
        newest = AccountingStockSnapshot.objects.filter(warehouse=self.wh1).order_by("-taken_at").first()
        self.assertEqual(level.snapshot_id, newest.id)
        self.assertEqual(AccountingStockSnapshot.objects.filter(warehouse=self.wh1).count(), 2)

    def test_zero_quantity_saved_as_zero_and_source_error_saved_as_null(self):
        sync_stock(
            self.adapter(
                {
                    "wh-1": [
                        item("p-1", "0"),
                        item("p-2", "-1.25"),
                        item("p-3", None, reliability=Reliability.SOURCE_ERROR),
                    ]
                }
            ),
            self.connection,
            make_job(self.connection),
            at=AT,
        )

        zero = self.level(self.wh1, "p-1")
        self.assertIsNotNone(zero.stock_qty)
        self.assertEqual(zero.stock_qty, Decimal("0"))
        self.assertEqual(self.level(self.wh1, "p-2").stock_qty, Decimal("-1.25"))
        unknown = self.level(self.wh1, "p-3")
        self.assertIsNone(unknown.stock_qty)
        self.assertEqual(unknown.reliability, Reliability.SOURCE_ERROR)
        # The source reported an item error, so the snapshot is partial, never complete.
        snapshot = AccountingStockSnapshot.objects.get(warehouse=self.wh1)
        self.assertEqual(snapshot.status, SnapshotStatus.PARTIAL)
        self.assertEqual(snapshot.error_count, 1)

    def test_missing_product_not_zeroed_without_confirmed_omits_zero_balances(self):
        self.seed_level(self.wh1, "p-2", "5")
        for capabilities in (
            {},
            {"omits_zero_balances": None},
            {"omits_zero_balances": "true"},
            {"omits_zero_balances": False},
        ):
            with self.subTest(capabilities=capabilities):
                self.connection.capabilities = capabilities
                self.connection.save(update_fields=["capabilities"])

                sync_stock(
                    self.adapter({"wh-1": [item("p-1", "1")]}), self.connection, make_job(self.connection), at=AT
                )

                self.assertEqual(self.level(self.wh1, "p-2").stock_qty, Decimal("5"))
                self.assertIsNone(self.level(self.wh1, "p-4"))

    def test_missing_product_zeroed_when_omits_zero_balances_confirmed(self):
        self.seed_level(self.wh1, "p-2", "5")
        self.seed_level(self.wh1, "p-3", None, reliability=Reliability.SOURCE_ERROR)
        other_wh = self.seed_level(self.wh2, "p-2", "7")

        # Both answers are non-empty (an empty answer over non-zero levels is PARTIAL, see below).
        stats = sync_stock(
            self.adapter({"wh-1": [item("p-1", "1")], "wh-2": [item("p-1", "2")]}),
            self.connection,
            make_job(self.connection),
            at=AT,
        )

        snapshot = AccountingStockSnapshot.objects.get(warehouse=self.wh1, job__isnull=False)
        self.assertEqual(snapshot.status, SnapshotStatus.COMPLETE)
        zeroed = self.level(self.wh1, "p-2")
        self.assertEqual(zeroed.stock_qty, Decimal("0"))
        self.assertEqual(zeroed.snapshot_id, snapshot.id)
        self.assertEqual(zeroed.reliability, Reliability.OK)
        self.assertEqual(self.level(self.wh1, "p-3").stock_qty, Decimal("0"))
        # Never returned by this warehouse: "no data", not zero.
        self.assertIsNone(self.level(self.wh1, "p-4"))
        # wh-2's own complete snapshot zeroes wh-2's known missing level, and never wh-1's.
        other_wh.refresh_from_db()
        self.assertEqual(other_wh.stock_qty, Decimal("0"))
        self.assertEqual(other_wh.snapshot.warehouse_id, self.wh2.id)
        self.assertEqual(stats.zero_filled, 3)

    def test_empty_answer_over_non_zero_levels_is_partial_and_keeps_them(self):
        positive = self.seed_level(self.wh1, "p-1", "5")
        zero = self.seed_level(self.wh1, "p-2", "0")
        job = make_job(self.connection)

        stats = sync_stock(self.adapter({}), self.connection, job, at=AT.replace(hour=11))

        snapshot = AccountingStockSnapshot.objects.get(warehouse=self.wh1, job=job)
        self.assertEqual((snapshot.status, snapshot.error_code), (SnapshotStatus.PARTIAL, "empty_answer"))
        for level, qty in ((positive, "5"), (zero, "0")):
            snapshot_before = level.snapshot_id
            level.refresh_from_db()
            self.assertEqual(level.stock_qty, Decimal(qty))
            self.assertEqual(level.snapshot_id, snapshot_before)
        self.assertEqual(stats.zero_filled, 0)
        error = next(e for e in stats.errors if e["external_warehouse_id"] == "wh-1")
        self.assertEqual(error["code"], "empty_answer")
        self.assertTrue(error["message"])

    def test_empty_answer_over_zero_or_null_levels_or_none_is_complete(self):
        self.seed_level(self.wh1, "p-1", "0")
        self.seed_level(self.wh1, "p-2", None, reliability=Reliability.SOURCE_ERROR)
        job = make_job(self.connection)

        sync_stock(self.adapter({}), self.connection, job, at=AT)

        statuses = dict(AccountingStockSnapshot.objects.filter(job=job).values_list("warehouse__external_id", "status"))
        # wh-1 only ever had zero/NULL levels, wh-2 has none at all.
        self.assertEqual(statuses, {"wh-1": SnapshotStatus.COMPLETE, "wh-2": SnapshotStatus.COMPLETE})

    def test_all_warehouses_failing_individually_fail_the_job_without_adapter_error(self):
        job, outcome = self.run_job(self.adapter({}, known=()))

        self.assertEqual(outcome.status, SyncStatus.FAILED)
        self.assertIsNone(outcome.error)
        self.assertTrue(outcome.ran)
        self.assertEqual(job.status, SyncStatus.FAILED)
        self.assertEqual(job.error_code, "warehouse_not_found")
        self.assertEqual(job.stats["failed"], 2)

    def test_outcome_ran_only_when_this_call_executed_the_job(self):
        job, first = self.run_job(self.adapter({"wh-1": [item("p-1", "1")]}))
        self.assertTrue(first.ran)
        with mock.patch("accounting.sync.runner.get_adapter") as get_adapter:
            again = run_sync_job(job.job_id)
        get_adapter.assert_not_called()
        self.assertFalse(again.ran)  # already finished: a redelivered task must not register twice

        lost = make_job(self.connection)
        AccountingSyncJob.objects.filter(pk=lost.pk).update(status=SyncStatus.RUNNING, started_at=AT)
        self.assertFalse(run_sync_job(lost.job_id).ran)  # the dead worker's run, not this call's

        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save(update_fields=["status"])
        skipped = run_sync_job(make_job(self.connection).job_id)
        self.assertEqual(skipped.status, SyncStatus.SKIPPED)
        self.assertTrue(skipped.ran)

    def test_partial_snapshot_is_never_zero_filled(self):
        self.seed_level(self.wh1, "p-2", "5")

        sync_stock(
            self.adapter({"wh-1": [item("p-1", "1"), item("p-3", None, reliability=Reliability.SOURCE_ERROR)]}),
            self.connection,
            make_job(self.connection),
            at=AT,
        )

        self.assertEqual(self.level(self.wh1, "p-2").stock_qty, Decimal("5"))

    def test_item_of_product_missing_from_catalogue_makes_snapshot_partial(self):
        self.seed_level(self.wh1, "p-2", "5")

        sync_stock(
            self.adapter({"wh-1": [item("p-1", "1"), item("p-new", "2")]}),
            self.connection,
            make_job(self.connection),
            at=AT,
        )

        snapshot = AccountingStockSnapshot.objects.filter(warehouse=self.wh1, job__isnull=False).get()
        self.assertEqual(snapshot.status, SnapshotStatus.PARTIAL)
        self.assertEqual(snapshot.item_count, 1)
        self.assertEqual(self.level(self.wh1, "p-2").stock_qty, Decimal("5"))

    def test_one_failed_warehouse_gives_partial_job_and_keeps_other_warehouses(self):
        # wh-2 is gone from the source: the adapter answers warehouse_not_found for it.
        job, outcome = self.run_job(self.adapter({"wh-1": [item("p-1", "4")]}, known=("wh-1",)))

        self.assertEqual(outcome.status, SyncStatus.PARTIAL)
        self.assertEqual(job.status, SyncStatus.PARTIAL)
        self.assertEqual(job.error_code, "warehouse_not_found")
        self.assertIsNone(outcome.error)
        self.assertEqual(self.level(self.wh1, "p-1").stock_qty, Decimal("4"))
        statuses = dict(AccountingStockSnapshot.objects.values_list("warehouse__external_id", "status"))
        self.assertEqual(statuses, {"wh-1": SnapshotStatus.COMPLETE, "wh-2": SnapshotStatus.FAILED})
        failed = AccountingStockSnapshot.objects.get(warehouse=self.wh2)
        self.assertEqual(failed.error_code, "warehouse_not_found")
        self.assertEqual(failed.job_id, job.job_id)

    def test_failed_warehouse_keeps_previous_levels(self):
        previous = self.seed_level(self.wh2, "p-1", "3")

        sync_stock(
            self.adapter({}, known=("wh-1",)), self.connection, make_job(self.connection), at=AT.replace(hour=11)
        )

        snapshot_before = previous.snapshot_id
        previous.refresh_from_db()
        self.assertEqual(previous.stock_qty, Decimal("3"))  # never zero-filled from a failed read
        self.assertEqual(previous.taken_at, AT)
        self.assertEqual(previous.snapshot_id, snapshot_before)
        latest = AccountingStockSnapshot.objects.filter(warehouse=self.wh2).exclude(pk=previous.snapshot_id).get()
        self.assertEqual(latest.status, SnapshotStatus.FAILED)
        self.assertNotEqual(previous.snapshot_id, latest.id)

    def test_signal_sent_once_per_venue_after_commit(self):
        wh3 = self.make_warehouse("wh-3", self.venue_a, "Бар A")
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        stock_snapshot_ready.connect(receiver)
        self.addCleanup(stock_snapshot_ready.disconnect, receiver)
        adapter = self.adapter({"wh-1": [item("p-1", "1")], "wh-3": [item("p-2", "2")]}, known=("wh-1", "wh-2", "wh-3"))

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            sync_stock(adapter, self.connection, make_job(self.connection), at=AT)
        self.assertEqual(received, [])  # nothing before commit

        for callback in callbacks:
            callback()

        self.assertEqual(len(received), 2)
        by_venue = {call["venue_id"]: call for call in received}
        self.assertEqual(set(by_venue), {self.venue_a.id, self.venue_b.id})
        call_a = by_venue[self.venue_a.id]
        self.assertEqual(call_a["connection_id"], self.connection.id)
        self.assertEqual(set(call_a["warehouse_ids"]), {self.wh1.id, wh3.id})
        self.assertEqual(
            set(call_a["snapshot_ids"]),
            set(AccountingStockSnapshot.objects.filter(venue=self.venue_a).values_list("id", flat=True)),
        )
        self.assertEqual(call_a["taken_at"], AT)
        self.assertEqual(by_venue[self.venue_b.id]["warehouse_ids"], [self.wh2.id])

    def test_failing_signal_receiver_does_not_break_the_sync(self):
        received = []

        def broken(sender, **kwargs):
            raise RuntimeError("секрет-receiver")

        def healthy(sender, **kwargs):
            received.append(kwargs["venue_id"])

        stock_snapshot_ready.connect(broken)
        stock_snapshot_ready.connect(healthy)
        self.addCleanup(stock_snapshot_ready.disconnect, broken)
        self.addCleanup(stock_snapshot_ready.disconnect, healthy)

        with self.assertLogs("accounting.sync.stock", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                sync_stock(
                    self.adapter({"wh-1": [item("p-1", "1")]}), self.connection, make_job(self.connection), at=AT
                )

        self.assertEqual(set(received), {self.venue_a.id, self.venue_b.id})
        self.assertNotIn("секрет", "\n".join(logs.output))

    def test_venue_without_stored_snapshot_gets_no_signal(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs["venue_id"])

        stock_snapshot_ready.connect(receiver)
        self.addCleanup(stock_snapshot_ready.disconnect, receiver)

        with self.captureOnCommitCallbacks(execute=True):
            sync_stock(self.adapter({}, known=("wh-1",)), self.connection, make_job(self.connection), at=AT)

        self.assertEqual(received, [self.venue_a.id])

    def test_job_stats_recorded(self):
        job, outcome = self.run_job(
            self.adapter({"wh-1": [item("p-1", "1"), item("p-2", "2", "л")], "wh-2": [item("p-1", "5")]})
        )

        self.assertEqual(outcome.status, SyncStatus.SUCCESS)
        self.assertIsNone(outcome.error)
        self.assertIsNone(outcome.retry_in)
        self.assertEqual(outcome.job.job_id, job.job_id)
        self.assertEqual(job.status, SyncStatus.SUCCESS)
        self.assertEqual(job.error_code, "")
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertLessEqual(job.started_at, job.finished_at)
        self.assertEqual(job.stats["kind"], SyncKind.STOCK)
        self.assertEqual(job.stats["warehouses"], 2)
        self.assertEqual(job.stats["complete"], 2)
        self.assertEqual(job.stats["partial"], 0)
        self.assertEqual(job.stats["failed"], 0)
        self.assertEqual(job.stats["items"], 3)
        self.assertEqual(job.stats["errors"], [])
        # taken_at is one moment for the whole sync
        self.assertEqual(len(set(AccountingStockSnapshot.objects.values_list("taken_at", flat=True))), 1)

    # -- carry-overs -------------------------------------------------------
    def test_runner_passes_product_units_and_source_version(self):
        calls = []
        self.run_job(self.adapter({}), calls=calls)
        self.assertEqual(
            calls,
            [
                {
                    "connection_id": self.connection.id,
                    "product_units": {"p-1": "кг", "p-2": "л", "p-3": "кг"},
                    "source_version": "7.9.1",
                }
            ],
        )

        self.connection.server_version = ""
        self.connection.save(update_fields=["server_version"])
        calls.clear()
        self.run_job(self.adapter({}), calls=calls)
        self.assertIsNone(calls[0]["source_version"])

    def test_only_live_linked_present_warehouses_are_read(self):
        venue_c = make_venue("Заведение C")
        link(self.connection, venue_c, status=LinkStatus.INACTIVE)
        self.make_warehouse("wh-inactive", venue_c, "Склад отключённого заведения")
        self.make_warehouse("wh-gone", self.venue_a, "Пропавший склад", is_present_in_source=False)
        self.make_warehouse("wh-none", None, "Без заведения")
        adapter = self.adapter({}, cls=RecordingStockAdapter)

        sync_stock(adapter, self.connection, make_job(self.connection), at=AT)

        self.assertEqual(adapter.stock_requests, [(["wh-1", "wh-2"], AT)])

    def test_adapter_error_before_work_fails_job_and_is_returned(self):
        error = AdapterError("auth_error", "Сервер iiko не принял логин и пароль.", False)
        job = make_job(self.connection)

        def refusing_get_adapter(connection, **options):
            raise error

        with mock.patch("accounting.sync.runner.get_adapter", refusing_get_adapter):
            outcome = run_sync_job(job.job_id)

        job.refresh_from_db()
        self.assertIs(outcome.error, error)
        self.assertEqual(outcome.status, SyncStatus.FAILED)
        self.assertEqual(job.status, SyncStatus.FAILED)
        self.assertEqual(job.error_code, "auth_error")
        self.assertEqual(job.stats["error"]["code"], "auth_error")
        self.assertIsNotNone(job.finished_at)
        self.assertFalse(AccountingStockSnapshot.objects.exists())

    def test_adapter_error_mid_read_keeps_stored_warehouses_and_is_returned(self):
        job, outcome = self.run_job(self.adapter({"wh-1": [item("p-1", "4")]}, cls=AbortAfterFirstAdapter))

        self.assertEqual(outcome.status, SyncStatus.PARTIAL)
        self.assertIsInstance(outcome.error, AdapterError)
        self.assertEqual(outcome.error.code, "rate_limited")
        self.assertEqual(job.error_code, "rate_limited")
        self.assertEqual(self.level(self.wh1, "p-1").stock_qty, Decimal("4"))
        unread = AccountingStockSnapshot.objects.get(warehouse=self.wh2)
        self.assertEqual(unread.status, SnapshotStatus.FAILED)
        self.assertEqual(unread.error_code, "rate_limited")

    def test_unexpected_exception_fails_job_with_safe_message_and_reraises(self):
        job = make_job(self.connection)
        with (
            mock.patch("accounting.sync.runner.get_adapter", adapter_factory(self.adapter({}))),
            mock.patch("accounting.sync.runner.sync_stock", side_effect=RuntimeError("token=abc секрет")),
        ):
            with self.assertRaises(RuntimeError):
                run_sync_job(job.job_id)

        job.refresh_from_db()
        self.assertEqual(job.status, SyncStatus.FAILED)
        self.assertEqual(job.error_code, "internal_error")
        self.assertIsNotNone(job.finished_at)
        self.assertNotIn("секрет", str(job.stats))
        self.assertNotIn("token", str(job.stats))
        self.assertNotIn("Traceback", str(job.stats))

    def test_finished_job_is_a_no_op_on_second_run(self):
        adapter = self.adapter({"wh-1": [item("p-1", "4")]})
        job, first = self.run_job(adapter)
        snapshots = AccountingStockSnapshot.objects.count()

        with mock.patch("accounting.sync.runner.get_adapter") as get_adapter:
            again = run_sync_job(job.job_id)

        get_adapter.assert_not_called()
        self.assertEqual(again.status, first.status)
        self.assertEqual(AccountingStockSnapshot.objects.count(), snapshots)

    def test_job_left_running_by_a_dead_worker_is_failed_not_rerun(self):
        job = make_job(self.connection)
        AccountingSyncJob.objects.filter(pk=job.pk).update(status=SyncStatus.RUNNING, started_at=AT)

        with mock.patch("accounting.sync.runner.get_adapter") as get_adapter:
            outcome = run_sync_job(job.job_id)

        get_adapter.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, SyncStatus.FAILED)
        self.assertEqual(job.error_code, "worker_lost")
        self.assertEqual(outcome.error.code, "worker_lost")
        self.assertFalse(AccountingStockSnapshot.objects.exists())

    def test_disabled_connection_skips_job(self):
        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save(update_fields=["status"])

        job = make_job(self.connection)

        with mock.patch("accounting.sync.runner.get_adapter") as get_adapter:
            outcome = run_sync_job(job.job_id)

        get_adapter.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(outcome.status, SyncStatus.SKIPPED)
        self.assertEqual(job.status, SyncStatus.SKIPPED)
        self.assertEqual(job.error_code, "connection_disabled")

    def test_stock_job_without_readable_warehouses_is_skipped(self):
        AccountingWarehouse.objects.filter(connection=self.connection).update(is_present_in_source=False)
        adapter = self.adapter({}, cls=RecordingStockAdapter)

        job, outcome = self.run_job(adapter)

        self.assertEqual(adapter.stock_requests, [])
        self.assertEqual(outcome.status, SyncStatus.SKIPPED)
        self.assertEqual(job.error_code, "no_linked_warehouses")

    def test_stock_of_second_connection_with_same_external_ids_is_independent(self):
        venue_c = make_venue("Заведение C")
        other = make_connection("https://b.iiko.example/resto", capabilities={"omits_zero_balances": True})
        link(other, venue_c)
        other_wh = self.make_warehouse("wh-1", venue_c, "Кухня", connection=other)
        AccountingProduct.objects.create(connection=other, external_id="p-1", name="Товар p-1")
        AccountingProduct.objects.create(connection=other, external_id="p-2", name="Товар p-2")
        other_level = self.seed_level(other_wh, "p-2", "9")

        sync_stock(self.adapter({"wh-1": [item("p-1", "1")]}), self.connection, make_job(self.connection), at=AT)

        other_level.refresh_from_db()
        self.assertEqual(other_level.stock_qty, Decimal("9"))
        self.assertIsNone(self.level(other_wh, "p-1"))
        self.assertFalse(AccountingStockSnapshot.objects.filter(connection=other, job__isnull=False).exists())
        own = self.level(self.wh1, "p-1")
        self.assertEqual(own.product.connection_id, self.connection.id)

    # -- fix round 1 ---------------------------------------------------------------------------------------
    def receive_venues(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs["venue_id"])

        stock_snapshot_ready.connect(receiver)
        self.addCleanup(stock_snapshot_ready.disconnect, receiver)
        return received

    def test_mixed_case_ids_match_stored_products_and_warehouses(self):
        AccountingWarehouse.objects.filter(pk=self.wh1.pk).update(external_id="5A1F-WH-UPPER")
        upper = AccountingProduct.objects.create(
            connection=self.connection, external_id="7E57-P-UPPER", name="Товар", unit_name="кг"
        )
        adapter = self.adapter(
            {"5A1F-WH-UPPER": [item("7e57-p-upper", "2"), item(" P-1 ", "3")], "wh-2": [item("P-2", "1", "л")]},
            known=("5A1F-WH-UPPER", "wh-2"),
            cls=SwappedCaseAdapter,
        )

        stats = sync_stock(adapter, self.connection, make_job(self.connection), at=AT)

        self.assertEqual((stats.complete, stats.partial, stats.failed), (2, 0, 0))
        self.assertEqual(stats.errors, [])
        self.assertEqual(AccountingStockLevel.objects.get(warehouse=self.wh1, product=upper).stock_qty, Decimal("2"))
        self.assertEqual(self.level(self.wh1, "p-1").stock_qty, Decimal("3"))
        self.assertEqual(self.level(self.wh2, "p-2").stock_qty, Decimal("1"))
        # What is stored as external_id is never rewritten.
        self.assertEqual(AccountingWarehouse.objects.get(pk=self.wh1.pk).external_id, "5A1F-WH-UPPER")
        self.assertEqual(AccountingProduct.objects.get(pk=upper.pk).external_id, "7E57-P-UPPER")

    def test_missing_quantity_is_null_source_error_whatever_the_item_says(self):
        self.seed_level(self.wh1, "p-3", "5")
        cases = (
            ("adapter forgot to report it", SilentQuantityAdapter, Reliability.OK),
            ("adapter reported it", FakeAdapter, Reliability.UNIT_UNKNOWN),
        )
        for label, cls, reliability in cases:
            with self.subTest(label):
                adapter = self.adapter(
                    {"wh-1": [item("p-1", None, reliability=reliability), item("p-2", "1", "л")]}, cls=cls
                )
                job = make_job(self.connection)

                sync_stock(adapter, self.connection, job, at=AT)

                level = self.level(self.wh1, "p-1")
                self.assertIsNone(level.stock_qty)
                self.assertEqual(level.reliability, Reliability.SOURCE_ERROR)
                snapshot = AccountingStockSnapshot.objects.get(warehouse=self.wh1, job=job)
                self.assertEqual(snapshot.status, SnapshotStatus.PARTIAL)
                self.assertEqual(snapshot.error_code, "quantity_missing")
                self.assertEqual(snapshot.error_count, 1)
                self.assertEqual(self.level(self.wh1, "p-3").stock_qty, Decimal("5"))  # partial: no zero-fill

    def test_duplicate_product_in_one_answer_is_stored_as_null_source_error(self):
        job = make_job(self.connection)

        sync_stock(
            self.adapter({"wh-1": [item("p-1", "3"), item("P-1", "4"), item("p-2", "1", "л")]}),
            self.connection,
            job,
            at=AT,
        )

        levels = AccountingStockLevel.objects.filter(warehouse=self.wh1, product__external_id="p-1")
        self.assertEqual(levels.count(), 1)
        self.assertIsNone(levels.get().stock_qty)
        self.assertEqual(levels.get().reliability, Reliability.SOURCE_ERROR)
        self.assertEqual(self.level(self.wh1, "p-2").stock_qty, Decimal("1"))
        snapshot = AccountingStockSnapshot.objects.get(warehouse=self.wh1, job=job)
        self.assertEqual(snapshot.status, SnapshotStatus.PARTIAL)
        self.assertEqual(snapshot.error_code, "duplicate_item")

    def test_database_error_rolls_back_only_that_warehouse_and_writes_failed_header(self):
        previous = self.seed_level(self.wh1, "p-1", "3")
        real_bulk_create = AccountingStockLevel.objects.bulk_create

        def flaky_bulk_create(objs, *args, **kwargs):
            objs = list(objs)
            if objs and objs[0].warehouse_id == self.wh1.id:
                raise DataError("numeric field overflow")
            return real_bulk_create(objs, *args, **kwargs)

        job = make_job(self.connection)
        with mock.patch.object(AccountingStockLevel.objects, "bulk_create", side_effect=flaky_bulk_create):
            stats = sync_stock(
                self.adapter({"wh-1": [item("p-1", "9")], "wh-2": [item("p-1", "5")]}), self.connection, job, at=AT
            )

        # The COMPLETE header of wh-1 was rolled back with its levels; only the FAILED one remains.
        headers = AccountingStockSnapshot.objects.filter(warehouse=self.wh1, job=job)
        self.assertEqual(list(headers.values_list("status", "error_code")), [(SnapshotStatus.FAILED, "store_failed")])
        snapshot_before = previous.snapshot_id
        previous.refresh_from_db()
        self.assertEqual(previous.stock_qty, Decimal("3"))
        self.assertEqual(previous.snapshot_id, snapshot_before)
        self.assertEqual(self.level(self.wh2, "p-1").stock_qty, Decimal("5"))
        self.assertEqual((stats.complete, stats.failed), (1, 1))

    def test_unexpected_error_in_one_warehouse_fails_only_that_warehouse(self):
        real_store = stock_module._store_warehouse
        received = self.receive_venues()

        def flaky_store(connection, warehouse, *args, **kwargs):
            if warehouse.external_id == "wh-1":
                raise RuntimeError("секрет-store")
            return real_store(connection, warehouse, *args, **kwargs)

        job = make_job(self.connection)
        with (
            mock.patch("accounting.sync.stock._store_warehouse", side_effect=flaky_store),
            self.assertLogs("accounting.sync.stock", level="ERROR") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            stats = sync_stock(
                self.adapter({"wh-1": [item("p-1", "9")], "wh-2": [item("p-1", "5")]}), self.connection, job, at=AT
            )

        failed = AccountingStockSnapshot.objects.get(warehouse=self.wh1, job=job)
        self.assertEqual((failed.status, failed.error_code), (SnapshotStatus.FAILED, "store_failed"))
        self.assertEqual(self.level(self.wh2, "p-1").stock_qty, Decimal("5"))
        self.assertEqual((stats.complete, stats.failed), (1, 1))
        self.assertEqual(received, [self.venue_b.id])
        self.assertNotIn("секрет", "\n".join(logs.output))

    def test_interrupt_still_announces_already_stored_venues(self):
        real_store = stock_module._store_warehouse
        received = self.receive_venues()

        def interrupted_store(connection, warehouse, *args, **kwargs):
            if warehouse.external_id == "wh-2":
                raise Interrupt
            return real_store(connection, warehouse, *args, **kwargs)

        with (
            mock.patch("accounting.sync.stock._store_warehouse", side_effect=interrupted_store),
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            with self.assertRaises(Interrupt):
                sync_stock(
                    self.adapter({"wh-1": [item("p-1", "1")]}), self.connection, make_job(self.connection), at=AT
                )

        for callback in callbacks:
            callback()
        self.assertEqual(received, [self.venue_a.id])

    def test_adapter_bug_mid_read_fails_unread_warehouses_announces_stored_and_reraises(self):
        received = self.receive_venues()
        job = make_job(self.connection)

        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError):
                sync_stock(
                    self.adapter({"wh-1": [item("p-1", "1")]}, cls=BrokenAfterFirstAdapter), self.connection, job, at=AT
                )

        unread = AccountingStockSnapshot.objects.get(warehouse=self.wh2, job=job)
        self.assertEqual((unread.status, unread.error_code), (SnapshotStatus.FAILED, "internal_error"))
        self.assertEqual(self.level(self.wh1, "p-1").stock_qty, Decimal("1"))
        self.assertEqual(received, [self.venue_a.id])

    def test_stock_sync_query_count_does_not_grow_with_products(self):
        AccountingProduct.objects.bulk_create(
            AccountingProduct(
                connection=self.connection, external_id=f"bulk-{n:03d}", name=f"Товар {n}", unit_name="кг"
            )
            for n in range(200)
        )
        stock = {
            "wh-1": [item(f"bulk-{n:03d}", str(n)) for n in range(200)],
            "wh-2": [item(f"BULK-{n:03d}", "1") for n in range(200)],
        }
        self.seed_level(self.wh1, "p-1", "5")  # will be zero-filled

        with CaptureQueriesContext(db_connection) as queries:
            stats = sync_stock(self.adapter(stock), self.connection, make_job(self.connection), at=AT)

        self.assertEqual((stats.complete, stats.items, stats.zero_filled), (2, 400, 1))
        # Per warehouse: savepoint, header, one level upsert, zero-fill, release; plus the two initial reads.
        self.assertLess(len(queries.captured_queries), 25, [q["sql"][:80] for q in queries.captured_queries])

    def test_runner_reads_the_product_map_once_with_case_insensitive_unit_keys(self):
        AccountingProduct.objects.create(
            connection=self.connection, external_id="7E57-UP", name="Товар", unit_name="шт"
        )
        calls = []
        with (
            mock.patch("accounting.sync.stock.load_products", side_effect=AssertionError("second product read")),
            mock.patch("accounting.sync.runner.sync_stock", wraps=sync_stock) as wrapped,
        ):
            job, outcome = self.run_job(self.adapter({"wh-1": [item("7e57-up", "2", "шт")]}), calls=calls)

        self.assertEqual(outcome.status, SyncStatus.SUCCESS)
        self.assertEqual(calls[0]["product_units"], {"p-1": "кг", "p-2": "л", "p-3": "кг", "7e57-up": "шт"})
        passed = wrapped.call_args.kwargs["products"]
        self.assertEqual(set(passed), {"p-1", "p-2", "p-3", "p-4", "7e57-up"})
        self.assertEqual(self.level(self.wh1, "7E57-UP").stock_qty, Decimal("2"))

    def test_finish_does_not_overwrite_a_job_failed_meanwhile(self):
        job = make_job(self.connection)

        def sync_then_reaped(adapter, connection, sync_job, **kwargs):
            stats = sync_stock(adapter, connection, sync_job, **kwargs)
            # recover_stalled_jobs (Task 12) or another runner marked it lost while we were working.
            AccountingSyncJob.objects.filter(pk=sync_job.pk).update(
                status=SyncStatus.FAILED, error_code="worker_lost", stats={"reaped": True}
            )
            return stats

        with (
            mock.patch(
                "accounting.sync.runner.get_adapter", adapter_factory(self.adapter({"wh-1": [item("p-1", "1")]}))
            ),
            mock.patch("accounting.sync.runner.sync_stock", side_effect=sync_then_reaped),
            self.assertLogs("accounting.sync.runner", level="WARNING"),
        ):
            outcome = run_sync_job(job.job_id)

        job.refresh_from_db()
        self.assertEqual(outcome.status, SyncStatus.FAILED)
        self.assertEqual(job.status, SyncStatus.FAILED)
        self.assertEqual(job.error_code, "worker_lost")
        self.assertEqual(job.stats, {"reaped": True})
        self.assertFalse(outcome.ran)  # lost the race: whoever wrote worker_lost registers it


class BusyGuardTests(StockFixtureMixin, TransactionTestCase):
    """Advisory locks are session-level, so the busy path needs real commits."""

    def setUp(self):
        self.make_world()

    def test_busy_connection_leaves_job_queued_with_retry(self):
        job = make_job(self.connection)
        with mock.patch("accounting.sync.runner.get_adapter") as get_adapter:
            with sync_guard(self.connection.id, self.connection.host, settings.ACCOUNTING_HOST_SLOTS) as held:
                self.assertTrue(held.acquired)
                outcome = run_sync_job(job.job_id)
                again = run_sync_job(job.job_id)

        get_adapter.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, SyncStatus.QUEUED)
        self.assertIsNone(job.started_at)
        self.assertEqual(outcome.status, SyncStatus.QUEUED)
        self.assertIsNone(outcome.error)
        self.assertGreaterEqual(outcome.retry_in, 30)
        self.assertLessEqual(outcome.retry_in, 60)
        self.assertEqual(outcome.retry_in, again.retry_in)  # deterministic per job
        self.assertFalse(outcome.ran)
        self.assertEqual(outcome.retry_in, busy_retry_delay(job.job_id))

        # Once the guard is free the same job runs normally.
        with mock.patch("accounting.sync.runner.get_adapter", adapter_factory(self.adapter({}))):
            done = run_sync_job(job.job_id)
        self.assertEqual(done.status, SyncStatus.SUCCESS)

    def test_busy_host_slot_also_leaves_job_queued(self):
        other = make_connection("https://a.iiko.example/other")  # same host, different connection
        job = make_job(self.connection)
        with (
            override_settings(ACCOUNTING_HOST_SLOTS=1),
            mock.patch("accounting.sync.runner.get_adapter") as get_adapter,
            sync_guard(other.id, other.host, 1),
        ):
            outcome = run_sync_job(job.job_id)

        get_adapter.assert_not_called()
        self.assertEqual(outcome.status, SyncStatus.QUEUED)
        self.assertIsNotNone(outcome.retry_in)

    def test_busy_outcome_reports_the_real_status_of_a_running_job(self):
        job = make_job(self.connection)
        AccountingSyncJob.objects.filter(pk=job.pk).update(status=SyncStatus.RUNNING, started_at=AT)

        with (
            mock.patch("accounting.sync.runner.get_adapter") as get_adapter,
            sync_guard(self.connection.id, self.connection.host, settings.ACCOUNTING_HOST_SLOTS),
        ):
            outcome = run_sync_job(job.job_id)

        get_adapter.assert_not_called()
        self.assertEqual(outcome.status, SyncStatus.RUNNING)
        self.assertIsNotNone(outcome.retry_in)
        job.refresh_from_db()
        self.assertEqual((job.status, job.started_at), (SyncStatus.RUNNING, AT))

    def test_guard_is_released_after_a_crash(self):
        job = make_job(self.connection)
        with (
            mock.patch("accounting.sync.runner.get_adapter", adapter_factory(self.adapter({}))),
            mock.patch("accounting.sync.runner.sync_stock", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                run_sync_job(job.job_id)

        with sync_guard(self.connection.id, self.connection.host, settings.ACCOUNTING_HOST_SLOTS) as guard:
            self.assertTrue(guard.acquired)
