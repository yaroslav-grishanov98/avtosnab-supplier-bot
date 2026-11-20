from django.db import models
from django.db.models import Q

from core.models import UUIDTimestampedModel


class ImportFileScope(models.TextChoices):
    SUPPLIER = "supplier", "Поставщик"
    OWNER = "owner", "Владелец справочника"
    VENUE = "venue", "Заведение"


class ImportFile(UUIDTimestampedModel):
    """Исходный файл импорта, сохранённый на срок хранения (30 дней) для повторной обработки воркером и аудита.

    Сам файл лежит в default_storage под неугадываемым ключом и никогда не отдаётся URL-ом. По истечении срока
    задача очистки удаляет файл из хранилища и ставит deleted_at; строка остаётся как след."""

    scope_type = models.CharField(max_length=16, choices=ImportFileScope.choices)
    scope_id = models.UUIDField()
    original_name = models.TextField(blank=True)
    content_type = models.CharField(max_length=128, blank=True)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.TextField()
    expires_at = models.DateTimeField()
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "import_files"
        constraints = [
            models.UniqueConstraint(
                fields=["scope_type", "scope_id", "sha256"],
                condition=Q(deleted_at__isnull=True),
                name="import_files_scope_sha_live_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["expires_at"], condition=Q(deleted_at__isnull=True), name="import_files_expiry_live_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.scope_type}:{self.scope_id}:{self.sha256[:8]}"


class ImportRowReceipt(UUIDTimestampedModel):
    """Compact immutable provenance; not owned by a temporary import session."""

    scope_type = models.CharField(max_length=16, choices=ImportFileScope.choices)
    scope_id = models.UUIDField()
    source_session_id = models.UUIDField()
    source_item_id = models.UUIDField(unique=True)
    target_type = models.CharField(max_length=32)
    target_id = models.UUIDField()
    parser_version = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)

    class Meta:
        db_table = "import_row_receipts"
        indexes = [models.Index(fields=["target_type", "target_id"], name="import_receipt_target_idx")]
