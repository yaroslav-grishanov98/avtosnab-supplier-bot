import time
import uuid
from unittest import mock

from django.db import connection, connections
from django.test import TransactionTestCase

from accounting import locks
from accounting.locks import GuardResult, sync_guard

HOST = "iiko.example.com"


def _advisory_lock_count():
    """Advisory locks currently held in this database by any session."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
        return cursor.fetchone()[0]


def _wait_until_no_advisory_locks(timeout=3.0):
    """Closing a client connection frees its locks once the backend notices the disconnect."""
    deadline = time.monotonic() + timeout
    while _advisory_lock_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    return _advisory_lock_count()


def _try_lock_from_fresh_connection(key):
    """True if the advisory lock ``key`` is free: takes and releases it on a separate session."""
    probe = connections["default"].copy(alias="accounting_lock_probe")
    try:
        with probe.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
            free = cursor.fetchone()[0]
            if free:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
        return free
    finally:
        probe.close()


class SyncGuardTests(TransactionTestCase):
    """Advisory locks are session-level, so they live outside the test transaction."""

    def setUp(self):
        self.addCleanup(self._assert_no_lock_left_behind)

    def test_still_held_detects_released_lock(self):
        connection_id = uuid.uuid4()
        with sync_guard(connection_id, HOST, 1) as guard:
            self.assertTrue(guard.still_held())
            with guard.database.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock_all()")
            self.assertFalse(guard.still_held())

    def _assert_no_lock_left_behind(self):
        self.assertEqual(_wait_until_no_advisory_locks(), 0, "a test leaked a PostgreSQL advisory lock")

    def test_second_guard_on_same_connection_is_busy(self):
        connection_id = uuid.uuid4()
        with sync_guard(connection_id, HOST, 2) as first:
            self.assertEqual(first, GuardResult(acquired=True, reason=None))
            with sync_guard(connection_id, HOST, 2) as second:
                self.assertEqual(second, GuardResult(acquired=False, reason="connection_busy"))
            # The failed attempt must not have dropped the first holder's lock.
            self.assertFalse(_try_lock_from_fresh_connection(locks._connection_lock_key(connection_id)))
        self.assertTrue(_try_lock_from_fresh_connection(locks._connection_lock_key(connection_id)))

    def test_different_connections_do_not_block(self):
        with sync_guard(uuid.uuid4(), HOST, 2) as first, sync_guard(uuid.uuid4(), HOST, 2) as second:
            self.assertTrue(first.acquired)
            self.assertTrue(second.acquired)

    def test_host_slots_limit(self):
        with sync_guard(uuid.uuid4(), HOST, 2) as first, sync_guard(uuid.uuid4(), HOST, 2) as second:
            self.assertTrue(first.acquired)
            self.assertTrue(second.acquired)
            with sync_guard(uuid.uuid4(), HOST, 2) as third:
                self.assertEqual(third, GuardResult(acquired=False, reason="host_busy"))
            # A different host has its own slots.
            with sync_guard(uuid.uuid4(), "other.example.com", 2) as other_host:
                self.assertTrue(other_host.acquired)

    def test_host_is_normalized_for_slot_keys(self):
        with sync_guard(uuid.uuid4(), "IIKO.Example.com ", 1) as first:
            self.assertTrue(first.acquired)
            with sync_guard(uuid.uuid4(), " iiko.example.com", 1) as second:
                self.assertEqual(second, GuardResult(acquired=False, reason="host_busy"))

    def test_host_busy_releases_the_connection_lock(self):
        busy_connection = uuid.uuid4()
        with sync_guard(uuid.uuid4(), HOST, 1):
            with sync_guard(busy_connection, HOST, 1) as result:
                self.assertEqual(result.reason, "host_busy")
            self.assertTrue(_try_lock_from_fresh_connection(locks._connection_lock_key(busy_connection)))

    def test_slot_released_on_exit_and_on_exception(self):
        with sync_guard(uuid.uuid4(), HOST, 1) as first:
            self.assertTrue(first.acquired)
        with sync_guard(uuid.uuid4(), HOST, 1) as after_exit:
            self.assertTrue(after_exit.acquired)

        with self.assertRaisesMessage(RuntimeError, "boom"):
            with sync_guard(uuid.uuid4(), HOST, 1):
                raise RuntimeError("boom")
        with sync_guard(uuid.uuid4(), HOST, 1) as after_exception:
            self.assertTrue(after_exception.acquired)

    def test_lock_released_when_dedicated_connection_is_closed_without_exit(self):
        opened = []
        real_open = locks._open_dedicated_connection

        def recording_open():
            database = real_open()
            opened.append(database)
            return database

        connection_id = uuid.uuid4()
        with mock.patch.object(locks, "_open_dedicated_connection", recording_open):
            guard = sync_guard(connection_id, HOST, 1)
            result = guard.__enter__()
            self.assertTrue(result.acquired)
            self.assertEqual(len(opened), 1, "the guard must use exactly one dedicated connection")
            self.assertIsNot(opened[0], connections["default"])
            self.assertFalse(_try_lock_from_fresh_connection(locks._connection_lock_key(connection_id)))
            self.assertFalse(_try_lock_from_fresh_connection(locks._host_slot_key(HOST, 0)))

            # A dead worker never reaches __exit__: its session simply goes away.
            opened[0].close()
            self.assertEqual(_wait_until_no_advisory_locks(), 0)
            self.assertTrue(_try_lock_from_fresh_connection(locks._connection_lock_key(connection_id)))
            self.assertTrue(_try_lock_from_fresh_connection(locks._host_slot_key(HOST, 0)))

            # Whatever the late exit does, it must not raise or leave a session behind.
            guard.__exit__(None, None, None)

    def test_connection_busy_does_not_consume_host_slot(self):
        connection_id = uuid.uuid4()
        with sync_guard(connection_id, HOST, 1):
            # The only slot is taken by the guard above, yet a second guard for the same
            # connection must report connection_busy, not host_busy, and take no slot.
            with sync_guard(connection_id, HOST, 1) as busy:
                self.assertEqual(busy, GuardResult(acquired=False, reason="connection_busy"))
        # With two slots: the busy attempt must leave the second slot free.
        connection_id = uuid.uuid4()
        with sync_guard(connection_id, HOST, 2):
            with sync_guard(connection_id, HOST, 2) as busy:
                self.assertEqual(busy.reason, "connection_busy")
            self.assertFalse(_try_lock_from_fresh_connection(locks._host_slot_key(HOST, 0)))
            self.assertTrue(_try_lock_from_fresh_connection(locks._host_slot_key(HOST, 1)))

    def test_dedicated_connection_is_closed_even_if_unlock_fails(self):
        opened = []
        real_open = locks._open_dedicated_connection

        def recording_open():
            database = real_open()
            opened.append(database)
            return database

        with mock.patch.object(locks, "_open_dedicated_connection", recording_open):
            with mock.patch.object(locks, "_unlock", side_effect=RuntimeError("unlock failed")):
                with self.assertLogs("accounting.locks", level="WARNING"):
                    with sync_guard(uuid.uuid4(), HOST, 1) as result:
                        self.assertTrue(result.acquired)
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0].connection, "the dedicated connection must be closed")

    def test_dedicated_connection_is_closed_when_not_acquired(self):
        opened = []
        real_open = locks._open_dedicated_connection

        def recording_open():
            database = real_open()
            opened.append(database)
            return database

        connection_id = uuid.uuid4()
        with mock.patch.object(locks, "_open_dedicated_connection", recording_open):
            with sync_guard(connection_id, HOST, 1):
                with sync_guard(connection_id, HOST, 1) as busy:
                    self.assertFalse(busy.acquired)
        self.assertEqual(len(opened), 2)
        for database in opened:
            self.assertIsNone(database.connection)

    def test_invalid_arguments_are_rejected(self):
        for host, slots in [(HOST, 0), (HOST, -1), ("", 1), ("   ", 1)]:
            with self.subTest(host=host, slots=slots):
                with self.assertRaises(ValueError):
                    with sync_guard(uuid.uuid4(), host, slots):
                        self.fail("the body must not run")
