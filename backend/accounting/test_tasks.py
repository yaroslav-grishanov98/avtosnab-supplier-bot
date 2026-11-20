from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import Retry
from django.conf import settings
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounting import tasks
from accounting.locks import sync_guard
from accounting.models import AccountingSyncJob, SyncStatus, SyncTrigger
from accounting.sync.runner import SyncOutcome
from accounting.test_support.sync import make_connection, make_job


class AccountingTaskTests(TransactionTestCase):
    def setUp(self):
        self.connection = make_connection()
        self.job = make_job(self.connection)

    def test_syncs_and_control_tasks_use_different_queues(self):
        # The cheap scheduler tasks must never wait behind long syncs on the thread workers (starvation).
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[tasks.run_sync_job_task.name]["queue"], settings.ACCOUNTING_QUEUE_SYNC
        )
        for task in (tasks.enqueue_due_syncs, tasks.recover_stalled_jobs):
            self.assertEqual(settings.CELERY_TASK_ROUTES[task.name]["queue"], settings.ACCOUNTING_QUEUE_CONTROL)
        self.assertNotEqual(settings.ACCOUNTING_QUEUE_CONTROL, settings.ACCOUNTING_QUEUE_SYNC)
        declared = {queue.name for queue in settings.CELERY_TASK_QUEUES}
        self.assertIn(settings.ACCOUNTING_QUEUE_CONTROL, declared)

    def test_every_accounting_task_acks_late_and_is_rejected_when_the_worker_is_lost(self):
        for task in (tasks.enqueue_due_syncs, tasks.run_sync_job_task, tasks.recover_stalled_jobs):
            self.assertTrue(task.acks_late)
            self.assertTrue(task.reject_on_worker_lost)

    @patch("accounting.tasks.run_sync_job")
    def test_busy_guard_retries_and_keeps_job_queued(self, run):
        run.return_value = SyncOutcome(self.job, SyncStatus.QUEUED, retry_in=35)
        with patch.object(tasks.run_sync_job_task, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                tasks.run_sync_job_task.run(str(self.job.pk))
        retry.assert_called_once_with(countdown=35, max_retries=20)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, SyncStatus.QUEUED)

    @patch("accounting.tasks.run_sync_job_task.apply_async")
    def test_manual_trigger_uses_manual_queue(self, publish):
        publish.return_value.id = "test-task"
        for trigger in (SyncTrigger.MANUAL, SyncTrigger.CONNECT):
            self.job.trigger = trigger
            tasks.enqueue_job(self.job)
            self.assertEqual(publish.call_args.kwargs["queue"], settings.ACCOUNTING_QUEUE_MANUAL)

    @override_settings(ACCOUNTING_SCHEDULED_SYNC_ENABLED=False)
    @patch("accounting.tasks.scheduling.claim_due_connections")
    def test_scheduler_does_nothing_when_disabled(self, claim):
        self.assertEqual(tasks.enqueue_due_syncs(), 0)
        claim.assert_not_called()

    @override_settings(ACCOUNTING_SCHEDULED_SYNC_ENABLED=False)
    @patch("accounting.tasks.run_sync_job")
    def test_manual_sync_works_when_schedule_disabled(self, run):
        run.return_value = SyncOutcome(self.job, SyncStatus.SUCCESS, ran=True)
        self.assertEqual(tasks.run_sync_job_task.run(str(self.job.pk)), SyncStatus.SUCCESS)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.last_success_at)

    @patch("accounting.tasks.run_sync_job")
    def test_redelivery_and_skipped_do_not_register_result(self, run):
        for status, ran in ((SyncStatus.SUCCESS, False), (SyncStatus.FAILED, False), (SyncStatus.SKIPPED, True)):
            run.return_value = SyncOutcome(self.job, status, ran=ran)
            tasks.run_sync_job_task.run(str(self.job.pk))
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.last_success_at)
        self.assertEqual(self.connection.consecutive_failures, 0)

    @patch("accounting.tasks.run_sync_job")
    def test_failed_without_adapter_error_uses_job_code(self, run):
        self.job.error_code = "warehouse_failed"
        run.return_value = SyncOutcome(self.job, SyncStatus.FAILED, ran=True)
        tasks.run_sync_job_task.run(str(self.job.pk))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.error_code, "warehouse_failed")
        self.assertEqual(self.connection.consecutive_failures, 1)

    def test_recover_stalled_jobs_marks_running_as_failed(self):
        self.job.status = SyncStatus.RUNNING
        self.job.started_at = timezone.now() - timedelta(hours=1)
        self.job.save()
        self.assertEqual(tasks.recover_stalled_jobs(), 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.error_code, "worker_lost")

    def test_recovery_leaves_a_long_job_running_while_its_worker_holds_the_connection_lock(self):
        self.job.status = SyncStatus.RUNNING
        self.job.started_at = timezone.now() - timedelta(hours=1)
        self.job.save()
        with sync_guard(self.connection.id, self.connection.host, settings.ACCOUNTING_HOST_SLOTS) as guard:
            self.assertTrue(guard.acquired)  # the live worker
            self.assertEqual(tasks.recover_stalled_jobs(), 0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, SyncStatus.RUNNING)
        self.assertEqual(self.job.error_code, "")
        # The worker is gone (lock released): now the job is recovered.
        self.assertEqual(tasks.recover_stalled_jobs(), 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, SyncStatus.FAILED)
        self.assertEqual(self.job.error_code, "worker_lost")

    def test_recovery_does_not_overwrite_a_job_that_finished_meanwhile(self):
        self.job.status = SyncStatus.RUNNING
        self.job.started_at = timezone.now() - timedelta(hours=1)
        self.job.save()

        def finish_during_probe(*args, **kwargs):
            AccountingSyncJob.objects.filter(pk=self.job.pk).update(status=SyncStatus.SUCCESS)
            return sync_guard(*args, **kwargs)

        with patch("accounting.tasks.sync_guard", finish_during_probe):
            self.assertEqual(tasks.recover_stalled_jobs(), 0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, SyncStatus.SUCCESS)

    @patch("accounting.tasks.enqueue_job")
    def test_recovery_republishes_queued_job_after_publish_gap(self, publish):
        self.job.queued_at = timezone.now() - timedelta(hours=1)
        self.job.save()
        tasks.recover_stalled_jobs()
        publish.assert_called_once()
