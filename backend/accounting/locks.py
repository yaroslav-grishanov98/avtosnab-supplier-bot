"""PostgreSQL advisory-lock guards for accounting syncs.

``sync_guard`` serialises syncs of one accounting connection and caps how many
syncs run at once against one iiko host. Both locks are taken on a single
dedicated database connection, not on the request/Celery task connection:
Celery's ``close_old_connections`` and request-finished hooks would otherwise
drop the session (and the lock with it) in the middle of a sync. Closing the
dedicated connection releases both locks, which also covers a dead worker.

No module-level state is shared, so the guard is safe in thread-pool workers.
"""

import hashlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from django.db import connections

logger = logging.getLogger(__name__)

CONNECTION_BUSY = "connection_busy"
HOST_BUSY = "host_busy"


@dataclass(frozen=True)
class GuardResult:
    acquired: bool
    # "connection_busy" | "host_busy" | None
    reason: str | None
    database: object = field(default=None, compare=False, repr=False)
    key: int | None = field(default=None, compare=False, repr=False)

    def still_held(self):
        if not self.acquired or self.database is None:
            return False
        try:
            # Check the original session; never reconnect and mistake a new session
            # for the holder of the lock that protected this sync.
            if self.database.connection is None:
                return False
            unsigned = self.key & ((1 << 64) - 1)
            with self.database.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND classid = %s AND objid = %s "
                    "AND objsubid = 1 AND granted)",
                    [unsigned >> 32, unsigned & 0xFFFFFFFF],
                )
                return cursor.fetchone()[0]
        except Exception:
            return False


def _lock_id(name: str) -> int:
    # Same derivation as file_imports.task_locks.import_task_lock (blake2b -> signed bigint).
    # Kept as a private copy so accounting does not depend on file_imports.
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)


def _connection_lock_key(connection_id: uuid.UUID) -> int:
    return _lock_id(f"accounting:conn:{connection_id}")


def _host_slot_key(host: str, slot: int) -> int:
    return _lock_id(f"accounting:host:{host}:{slot}")


def _open_dedicated_connection():
    """A new database connection outside the pool; the caller must close it."""
    database = connections["default"].copy(alias="accounting_sync_lock")
    database.settings_dict["OPTIONS"] = {
        **database.settings_dict.get("OPTIONS", {}),
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
    return database


def _try_lock(cursor, key: int) -> bool:
    cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
    return cursor.fetchone()[0]


def _unlock(database, keys: list[int]) -> None:
    with database.cursor() as cursor:
        for key in keys:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def _acquire(database, connection_id: uuid.UUID, host: str, host_slots: int) -> tuple[GuardResult, list[int]]:
    """Take the connection lock, then the first free host slot. Returns the result and the keys held."""
    with database.cursor() as cursor:
        connection_key = _connection_lock_key(connection_id)
        if not _try_lock(cursor, connection_key):
            return GuardResult(acquired=False, reason=CONNECTION_BUSY), []
        for slot in range(host_slots):
            slot_key = _host_slot_key(host, slot)
            if _try_lock(cursor, slot_key):
                return GuardResult(acquired=True, reason=None), [connection_key, slot_key]
        cursor.execute("SELECT pg_advisory_unlock(%s)", [connection_key])
    return GuardResult(acquired=False, reason=HOST_BUSY), []


@contextmanager
def sync_guard(connection_id: uuid.UUID, host: str, host_slots: int) -> Iterator[GuardResult]:
    """Hold the per-connection lock and one of ``host_slots`` per-host slots for the ``with`` body.

    The connection lock is tried first, so a busy connection never consumes a
    host slot. If no slot is free, the connection lock is released again.
    """
    host = host.strip().lower() if isinstance(host, str) else ""
    if not host:
        raise ValueError("Не указан хост iiko для блокировки синхронизации.")
    if not isinstance(host_slots, int) or isinstance(host_slots, bool) or host_slots < 1:
        raise ValueError("Количество слотов хоста должно быть целым числом не меньше 1.")

    database = _open_dedicated_connection()
    held: list[int] = []
    try:
        result, held = _acquire(database, connection_id, host, host_slots)
        if result.acquired:
            result = replace(result, database=database, key=_connection_lock_key(connection_id))
        yield result
    finally:
        try:
            if held:
                try:
                    _unlock(database, held)
                except Exception:
                    # Closing the connection below releases the locks anyway.
                    logger.warning("Не удалось явно снять advisory-блокировки синхронизации", exc_info=True)
        finally:
            # Closing the connection releases the advisory locks, including on worker death.
            database.close()
