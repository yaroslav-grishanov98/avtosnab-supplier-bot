from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from accounting.dto import AdapterError, ConsumptionEventDTO, ConsumptionResult, OperationKind
from accounting.models import (
    AccountingConsumptionCoverage,
    AccountingConsumptionDaily,
    AccountingProduct,
    AccountingWarehouse,
    CoverageStatus,
    SyncKind,
    SyncStatus,
)
from accounting.sync.consumption import (
    CONSUMPTION_UNSUPPORTED,
    ConsumptionStats,
    load_product_catalog,
    sync_consumption,
)
from accounting.sync.runner import run_sync_job
from accounting.test_support.sync import adapter_factory, link, make_connection, make_job, make_venue

TODAY = date(2026, 10, 23)
D1, D2, D3 = TODAY - timedelta(days=1), TODAY - timedelta(days=2), TODAY - timedelta(days=3)
TZ = timezone.get_default_timezone()
# A day settles ACCOUNTING_CONSUMPTION_SETTLE_HOURS (6) after its end; D1 ended at 2026-10-23 00:00.
SETTLED = datetime(2026, 10, 23, 12, 0, tzinfo=TZ)
EARLY = datetime(2026, 10, 23, 2, 0, tzinfo=TZ)
CATALOGUE_AT = datetime(2026, 10, 23, 8, 0, tzinfo=TZ)


def event(product, warehouse, day, qty, kind="SESSION_WRITEOFF", uom="кг"):
    return ConsumptionEventDTO(
        external_product_id=product,
        external_warehouse_id=warehouse,
        occurred_at=datetime(day.year, day.month, day.day, tzinfo=TZ),
        qty=Decimal(qty),
        source_uom=uom,
        transaction_type=kind,
        kind=OperationKind.UNKNOWN,
    )


