import base64
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounting.adapters import FakeAdapter
from accounting.management.commands.accounting_load_simulation import percentile, run_simulation
from accounting.models import AccountingConnection, AccountingSyncJob, ConnectionStatus, SyncKind, SyncStatus
from accounting.sync.runner import run_sync_job
from accounting.test_support.sync import adapter_factory, link, make_connection, make_job, make_venue, warehouse
from core.models import Venue

SECRET_KEY = base64.b64encode(b"0" * 32).decode("ascii")


class LoadSimulationTests(TransactionTestCase):
    def test_simulation_respects_host_slots_and_never_double_syncs(self):
        result = run_simulation(connections_count=60, workers=8, latency_ms=10, hosts=12)
        self.assertEqual((result.jobs, result.finished, result.successes), (60, 60, 60))
        self.assertLessEqual(result.max_per_host, 1)  # ACCOUNTING_HOST_SLOTS defaults to 1
        self.assertGreaterEqual(result.max_per_host, 1)
        self.assertEqual(result.double_runs, 0)
        self.assertGreaterEqual(result.wait_p95, result.wait_p50)
        # The simulation removes what it created.
        self.assertFalse(AccountingConnection.objects.exists())
        self.assertFalse(AccountingSyncJob.objects.exists())
        self.assertFalse(Venue.objects.filter(display_name__startswith="SIM").exists())

    @override_settings(ACCOUNTING_HOST_SLOTS=2)
    def test_more_host_slots_allow_more_parallel_syncs_but_never_more_than_the_slots(self):
        result = run_simulation(connections_count=24, workers=6, latency_ms=20, hosts=2)
        self.assertLessEqual(result.max_per_host, 2)
        self.assertEqual((result.finished, result.double_runs), (24, 0))

    def test_the_command_asks_for_confirmation_and_creates_nothing_without_it(self):
        with self.assertRaises(CommandError):
            call_command("accounting_load_simulation", connections=3, stdout=StringIO())
        self.assertFalse(AccountingConnection.objects.exists())

    def test_the_command_prints_the_table(self):
        out = StringIO()
        call_command(
            "accounting_load_simulation", connections=6, workers=3, latency_ms=5, hosts=3, confirm_writes=True,
            stdout=out,
        )  # fmt: skip
        text = out.getvalue()
        for label in ("слотов на хост", "макс. одновременных синков на хост", "двойных запусков", "p50 / p95"):
            self.assertIn(label, text)

    def test_percentile(self):
        self.assertEqual(percentile([], 0.5), 0.0)
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 3)
        self.assertEqual(percentile([1, 2, 3, 4], 0.95), 4)


@override_settings(ACCOUNTING_SECRET_KEYS={1: SECRET_KEY}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1)
class SyncReportTests(TestCase):
    def report(self, *args):
        out = StringIO()
        call_command("accounting_sync_report", *args, stdout=out)
        return out.getvalue()

    def line(self, text, connection):
        return next(line for line in text.splitlines() if str(connection.id) in line)

    def test_sync_report_flags_lagging_and_needs_attention_connections(self):
        now = timezone.now()
        healthy = make_connection("https://ok.iiko.example/resto", last_success_at=now - timedelta(minutes=30))
        lagging = make_connection("https://lag.iiko.example/resto", last_success_at=now - timedelta(minutes=150))
        stuck = make_connection(
            "https://stuck.iiko.example/resto", status=ConnectionStatus.NEEDS_ATTENTION, consecutive_failures=3
        )
        never = make_connection("https://never.iiko.example/resto")
        AccountingConnection.objects.filter(pk=never.pk).update(created_at=now - timedelta(days=1))
        fresh = make_connection("https://fresh.iiko.example/resto")  # just created: not "never synced" yet
        off = make_connection("https://off.iiko.example/resto", status=ConnectionStatus.DISABLED)

        text = self.report()
        self.assertNotRegex(self.line(text, healthy), "LAG|NEEDS_ATTENTION|NEVER_SYNCED|DISABLED")
        self.assertIn("LAG", self.line(text, lagging))
        self.assertIn("NEEDS_ATTENTION", self.line(text, stuck))
        self.assertIn("ошибок подряд:   3", self.line(text, stuck))
        self.assertIn("NEVER_SYNCED", self.line(text, never))
        self.assertNotRegex(self.line(text, fresh), "LAG|NEVER_SYNCED")
        self.assertIn("DISABLED", self.line(text, off))
        self.assertIn("отстают: 1", text)
        self.assertIn("требуют внимания: 1", text)

    def test_the_lag_factor_is_configurable(self):
        now = timezone.now()
        connection = make_connection(last_success_at=now - timedelta(minutes=90))  # interval 60 minutes
        self.assertNotIn("LAG", self.line(self.report(), connection))
        self.assertIn("LAG", self.line(self.report("--max-lag-factor", "1"), connection))

    def test_the_interval_of_each_connection_counts(self):
        now = timezone.now()
        slow = make_connection(sync_interval_minutes=720, last_success_at=now - timedelta(hours=10))
        self.assertNotIn("LAG", self.line(self.report(), slow))

    def test_sync_report_prints_no_secrets(self):
        connection = make_connection("https://secret.iiko.example/resto/path-in-url?key=token-in-url")
        connection.set_credentials("report-login-XYZ", "report-password-XYZ")
        connection.save()
        link(connection, make_venue("Название заведения"))
        text = self.report()
        for secret in (
            "report-login-XYZ", "report-password-XYZ", connection.credentials().pass_hash, "path-in-url",
            "token-in-url", "Название заведения", "ciphertext",
        ):  # fmt: skip
            self.assertNotIn(secret, text)
        self.assertIn("secret.iiko.example", text)  # the host is shown, nothing else of the URL

    def test_an_empty_database_still_reports(self):
        self.assertIn("Всего подключений: 0", self.report())


class StructuredLogTests(TestCase):
    def test_the_runner_logs_ids_and_numbers_only(self):
        venue = make_venue("Секретное название заведения")
        connection = make_connection(capabilities={"omits_zero_balances": True})
        link(connection, venue)
        from accounting.models import AccountingWarehouse

        AccountingWarehouse.objects.create(connection=connection, venue=venue, external_id="wh-1", name="Кухня")
        job = make_job(connection, kind=SyncKind.STOCK)
        adapter = FakeAdapter(warehouses=[warehouse("wh-1", None)], stock={"wh-1": []}, products=[], departments=[])
        with (
            mock.patch("accounting.sync.runner.get_adapter", adapter_factory(adapter)),
            self.assertLogs("accounting.sync.runner", level="INFO") as logs,
        ):
            outcome = run_sync_job(job.job_id)
        self.assertEqual(outcome.status, SyncStatus.SUCCESS)
        (record,) = [r for r in logs.records if "завершена" in r.getMessage()]
        self.assertEqual(
            (record.connection_id, record.job_id, record.kind, record.status),
            (str(connection.id), str(job.job_id), "stock", "success"),
        )
        self.assertIsInstance(record.duration_ms, int)
        self.assertGreaterEqual(record.duration_ms, 0)
        text = record.getMessage() + repr(record.__dict__)
        self.assertNotIn("Секретное название заведения", text)
        self.assertNotIn(connection.base_url, text)
