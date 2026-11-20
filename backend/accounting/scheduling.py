"""Database claims and connection backoff, independent of Celery delivery."""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef

from accounting.models import (
    AccountingConnection,
    AccountingSyncJob,
    ConnectionStatus,
    LinkStatus,
    SyncKind,
    SyncStatus,
    SyncTrigger,
)
from accounting.sync.consumption import consumption_enabled
from core.models import EntityStatus


def jitter_for(connection_id, interval_minutes):
    interval_minutes = max(1, interval_minutes)
    value = int.from_bytes(hashlib.sha256(str(connection_id).encode()).digest()[:8], "big")
    return timedelta(minutes=value % interval_minutes)


def consumption_due(connection, now):
    """Consumption runs once a day, and again at once while a backfill still has days left.

    Skipped jobs count as done (a connection without linked warehouses must not be asked every minute); failed
    ones do not, so the connection's own backoff decides when to try again."""
    if not consumption_enabled(connection):
        return False
    last = (
        connection.sync_jobs.filter(
            kind=SyncKind.CONSUMPTION, status__in=(SyncStatus.SUCCESS, SyncStatus.PARTIAL, SyncStatus.SKIPPED)
        )
        .order_by("-finished_at")
        .first()
    )
    if last is None or last.finished_at is None:
        return True
    if (last.stats or {}).get("remaining", 0) > 0:
        return True
    return last.finished_at <= now - timedelta(hours=settings.ACCOUNTING_CONSUMPTION_INTERVAL_HOURS)


def decide_job_kind(connection, now):
    """The catalogue first (daily), then consumption (daily), otherwise stock.

    A claim creates one job, so on the day the catalogue or consumption is due, the stock sync waits for the
    next interval."""
    if connection.catalog_synced_at is None or connection.catalog_synced_at <= now - timedelta(
        hours=settings.ACCOUNTING_CATALOG_INTERVAL_HOURS
    ):
        return SyncKind.CATALOG
    if consumption_due(connection, now):
        return SyncKind.CONSUMPTION
    return SyncKind.STOCK


@transaction.atomic
def claim_due_connections(now, limit=100):
    busy = AccountingSyncJob.objects.filter(
        connection_id=OuterRef("pk"), status__in=(SyncStatus.QUEUED, SyncStatus.RUNNING)
    )
    eligible = (
        AccountingConnection.objects.filter(
            status=ConnectionStatus.ACTIVE,
            next_sync_at__lte=now,
            venue_links__status=LinkStatus.ACTIVE,
            venue_links__venue__status=EntityStatus.ACTIVE,
        )
        .filter(~Exists(busy))
        .values("pk")
    )
    connections = (
        AccountingConnection.objects.filter(pk__in=eligible)
        .select_for_update(skip_locked=True)
        .order_by("next_sync_at", "pk")[:limit]
    )
    jobs = []
    for connection in connections:
        jobs.append(
            AccountingSyncJob.objects.create(
                connection=connection,
                kind=decide_job_kind(connection, now),
                trigger=SyncTrigger.SCHEDULE,
                queued_at=now,
            )
        )
        connection.next_sync_at = (
            now
            + timedelta(minutes=max(1, connection.sync_interval_minutes))
            + jitter_for(connection.pk, connection.sync_interval_minutes)
        )
        connection.save(update_fields=["next_sync_at", "updated_at"])
    return jobs


@transaction.atomic
def register_success(connection, now):
    current = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
    # A disconnect or credential error recorded during the sync must survive its result.
    if current.status != ConnectionStatus.ACTIVE:
        return
    current.last_attempt_at = now
    current.last_success_at = now
    current.consecutive_failures = 0
    current.error_code = current.stale_reason = ""
    current.next_sync_at = (
        now
        + timedelta(minutes=max(1, current.sync_interval_minutes))
        + jitter_for(current.pk, current.sync_interval_minutes)
    )
    current.save(
        update_fields=[
            "last_attempt_at",
            "last_success_at",
            "consecutive_failures",
            "error_code",
            "stale_reason",
            "next_sync_at",
            "updated_at",
        ]
    )


@transaction.atomic
def register_failure(connection, error, now):
    current = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
    if current.status != ConnectionStatus.ACTIVE:
        return
    current.last_attempt_at = now
    current.error_code = current.stale_reason = error.code[:64]
    current.consecutive_failures += 1
    if not error.retryable:
        current.status = ConnectionStatus.NEEDS_ATTENTION
        current.next_sync_at = None
    else:
        delay = min(60 * 2 ** min(current.consecutive_failures, 10), 3600)
        if current.consecutive_failures >= settings.ACCOUNTING_BREAKER_THRESHOLD:
            delay = max(delay, settings.ACCOUNTING_BREAKER_COOLDOWN_MINUTES * 60)
        delay = max(delay, error.retry_after or 0)
        current.next_sync_at = now + timedelta(seconds=delay) + jitter_for(current.pk, 2)
    current.save(
        update_fields=[
            "last_attempt_at",
            "error_code",
            "stale_reason",
            "consecutive_failures",
            "status",
            "next_sync_at",
            "updated_at",
        ]
    )
