import logging

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections

from core.models import EntityStatus
from file_imports.task_locks import exclusive_import_task
from suppliers.models import (
    SupplierPriceUpload,
    SupplierProduct,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSession,
    UploadStatus,
)

logger = logging.getLogger("autosnab.suppliers.tasks")


@shared_task(
    bind=True,
    name="suppliers.sync_supplier_catalog_to_qdrant",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def sync_supplier_catalog_to_qdrant_task(self, *, supplier_id, upload_id=None):
    from api.v1.catalog.embeddings import ensure_product_for_supplier_product, sync_supplier_products_to_qdrant

    close_old_connections()
    try:
        items = list(
            SupplierProduct.objects.select_related(
                "supplier", "product", "unit", "brand", "category", "category__parent", "category__parent__parent"
            )
            .filter(supplier_id=supplier_id, status=EntityStatus.ACTIVE)
            .order_by("normalized_name", "id")
        )
        for item in items:
            ensure_product_for_supplier_product(item)
        processed, skipped = sync_supplier_products_to_qdrant(
            items,
            batch_size=max(1, settings.CATALOG_QDRANT_SYNC_BATCH_SIZE),
        )
        logger.info(
            "supplier_catalog.qdrant_sync.finish supplier_id=%s upload_id=%s task_id=%s processed=%s skipped=%s",
            supplier_id,
            upload_id,
            self.request.id,
            processed,
            skipped,
        )
        return {"supplier_id": supplier_id, "upload_id": upload_id, "processed": processed, "skipped": skipped}
    finally:
        close_old_connections()


@shared_task(bind=True, name="suppliers.normalize_supplier_price_upload")
def normalize_supplier_price_upload_task(
    self,
    *,
    upload_id,
    use_ai=True,
    ai_mode=None,
    confirm_full=False,
    run_ai_cleanup=False,
    ai_cleanup_scope="all",
):
    from api.v1.suppliers.services import _json_safe, _run_supplier_price_upload_normalization

    close_old_connections()
    try:
        upload = SupplierPriceUpload.objects.select_related("supplier").get(pk=upload_id)
        if upload.status != UploadStatus.PROCESSING:
            logger.warning(
                "supplier_price_upload.normalize.celery_skip upload_id=%s status=%s task_id=%s",
                upload_id,
                upload.status,
                self.request.id,
            )
            return {"upload_id": upload_id, "skipped": True, "status": upload.status}

        result = _run_supplier_price_upload_normalization(
            upload=upload,
            use_ai=use_ai,
            ai_mode=ai_mode,
            confirm_full=confirm_full,
        )
        if run_ai_cleanup and result.get("status") in {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW}:
            from api.v1.suppliers.services import start_supplier_price_upload_ai_cleanup

            try:
                cleanup = start_supplier_price_upload_ai_cleanup(
                    upload_id=upload_id,
                    scope=ai_cleanup_scope,
                )
            except Exception as error:
                logger.exception(
                    "supplier_price_upload.normalize.ai_cleanup_enqueue_failed upload_id=%s task_id=%s error=%s",
                    upload_id,
                    self.request.id,
                    error,
                )
                result = {**result, "ai_cleanup_accepted": False, "ai_cleanup_enqueue_error": str(error)}
            else:
                result = {
                    **result,
                    "ai_cleanup": cleanup.get("ai_cleanup"),
                    "ai_cleanup_accepted": cleanup.get("accepted"),
                }
        return _json_safe(result)
    except SupplierPriceUpload.DoesNotExist:
        logger.warning(
            "supplier_price_upload.normalize.celery_missing upload_id=%s task_id=%s",
            upload_id,
            self.request.id,
        )
        return {"upload_id": upload_id, "missing": True}
    except Exception as error:
        logger.exception(
            "supplier_price_upload.normalize.celery_failed upload_id=%s task_id=%s error=%s",
            upload_id,
            self.request.id,
            error,
        )
        raise
    finally:
        close_old_connections()


@shared_task(bind=True, name="suppliers.ai_cleanup_supplier_price_upload")
def ai_cleanup_supplier_price_upload_task(self, *, upload_id, scope="all", force=False):
    from api.v1.suppliers.services import _json_safe, _run_supplier_price_upload_ai_cleanup

    close_old_connections()
    try:
        upload = SupplierPriceUpload.objects.select_related("supplier").get(pk=upload_id)
        result = _run_supplier_price_upload_ai_cleanup(
            upload=upload,
            scope=scope,
            force=force,
        )
        return _json_safe(result)
    except SupplierPriceUpload.DoesNotExist:
        logger.warning(
            "supplier_price_upload.ai_cleanup.celery_missing upload_id=%s task_id=%s",
            upload_id,
            self.request.id,
        )
        return {"upload_id": upload_id, "missing": True}
    except Exception as error:
        logger.exception(
            "supplier_price_upload.ai_cleanup.celery_failed upload_id=%s task_id=%s error=%s",
            upload_id,
            self.request.id,
            error,
        )
        raise
    finally:
        close_old_connections()


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    name="suppliers.process_supplier_product_import_session",
    time_limit=settings.SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT,
    soft_time_limit=settings.SUPPLIER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT,
)
@exclusive_import_task("supplier", "session_id")
def process_supplier_product_import_session_task(self, *, session_id):
    """TASK-132: медленная часть импорта товаров поставщика (ENRICH через LLM + MATCH) для файла любого
    размера. Разбор файла и строки сессии уже созданы во view; здесь только дозаполнение/сопоставление
    пачками и перевод сессии pending -> processing -> ready/failed (тот же паттерн, что
    catalog.process_master_product_import_upload)."""
    from api.v1.suppliers.product_import_selectors import process_supplier_product_import_session

    close_old_connections()
    try:
        session = process_supplier_product_import_session(session_id=session_id)
        return {"session_id": str(session_id), "status": session.processing_status}
    except Exception as error:
        logger.exception("supplier_product_import.process.failed session_id=%s error=%s", session_id, error)
        raise
    finally:
        close_old_connections()


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    name="suppliers.extract_supplier_product_import_session",
    time_limit=settings.SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT,
    soft_time_limit=settings.SUPPLIER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT,
)
@exclusive_import_task("supplier", "session_id")
def extract_supplier_product_import_session_task(self, *, session_id):
    """Извлечение PDF/DOCX/DOC/TXT/image из сохранённого файла + дозаполнение/сопоставление (см.
    extract_supplier_product_import_session). Нештатный сбой извлекателя -> сессия failed, а не вечный processing."""
    from api.v1.suppliers.product_import_selectors import extract_supplier_product_import_session

    close_old_connections()
    try:
        session = extract_supplier_product_import_session(session_id=session_id)
        return {"session_id": str(session_id), "status": session.processing_status}
    except Exception as error:
        logger.exception("supplier_product_import.extract.failed session_id=%s error=%s", session_id, error)
        SupplierProductImportSession.objects.filter(
            pk=session_id,
            processing_status__in=(
                SupplierProductImportProcessingStatus.PENDING,
                SupplierProductImportProcessingStatus.PROCESSING,
            ),
        ).update(
            processing_status=SupplierProductImportProcessingStatus.FAILED,
            error_message="Не удалось прочитать файл.",
        )
        raise
    finally:
        close_old_connections()


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    name="suppliers.commit_supplier_product_import_session",
    time_limit=settings.SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT,
    soft_time_limit=settings.SUPPLIER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT,
)
@exclusive_import_task("supplier_commit", "session_id")
def commit_supplier_product_import_session_task(self, *, session_id, actor=None):
    """Применение импорта поставщика (commit) вне HTTP-запроса, чтобы большой прайс не упирался в таймаут gunicorn.
    Статусы и итог -- в SupplierProductImportSession.commit_* (см. run_supplier_product_import_commit). actor --
    сериализуемая ссылка на того, кто нажал «Применить» (commit_actor_ref)."""
    from api.v1.suppliers.product_import_services import resolve_commit_actor, run_supplier_product_import_commit

    close_old_connections()
    try:
        result = run_supplier_product_import_commit(session_id=session_id, actor=resolve_commit_actor(actor))
        status = (
            SupplierProductImportSession.objects.filter(pk=session_id).values_list("commit_status", flat=True).first()
        )
        return {"session_id": str(session_id), "status": status, "skipped": result is None}
    finally:
        close_old_connections()


