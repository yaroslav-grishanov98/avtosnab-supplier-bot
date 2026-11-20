from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from accounting.models import (
    AccountingConnection,
    AccountingConsumptionCoverage,
    AccountingConsumptionDaily,
    AccountingProduct,
    AccountingStockLevel,
    AccountingStockSnapshot,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionStatus,
    CoverageStatus,
    LinkStatus,
    Provider,
    Reliability,
    SnapshotStatus,
)
from accounting.selectors import (
    CONSUMPTION_OK,
    CONSUMPTION_UNSUPPORTED,
    get_venue_consumption,
    get_venue_stock,
    list_venue_warehouses,
)
from accounting.test_support.sync import link, make_connection, make_venue

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.get_default_timezone())
END = date(2026, 10, 22)
D1, D2, D3 = END, END - timedelta(days=1), END - timedelta(days=2)


class SelectorFixtureMixin:
    """One connection shared by two venues: a warehouse and a stock level each."""

    def make_world(self):
        self.venue_a = make_venue("Заведение A")
        self.venue_b = make_venue("Заведение B")
        self.connection = make_connection(capabilities={"read_consumption": True})
        link(self.connection, self.venue_a)
        link(self.connection, self.venue_b)
        self.product = AccountingProduct.objects.create(
            connection=self.connection, external_id="p-1", name="Товар", unit_name="кг"
        )
        self.wh_a = self.warehouse(self.connection, self.venue_a, "wh-a", "Кухня")
        self.wh_b = self.warehouse(self.connection, self.venue_b, "wh-b", "Бар")
        self.level_a = self.level(self.wh_a, self.product, "5")
        self.level_b = self.level(self.wh_b, self.product, "7")

    def warehouse(self, connection, venue, external_id, name, **fields):
        return AccountingWarehouse.objects.create(
            connection=connection, venue=venue, external_id=external_id, name=name, **fields
        )

    def level(self, wh, product, qty, taken_at=NOW, reliability=Reliability.OK):
        snapshot = AccountingStockSnapshot.objects.create(
            connection=wh.connection, venue=wh.venue, warehouse=wh, taken_at=taken_at, status=SnapshotStatus.COMPLETE
        )
        return AccountingStockLevel.objects.create(
            connection=wh.connection,
            venue=wh.venue,
            warehouse=wh,
            product=product,
            snapshot=snapshot,
            stock_qty=None if qty is None else Decimal(qty),
            source_uom="кг",
            taken_at=taken_at,
            reliability=reliability,
        )

    def consume(self, wh, product, day, qty, kind="SESSION_WRITEOFF", coverage=CoverageStatus.COMPLETE):
        AccountingConsumptionCoverage.objects.get_or_create(
            warehouse=wh, day=day, defaults={"connection": wh.connection, "venue": wh.venue, "status": coverage}
        )
        return AccountingConsumptionDaily.objects.create(
            connection=wh.connection, venue=wh.venue, warehouse=wh, product=product, day=day,
            transaction_type=kind, qty=Decimal(qty), source_uom="кг",
        )  # fmt: skip

    def cover(self, wh, day, status=CoverageStatus.COMPLETE):
        AccountingConsumptionCoverage.objects.create(
            connection=wh.connection, venue=wh.venue, warehouse=wh, day=day, status=status
        )


