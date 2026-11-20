import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounting import scheduling
from accounting.dto import AdapterError
from accounting.models import AccountingSyncJob, ConnectionStatus, SyncKind
from accounting.test_support.sync import link, make_connection, make_venue
from core.models import EntityStatus


class SchedulingTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.connection = make_connection(next_sync_at=self.now)
        link(self.connection, make_venue("Test venue"))

    def test_jitter_is_deterministic_and_spreads_connections(self):
        counts = {}
        for i in range(300):
            key = uuid.UUID(int=i)
            value = scheduling.jitter_for(key, 60)
            self.assertEqual(value, scheduling.jitter_for(key, 60))
            counts[value] = counts.get(value, 0) + 1
        self.assertLessEqual(max(counts.values()), 10)
        self.assertGreater(len(counts), 50)

    def test_claim_only_once_and_skips_busy_connection(self):
        jobs = scheduling.claim_due_connections(self.now)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, SyncKind.CATALOG)
        self.assertEqual(scheduling.claim_due_connections(self.now), [])
        self.connection.next_sync_at = self.now
        self.connection.save()
        self.assertEqual(scheduling.claim_due_connections(self.now), [])

    def test_claim_skips_connections_without_active_venue(self):
        self.connection.venue_links.update(status="inactive")
        self.assertEqual(scheduling.claim_due_connections(self.now), [])
        self.connection.venue_links.update(status="active")
        venue = self.connection.venue_links.get().venue
        venue.status = EntityStatus.ARCHIVED
        venue.save()
        self.assertEqual(scheduling.claim_due_connections(self.now), [])

    def test_decide_job_kind_priority(self):
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.CATALOG)
        self.connection.catalog_synced_at = self.now
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.STOCK)

    def finished_consumption_job(self, status, finished_ago, stats=None):
        job = AccountingSyncJob.objects.create(
            connection=self.connection, kind=SyncKind.CONSUMPTION, trigger="schedule", stats=stats or {}
        )
        AccountingSyncJob.objects.filter(pk=job.pk).update(status=status, finished_at=self.now - finished_ago)
        return job

    def consumption_connection(self, enabled=True):
        self.connection.catalog_synced_at = self.now
        self.connection.capabilities = {"read_consumption": enabled}
        self.connection.save()

    def test_consumption_is_dispatched_daily_once_the_catalogue_is_fresh(self):
        self.consumption_connection()
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.CONSUMPTION)  # never done
        self.finished_consumption_job("success", timedelta(hours=3))
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.STOCK)
        self.finished_consumption_job("success", timedelta(hours=25))  # an older one does not matter
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.STOCK)
        AccountingSyncJob.objects.filter(kind=SyncKind.CONSUMPTION).update(finished_at=self.now - timedelta(hours=25))
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.CONSUMPTION)

    def test_a_backfill_with_days_left_continues_without_waiting_a_day(self):
        self.consumption_connection()
        self.finished_consumption_job("partial", timedelta(hours=1), {"remaining": 4})
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.CONSUMPTION)
        AccountingSyncJob.objects.filter(kind=SyncKind.CONSUMPTION).update(stats={"remaining": 0})
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.STOCK)

    def test_failed_consumption_job_does_not_count_as_done(self):
        self.consumption_connection()
        self.finished_consumption_job("failed", timedelta(hours=1))
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.CONSUMPTION)

    def test_skipped_consumption_job_counts_as_done_so_stock_is_not_starved(self):
        self.consumption_connection()
        self.finished_consumption_job("skipped", timedelta(hours=1))
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.STOCK)

    def test_consumption_is_never_dispatched_without_the_capability_and_catalogue_comes_first(self):
        self.consumption_connection(enabled=False)
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.STOCK)
        self.consumption_connection(enabled=True)
        self.connection.catalog_synced_at = self.now - timedelta(days=2)
        self.assertEqual(scheduling.decide_job_kind(self.connection, self.now), SyncKind.CATALOG)

    def test_auth_error_stops_schedule_and_sets_needs_attention(self):
        scheduling.register_failure(self.connection, AdapterError("auth_error", "Ошибка доступа."), self.now)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, ConnectionStatus.NEEDS_ATTENTION)
        self.assertIsNone(self.connection.next_sync_at)

    @override_settings(ACCOUNTING_BREAKER_THRESHOLD=2, ACCOUNTING_BREAKER_COOLDOWN_MINUTES=60)
    def test_timeouts_open_breaker_and_success_resets(self):
        error = AdapterError("timeout", "Нет ответа.", retryable=True)
        for _ in range(2):
            scheduling.register_failure(self.connection, error, self.now)
        self.connection.refresh_from_db()
        self.assertGreaterEqual(self.connection.next_sync_at, self.now + timedelta(hours=1))
        scheduling.register_success(self.connection, self.now)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.consecutive_failures, 0)
        self.assertEqual(self.connection.last_success_at, self.now)

    def test_rate_limited_respects_retry_after(self):
        scheduling.register_failure(
            self.connection, AdapterError("rate_limited", "Повторите позже.", True, 7200), self.now
        )
        self.connection.refresh_from_db()
        self.assertGreaterEqual(self.connection.next_sync_at, self.now + timedelta(seconds=7200))

    def test_success_does_not_reactivate_disabled_connection(self):
        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save()
        scheduling.register_success(self.connection, self.now)
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.last_success_at)


class ConcurrentClaimTests(TransactionTestCase):
    def test_claim_due_connections_never_double_claims(self):
        now = timezone.now()
        connection = make_connection(next_sync_at=now)
        link(connection, make_venue("Concurrent"))

        def claim(_):
            try:
                return [job.pk for job in scheduling.claim_due_connections(now)]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = sum(pool.map(claim, range(2)), [])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(AccountingSyncJob.objects.count(), 1)
