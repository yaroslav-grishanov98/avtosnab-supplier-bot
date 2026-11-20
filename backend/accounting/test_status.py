from datetime import datetime, timedelta

from django.db import connection as db_connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounting.models import (
    AccountingStockSnapshot,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionStatus,
    LinkStatus,
    SnapshotStatus,
)
from accounting.status import ConnectionState, compute_status, statuses_for_venues
from accounting.test_support.sync import link, make_connection, make_venue

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.get_default_timezone())
CATALOGUE_AT = NOW - timedelta(hours=3)


@override_settings(ACCOUNTING_STOCK_STALE_AFTER_MINUTES=180)
class VenueStatusTests(TestCase):
    def setUp(self):
        self.venue = make_venue("Заведение")
        self.connection = make_connection(catalog_synced_at=CATALOGUE_AT)

    def connect(self, venue=None, status=LinkStatus.ACTIVE, connection=None):
        return link(connection or self.connection, venue or self.venue, status)

    def warehouse(self, venue=None, connection=None, external_id="wh-1"):
        return AccountingWarehouse.objects.create(
            connection=connection or self.connection, venue=venue or self.venue, external_id=external_id, name="Кухня"
        )

    def snapshot(self, wh, taken_at=NOW, status=SnapshotStatus.COMPLETE):
        return AccountingStockSnapshot.objects.create(
            connection=wh.connection, venue=wh.venue, warehouse=wh, taken_at=taken_at, synced_at=taken_at, status=status
        )

    def status(self, venue=None):
        return compute_status((venue or self.venue).id, NOW)

    def test_state_not_connected_without_link(self):
        status = self.status()
        self.assertEqual(
            (status.state, status.connection_id, status.provider), (ConnectionState.NOT_CONNECTED, None, None)
        )
        self.assertEqual((status.reason_code, status.last_success_at, status.warehouses_count), (None, None, 0))

    def test_state_needs_department_link_when_connected_but_no_department_assigned(self):
        self.connect(status=LinkStatus.PENDING)
        status = self.status()
        self.assertEqual(status.state, ConnectionState.NEEDS_DEPARTMENT_LINK)
        self.assertEqual((status.reason_code, status.connection_id), ("no_department_assigned", self.connection.id))

    def test_state_first_sync_pending_before_the_catalogue_is_loaded(self):
        self.connection.catalog_synced_at = None
        self.connection.save()
        self.connect(status=LinkStatus.PENDING)
        self.assertEqual(self.status().state, ConnectionState.FIRST_SYNC_PENDING)

    def test_state_first_sync_pending_before_first_success(self):
        self.connect()
        self.warehouse()
        status = self.status()
        self.assertEqual((status.state, status.warehouses_count), (ConnectionState.FIRST_SYNC_PENDING, 1))
        self.assertIsNone(status.last_success_at)

    def test_a_failed_snapshot_is_not_a_success(self):
        self.connect()
        self.snapshot(self.warehouse(), status=SnapshotStatus.FAILED)
        self.assertEqual(self.status().state, ConnectionState.FIRST_SYNC_PENDING)

    def test_state_active_after_success(self):
        self.connect()
        self.snapshot(self.warehouse(), taken_at=NOW - timedelta(minutes=30))
        status = self.status()
        self.assertEqual(status.state, ConnectionState.ACTIVE)
        self.assertEqual(
            (status.last_success_at, status.stale_since, status.reason_code), (NOW - timedelta(minutes=30), None, None)
        )
        self.assertEqual((status.provider, status.connection_id), ("iiko", self.connection.id))

    def test_a_partial_snapshot_counts_as_a_success(self):
        self.connect()
        self.snapshot(self.warehouse(), taken_at=NOW - timedelta(minutes=5), status=SnapshotStatus.PARTIAL)
        self.assertEqual(self.status().state, ConnectionState.ACTIVE)

    def test_state_stale_when_last_success_older_than_threshold(self):
        self.connect()
        last = NOW - timedelta(minutes=181)
        self.snapshot(self.warehouse(), taken_at=last)
        status = self.status()
        self.assertEqual((status.state, status.reason_code), (ConnectionState.STALE, "stock_stale"))
        self.assertEqual((status.last_success_at, status.stale_since), (last, last + timedelta(minutes=180)))

    def test_exactly_the_threshold_is_not_stale(self):
        self.connect()
        self.snapshot(self.warehouse(), taken_at=NOW - timedelta(minutes=180))
        self.assertEqual(self.status().state, ConnectionState.ACTIVE)

    def test_state_needs_attention_after_auth_error(self):
        self.connect()
        self.snapshot(self.warehouse(), taken_at=NOW - timedelta(minutes=10))
        self.connection.status = ConnectionStatus.NEEDS_ATTENTION
        self.connection.error_code = "auth_error"
        self.connection.save()
        status = self.status()
        self.assertEqual((status.state, status.reason_code), (ConnectionState.NEEDS_ATTENTION, "auth_error"))
        self.assertEqual(status.last_success_at, NOW - timedelta(minutes=10))  # the last good data is still told

    def test_needs_attention_without_a_code_gets_a_generic_reason(self):
        self.connect()
        self.connection.status = ConnectionStatus.NEEDS_ATTENTION
        self.connection.save()
        self.assertEqual(self.status().reason_code, "needs_attention")

    def test_state_disconnected_after_disconnect(self):
        self.connect()
        self.snapshot(self.warehouse(), taken_at=NOW - timedelta(minutes=5))
        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save()
        AccountingVenueLink.objects.filter(connection=self.connection).update(status=LinkStatus.INACTIVE)
        status = self.status()
        self.assertEqual((status.state, status.reason_code), (ConnectionState.DISCONNECTED, "disconnected"))
        self.assertEqual((status.connection_id, status.last_success_at), (None, None))  # nothing is shown as current

    def test_an_active_link_of_a_disabled_connection_is_still_disconnected(self):
        self.connect()
        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save()
        self.assertEqual(self.status().state, ConnectionState.DISCONNECTED)

    def test_venues_of_one_connection_have_their_own_state(self):
        other = make_venue("Второе")
        self.connect()
        self.connect(venue=other)
        self.snapshot(self.warehouse(), taken_at=NOW - timedelta(minutes=5))
        states = statuses_for_venues([self.venue.id, other.id], NOW)
        self.assertEqual(states[self.venue.id].state, ConnectionState.ACTIVE)
        self.assertEqual(states[other.id].state, ConnectionState.NEEDS_DEPARTMENT_LINK)

    def test_unknown_and_duplicate_ids_are_answered_once(self):
        stranger = make_venue("Чужое")
        states = statuses_for_venues([stranger.id, stranger.id], NOW)
        self.assertEqual(list(states), [stranger.id])
        self.assertEqual(statuses_for_venues([], NOW), {})

    def test_statuses_for_venues_uses_constant_number_of_queries(self):
        def build(count):
            venues = []
            for index in range(count):
                venue = make_venue(f"Заведение {index}")
                connection = make_connection(
                    base_url=f"https://c{count}-{index}.iiko.example/resto", catalog_synced_at=CATALOGUE_AT
                )
                link(connection, venue)
                self.snapshot(self.warehouse(venue, connection), taken_at=NOW - timedelta(minutes=index))
                venues.append(venue.id)
            return venues

        few, many = build(1), build(8)
        with CaptureQueriesContext(db_connection) as one:
            statuses_for_venues(few, NOW)
        with CaptureQueriesContext(db_connection) as eight:
            result = statuses_for_venues(many, NOW)
        self.assertEqual(len(one.captured_queries), len(eight.captured_queries))
        self.assertLessEqual(len(eight.captured_queries), 3)
        self.assertTrue(all(status.state == ConnectionState.ACTIVE for status in result.values()))