@override_settings(TIME_ZONE="Europe/Moscow", ACCOUNTING_STOCK_STALE_AFTER_MINUTES=180)
class VenueScopeTests(SelectorFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_venue_never_sees_other_venues_stock_even_on_shared_connection(self):
        self.consume(self.wh_a, self.product, D1, "2")
        self.consume(self.wh_b, self.product, D1, "9")
        warehouses = list_venue_warehouses(self.venue_a.id)
        stock = get_venue_stock(self.venue_a.id, now=NOW)
        consumption = get_venue_consumption(self.venue_a.id, kinds=["SESSION_WRITEOFF"], end_day=END, days=1)

        self.assertEqual([w.warehouse_id for w in warehouses], [self.wh_a.id])
        self.assertEqual([(s.warehouse_id, s.stock_qty) for s in stock], [(self.wh_a.id, Decimal("5"))])
        self.assertEqual(list(consumption.series), [(self.wh_a.id, "p-1")])
        self.assertEqual(consumption.series[(self.wh_a.id, "p-1")][0].qty, Decimal("2"))
        self.assertEqual(set(consumption.coverage), {self.wh_a.id})

        other = get_venue_stock(self.venue_b.id, now=NOW)
        self.assertEqual([(s.warehouse_id, s.stock_qty) for s in other], [(self.wh_b.id, Decimal("7"))])

    def test_unknown_venue_gets_nothing(self):
        stranger = make_venue("Чужое")
        self.assertEqual(list_venue_warehouses(stranger.id), [])
        self.assertEqual(get_venue_stock(stranger.id, now=NOW), [])
        view = get_venue_consumption(stranger.id, kinds=["WRITEOFF"], end_day=END)
        self.assertEqual((view.status, view.series, view.coverage), (CONSUMPTION_OK, {}, {}))

    def test_same_external_product_id_in_two_connections_is_not_mixed(self):
        # The same venue is served by a second connection (another provider: one live link per provider) whose
        # warehouse and product carry identical external ids.
        other = AccountingConnection.objects.create(
            provider=Provider.STOREHOUSE, base_url="https://other.example/api", capabilities={"read_consumption": True}
        )
        link(other, self.venue_a)
        other_wh = self.warehouse(other, self.venue_a, "wh-a", "Кухня (вторая база)")
        other_product = AccountingProduct.objects.create(connection=other, external_id="p-1", name="Другой товар")
        self.level(other_wh, other_product, "100")
        self.consume(self.wh_a, self.product, D1, "2")
        self.consume(other_wh, other_product, D1, "50")

        stock = {
            (s.warehouse_id, s.external_product_id): s.stock_qty for s in get_venue_stock(self.venue_a.id, now=NOW)
        }
        self.assertEqual(stock, {(self.wh_a.id, "p-1"): Decimal("5"), (other_wh.id, "p-1"): Decimal("100")})
        series = get_venue_consumption(self.venue_a.id, kinds=["SESSION_WRITEOFF"], end_day=END, days=1).series
        self.assertEqual(series[(self.wh_a.id, "p-1")][0].qty, Decimal("2"))
        self.assertEqual(series[(other_wh.id, "p-1")][0].qty, Decimal("50"))

    def test_disconnected_connection_returns_nothing_by_default_and_history_with_flag(self):
        self.consume(self.wh_a, self.product, D1, "2")
        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save()
        self.assertEqual(list_venue_warehouses(self.venue_a.id), [])
        self.assertEqual(get_venue_stock(self.venue_a.id, now=NOW), [])
        self.assertEqual(get_venue_consumption(self.venue_a.id, kinds=["SESSION_WRITEOFF"], end_day=END).series, {})
        history = get_venue_stock(self.venue_a.id, now=NOW, include_disconnected=True)
        self.assertEqual([(s.stock_qty, s.connection_status) for s in history], [(Decimal("5"), "disabled")])
        self.assertEqual(len(list_venue_warehouses(self.venue_a.id, include_disconnected=True)), 1)
        kept = get_venue_consumption(
            self.venue_a.id, kinds=["SESSION_WRITEOFF"], end_day=END, days=1, include_disconnected=True
        )
        self.assertEqual(kept.series[(self.wh_a.id, "p-1")][0].qty, Decimal("2"))

    def test_an_inactive_link_hides_the_venues_data_even_when_the_connection_is_active(self):
        # The composite foreign keys accept a link in any status; the readers must not.
        AccountingVenueLink.objects.filter(connection=self.connection, venue=self.venue_a).update(
            status=LinkStatus.INACTIVE
        )
        self.assertEqual(get_venue_stock(self.venue_a.id, now=NOW), [])
        self.assertEqual(list_venue_warehouses(self.venue_a.id), [])
        self.assertEqual(len(get_venue_stock(self.venue_b.id, now=NOW)), 1)  # the other venue's link is untouched

    def test_needs_attention_connection_is_not_served_as_current(self):
        self.connection.status = ConnectionStatus.NEEDS_ATTENTION
        self.connection.save()
        self.assertEqual(get_venue_stock(self.venue_a.id, now=NOW), [])

    def test_warehouse_view_carries_connection_and_source_flags(self):
        AccountingWarehouse.objects.filter(pk=self.wh_a.pk).update(is_present_in_source=False)
        (view,) = list_venue_warehouses(self.venue_a.id)
        self.assertEqual(
            (view.external_id, view.name, view.connection_id, view.connection_status, view.is_present_in_source),
            ("wh-a", "Кухня", self.connection.id, "active", False),
        )


@override_settings(TIME_ZONE="Europe/Moscow", ACCOUNTING_STOCK_STALE_AFTER_MINUTES=180)
class StockTests(SelectorFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_stale_flag_follows_threshold(self):
        AccountingStockLevel.objects.filter(pk=self.level_a.pk).update(taken_at=NOW - timedelta(minutes=180))
        self.assertFalse(get_venue_stock(self.venue_a.id, now=NOW)[0].is_stale)  # exactly the threshold
        AccountingStockLevel.objects.filter(pk=self.level_a.pk).update(taken_at=NOW - timedelta(minutes=181))
        self.assertTrue(get_venue_stock(self.venue_a.id, now=NOW)[0].is_stale)

    def test_zero_stock_is_decimal_zero_and_missing_is_none(self):
        zero = AccountingProduct.objects.create(connection=self.connection, external_id="p-zero", name="Ноль")
        unknown = AccountingProduct.objects.create(connection=self.connection, external_id="p-null", name="Нет числа")
        self.level(self.wh_a, zero, "0")
        self.level(self.wh_a, unknown, None, reliability=Reliability.SOURCE_ERROR)
        stock = {s.external_product_id: s for s in get_venue_stock(self.venue_a.id, now=NOW)}
        self.assertEqual(stock["p-zero"].stock_qty, Decimal("0"))
        self.assertIsInstance(stock["p-zero"].stock_qty, Decimal)
        self.assertIsNone(stock["p-null"].stock_qty)
        self.assertEqual(stock["p-null"].reliability, Reliability.SOURCE_ERROR)
        self.assertNotIn("p-never-reported", stock)  # a product the warehouse never reported has no row at all

    def test_stock_view_fields(self):
        (view,) = get_venue_stock(self.venue_a.id, now=NOW)
        self.assertEqual(
            (view.external_product_id, view.source_uom, view.taken_at, view.connection_status, view.reliability),
            ("p-1", "кг", NOW, "active", "ok"),
        )


@override_settings(TIME_ZONE="Europe/Moscow")
class ConsumptionTests(SelectorFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def view(self, kinds=("SESSION_WRITEOFF",), **kwargs):
        kwargs.setdefault("end_day", END)
        kwargs.setdefault("days", 3)
        return get_venue_consumption(self.venue_a.id, kinds=kinds, **kwargs)

    def test_consumption_zero_vs_no_history(self):
        self.consume(self.wh_a, self.product, D1, "2")  # D1 covered with a row
        self.cover(self.wh_a, D2)  # D2 covered without rows: a real zero
        # D3 has no coverage at all: no data
        values = self.view().series[(self.wh_a.id, "p-1")]
        self.assertEqual([v.day for v in values], [D3, D2, D1])  # oldest first
        self.assertEqual([v.qty for v in values], [None, Decimal("0"), Decimal("2")])
        self.assertIsInstance(values[1].qty, Decimal)

    def test_a_partial_day_is_flagged(self):
        self.consume(self.wh_a, self.product, D1, "2", coverage=CoverageStatus.PARTIAL)
        values = self.view(days=1).series[(self.wh_a.id, "p-1")]
        self.assertEqual((values[0].qty, values[0].partial), (Decimal("2"), True))
        self.assertEqual(self.view(days=1).coverage[self.wh_a.id], {D1: "partial"})

    def test_consumption_status_unsupported_when_capability_false(self):
        self.consume(self.wh_a, self.product, D1, "2")
        self.connection.capabilities = {"read_consumption": False}
        self.connection.save()
        view = self.view()
        self.assertEqual((view.status, view.series, view.available_kinds), (CONSUMPTION_UNSUPPORTED, {}, []))

    def test_consumption_sums_only_the_requested_kinds(self):
        self.consume(self.wh_a, self.product, D1, "2", "SESSION_WRITEOFF")
        self.consume(self.wh_a, self.product, D1, "3", "WRITEOFF")
        self.consume(self.wh_a, self.product, D1, "-5", "DISASSEMBLE")
        only_sales = self.view(kinds=["SESSION_WRITEOFF"], days=1).series[(self.wh_a.id, "p-1")][0].qty
        both = self.view(kinds=["SESSION_WRITEOFF", "WRITEOFF"], days=1).series[(self.wh_a.id, "p-1")][0].qty
        self.assertEqual((only_sales, both), (Decimal("2"), Decimal("5")))

    def test_consumption_requires_explicit_kinds(self):
        with self.assertRaises(TypeError):
            get_venue_consumption(self.venue_a.id)
        with self.assertRaises(ValueError):
            get_venue_consumption(self.venue_a.id, kinds=[])  # nothing asked is not "zero consumption"

    def test_available_kinds_lists_stored_transaction_types(self):
        self.consume(self.wh_a, self.product, D1, "2", "WRITEOFF")
        self.consume(self.wh_a, self.product, D2, "1", "SESSION_WRITEOFF")
        self.consume(self.wh_b, self.product, D1, "1", "TRANSFER")  # another venue's types are not listed
        view = self.view(kinds=["WRITEOFF"])
        self.assertEqual(view.available_kinds, ["SESSION_WRITEOFF", "WRITEOFF"])

    def test_a_kind_without_rows_gives_no_series_but_keeps_the_coverage(self):
        self.consume(self.wh_a, self.product, D1, "2", "WRITEOFF")
        view = self.view(kinds=["SESSION_WRITEOFF"], days=1)
        self.assertEqual(view.series, {})
        self.assertEqual(view.coverage[self.wh_a.id], {D1: "complete"})  # covered days are zeros for the reader

    def test_days_outside_the_window_are_ignored(self):
        self.consume(self.wh_a, self.product, END - timedelta(days=10), "9")
        self.assertEqual(self.view().series, {})

    def test_end_day_defaults_to_yesterday(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.consume(self.wh_a, self.product, yesterday, "4")
        values = get_venue_consumption(self.venue_a.id, kinds=["SESSION_WRITEOFF"], days=2).series[
            (self.wh_a.id, "p-1")
        ]
        self.assertEqual(values[-1].day, yesterday)
        self.assertEqual(values[-1].qty, Decimal("4"))

    def test_no_connection_at_all_is_ok_and_empty(self):
        lonely = make_venue("Без подключения")
        view = get_venue_consumption(lonely.id, kinds=["WRITEOFF"], end_day=END)
        self.assertEqual((view.status, view.series), (CONSUMPTION_OK, {}))
