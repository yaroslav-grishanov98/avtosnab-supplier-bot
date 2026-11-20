import uuid

from django.db import models
from django.utils import timezone

from accounting.models.connection import AccountingConnection


class SyncKind(models.TextChoices):
    CATALOG = "catalog", "Catalog"
    STOCK = "stock", "Stock"
    CONSUMPTION = "consumption", "Consumption"


class SyncTrigger(models.TextChoices):
    SCHEDULE = "schedule", "Schedule"
    MANUAL = "manual", "Manual"
    CONNECT = "connect", "Connect"


class SyncStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class AccountingSyncJob(models.Model):
    # Named job_id rather than id: it is the public handle returned by the sync API.
    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="sync_jobs")
    kind = models.CharField(max_length=16, choices=SyncKind.choices)
    trigger = models.CharField(max_length=16, choices=SyncTrigger.choices)
    status = models.CharField(max_length=16, choices=SyncStatus.choices, default=SyncStatus.QUEUED)
    # Django admins are auth.User, not core.User, so it stays empty for them and for the schedule.
    requested_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accounting_sync_jobs",
    )
    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="", help_text="Error code, empty if none.")
    stats = models.JSONField(default=dict, blank=True, help_text="Counters of the run. Never contains secrets.")
    celery_task_id = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "accounting_sync_jobs"
        indexes = [
            models.Index(fields=["connection", "-queued_at"], name="acc_job_conn_queued_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.status} ({self.job_id})"
