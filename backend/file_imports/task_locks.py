"""Exclusive import execution without holding row transactions during extraction/AI."""

import hashlib
from contextlib import contextmanager
from functools import wraps

from django.db import connections


@contextmanager
def import_task_lock(kind, identifier):
    # A dedicated connection keeps the lock alive across close_old_connections in tasks.
    lock_id = int.from_bytes(
        hashlib.blake2b(f"{kind}:{identifier}".encode(), digest_size=8).digest(), "big", signed=True
    )
    database = connections["default"].copy(alias="import_lock")
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
            acquired = cursor.fetchone()[0]
        yield acquired
    finally:
        # Closing the connection releases the advisory lock, including on worker death.
        database.close()


def exclusive_import_task(kind, id_field):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with import_task_lock(kind, kwargs[id_field]) as acquired:
                if not acquired:
                    return {id_field: str(kwargs[id_field]), "skipped": True, "reason": "already_processing"}
                return function(*args, **kwargs)

        return run

    return decorate