@shared_task(name="suppliers.purge_committed_product_import_sessions")
def purge_committed_product_import_sessions_task():
    """Удаляет завершённые фоновым commit сессии импорта (status=committed, commit_result заполнен), которым больше
    SUPPLIER_PRODUCT_IMPORT_COMMITTED_TTL_HOURS: клиент уже прочитал итог. Старые committed-записи без commit_result
    (синхронный режим) не трогаем."""
    from datetime import timedelta

    from django.utils import timezone

    from suppliers.models import SupplierProductImportProcessingStatus, SupplierProductImportSessionStatus

    cutoff = timezone.now() - timedelta(hours=settings.SUPPLIER_PRODUCT_IMPORT_COMMITTED_TTL_HOURS)
    _, per_model = SupplierProductImportSession.objects.filter(
        status=SupplierProductImportSessionStatus.COMMITTED, commit_result__isnull=False, updated_at__lt=cutoff
    ).delete()
    # Упавшая обработка: пользователю там нечего разбирать, а список «Незавершённые импорты» засоряется.
    # То же с сессией, которая застряла в pending/processing (воркер упал, задача потерялась): руками её не удалить.
    from django.db.models import Q

    failed_cutoff = timezone.now() - timedelta(hours=settings.SUPPLIER_PRODUCT_IMPORT_FAILED_TTL_HOURS)
    stuck_cutoff = timezone.now() - timedelta(hours=settings.SUPPLIER_PRODUCT_IMPORT_STUCK_TTL_HOURS)
    _, failed_per_model = SupplierProductImportSession.objects.filter(
        Q(processing_status=SupplierProductImportProcessingStatus.FAILED, updated_at__lt=failed_cutoff)
        | Q(
            processing_status__in=[
                SupplierProductImportProcessingStatus.PENDING,
                SupplierProductImportProcessingStatus.PROCESSING,
            ],
            updated_at__lt=stuck_cutoff,
        )
    ).delete()
    return {
        "purged": per_model.get(SupplierProductImportSession._meta.label, 0),
        "purged_failed": failed_per_model.get(SupplierProductImportSession._meta.label, 0),
    }
