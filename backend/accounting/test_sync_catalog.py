from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounting.adapters import FakeAdapter
from accounting.models import (
    AccountingConnection,
    AccountingDepartment,
    AccountingProduct,
    AccountingStockSnapshot,
    AccountingWarehouse,
    ConnectionStatus,
    SnapshotStatus,
    SyncKind,
    SyncStatus,
)
from accounting.sync.catalog import sync_catalog
from accounting.sync.runner import run_sync_job
from accounting.sync.stock import sync_stock
from accounting.test_support.sync import (
    adapter_factory,
    department,
    link,
    make_connection,
    make_job,
    make_venue,
    product,
    warehouse,
)


class RecordingStockAdapter(FakeAdapter):
    """FakeAdapter that remembers which warehouses the stock sync asked for."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stock_requests = []

    def read_stock(self, warehouse_ids, at):
        self.stock_requests.append(list(warehouse_ids))
        return super().read_stock(warehouse_ids, at)


class SyncCatalogTests(TestCase):
    def setUp(self):
        self.venue_a = make_venue("Заведение A")
        self.venue_b = make_venue("Заведение B")
        self.connection = make_connection()
        link(self.connection, self.venue_a)
        link(self.connection, self.venue_b)

    def assign(self, external_id, venue, connection=None, name="Подразделение"):
        """What the department assignment service (Task 13) does: an admin picks the venue."""
        return AccountingDepartment.objects.create(
            connection=connection or self.connection, external_id=external_id, name=name, venue=venue
        )

    def run_catalog(self, adapter, connection=None):
        connection = connection or self.connection
        return sync_catalog(adapter, connection, make_job(connection, kind=SyncKind.CATALOG))

    def wh(self, external_id, connection=None):
        return AccountingWarehouse.objects.get(connection=connection or self.connection, external_id=external_id)

    def test_warehouse_gets_venue_through_its_department(self):
        self.assign("dep-1", self.venue_a, name="Старое имя")
        adapter = FakeAdapter(
            departments=[department("dep-1", name="Точка на Ленина")],
            warehouses=[warehouse("wh-1", "dep-1", name="Кухня")],
            products=[product("p-1")],
        )

        self.run_catalog(adapter)

        self.assertEqual(self.wh("wh-1").venue_id, self.venue_a.id)
        dep = AccountingDepartment.objects.get(connection=self.connection, external_id="dep-1")
        self.assertEqual(dep.name, "Точка на Ленина")
        # The admin's assignment survives every catalogue sync.
        self.assertEqual(dep.venue_id, self.venue_a.id)
        self.assertIsNotNone(dep.last_seen_at)

    def test_several_departments_of_one_venue_feed_that_venue(self):
        self.assign("dep-1", self.venue_a)
        self.assign("dep-2", self.venue_a)
        adapter = FakeAdapter(
            departments=[department("dep-1"), department("dep-2")],
            warehouses=[warehouse("wh-1", "dep-1", "Кухня"), warehouse("wh-2", "dep-2", "Бар")],
            products=[],
        )

        self.run_catalog(adapter)

        self.assertEqual(self.wh("wh-1").venue_id, self.venue_a.id)
        self.assertEqual(self.wh("wh-2").venue_id, self.venue_a.id)

    def test_unlinked_warehouse_has_no_venue_and_is_skipped_by_stock_sync(self):
        self.assign("dep-1", self.venue_a)
        adapter = RecordingStockAdapter(
            departments=[department("dep-1"), department("dep-new")],
            warehouses=[
                warehouse("wh-1", "dep-1"),
                warehouse("wh-orphan", None, "Без подразделения"),
                warehouse("wh-unassigned", "dep-new", "Подразделение не назначено"),
            ],
            products=[product("p-1")],
            stock={},
        )

        self.run_catalog(adapter)
        # A department that is new in the source waits for the admin: no venue yet.
        new_dep = AccountingDepartment.objects.get(connection=self.connection, external_id="dep-new")
        self.assertIsNone(new_dep.venue_id)
        self.assertIsNone(self.wh("wh-orphan").venue_id)
        self.assertIsNone(self.wh("wh-unassigned").venue_id)

        sync_stock(adapter, self.connection, make_job(self.connection))

        self.assertEqual(adapter.stock_requests, [["wh-1"]])
        self.assertEqual(
            set(AccountingStockSnapshot.objects.values_list("warehouse__external_id", flat=True)), {"wh-1"}
        )

    def test_same_name_warehouses_in_two_venues_are_separate_rows(self):
        self.assign("dep-1", self.venue_a)
        self.assign("dep-2", self.venue_b)
        adapter = FakeAdapter(
            departments=[department("dep-1"), department("dep-2")],
            warehouses=[warehouse("wh-1", "dep-1", "Кухня"), warehouse("wh-2", "dep-2", "Кухня")],
            products=[],
        )

        self.run_catalog(adapter)

        rows = AccountingWarehouse.objects.filter(connection=self.connection, name="Кухня")
        self.assertEqual(rows.count(), 2)
        self.assertEqual({row.venue_id for row in rows}, {self.venue_a.id, self.venue_b.id})

    def test_missing_in_source_is_flagged_not_deleted(self):
        self.assign("dep-1", self.venue_a)
        full = FakeAdapter(
            departments=[department("dep-1")],
            warehouses=[warehouse("wh-1", "dep-1"), warehouse("wh-2", "dep-1", "Бар")],
            products=[product("p-1"), product("p-2")],
        )
        self.run_catalog(full)

        stats = self.run_catalog(
            FakeAdapter(
                departments=[department("dep-1")], warehouses=[warehouse("wh-1", "dep-1")], products=[product("p-1")]
            )
        )

        gone_wh = self.wh("wh-2")
        self.assertFalse(gone_wh.is_present_in_source)
        self.assertEqual(gone_wh.venue_id, self.venue_a.id)
        self.assertTrue(self.wh("wh-1").is_present_in_source)
        gone_product = AccountingProduct.objects.get(connection=self.connection, external_id="p-2")
        self.assertTrue(gone_product.is_deleted)
        self.assertFalse(AccountingProduct.objects.get(connection=self.connection, external_id="p-1").is_deleted)
        self.assertEqual(stats.warehouses_missing, 1)
        self.assertEqual(stats.products_missing, 1)

        # Back in the source: flags are restored, still the same rows.
        self.run_catalog(full)
        self.assertTrue(self.wh("wh-2").is_present_in_source)
        self.assertEqual(self.wh("wh-2").id, gone_wh.id)
        self.assertFalse(AccountingProduct.objects.get(pk=gone_product.pk).is_deleted)

    def test_second_connection_with_same_external_ids_is_independent(self):
        venue_c = make_venue("Заведение C")
        other = make_connection("https://b.iiko.example/resto")
        link(other, venue_c)
        self.assign("dep-1", self.venue_a)
        self.assign("dep-1", venue_c, connection=other)
        catalogue = {
            "departments": [department("dep-1")],
            "warehouses": [warehouse("wh-1", "dep-1", "Кухня"), warehouse("wh-2", "dep-1", "Бар")],
            "products": [product("p-1"), product("p-2")],
        }
        self.run_catalog(FakeAdapter(**catalogue))
        self.run_catalog(FakeAdapter(**catalogue), connection=other)

        self.assertEqual(AccountingWarehouse.objects.filter(connection=self.connection).count(), 2)
        self.assertEqual(AccountingWarehouse.objects.filter(connection=other).count(), 2)
        self.assertEqual(self.wh("wh-1").venue_id, self.venue_a.id)
        self.assertEqual(self.wh("wh-1", connection=other).venue_id, venue_c.id)
        self.assertNotEqual(self.wh("wh-1").id, self.wh("wh-1", connection=other).id)

        # The second source loses wh-2 and p-2; the first connection's identical ids are untouched.
        self.run_catalog(
            FakeAdapter(
                departments=[department("dep-1")], warehouses=[warehouse("wh-1", "dep-1")], products=[product("p-1")]
            ),
            connection=other,
        )
        self.assertFalse(self.wh("wh-2", connection=other).is_present_in_source)
        self.assertTrue(self.wh("wh-2").is_present_in_source)
        self.assertTrue(AccountingProduct.objects.get(connection=other, external_id="p-2").is_deleted)
        self.assertFalse(AccountingProduct.objects.get(connection=self.connection, external_id="p-2").is_deleted)

    # -- carry-overs -------------------------------------------------------
    def test_department_venue_assigned_later_moves_warehouse_on_resync(self):
        adapter = FakeAdapter(departments=[department("dep-1")], warehouses=[warehouse("wh-1", "dep-1")], products=[])
        self.run_catalog(adapter)
        self.assertIsNone(self.wh("wh-1").venue_id)

        AccountingDepartment.objects.filter(connection=self.connection, external_id="dep-1").update(venue=self.venue_b)
        stats = self.run_catalog(adapter)

        self.assertEqual(self.wh("wh-1").venue_id, self.venue_b.id)
        self.assertEqual(stats.errors, [])

    def test_venue_change_blocked_by_stock_rows_is_recorded_not_fatal(self):
        self.assign("dep-1", self.venue_a)
        adapter = FakeAdapter(
            departments=[department("dep-1")],
            warehouses=[warehouse("wh-1", "dep-1", "Кухня"), warehouse("wh-2", None, "Резерв")],
            products=[product("p-1")],
        )
        self.run_catalog(adapter)
        wh1 = self.wh("wh-1")
        AccountingStockSnapshot.objects.create(
            connection=self.connection,
            venue=self.venue_a,
            warehouse=wh1,
            taken_at=timezone.now(),
            status=SnapshotStatus.COMPLETE,
        )

        AccountingDepartment.objects.filter(connection=self.connection, external_id="dep-1").update(venue=self.venue_b)
        renamed = FakeAdapter(
            departments=[department("dep-1")],
            warehouses=[warehouse("wh-1", "dep-1", "Кухня новая"), warehouse("wh-2", None, "Резерв")],
            products=[product("p-1"), product("p-2")],
        )
        stats = self.run_catalog(renamed)

        wh1.refresh_from_db()
        self.assertEqual(wh1.venue_id, self.venue_a.id)  # the composite key keeps it with its stock history
        self.assertEqual(wh1.name, "Кухня новая")  # the rest of the catalogue is still updated
        self.assertTrue(AccountingProduct.objects.filter(connection=self.connection, external_id="p-2").exists())
        self.assertEqual(len(stats.errors), 1)
        self.assertEqual(stats.errors[0]["external_id"], "wh-1")
        self.assertEqual(stats.errors[0]["code"], "warehouse_venue_change_blocked")

    def test_catalog_updates_connection_sync_time_capabilities_and_version(self):
        before = timezone.now()
        self.run_catalog(FakeAdapter(departments=[], warehouses=[], products=[]))

        self.connection.refresh_from_db()
        self.assertGreaterEqual(self.connection.catalog_synced_at, before)
        self.assertIs(self.connection.capabilities["omits_zero_balances"], True)
        self.assertEqual(self.connection.server_version, "fake-1.0")

    def test_catalog_job_through_runner_records_stats(self):
        self.assign("dep-1", self.venue_a)
        adapter = FakeAdapter(
            departments=[department("dep-1")], warehouses=[warehouse("wh-1", "dep-1")], products=[product("p-1")]
        )
        calls = []
        job = make_job(self.connection, kind=SyncKind.CATALOG)

        with mock.patch("accounting.sync.runner.get_adapter", adapter_factory(adapter, calls)):
            outcome = run_sync_job(job.job_id)

        job.refresh_from_db()
        self.assertEqual(outcome.status, SyncStatus.SUCCESS)
        self.assertIsNone(outcome.error)
        self.assertEqual(job.status, SyncStatus.SUCCESS)
        self.assertEqual(job.stats["warehouses"], 1)
        self.assertEqual(job.stats["products"], 1)
        self.assertEqual(job.stats["departments"], 1)
        self.assertLessEqual(job.started_at, job.finished_at)
        # The catalogue job refreshes the version and units itself, so it gets no stock-only options.
        self.assertEqual(calls, [{"connection_id": self.connection.id}])

    def test_catalog_job_with_blocked_venue_change_is_partial(self):
        self.assign("dep-1", self.venue_a)
        adapter = FakeAdapter(departments=[department("dep-1")], warehouses=[warehouse("wh-1", "dep-1")], products=[])
        self.run_catalog(adapter)
        AccountingStockSnapshot.objects.create(
            connection=self.connection,
            venue=self.venue_a,
            warehouse=self.wh("wh-1"),
            taken_at=timezone.now(),
            status=SnapshotStatus.COMPLETE,
        )
        AccountingDepartment.objects.filter(connection=self.connection, external_id="dep-1").update(venue=self.venue_b)
        job = make_job(self.connection, kind=SyncKind.CATALOG)

        with mock.patch("accounting.sync.runner.get_adapter", adapter_factory(adapter)):
            outcome = run_sync_job(job.job_id)

        job.refresh_from_db()
        self.assertEqual(outcome.status, SyncStatus.PARTIAL)
        self.assertEqual(job.error_code, "warehouse_venue_change_blocked")
        self.assertEqual(job.stats["errors"][0]["external_id"], "wh-1")

    # -- iiko GUIDs may differ in letter case between methods (adapters/iiko.py) ----------------------------
    def test_mixed_case_department_id_still_gives_warehouse_its_venue(self):
        self.assign("D3A7-DEP-UPPER", self.venue_a)
        adapter = FakeAdapter(
            departments=[department("d3a7-dep-upper", name="Точка")],
            warehouses=[warehouse("wh-1", " D3a7-Dep-Upper ", "Кухня")],
            products=[],
        )

        self.run_catalog(adapter)

        self.assertEqual(self.wh("wh-1").venue_id, self.venue_a.id)
        departments = AccountingDepartment.objects.filter(connection=self.connection)
        self.assertEqual(list(departments.values_list("external_id", flat=True)), ["D3A7-DEP-UPPER"])
        self.assertEqual(departments.get().venue_id, self.venue_a.id)
        self.assertEqual(departments.get().name, "Точка")

    def test_mixed_case_ids_on_resync_update_the_stored_rows(self):
        self.assign("dep-1", self.venue_a)
        self.run_catalog(
            FakeAdapter(
                departments=[department("dep-1")],
                warehouses=[warehouse("5A1F-WH-1", "dep-1", "Кухня")],
                products=[product("7E57-P-1")],
            )
        )

        stats = self.run_catalog(
            FakeAdapter(
                departments=[department("DEP-1")],
                warehouses=[warehouse("5a1f-wh-1", "DEP-1", "Кухня новая")],
                products=[product("7e57-p-1", unit="л")],
            )
        )

        warehouses = AccountingWarehouse.objects.filter(connection=self.connection)
        self.assertEqual(list(warehouses.values_list("external_id", flat=True)), ["5A1F-WH-1"])
        stored = warehouses.get()
        self.assertTrue(stored.is_present_in_source)
        self.assertEqual(stored.name, "Кухня новая")
        self.assertEqual(stored.venue_id, self.venue_a.id)
        products = AccountingProduct.objects.filter(connection=self.connection)
        self.assertEqual(list(products.values_list("external_id", flat=True)), ["7E57-P-1"])
        self.assertFalse(products.get().is_deleted)
        self.assertEqual(products.get().unit_name, "л")
        self.assertEqual(AccountingDepartment.objects.filter(connection=self.connection).count(), 1)
        self.assertEqual((stats.warehouses_missing, stats.products_missing), (0, 0))

    # -- G3 review: empty answers and connection columns ----------------------------------------------------
    def seed_catalogue(self):
        self.assign("dep-1", self.venue_a)
        self.run_catalog(
            FakeAdapter(
                departments=[department("dep-1")],
                warehouses=[warehouse("wh-1", "dep-1"), warehouse("wh-2", "dep-1", "Бар")],
                products=[product("p-1"), product("p-2")],
            )
        )

    def test_empty_warehouse_list_does_not_flag_stored_warehouses(self):
        self.seed_catalogue()

        stats = self.run_catalog(
            FakeAdapter(departments=[department("dep-1")], warehouses=[], products=[product("p-1"), product("p-2")])
        )

        self.assertTrue(
            all(
                AccountingWarehouse.objects.filter(connection=self.connection).values_list(
                    "is_present_in_source", flat=True
                )
            )
        )
        self.assertEqual(stats.warehouses_missing, 0)
        self.assertEqual([(e["external_id"], e["code"]) for e in stats.errors], [("", "catalog_empty_answer")])
        self.assertIn("склад", stats.errors[0]["message"])

    def test_empty_product_list_does_not_flag_stored_products(self):
        self.seed_catalogue()

        stats = self.run_catalog(
            FakeAdapter(
                departments=[department("dep-1")],
                warehouses=[warehouse("wh-1", "dep-1"), warehouse("wh-2", "dep-1", "Бар")],
                products=[],
            )
        )

        self.assertFalse(
            any(AccountingProduct.objects.filter(connection=self.connection).values_list("is_deleted", flat=True))
        )
        self.assertEqual(stats.products_missing, 0)
        self.assertEqual([(e["external_id"], e["code"]) for e in stats.errors], [("", "catalog_empty_answer")])
        self.assertIn("товар", stats.errors[0]["message"])

    def test_empty_lists_on_a_first_sync_are_not_an_error(self):
        stats = self.run_catalog(FakeAdapter(departments=[], warehouses=[], products=[]))
        self.assertEqual(stats.errors, [])

    def test_catalog_job_with_an_empty_answer_is_partial(self):
        self.seed_catalogue()
        job = make_job(self.connection, kind=SyncKind.CATALOG)
        adapter = FakeAdapter(departments=[department("dep-1")], warehouses=[], products=[])

        with mock.patch("accounting.sync.runner.get_adapter", adapter_factory(adapter)):
            outcome = run_sync_job(job.job_id)

        job.refresh_from_db()
        self.assertEqual(outcome.status, SyncStatus.PARTIAL)
        self.assertEqual(job.error_code, "catalog_empty_answer")
        self.assertEqual(len(job.stats["errors"]), 2)

    def test_catalog_writes_only_its_own_connection_columns(self):
        connection = self.connection

        class AdminEditsMidSync(FakeAdapter):
            def capabilities(self):
                # An admin changes the address and status while the sync runs (out of band, own transaction).
                AccountingConnection.objects.filter(pk=connection.pk).update(
                    base_url="https://moved.iiko.example/resto",
                    host="moved.iiko.example",
                    status=ConnectionStatus.NEEDS_ATTENTION,
                )
                return super().capabilities()

        before = timezone.now()
        self.run_catalog(AdminEditsMidSync(departments=[], warehouses=[], products=[]))

        stored = AccountingConnection.objects.get(pk=connection.pk)
        self.assertEqual(
            (stored.base_url, stored.host, stored.status),
            ("https://moved.iiko.example/resto", "moved.iiko.example", ConnectionStatus.NEEDS_ATTENTION),
        )
        self.assertGreaterEqual(stored.catalog_synced_at, before)
        self.assertIs(stored.capabilities["omits_zero_balances"], True)
        self.assertEqual(stored.server_version, "fake-1.0")
        # The in-memory instance carries the synced values too, without a recomputed host.
        self.assertEqual(connection.catalog_synced_at, stored.catalog_synced_at)

    def test_unusable_stale_base_url_on_the_instance_does_not_break_the_catalogue(self):
        # save() would re-derive host from the instance's base_url and raise; the sync must not depend on it.
        self.connection.base_url = "https://[broken/resto"

        stats = self.run_catalog(FakeAdapter(departments=[], warehouses=[], products=[]))

        self.assertEqual(stats.errors, [])
        stored = AccountingConnection.objects.get(pk=self.connection.pk)
        self.assertEqual(stored.base_url, "https://a.iiko.example/resto")
        self.assertIsNotNone(stored.catalog_synced_at)