class ScriptedAdapter:
    """Answers read_consumption from ``script(warehouse_ids, day_from, day_to) -> ConsumptionResult``."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def read_consumption(self, warehouse_ids, day_from, day_to):
        self.calls.append((list(warehouse_ids), day_from, day_to))
        return self.script(list(warehouse_ids), day_from, day_to)


def complete(events_by_pair=None):
    """Every asked pair is covered; ``events_by_pair`` maps (warehouse, day) -> events."""
    events_by_pair = events_by_pair or {}

    def script(warehouse_ids, day_from, day_to):
        events = [e for wh in warehouse_ids for e in events_by_pair.get((wh, day_from), [])]
        return ConsumptionResult(events=events, covered={(wh, day_from): True for wh in warehouse_ids})

    return script


class ConsumptionFixtureMixin:
    def make_world(self, capabilities=None):
        self.venue_a = make_venue("Заведение A")
        self.venue_b = make_venue("Заведение B")
        self.connection = make_connection(
            capabilities={"read_consumption": True} if capabilities is None else capabilities,
            catalog_synced_at=CATALOGUE_AT,
        )
        link(self.connection, self.venue_a)
        link(self.connection, self.venue_b)
        self.wh1 = AccountingWarehouse.objects.create(
            connection=self.connection, venue=self.venue_a, external_id="wh-1", name="Кухня"
        )
        self.wh2 = AccountingWarehouse.objects.create(
            connection=self.connection, venue=self.venue_b, external_id="wh-2", name="Бар"
        )
        self.products = {
            ext: AccountingProduct.objects.create(
                connection=self.connection, external_id=ext, article=ext[-1], name=f"Товар {ext}", unit_name=unit
            )
            for ext, unit in (("p-1", "кг"), ("p-2", "л"), ("p-3", "кг"))
        }

    def sync(self, adapter, **kwargs):
        job = make_job(self.connection, kind=SyncKind.CONSUMPTION)
        return sync_consumption(adapter, self.connection, job, today=TODAY, **kwargs)

    def rows(self, **filters):
        return AccountingConsumptionDaily.objects.filter(**filters)

    def coverage(self, wh, day):
        return AccountingConsumptionCoverage.objects.filter(warehouse=wh, day=day).first()


@override_settings(TIME_ZONE="Europe/Moscow", ACCOUNTING_CONSUMPTION_WINDOW_DAYS=3)
class SyncConsumptionTests(ConsumptionFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_excluded_transaction_types_are_not_stored(self):
        pair = {
            ("wh-1", D1): [
                event("p-1", "wh-1", D1, "2", "WRITEOFF"),
                event("p-1", "wh-1", D1, "9", "INVOICE"),
                event("p-1", "wh-1", D1, "9", "SALES_REVENUE"),
                event("p-1", "wh-1", D1, "9", "DISCOUNT"),
            ]
        }
        self.sync(ScriptedAdapter(complete(pair)))
        self.assertEqual(list(self.rows(warehouse=self.wh1).values_list("transaction_type", "qty")), [("WRITEOFF", 2)])

    def test_rows_are_aggregated_per_transaction_type(self):
        pair = {
            ("wh-1", D1): [
                event("p-1", "wh-1", D1, "1.5", "SESSION_WRITEOFF"),
                event("p-1", "wh-1", D1, "0.5", "SESSION_WRITEOFF"),
                event("p-1", "wh-1", D1, "3", "WRITEOFF"),
            ]
        }
        self.sync(ScriptedAdapter(complete(pair)))
        stored = {t: q for t, q in self.rows(warehouse=self.wh1, day=D1).values_list("transaction_type", "qty")}
        self.assertEqual(stored, {"SESSION_WRITEOFF": Decimal("2"), "WRITEOFF": Decimal("3")})

    def test_unknown_transaction_type_is_stored_as_is(self):
        self.sync(ScriptedAdapter(complete({("wh-1", D1): [event("p-2", "wh-1", D1, "4", "TRANSFER", "л")]})))
        row = self.rows(warehouse=self.wh1).get()
        self.assertEqual((row.transaction_type, row.qty, row.source_uom), ("TRANSFER", Decimal("4"), "л"))

    def test_covered_day_without_events_has_coverage_and_no_rows(self):
        stats = self.sync(ScriptedAdapter(complete()))
        self.assertFalse(self.rows().exists())
        self.assertEqual(self.coverage(self.wh1, D1).status, CoverageStatus.COMPLETE)  # a real zero
        self.assertEqual((stats.complete, stats.failed, stats.remaining), (6, 0, 0))

    def test_uncovered_day_has_no_coverage(self):
        def script(warehouse_ids, day_from, day_to):
            ok = day_from != D2  # the report of D2 failed
            return ConsumptionResult(events=[], covered={(wh, day_from): ok for wh in warehouse_ids})

        stats = self.sync(ScriptedAdapter(script))
        self.assertIsNone(self.coverage(self.wh1, D2))  # "no data", not zero
        self.assertEqual(self.coverage(self.wh1, D1).status, CoverageStatus.COMPLETE)
        self.assertEqual((stats.failed, stats.complete), (2, 4))
        self.assertEqual({e["code"] for e in stats.errors}, {"day_not_read"})

    def test_partial_warehouse_read_gives_partial_coverage(self):
        def script(warehouse_ids, day_from, day_to):
            partial = frozenset({("wh-1", day_from)})
            return ConsumptionResult(
                events=[event("p-1", "wh-1", day_from, "5")],
                covered={(wh, day_from): (wh, day_from) not in partial for wh in warehouse_ids},
                partial=partial,
            )

        stats = self.sync(ScriptedAdapter(script))
        self.assertEqual(self.coverage(self.wh1, D1).status, CoverageStatus.PARTIAL)
        self.assertEqual(self.coverage(self.wh2, D1).status, CoverageStatus.COMPLETE)
        self.assertEqual(self.rows(warehouse=self.wh1, day=D1).get().qty, Decimal("5"))  # mapped rows are kept
        self.assertEqual(stats.partial, 3)

    def test_product_missing_from_the_stored_catalogue_makes_the_pair_partial(self):
        pair = {("wh-1", D1): [event("p-1", "wh-1", D1, "1"), event("p-new", "wh-1", D1, "7")]}
        self.sync(ScriptedAdapter(complete(pair)))
        self.assertEqual(self.coverage(self.wh1, D1).status, CoverageStatus.PARTIAL)
        self.assertEqual(self.rows(warehouse=self.wh1, day=D1).count(), 1)

    def test_today_is_never_fetched_as_complete_day(self):
        adapter = ScriptedAdapter(complete())
        self.sync(adapter)
        days = sorted({day for _, day, _ in adapter.calls})
        self.assertEqual(days, [D3, D2, D1])
        self.assertNotIn(TODAY, days)
        self.assertFalse(AccountingConsumptionCoverage.objects.filter(day__gte=TODAY).exists())

    def test_newest_day_is_asked_first_one_call_per_day(self):
        adapter = ScriptedAdapter(complete())
        self.sync(adapter)
        self.assertEqual([(day_from, day_to) for _, day_from, day_to in adapter.calls], [(D1, D1), (D2, D2), (D3, D3)])
        self.assertEqual(adapter.calls[0][0], ["wh-1", "wh-2"])

    def test_rerun_is_idempotent_for_same_day(self):
        pair = {("wh-1", D1): [event("p-1", "wh-1", D1, "2")]}
        self.sync(ScriptedAdapter(complete(pair)))
        AccountingConsumptionCoverage.objects.update(fetched_at=SETTLED)  # read after the days settled
        second = ScriptedAdapter(complete(pair))
        stats = self.sync(second)
        self.assertEqual(second.calls, [])  # covered pairs are never asked again
        self.assertEqual(self.rows(warehouse=self.wh1, day=D1).count(), 1)
        self.assertEqual((stats.complete, stats.partial, stats.failed, stats.remaining), (0, 0, 0, 0))

    def test_only_uncovered_pairs_are_requested(self):
        AccountingConsumptionCoverage.objects.create(
            connection=self.connection, venue=self.venue_a, warehouse=self.wh1, day=D1,
            status=CoverageStatus.COMPLETE, fetched_at=SETTLED,
        )  # fmt: skip
        adapter = ScriptedAdapter(complete())
        self.sync(adapter)
        asked = {(tuple(ids), day) for ids, day, _ in adapter.calls}
        self.assertIn((("wh-2",), D1), asked)
        self.assertNotIn((("wh-1", "wh-2"), D1), asked)

    def test_a_day_read_before_it_settled_is_read_again_and_its_rows_are_replaced(self):
        pair = {("wh-1", D1): [event("p-1", "wh-1", D1, "2")]}
        self.sync(ScriptedAdapter(complete(pair)))
        # Read shortly after midnight: a late shift closing or a back-dated document may still add rows.
        AccountingConsumptionCoverage.objects.update(fetched_at=EARLY)
        late = {("wh-1", D1): [event("p-1", "wh-1", D1, "2.5"), event("p-2", "wh-1", D1, "1", uom="л")]}
        adapter = ScriptedAdapter(complete(late))
        self.sync(adapter)
        self.assertTrue(any(day == D1 for _, day, _ in adapter.calls))
        self.assertEqual(
            sorted(self.rows(warehouse=self.wh1, day=D1).values_list("product__external_id", "qty")),
            [("p-1", Decimal("2.5")), ("p-2", Decimal("1"))],
        )  # replaced, not duplicated
        self.assertGreater(self.coverage(self.wh1, D1).fetched_at, EARLY)

    def test_a_day_read_after_it_settled_is_not_read_again(self):
        self.sync(ScriptedAdapter(complete()))
        AccountingConsumptionCoverage.objects.update(fetched_at=SETTLED)
        adapter = ScriptedAdapter(complete())
        self.sync(adapter)
        self.assertEqual(adapter.calls, [])

    def test_a_partial_pair_is_not_reread_until_the_catalogue_is_newer(self):
        def partial_script(warehouse_ids, day_from, day_to):
            return ConsumptionResult(
                events=[], covered={(wh, day_from): False for wh in warehouse_ids},
                partial=frozenset((wh, day_from) for wh in warehouse_ids),
            )  # fmt: skip

        self.sync(ScriptedAdapter(partial_script))
        AccountingConsumptionCoverage.objects.update(fetched_at=SETTLED)
        again = ScriptedAdapter(complete())
        self.sync(again)
        self.assertEqual(again.calls, [])  # known incomplete: not re-asked every session
        self.connection.catalog_synced_at = SETTLED + timedelta(hours=6)  # a newer catalogue may map the products
        self.connection.save(update_fields=["catalog_synced_at"])
        after_catalog = ScriptedAdapter(complete())
        self.sync(after_catalog)
        self.assertEqual(len(after_catalog.calls), 3)
        self.assertEqual(self.coverage(self.wh1, D1).status, CoverageStatus.COMPLETE)

    def test_stop_keeps_what_was_read_and_counts_the_rest_as_remaining(self):
        stop = AdapterError("rate_limited", "Сервер iiko просит снизить частоту запросов.", True, 60.0)

        def script(warehouse_ids, day_from, day_to):
            if day_from == D1:
                return ConsumptionResult(
                    events=[event("p-1", "wh-1", D1, "1")],
                    covered={("wh-1", D1): True, ("wh-2", D1): False},
                    stopped=stop,
                )
            raise AssertionError("nothing is asked after a stop")

        adapter = ScriptedAdapter(script)
        stats = self.sync(adapter)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(self.coverage(self.wh1, D1).status, CoverageStatus.COMPLETE)
        self.assertIsNone(self.coverage(self.wh2, D1))
        self.assertEqual((stats.complete, stats.failed, stats.remaining), (1, 0, 5))
        self.assertEqual(stats.stopped.code, "rate_limited")
        self.assertEqual(stats.to_json()["stopped"], {"code": "rate_limited", "message": stop.message})

    def test_zero_quantities_are_not_stored(self):
        self.sync(
            ScriptedAdapter(complete({("wh-1", D1): [event("p-1", "wh-1", D1, "0"), event("p-2", "wh-1", D1, "1")]}))
        )
        self.assertEqual(list(self.rows(warehouse=self.wh1).values_list("product__external_id", flat=True)), ["p-2"])

    def test_quantities_are_rounded_to_the_column_scale(self):
        self.sync(ScriptedAdapter(complete({("wh-1", D1): [event("p-1", "wh-1", D1, "0.12345678")]})))
        self.assertEqual(self.rows(warehouse=self.wh1).get().qty, Decimal("0.123457"))

    def test_unlinked_warehouse_is_not_read(self):
        self.connection.venue_links.filter(venue=self.venue_b).update(status="inactive")
        adapter = ScriptedAdapter(complete())
        stats = self.sync(adapter)
        self.assertTrue(all(ids == ["wh-1"] for ids, _, _ in adapter.calls))
        self.assertEqual(stats.warehouses, 1)

    def test_warehouse_ids_are_matched_ignoring_case(self):
        def script(warehouse_ids, day_from, day_to):
            return ConsumptionResult(
                events=[event("P-1", "WH-1", day_from, "2")], covered={(wh, day_from): True for wh in warehouse_ids}
            )

        self.sync(ScriptedAdapter(script))
        self.assertEqual(self.rows(warehouse=self.wh1, day=D1).get().product.external_id, "p-1")

    def test_second_connection_with_same_external_ids_is_independent(self):
        other = make_connection(
            base_url="https://b.iiko.example/resto", capabilities={"read_consumption": True},
            catalog_synced_at=CATALOGUE_AT,
        )  # fmt: skip
        venue = make_venue("Чужое")
        link(other, venue)
        other_wh = AccountingWarehouse.objects.create(connection=other, venue=venue, external_id="wh-1", name="Кухня")
        AccountingProduct.objects.create(connection=other, external_id="p-1", name="Чужой", unit_name="кг")
        self.sync(ScriptedAdapter(complete({("wh-1", D1): [event("p-1", "wh-1", D1, "2")]})))
        self.assertTrue(self.rows(warehouse=self.wh1).exists())
        self.assertFalse(self.rows(warehouse=other_wh).exists())
        self.assertFalse(AccountingConsumptionCoverage.objects.filter(warehouse=other_wh).exists())

    def test_unsupported_connection_writes_nothing_and_reports_unsupported(self):
        self.connection.capabilities = {"read_consumption": False}
        adapter = ScriptedAdapter(complete())
        stats = self.sync(adapter)
        self.assertTrue(stats.unsupported)
        self.assertEqual((adapter.calls, AccountingConsumptionCoverage.objects.count()), ([], 0))

    def test_adapter_without_olap_support_downgrades_the_capability_for_good(self):
        def refuse(*args):
            raise AdapterError("api_not_supported", "Отчёт OLAP на этом сервере не поддерживается.", False)

        stats = self.sync(ScriptedAdapter(refuse))
        self.assertTrue(stats.unsupported)
        self.connection.refresh_from_db()
        self.assertIs(self.connection.capabilities["read_consumption"], False)

    def test_other_adapter_errors_are_not_swallowed(self):
        def fail(*args):
            raise AdapterError("forbidden", "Нет прав.", False)

        with self.assertRaises(AdapterError):
            self.sync(ScriptedAdapter(fail))
        self.connection.refresh_from_db()
        self.assertIs(self.connection.capabilities["read_consumption"], True)

    def test_stats_json_carries_the_counts_the_scheduler_needs(self):
        stats = ConsumptionStats(warehouses=2, complete=1, partial=1, failed=2, remaining=3, rows=9)
        data = stats.to_json()
        self.assertEqual(
            (data["kind"], data["remaining"], data["failed"], data["rows"]), (SyncKind.CONSUMPTION.value, 3, 2, 9)
        )

    def test_load_product_catalogue_keys_by_article_and_drops_shared_articles(self):
        AccountingProduct.objects.create(connection=self.connection, external_id="p-9", article="1", name="Двойник")
        AccountingProduct.objects.create(
            connection=self.connection, external_id="p-8", article="8", name="Удалён", is_deleted=True
        )
        catalogue = load_product_catalog(self.connection)
        self.assertNotIn("1", catalogue)  # p-1 and p-9 share article 1: ambiguous, left out
        self.assertEqual(catalogue["2"], ("p-2", "л"))
        self.assertNotIn("8", catalogue)


@override_settings(TIME_ZONE="Europe/Moscow", ACCOUNTING_CONSUMPTION_WINDOW_DAYS=2)
class ConsumptionRunnerTests(ConsumptionFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def run_job(self, adapter, calls=None):
        job = make_job(self.connection, kind=SyncKind.CONSUMPTION)
        with (
            mock.patch("accounting.sync.runner.get_adapter", adapter_factory(adapter, calls)),
            mock.patch("accounting.sync.runner.timezone.localdate", return_value=TODAY),
        ):
            outcome = run_sync_job(job.job_id)
        job.refresh_from_db()
        return job, outcome

    def test_consumption_job_runs_and_passes_the_stored_product_catalogue(self):
        calls = []
        job, outcome = self.run_job(ScriptedAdapter(complete({("wh-1", D1): [event("p-1", "wh-1", D1, "2")]})), calls)
        self.assertEqual((job.status, outcome.ran), (SyncStatus.SUCCESS, True))
        self.assertEqual(calls[0]["product_catalog"]["2"], ("p-2", "л"))
        self.assertEqual(self.rows(warehouse=self.wh1, day=D1).get().qty, Decimal("2"))
        self.assertEqual(job.stats["kind"], "consumption")

    def test_unsupported_connection_is_skipped_without_opening_a_session(self):
        self.connection.capabilities = {"read_consumption": False}
        self.connection.save(update_fields=["capabilities"])
        calls = []
        job, outcome = self.run_job(ScriptedAdapter(complete()), calls)
        self.assertEqual((job.status, job.error_code, calls), (SyncStatus.SKIPPED, CONSUMPTION_UNSUPPORTED, []))

    def test_no_linked_warehouse_is_skipped_without_opening_a_session(self):
        AccountingWarehouse.objects.filter(connection=self.connection).update(venue=None)
        calls = []
        job, _ = self.run_job(ScriptedAdapter(complete()), calls)
        self.assertEqual((job.status, job.error_code, calls), (SyncStatus.SKIPPED, "no_linked_warehouses", []))

    def test_nothing_pending_is_a_success(self):
        AccountingConsumptionCoverage.objects.bulk_create(
            AccountingConsumptionCoverage(
                connection=self.connection, venue=wh.venue, warehouse=wh, day=day,
                status=CoverageStatus.COMPLETE, fetched_at=SETTLED,
            )
            for wh in (self.wh1, self.wh2) for day in (D1, D2)
        )  # fmt: skip
        job, _ = self.run_job(ScriptedAdapter(complete()))
        self.assertEqual(job.status, SyncStatus.SUCCESS)

    def test_status_is_partial_when_some_days_are_left_and_failed_when_none_were_stored(self):
        stop = AdapterError("rate_limited", "Сервер iiko просит снизить частоту запросов.", True, 60.0)
        first = ScriptedAdapter(
            lambda ids, d, _: ConsumptionResult([], {(wh, d): d == D1 for wh in ids}, stopped=stop if d != D1 else None)
        )
        job, outcome = self.run_job(first)
        self.assertEqual(job.status, SyncStatus.PARTIAL)
        self.assertEqual(outcome.error.code, "rate_limited")

        AccountingConsumptionCoverage.objects.all().delete()
        stopped_at_once = ScriptedAdapter(
            lambda ids, d, _: ConsumptionResult([], {(wh, d): False for wh in ids}, stopped=stop)
        )
        job, outcome = self.run_job(stopped_at_once)
        self.assertEqual((job.status, job.error_code), (SyncStatus.FAILED, "rate_limited"))
        self.assertEqual((outcome.error.code, outcome.error.retry_after), ("rate_limited", 60.0))

    def test_catalogue_sync_keeps_a_recorded_downgrade(self):
        from accounting.sync.catalog import merge_capabilities

        merged = merge_capabilities(
            {"read_consumption": False, "read_stock": True, "omits_zero_balances": None},
            {"read_consumption": True, "read_stock": True, "omits_zero_balances": True, "notes": "новое"},
        )
        self.assertEqual(
            merged, {"read_consumption": False, "read_stock": True, "omits_zero_balances": True, "notes": "новое"}
        )
        self.assertEqual(merge_capabilities(None, {"read_consumption": True}), {"read_consumption": True})
        self.assertEqual(merge_capabilities("junk", {"read_consumption": True}), {"read_consumption": True})
