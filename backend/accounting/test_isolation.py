import datetime
import uuid
from contextlib import contextmanager
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

from accounting.models import (
    AccountingConnection,
    AccountingConsumptionCoverage,
    AccountingConsumptionDaily,
    AccountingDepartment,
    AccountingProduct,
    AccountingStockLevel,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    CoverageStatus,
    LinkStatus,
    Provider,
    Reliability,
    SnapshotStatus,
    SyncKind,
    SyncStatus,
    SyncTrigger,
)
from core.models import Venue

# The composite foreign keys added by accounting/migrations/0003_composite_fks.py.
COMPOSITE_FKS = {
    "acc_dept_conn_venue_link_fk",
    "acc_wh_conn_venue_link_fk",
    "acc_snap_wh_conn_venue_fk",
    "acc_level_wh_conn_venue_fk",
    "acc_level_snap_conn_venue_fk",
    "acc_level_product_conn_fk",
    "acc_cons_wh_conn_venue_fk",
    "acc_cons_product_conn_fk",
    "acc_cov_wh_conn_venue_fk",
}


def make_connection(base_url):
    return AccountingConnection.objects.create(provider=Provider.IIKO, base_url=base_url)


def link(conn, venue, status=LinkStatus.ACTIVE):
    return AccountingVenueLink.objects.create(connection=conn, venue=venue, status=status)


def make_warehouse(conn, venue, external_id="store-1", name="Кухня"):
    return AccountingWarehouse.objects.create(connection=conn, venue=venue, external_id=external_id, name=name)


def make_product(conn, external_id="prod-1"):
    return AccountingProduct.objects.create(connection=conn, external_id=external_id, name=f"Товар {external_id}")


def make_snapshot(warehouse, **overrides):
    values = {
        "connection_id": warehouse.connection_id,
        "venue_id": warehouse.venue_id,
        "warehouse": warehouse,
        "taken_at": timezone.now(),
        "status": SnapshotStatus.COMPLETE,
    }
    values.update(overrides)
    return AccountingStockSnapshot.objects.create(**values)


def make_level(warehouse, product, snapshot, **overrides):
    values = {
        "connection_id": warehouse.connection_id,
        "venue_id": warehouse.venue_id,
        "warehouse": warehouse,
        "product": product,
        "snapshot": snapshot,
        "stock_qty": Decimal("1.500000"),
        "source_uom": "кг",
        "taken_at": snapshot.taken_at,
    }
    values.update(overrides)
    return AccountingStockLevel.objects.create(**values)


def make_daily(warehouse, product, **overrides):
    values = {
        "connection_id": warehouse.connection_id,
        "venue_id": warehouse.venue_id,
        "warehouse": warehouse,
        "product": product,
        "day": datetime.date(2026, 10, 20),
        "transaction_type": "WRITEOFF",
        "qty": Decimal("2.250000"),
        "source_uom": "кг",
    }
    values.update(overrides)
    return AccountingConsumptionDaily.objects.create(**values)


def make_coverage(warehouse, **overrides):
    values = {
        "connection_id": warehouse.connection_id,
        "venue_id": warehouse.venue_id,
        "warehouse": warehouse,
        "day": datetime.date(2026, 10, 20),
        "status": CoverageStatus.COMPLETE,
    }
    values.update(overrides)
    return AccountingConsumptionCoverage.objects.create(**values)


def raw_insert(table, **values):
    """INSERT that bypasses the ORM, as a bug in raw SQL or a manual fix would."""
    now = timezone.now()
    values = {"id": uuid.uuid4(), "created_at": now, "updated_at": now, **values}
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    with connection.cursor() as cursor:
        cursor.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values()))


def raw_execute(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def composite_fk_names():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'f' AND conname = ANY(%s)",
            [sorted(COMPOSITE_FKS)],
        )
        return {row[0] for row in cursor.fetchall()}


