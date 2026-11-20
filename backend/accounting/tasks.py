"""Queue delivery for accounting syncs. All provider I/O lives in the runner."""

import logging
import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from accounting import scheduling
from accounting.dto import AdapterError
from accounting.locks import CONNECTION_BUSY, sync_guard
from accounting.models import AccountingSyncJob, SyncStatus, SyncTrigger
from accounting.sync.runner import run_sync_job

logger = logging.getLogger(__name__)


def enqueue_job(job):
    queue = (
        settings.ACCOUNTING_QUEUE_MANUAL
        if job.trigger in (SyncTrigger.MANUAL, SyncTrigger.CONNECT)
        else settings.ACCOUNTING_QUEUE_SYNC
    )
    result = run_sync_job_task.apply_async(args=[str(job.pk)], queue=queue)
    AccountingSyncJob.objects.filter(pk=job.pk).update(celery_task_id=result.id)
    return result


@shared_task(name="accounting.enqueue_due_syncs", acks_late=True, reject_on_worker_lost=True)
def enqueue_due_syncs():
    if not settings.ACCOUNTING_SCHEDULED_SYNC_ENABLED:
        return 0
    jobs = scheduling.claim_due_connections(timezone.now())
    for job in jobs:
        enqueue_job(job)
    return len(jobs)


@shared_task(bind=True, name="accounting.run_sync_job", acks_late=True, reject_on_worker_lost=True)
def run_sync_job_task(self, job_id):
    close_old_connections()
    started = time.monotonic()
    try:
        outcome = run_sync_job(job_id)
        if outcome.retry_in is not None and outcome.status in (SyncStatus.QUEUED, SyncStatus.RUNNING):
            raise self.retry(countdown=outcome.retry_in, max_retries=20)
        if outcome.ran:
            if outcome.status in (SyncStatus.SUCCESS, SyncStatus.PARTIAL):
                scheduling.register_success(outcome.job.connection, timezone.now())
            elif outcome.status == SyncStatus.FAILED:
                error = outcome.error or AdapterError(
                    outcome.job.error_code or "sync_failed", "Синхронизация не выполнена.", retryable=True
                )
                scheduling.register_failure(outcome.job.connection, error, timezone.now())
        logger.info(
            "Accounting sync finished",
            extra={
                "connection_id": str(outcome.job.connection_id),
                "job_id": str(job_id),
                "kind": outcome.job.kind,
                "status": outcome.status,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return outcome.status
    finally:
        close_old_connections()


def _worker_holds_connection_lock(connection) -> bool:
    """True when another session holds the connection lock now, i.e. the worker of a RUNNING job is still alive.

    ``host_busy`` also proves the connection lock was free: the guard takes it first and releases it again."""
    with sync_guard(connection.id, connection.host or str(connection.id), settings.ACCOUNTING_HOST_SLOTS) as guard:
        return not guard.acquired and guard.reason == CONNECTION_BUSY


@shared_task(name="accounting.recover_stalled_jobs", acks_late=True, reject_on_worker_lost=True)
def recover_stalled_jobs():
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.ACCOUNTING_JOB_STALL_SECONDS)
    lost = 0
    stalled = AccountingSyncJob.objects.filter(status=SyncStatus.RUNNING, started_at__lt=cutoff).select_related(
        "connection"
    )
    for job in stalled:
        # A long but live sync keeps the connection lock on its own database session; only a dead worker has lost it.
        if _worker_holds_connection_lock(job.connection):
            continue
        lost += AccountingSyncJob.objects.filter(pk=job.pk, status=SyncStatus.RUNNING).update(
            status=SyncStatus.FAILED, error_code="worker_lost", finished_at=timezone.now()
        )
    # Re-delivery also repairs a crash between DB claim and broker publish. The runner
    # serializes duplicate deliveries and never executes a finished job again.
    for job in AccountingSyncJob.objects.filter(status=SyncStatus.QUEUED, queued_at__lt=cutoff).iterator(
        chunk_size=100
    ):
        try:
            enqueue_job(job)
        except Exception as exc:
            logger.warning("Accounting job %s delivery failed (%s)", job.pk, type(exc).__name__)
    return lost
