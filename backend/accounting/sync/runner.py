"""Runs one ``AccountingSyncJob`` end to end: guard, adapter session, the sync, status and stats.

The scheduler and Celery tasks (Task 12) call ``run_sync_job`` and react to the
returned ``SyncOutcome``; this module does not depend on scheduling.

* Busy connection or host (``sync_guard`` not acquired): the job row is left
  untouched, the outcome reports its real status (QUEUED, or RUNNING under
  another worker) and ``retry_in`` says when to try again (deterministic
  jitter per job).
* ``SyncOutcome.ran`` is True only when this call executed the job; callers
  register success/failure only then. Never call ``run_sync_job`` inside
  ``transaction.atomic``: RUNNING must be visible to other processes and the
  per-warehouse commits and ``on_commit`` signals must not be deferred.
* Every final write is conditional on the status the runner expects, so a
  FAILED "worker_lost" written meanwhile elsewhere is never overwritten; the
  outcome then reports the job as it is.
* ``AdapterError`` is neither raised nor swallowed: it is returned in
  ``SyncOutcome.error`` and its code is stored on the job.
* Any other exception marks the job FAILED with a fixed message (no exception
  text or traceback in the database) and is re-raised.
* A job that already finished is a no-op returning its stored status, so a
  redelivered task never adds snapshots. A job found RUNNING while we hold the
  connection lock was left by a dead worker: it becomes FAILED ("worker_lost")
  instead of being run a second time.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils import timezone

from accounting.adapters import get_adapter
from accounting.dto import AdapterError
from accounting.locks import sync_guard
from accounting.models import (
    AccountingSyncJob,
    ConnectionStatus,
    SyncKind,
    SyncStatus,
)
from accounting.sync.catalog import CatalogStats, sync_catalog
from accounting.sync.consumption import (
    CONSUMPTION_UNSUPPORTED,
    ConsumptionStats,
    consumption_enabled,
    load_product_catalog,
    sync_consumption,
)
from accounting.sync.stock import ProductRef, StockStats, load_products, readable_warehouses, sync_stock

logger = logging.getLogger(__name__)

FINAL_STATUSES = frozenset({SyncStatus.SUCCESS, SyncStatus.PARTIAL, SyncStatus.FAILED, SyncStatus.SKIPPED})

BUSY_RETRY_BASE_SECONDS = 30
BUSY_RETRY_SPREAD_SECONDS = 30

INTERNAL_ERROR = "internal_error"
WORKER_LOST = "worker_lost"
CONNECTION_DISABLED = "connection_disabled"
NO_LINKED_WAREHOUSES = "no_linked_warehouses"
KIND_NOT_SUPPORTED = "kind_not_supported"

_INTERNAL_ERROR_MESSAGE = "Внутренняя ошибка синхронизации. Подробности — в журнале сервера."
_WORKER_LOST_MESSAGE = "Предыдущий запуск синхронизации прервался, не завершившись."


@dataclass(frozen=True)
class SyncOutcome:
    job: AccountingSyncJob
    status: str
    error: AdapterError | None = None
    retry_in: float | None = None
    # True only when this call executed the job: took it out of QUEUED and wrote its final status (also
    # SKIPPED). False for a busy guard, an already finished job, a redelivered task, a job found RUNNING
    # (worker_lost: the dead worker's run) and a lost race on the final write. The scheduler (Task 12) must
    # register success/failure only when ran is True, or a redelivery counts the same failure twice.
    ran: bool = False


def busy_retry_delay(job_id: uuid.UUID) -> float:
    """Seconds until a job that found its connection or host busy is tried again: 30–60, the same for one job.

    Derived from the job id, so retries of many jobs spread out and tests stay stable.
    """
    return float(BUSY_RETRY_BASE_SECONDS + uuid.UUID(str(job_id)).int % (BUSY_RETRY_SPREAD_SECONDS + 1))


def run_sync_job(job_id: uuid.UUID) -> SyncOutcome:
    job = AccountingSyncJob.objects.select_related("connection").get(pk=job_id)
    if job.status in FINAL_STATUSES:
        return SyncOutcome(job=job, status=job.status)
    connection = job.connection
    with sync_guard(connection.id, connection.host, settings.ACCOUNTING_HOST_SLOTS) as guard:
        if not guard.acquired:
            logger.info("Синхронизация %s отложена: %s", job.job_id, guard.reason)
            # The job is left as it is; its real status is reported (RUNNING under another worker stays RUNNING).
            return SyncOutcome(job=job, status=job.status, retry_in=busy_retry_delay(job.job_id))
        return _run_guarded(job.job_id, guard=guard)


def _run_guarded(job_id: uuid.UUID, *, guard=None) -> SyncOutcome:
    # Re-read under the connection lock: nobody else can be running this connection now.
    job = AccountingSyncJob.objects.select_related("connection").get(pk=job_id)
    connection = job.connection
    if job.status in FINAL_STATUSES:
        return SyncOutcome(job=job, status=job.status)
    if job.status == SyncStatus.RUNNING:
        error = AdapterError(code=WORKER_LOST, message=_WORKER_LOST_MESSAGE, retryable=True)
        stats = {**(job.stats or {}), "error": _error_json(error)}
        if not _finish(job, SyncStatus.RUNNING, SyncStatus.FAILED, WORKER_LOST, stats):
            return SyncOutcome(job=job, status=job.status)
        return SyncOutcome(job=job, status=SyncStatus.FAILED, error=error)
    skip_code = ""
    if connection.status == ConnectionStatus.DISABLED:
        skip_code = CONNECTION_DISABLED
    elif job.kind not in (SyncKind.CATALOG, SyncKind.STOCK, SyncKind.CONSUMPTION):
        skip_code = KIND_NOT_SUPPORTED
    elif job.kind == SyncKind.CONSUMPTION and not consumption_enabled(connection):
        # The source cannot give consumption (recorded capability): no session, no licence slot.
        skip_code = CONSUMPTION_UNSUPPORTED
    elif job.kind in (SyncKind.STOCK, SyncKind.CONSUMPTION) and not readable_warehouses(connection).exists():
        # Nothing to read: do not open an iiko session (and spend a licence slot) for it.
        skip_code = NO_LINKED_WAREHOUSES
    if skip_code:
        ran = _finish(job, SyncStatus.QUEUED, SyncStatus.SKIPPED, skip_code, {"kind": job.kind})
        return SyncOutcome(job=job, status=job.status, ran=ran)

    started_at = timezone.now()
    claimed = AccountingSyncJob.objects.filter(pk=job.pk, status=SyncStatus.QUEUED).update(
        status=SyncStatus.RUNNING, started_at=started_at
    )
    if not claimed:
        job.refresh_from_db()
        return SyncOutcome(job=job, status=job.status)
    job.status, job.started_at = SyncStatus.RUNNING, started_at

    try:
        if job.kind == SyncKind.CATALOG:
            # A catalogue job gets no adapter options: it downloads the units and asks the server version
            # itself, refreshing both.
            with get_adapter(connection) as adapter:
                stats: CatalogStats | StockStats | ConsumptionStats = sync_catalog(adapter, connection, job)
        elif job.kind == SyncKind.CONSUMPTION:
            # The stored catalogue serves the adapter's article map, so no product download per session (D9/D13).
            with get_adapter(connection, product_catalog=load_product_catalog(connection)) as adapter:
                stats = sync_consumption(adapter, connection, job, today=timezone.localdate())
        else:
            # The stored catalogue is read once and serves both the adapter's unit map and the stock sync.
            products = load_products(connection)
            with get_adapter(connection, **_stock_adapter_options(connection, products)) as adapter:
                stats = sync_stock(adapter, connection, job, products=products)
    except AdapterError as exc:
        if guard is not None and not guard.still_held():
            return _lock_lost(job)
        logger.warning("Синхронизация %s (%s) не выполнена: %s", job.job_id, job.kind, exc.code)
        if not _finish(
            job, SyncStatus.RUNNING, SyncStatus.FAILED, exc.code, {"kind": job.kind, "error": _error_json(exc)}
        ):
            return SyncOutcome(job=job, status=job.status)
        return SyncOutcome(job=job, status=SyncStatus.FAILED, error=exc, ran=True)
    except Exception as exc:
        # Type only: the message or traceback may carry data that must not reach logs or the database.
        logger.error("Синхронизация %s (%s) упала с внутренней ошибкой %s", job.job_id, job.kind, type(exc).__name__)
        try:
            _finish(
                job,
                SyncStatus.RUNNING,
                SyncStatus.FAILED,
                INTERNAL_ERROR,
                {"kind": job.kind, "error": {"code": INTERNAL_ERROR, "message": _INTERNAL_ERROR_MESSAGE}},
            )
        except Exception as finish_exc:
            logger.error("Не удалось отметить задачу %s как неуспешную (%s)", job.job_id, type(finish_exc).__name__)
        raise

    status, error_code, error = _result(stats)
    if guard is not None and not guard.still_held():
        return _lock_lost(job)
    if not _finish(job, SyncStatus.RUNNING, status, error_code, stats.to_json()):
        return SyncOutcome(job=job, status=job.status)
    logger.info(
        "Синхронизация %s (%s) завершена: %s",
        job.job_id,
        job.kind,
        status,
        # Structured fields for log search: ids and numbers only, never a venue name, login or URL.
        extra={
            "connection_id": str(job.connection_id),
            "job_id": str(job.job_id),
            "kind": job.kind,
            "duration_ms": round((job.finished_at - started_at).total_seconds() * 1000),
            "status": status,
        },
    )
    return SyncOutcome(job=job, status=status, error=error, ran=True)


def _lock_lost(job):
    error = AdapterError(code=WORKER_LOST, message=_WORKER_LOST_MESSAGE, retryable=True)
    _finish(job, SyncStatus.RUNNING, SyncStatus.FAILED, WORKER_LOST, {"kind": job.kind, "error": _error_json(error)})
    return SyncOutcome(job=job, status=job.status, error=error)


def _stock_adapter_options(connection, products: dict[str, ProductRef]) -> dict[str, Any]:
    """The stored unit map (keyed by ``id_key``, as the iiko adapter looks it up) and server version (plan D9)."""
    units = {key: product.unit_name for key, product in products.items() if product.unit_name}
    return {"product_units": units, "source_version": connection.server_version or None}


def _result(stats: CatalogStats | StockStats | ConsumptionStats) -> tuple[str, str, AdapterError | None]:
    """Job status, error code and the adapter error to return, from what the sync stored."""
    first_code = stats.errors[0]["code"] if stats.errors else ""
    if isinstance(stats, CatalogStats):
        return (SyncStatus.PARTIAL if stats.errors else SyncStatus.SUCCESS), first_code, None
    if isinstance(stats, ConsumptionStats):
        stop = stats.stopped
        code = stop.code if stop is not None else first_code
        if stats.unsupported:
            return SyncStatus.SKIPPED, CONSUMPTION_UNSUPPORTED, None
        if stats.warehouses == 0:
            return SyncStatus.SKIPPED, NO_LINKED_WAREHOUSES, None
        if stats.stored == 0 and (stats.failed or stats.remaining):
            return SyncStatus.FAILED, code, stop
        if stats.partial or stats.failed or stats.remaining or stop is not None:
            return SyncStatus.PARTIAL, code, stop
        return SyncStatus.SUCCESS, "", None
    error = stats.error
    code = error.code if error is not None else first_code
    if stats.warehouses == 0:
        return SyncStatus.SKIPPED, NO_LINKED_WAREHOUSES, error
    if stats.stored == 0:
        return SyncStatus.FAILED, code, error
    if stats.partial or stats.failed or error is not None:
        return SyncStatus.PARTIAL, code, error
    return SyncStatus.SUCCESS, "", None


def _error_json(error: AdapterError) -> dict[str, str]:
    return {"code": error.code, "message": error.message}


def _finish(job: AccountingSyncJob, expected: str, status: str, error_code: str, stats: dict[str, Any]) -> bool:
    """Move the job from ``expected`` to its final ``status``; False (job refreshed) if someone changed it first.

    Conditional, so a FAILED "worker_lost" written meanwhile by another runner or by recover_stalled_jobs
    (Task 12) is never overwritten.
    """
    values = {
        "status": status,
        "finished_at": timezone.now(),
        "error_code": (error_code or "")[: AccountingSyncJob._meta.get_field("error_code").max_length],
        "stats": stats,
    }
    if not AccountingSyncJob.objects.filter(pk=job.pk, status=expected).update(**values):
        logger.warning("Задача синхронизации %s уже изменена другим процессом, итог не записан", job.job_id)
        job.refresh_from_db()
        return False
    for name, value in values.items():
        setattr(job, name, value)
    return True
