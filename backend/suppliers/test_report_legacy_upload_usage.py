from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from suppliers.models import SourceChannel, Supplier, SupplierPriceUpload, UploadStatus


def _supplier(name):
    return Supplier.objects.create(legal_name=name, display_name=name)


def _upload(supplier, channel, days_ago):
    upload = SupplierPriceUpload.objects.create(
        supplier=supplier, source_channel=channel, status=UploadStatus.APPLIED, raw_payload={"kind": "file"}
    )
    SupplierPriceUpload.objects.filter(pk=upload.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    return upload


class ReportLegacyUploadUsageTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("report_legacy_upload_usage", *args, stdout=out)
        return out.getvalue()

    def test_counts_uploads_and_distinct_suppliers_per_channel(self):
        a, b = _supplier("A"), _supplier("B")
        _upload(a, SourceChannel.TELEGRAM, 1)
        _upload(a, SourceChannel.TELEGRAM, 2)
        _upload(b, SourceChannel.TELEGRAM, 3)
        _upload(b, SourceChannel.WEB, 4)

        output = self._run("--days", "30")

        self.assertIn("Всего загрузок за 30 дн.: 4", output)
        self.assertRegex(output, r"telegram\s+3\s+2\s+\d{4}-\d{2}-\d{2}")
        self.assertRegex(output, r"web\s+1\s+1\s+\d{4}-\d{2}-\d{2}")

    def test_uploads_older_than_the_period_are_not_counted(self):
        a = _supplier("A")
        _upload(a, SourceChannel.TELEGRAM, 5)
        _upload(a, SourceChannel.TELEGRAM, 200)

        output = self._run("--days", "30")

        self.assertIn("Всего загрузок за 30 дн.: 1", output)

    def test_no_uploads_says_so(self):
        output = self._run("--days", "30")
        self.assertIn("Всего загрузок за 30 дн.: 0", output)
        self.assertIn("Legacy-загрузок за период не было.", output)

    def test_weekly_trend_is_listed(self):
        a = _supplier("A")
        _upload(a, SourceChannel.API, 1)
        output = self._run("--days", "30")
        self.assertIn("По неделям", output)

    def test_command_does_not_write_anything(self):
        a = _supplier("A")
        _upload(a, SourceChannel.TELEGRAM, 1)
        before = list(SupplierPriceUpload.objects.values_list("pk", "status", "updated_at"))
        self._run()
        self.assertEqual(before, list(SupplierPriceUpload.objects.values_list("pk", "status", "updated_at")))