class TenantIsolationTests(TransactionTestCase):
    """docs/adr/0020: composite foreign keys keep a row on its warehouse's connection and venue."""

    def setUp(self):
        self.venue_a = Venue.objects.create(display_name="A")
        self.venue_b = Venue.objects.create(display_name="B")
        self.connection = make_connection("https://a.iiko.it/resto")
        link(self.connection, self.venue_a)
        self.other_connection = make_connection("https://b.iiko.it/resto")
        link(self.other_connection, self.venue_b)
        self.warehouse_a = make_warehouse(self.connection, self.venue_a)
        self.product = make_product(self.connection)
        self.snapshot_a = make_snapshot(self.warehouse_a)

    @contextmanager
    def assertViolates(self, *constraints):
        """IntegrityError from one of the named constraints, so a NOT NULL or other error cannot pass for it."""
        with self.assertRaises(IntegrityError) as caught, transaction.atomic():
            yield
        self.assertIn(caught.exception.__cause__.diag.constraint_name, constraints)

    def link_connection_to_venue_b(self):
        # A second link of the same provider would hit the live (venue, provider) guard.
        AccountingVenueLink.objects.filter(connection=self.other_connection).update(status=LinkStatus.INACTIVE)
        link(self.connection, self.venue_b)

    # Warehouses and departments: the venue must be linked to the connection.

    def test_warehouse_of_other_venue_rejected(self):
        with self.assertViolates("acc_wh_conn_venue_link_fk"):
            make_warehouse(self.connection, self.venue_b, external_id="store-b")

    def test_warehouse_of_other_venue_rejected_through_raw_sql(self):
        with self.assertViolates("acc_wh_conn_venue_link_fk"):
            raw_insert(
                "accounting_warehouses",
                connection_id=self.connection.id,
                venue_id=self.venue_b.id,
                external_id="store-b",
                name="Бар",
                external_department_id="",
                store_type="",
                is_present_in_source=True,
                first_seen_at=timezone.now(),
            )

    def test_warehouse_moved_to_unlinked_venue_rejected(self):
        # A warehouse without snapshots, so only the link key can object to the move.
        warehouse = make_warehouse(self.connection, self.venue_a, external_id="store-empty")

        warehouse.venue = self.venue_b
        with self.assertViolates("acc_wh_conn_venue_link_fk"):
            warehouse.save(update_fields=["venue"])

    def test_warehouse_without_venue_is_allowed(self):
        # MATCH SIMPLE: a NULL venue skips the link check until a department is assigned.
        warehouse = make_warehouse(self.connection, None, external_id="store-unassigned")

        self.assertIsNone(AccountingWarehouse.objects.get(pk=warehouse.pk).venue_id)

    def test_link_status_is_not_checked_by_the_database(self):
        AccountingVenueLink.objects.filter(connection=self.connection).update(status=LinkStatus.INACTIVE)

        make_warehouse(self.connection, self.venue_a, external_id="store-2")

        self.assertEqual(AccountingWarehouse.objects.filter(venue=self.venue_a).count(), 2)

    def test_department_of_unlinked_venue_rejected(self):
        with self.assertViolates("acc_dept_conn_venue_link_fk"):
            AccountingDepartment.objects.create(
                connection=self.connection, external_id="dep-b", name="Ресторан Б", venue=self.venue_b
            )

    def test_department_without_venue_is_allowed(self):
        # MATCH SIMPLE: a department nobody has assigned yet has no venue to check.
        department = AccountingDepartment.objects.create(connection=self.connection, external_id="dep-x", name="X")

        self.assertIsNone(AccountingDepartment.objects.get(pk=department.pk).venue_id)

    def test_one_venue_can_have_several_departments(self):
        for index in range(2):
            AccountingDepartment.objects.create(
                connection=self.connection, external_id=f"dep-{index}", name=f"Отдел {index}", venue=self.venue_a
            )

        self.assertEqual(AccountingDepartment.objects.filter(venue=self.venue_a).count(), 2)

    # Stock, snapshots and consumption: connection and venue must be the warehouse's own.

    def test_legitimate_rows_insert(self):
        level = make_level(self.warehouse_a, self.product, self.snapshot_a)
        make_daily(self.warehouse_a, self.product)
        make_coverage(self.warehouse_a)

        level.refresh_from_db()
        self.assertEqual(level.stock_qty, Decimal("1.500000"))
        self.assertEqual(level.reliability, Reliability.OK)
        self.assertEqual(AccountingConsumptionDaily.objects.get().qty, Decimal("2.250000"))
        self.assertEqual(AccountingConsumptionCoverage.objects.get().status, CoverageStatus.COMPLETE)

    def test_stock_level_with_foreign_connection_product_rejected(self):
        foreign_product = make_product(self.other_connection, external_id="prod-foreign")

        with self.assertViolates("acc_level_product_conn_fk"):
            make_level(self.warehouse_a, foreign_product, self.snapshot_a)

    def test_stock_level_venue_must_match_warehouse_venue(self):
        self.link_connection_to_venue_b()

        with self.assertViolates("acc_level_wh_conn_venue_fk", "acc_level_snap_conn_venue_fk"):
            make_level(self.warehouse_a, self.product, self.snapshot_a, venue_id=self.venue_b.id)

    def test_stock_level_connection_must_match_warehouse_connection(self):
        with self.assertViolates(
            "acc_level_wh_conn_venue_fk", "acc_level_snap_conn_venue_fk", "acc_level_product_conn_fk"
        ):
            make_level(
                self.warehouse_a,
                self.product,
                self.snapshot_a,
                connection_id=self.other_connection.id,
                venue_id=self.venue_b.id,
            )

    def test_stock_level_snapshot_must_be_of_the_same_venue(self):
        self.link_connection_to_venue_b()
        warehouse_b = make_warehouse(self.connection, self.venue_b, external_id="store-b")
        snapshot_b = make_snapshot(warehouse_b)

        with self.assertViolates("acc_level_snap_conn_venue_fk"):
            make_level(self.warehouse_a, self.product, snapshot_b)

    def test_stock_level_warehouse_key_alone_rejects_a_foreign_warehouse(self):
        # Snapshot and product match the level's (connection, venue B); only the warehouse (venue A) does not.
        self.link_connection_to_venue_b()
        warehouse_b = make_warehouse(self.connection, self.venue_b, external_id="store-b")
        snapshot_b = make_snapshot(warehouse_b)

        with self.assertViolates("acc_level_wh_conn_venue_fk"):
            make_level(self.warehouse_a, self.product, snapshot_b, venue_id=self.venue_b.id)

    def test_stock_level_mismatch_rejected_through_raw_sql(self):
        self.link_connection_to_venue_b()
        now = timezone.now()

        with self.assertViolates("acc_level_wh_conn_venue_fk", "acc_level_snap_conn_venue_fk"):
            raw_insert(
                "accounting_stock_levels",
                connection_id=self.connection.id,
                venue_id=self.venue_b.id,
                warehouse_id=self.warehouse_a.id,
                product_id=self.product.id,
                snapshot_id=self.snapshot_a.id,
                stock_qty=Decimal("1"),
                source_uom="кг",
                taken_at=now,
                synced_at=now,
                reliability=Reliability.OK,
            )

    def test_snapshot_venue_must_match_warehouse_venue(self):
        self.link_connection_to_venue_b()

        with self.assertViolates("acc_snap_wh_conn_venue_fk"):
            make_snapshot(self.warehouse_a, venue_id=self.venue_b.id)

    def test_snapshot_of_warehouse_without_venue_rejected(self):
        warehouse = make_warehouse(self.connection, None, external_id="store-unassigned")

        with self.assertViolates("acc_snap_wh_conn_venue_fk"):
            make_snapshot(warehouse, venue_id=self.venue_a.id)

    def test_consumption_venue_must_match_warehouse_venue(self):
        self.link_connection_to_venue_b()

        with self.assertViolates("acc_cons_wh_conn_venue_fk"):
            make_daily(self.warehouse_a, self.product, venue_id=self.venue_b.id)

    def test_consumption_with_foreign_connection_product_rejected(self):
        foreign_product = make_product(self.other_connection, external_id="prod-foreign")

        with self.assertViolates("acc_cons_product_conn_fk"):
            make_daily(self.warehouse_a, foreign_product)

    def test_consumption_mismatch_rejected_through_raw_sql(self):
        with self.assertViolates("acc_cons_wh_conn_venue_fk", "acc_cons_product_conn_fk"):
            raw_insert(
                "accounting_consumption_daily",
                connection_id=self.other_connection.id,
                venue_id=self.venue_b.id,
                warehouse_id=self.warehouse_a.id,
                product_id=self.product.id,
                day=datetime.date(2026, 10, 20),
                transaction_type="WRITEOFF",
                qty=Decimal("1"),
                source_uom="кг",
            )

    def test_coverage_venue_must_match_warehouse_venue(self):
        self.link_connection_to_venue_b()

        with self.assertViolates("acc_cov_wh_conn_venue_fk"):
            make_coverage(self.warehouse_a, venue_id=self.venue_b.id)

    def test_warehouse_with_stock_cannot_move_to_another_venue(self):
        self.link_connection_to_venue_b()
        make_level(self.warehouse_a, self.product, self.snapshot_a)

        self.warehouse_a.venue = self.venue_b
        with self.assertViolates("acc_snap_wh_conn_venue_fk", "acc_level_wh_conn_venue_fk"):
            self.warehouse_a.save(update_fields=["venue"])

    def test_consumption_is_kept_per_transaction_type(self):
        make_daily(self.warehouse_a, self.product, transaction_type="WRITEOFF")
        make_daily(self.warehouse_a, self.product, transaction_type="SESSION_WRITEOFF")

        with self.assertViolates("accounting_consumption_daily_wh_prod_day_type_uniq"):
            make_daily(self.warehouse_a, self.product, transaction_type="WRITEOFF")
        self.assertEqual(AccountingConsumptionDaily.objects.count(), 2)

    def test_one_stock_level_per_warehouse_and_product(self):
        make_level(self.warehouse_a, self.product, self.snapshot_a)

        with self.assertViolates("accounting_stock_levels_wh_product_uniq"):
            make_level(self.warehouse_a, self.product, self.snapshot_a)

    # External ids and names.

    def test_same_external_id_allowed_in_two_connections(self):
        make_warehouse(self.other_connection, self.venue_b, external_id="store-1")
        make_product(self.other_connection, external_id="prod-1")

        self.assertEqual(AccountingWarehouse.objects.filter(external_id="store-1").count(), 2)
        self.assertEqual(AccountingProduct.objects.filter(external_id="prod-1").count(), 2)

    def test_same_external_id_twice_in_one_connection_rejected(self):
        with self.subTest("warehouse"), self.assertViolates("accounting_warehouses_conn_ext_uniq"):
            make_warehouse(self.connection, self.venue_a, external_id="store-1", name="Другой")
        with self.subTest("product"), self.assertViolates("accounting_products_conn_ext_uniq"):
            make_product(self.connection, external_id="prod-1")

    def test_same_warehouse_name_in_two_venues_stays_two_rows(self):
        self.link_connection_to_venue_b()
        make_warehouse(self.connection, self.venue_b, external_id="store-2", name="Кухня")

        kitchens = AccountingWarehouse.objects.filter(connection=self.connection, name="Кухня")
        self.assertEqual(kitchens.count(), 2)
        self.assertEqual({w.venue_id for w in kitchens}, {self.venue_a.id, self.venue_b.id})

    # Deletion: disconnecting is a status change, never a delete.

    def test_connection_and_link_cannot_be_deleted_with_data(self):
        link_a = AccountingVenueLink.objects.get(connection=self.connection, venue=self.venue_a)

        with self.subTest("connection via ORM"), self.assertRaises(ProtectedError), transaction.atomic():
            self.connection.delete()
        with self.subTest("link via ORM"), self.assertViolates("acc_wh_conn_venue_link_fk"):
            link_a.delete()
        with self.subTest("connection via SQL"), self.assertRaises(IntegrityError), transaction.atomic():
            raw_execute("DELETE FROM accounting_connections WHERE id = %s", [self.connection.id])
        with self.subTest("link via SQL"), self.assertViolates("acc_wh_conn_venue_link_fk"):
            raw_execute("DELETE FROM accounting_venue_links WHERE id = %s", [link_a.id])
        self.assertTrue(AccountingConnection.objects.filter(pk=self.connection.pk).exists())
        self.assertTrue(AccountingVenueLink.objects.filter(pk=link_a.pk).exists())

    def test_sync_job_defaults(self):
        job = AccountingSyncJob.objects.create(
            connection=self.connection, kind=SyncKind.STOCK, trigger=SyncTrigger.MANUAL
        )
        job.refresh_from_db()

        self.assertIsInstance(job.job_id, uuid.UUID)
        self.assertEqual(job.pk, job.job_id)
        self.assertEqual(job.status, SyncStatus.QUEUED)
        self.assertEqual(job.stats, {})
        self.assertIsNotNone(job.queued_at)


class CompositeForeignKeyMigrationTests(TransactionTestCase):
    """0003_composite_fks is plain RunSQL; it must apply and reverse cleanly."""

    def setUp(self):
        # Leave the shared test database at the latest schema whatever happens below.
        self.addCleanup(self._migrate_to_latest)

    def test_composite_fks_are_dropped_and_restored(self):
        self.assertEqual(composite_fk_names(), COMPOSITE_FKS)

        executor = MigrationExecutor(connection)
        executor.migrate([("accounting", "0002_inventory")])
        self.assertEqual(composite_fk_names(), set())

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([("accounting", "0003_composite_fks")])
        self.assertEqual(composite_fk_names(), COMPOSITE_FKS)

    @staticmethod
    def _migrate_to_latest():
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
