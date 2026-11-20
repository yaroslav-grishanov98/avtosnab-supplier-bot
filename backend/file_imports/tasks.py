from celery import shared_task

from file_imports.services import purge_expired_import_files


@shared_task(name="file_imports.purge_expired_import_files")
def purge_expired_import_files_task():
    return {"purged": purge_expired_import_files()}


@shared_task(name="file_imports.recover_stalled_imports")
def recover_stalled_imports_task():
    """Recover only stale, unlocked jobs with stored sources, at most twice."""
    import logging
    from datetime import timedelta

    from django.conf import settings
    from django.db import transaction
    from django.utils import timezone

    from api.v1.catalog.tasks import process_master_product_import_upload_task, process_venue_import_session_task
    from api.v1.suppliers.tasks import extract_supplier_product_import_session_task
    from catalog.models import CatalogImportSession, MasterProductImportUpload
    from file_imports.task_locks import import_task_lock
    from suppliers.models import SupplierProductImportSession

    if not getattr(settings, "IMPORT_RECOVERY_ENABLED", False):
        return {"requeued": 0, "failed": 0}
    logger = logging.getLogger("autosnab.file_imports")
    result = {"requeued": 0, "failed": 0}
    jobs = []
    for model, kind, task, limit in (
        (
            SupplierProductImportSession,
            "supplier",
            extract_supplier_product_import_session_task,
            settings.SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT,
        ),
        (CatalogImportSession, "venue", process_venue_import_session_task, settings.VENUE_IMPORT_TASK_TIME_LIMIT),
        (
            MasterProductImportUpload,
            "master",
            process_master_product_import_upload_task,
            settings.MASTER_PRODUCT_IMPORT_TASK_TIME_LIMIT,
        ),
    ):
        status_field = "status" if kind == "master" else "processing_status"
        cutoff = timezone.now() - timedelta(seconds=limit + 300)
        candidates = model.objects.filter(
            **{f"{status_field}__in": ["pending", "processing"], "updated_at__lt": cutoff}
        )
        if kind == "venue":
            candidates = candidates.filter(domain="venue", status="open")
        elif kind == "supplier":
            candidates = candidates.filter(status="open")
        for identifier in list(candidates.values_list("pk", flat=True)[:100]):
            with import_task_lock(kind, identifier) as acquired:
                if not acquired:
                    continue
                with transaction.atomic():
                    record = candidates.select_for_update().filter(pk=identifier).first()
                    if record is None:
                        continue
                    if record.import_file_id is None or record.recovery_attempts >= 2:
                        setattr(record, status_field, "failed")
                        record.error_message = "Не удалось восстановить обработку. Загрузите файл повторно."
                        record.save(update_fields=[status_field, "error_message", "updated_at"])
                        result["failed"] += 1
                        logger.error(
                            "import.failed kind=%s id=%s stage=recovery reason=retry_limit_or_missing_source",
                            kind,
                            identifier,
                        )
                        continue
                    record.recovery_attempts += 1
                    record.save(update_fields=["recovery_attempts", "updated_at"])
                jobs.append(
                    (kind, task, identifier, record.owner_id if kind == "master" else None, record.recovery_attempts)
                )
    # Dispatch only after releasing the claim, so an immediately started worker can acquire it.
    for kind, task, identifier, owner_id, attempt in jobs:
        try:
            if kind == "master":
                task.delay(upload_id=str(identifier), owner_id=str(owner_id), rows=None, parsed_meta={})
            else:
                task.delay(session_id=str(identifier))
            result["requeued"] += 1
            logger.warning("import.recovered kind=%s id=%s attempt=%s", kind, identifier, attempt)
        except Exception:
            logger.exception("import.recovery_queue_failed kind=%s id=%s", kind, identifier)

    # Фоновое применение (commit) поставщика: зависшее pending/running не перезапускаем само -- commit меняет
    # каталог, пользователь решает повторять ли; переводим в failed, чтобы UI не ждал вечно (повтор commit разрешён).
    commit_cutoff = timezone.now() - timedelta(seconds=settings.SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT + 300)
    stalled_commits = SupplierProductImportSession.objects.filter(
        commit_status__in=["pending", "running"], updated_at__lt=commit_cutoff
    )
    for identifier in list(stalled_commits.values_list("pk", flat=True)[:100]):
        with import_task_lock("supplier_commit", identifier) as acquired:
            if not acquired:
                continue
            if stalled_commits.filter(pk=identifier).update(
                commit_status="failed",
                commit_error="Применение не завершилось. Повторите попытку.",
                updated_at=timezone.now(),
            ):
                result["failed"] += 1
                logger.error("import.failed kind=supplier_commit id=%s stage=recovery reason=stalled", identifier)
    return result
