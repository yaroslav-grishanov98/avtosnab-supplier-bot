import hashlib
import logging
import re
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from file_imports.models import ImportFile

logger = logging.getLogger("autosnab.file_imports")


class ImportFileUnavailable(Exception):
    """Файл не удалось сохранить или прочитать (сбой хранилища, истёк срок, удалён)."""


def import_file_storage_enabled():
    return bool(getattr(settings, "IMPORT_FILE_STORAGE_ENABLED", False))


def _retention():
    return timedelta(days=int(getattr(settings, "IMPORT_FILE_RETENTION_DAYS", 30) or 30))


def _safe_extension(name):
    extension = Path(name or "").suffix.lower()
    return extension if re.fullmatch(r"\.[a-z0-9]{1,8}", extension) else ""


def _live_file(scope_type, scope_id, sha256):
    return ImportFile.objects.filter(
        scope_type=scope_type, scope_id=scope_id, sha256=sha256, deleted_at__isnull=True
    ).first()


def store_import_file(*, uploaded_file, scope_type, scope_id, content_sha256=None):
    """Сохраняет исходный файл импорта и возвращает ImportFile. Тот же файл (по sha256) в том же scope
    переиспользуется, срок хранения продлевается."""
    if getattr(uploaded_file, "size", 0) > 10 * 1024 * 1024:
        raise ImportFileUnavailable("Файл слишком большой. Максимум — 10 МБ.")
    uploaded_file.seek(0)
    content = uploaded_file.read()
    uploaded_file.seek(0)
    sha256 = content_sha256 or hashlib.sha256(content).hexdigest()
    now = timezone.now()
    expires_at = now + _retention()

    existing = _live_file(scope_type, scope_id, sha256)
    if existing is not None:
        if default_storage.exists(existing.storage_key):
            existing.expires_at = expires_at
            existing.save(update_fields=["expires_at", "updated_at"])
            logger.info(
                "import_file.reused id=%s scope=%s:%s key=%s size=%s",
                existing.pk,
                scope_type,
                scope_id,
                existing.storage_key,
                existing.size,
            )
            return existing
        # Запись есть, файла в хранилище нет (удалён руками) -- старая запись больше не годится.
        existing.deleted_at = now
        existing.save(update_fields=["deleted_at", "updated_at"])

    original_name = getattr(uploaded_file, "name", "") or ""
    key = f"import_files/{scope_type}/{scope_id}/{uuid.uuid4().hex}{_safe_extension(original_name)}"
    try:
        saved_key = default_storage.save(key, ContentFile(content))
    except Exception as error:  # noqa: BLE001 -- любой сбой хранилища одинаково означает «файл недоступен»
        logger.exception("import_file.store_failed scope=%s:%s error=%s", scope_type, scope_id, error)
        raise ImportFileUnavailable("Не удалось сохранить исходный файл.") from error

    try:
        with transaction.atomic():
            import_file = ImportFile.objects.create(
                scope_type=scope_type,
                scope_id=scope_id,
                original_name=original_name,
                content_type=getattr(uploaded_file, "content_type", "") or "",
                size=len(content),
                sha256=sha256,
                storage_key=saved_key,
                expires_at=expires_at,
            )
        logger.info(
            "import_file.stored id=%s scope=%s:%s key=%s size=%s",
            import_file.pk,
            scope_type,
            scope_id,
            saved_key,
            import_file.size,
        )
        return import_file
    except IntegrityError:
        # Параллельная загрузка того же файла успела раньше -- берём её запись, свою копию удаляем.
        default_storage.delete(saved_key)
        winner = _live_file(scope_type, scope_id, sha256)
        if winner is None:
            raise
        return winner


def import_file_as_upload(import_file):
    """Файл из хранилища в виде загруженного файла (с .name) -- для парсеров, которым нужен .seek/.read/.name."""
    if import_file.deleted_at is not None or import_file.expires_at <= timezone.now():
        raise ImportFileUnavailable("Срок хранения исходного файла истёк.")
    try:
        with default_storage.open(import_file.storage_key, "rb") as stored:
            content = stored.read()
    except Exception as error:  # noqa: BLE001
        logger.exception("import_file.read_failed id=%s error=%s", import_file.pk, error)
        raise ImportFileUnavailable("Исходный файл недоступен.") from error
    logger.info("import_file.read id=%s key=%s size=%s", import_file.pk, import_file.storage_key, len(content))
    return SimpleUploadedFile(
        import_file.original_name or "import", content, content_type=import_file.content_type or None
    )


def purge_expired_import_files(*, now=None, limit=500):
    """Удаляет из хранилища файлы с истёкшим сроком и ставит deleted_at. Ошибка на одном файле не останавливает
    остальные -- он попадёт в следующий запуск."""
    now = now or timezone.now()
    purged = 0
    expired = ImportFile.objects.filter(deleted_at__isnull=True, expires_at__lte=now).order_by("expires_at")[:limit]
    for import_file in expired:
        try:
            default_storage.delete(import_file.storage_key)
        except Exception as error:  # noqa: BLE001
            logger.exception("import_file.purge_failed id=%s error=%s", import_file.pk, error)
            continue
        import_file.deleted_at = now
        import_file.save(update_fields=["deleted_at", "updated_at"])
        purged += 1
    logger.info("import_file.purge purged=%s", purged)
    return purged
