import json
import logging

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import JSONRenderer

from api.v1.catalog.import_sessions import (
    build_venue_preview_rows,
    create_catalog_import_items,
    persist_catalog_import_session,
)
from api.v1.catalog.importers import parse_catalog_file
from api.v1.catalog.selectors import resolve_master_product_import_rows, resolve_venue_import_rows
from api.v1.catalog.serializers import VenueImportFilePreviewRowSerializer
from catalog.models import CatalogImportDomain
from file_imports.services import ImportFileUnavailable, import_file_as_upload
from file_imports.task_locks import exclusive_import_task
from file_imports.versions import is_canonical

logger = logging.getLogger("autosnab.catalog.master_product_import")


def _import_batch_size():
    try:
        value = int(getattr(settings, "MASTER_PRODUCT_IMPORT_BATCH_SIZE", 500) or 500)
    except (TypeError, ValueError):
        return 500
    return value if value > 0 else 500


def _readable_error_message(error):
    from rest_framework.exceptions import ValidationError as DRFValidationError

    if isinstance(error, ImportFileUnavailable):
        return "Исходный файл недоступен: срок хранения истёк. Загрузите файл ещё раз."

    if isinstance(error, DRFValidationError):
        detail = error.detail
        first = next(iter(detail.values())) if isinstance(detail, dict) else detail
        if isinstance(first, list):
            first = first[0]
        return str(first)
    return str(error)


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    name="catalog.process_master_product_import_upload",
    time_limit=getattr(settings, "MASTER_PRODUCT_IMPORT_TASK_TIME_LIMIT", 3600),
    soft_time_limit=getattr(settings, "MASTER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT", 3300),
)
@exclusive_import_task("master", "upload_id")
def process_master_product_import_upload_task(self, *, upload_id, owner_id, rows, parsed_meta):
    """rows: list[dict] — parsed file rows (parse_catalog_file(...).rows), passed directly as a
    task argument (plain JSON-serializable list, no intermediate DB storage of raw rows).
    rows=None: файл сохранён (file_imports) -- воркер сам читает его из хранилища.
    parsed_meta: {"sheet_name", "header_row_number", "detected_columns", "rows_without_name"} —
    computed once at parse time, unchanged across batches.

    resolve_master_product_import_rows is imported at module level (unlike the three lazy
    imports below) specifically so tests can patch it at
    api.v1.catalog.tasks.resolve_master_product_import_rows — the other three stay lazy,
    matching this project's existing pattern of deferred model/serializer imports inside
    @shared_task functions (e.g. api/v1/suppliers/tasks.py), to avoid Django app-loading-order
    issues at module import time."""
    from api.v1.catalog.serializers import MasterProductImportPreviewResponseSerializer
    from catalog.models import MasterProductImportUpload, MasterProductImportUploadStatus
    from core.models import User

    close_old_connections()
    try:
        upload = MasterProductImportUpload.objects.get(pk=upload_id)
        if upload.status != MasterProductImportUploadStatus.PROCESSING:
            logger.warning("master_product_import.process.skip upload_id=%s status=%s", upload_id, upload.status)
            return {"upload_id": str(upload_id), "skipped": True, "status": upload.status}

        owner = User.objects.get(pk=owner_id)
        if upload.parsed_rows:
            rows = upload.parsed_rows
        if rows is None:
            if upload.import_file is None:
                raise ValueError("Нет ни строк, ни сохранённого файла.")
            parsed = parse_catalog_file(
                import_file_as_upload(upload.import_file),
                max_rows=int(getattr(settings, "MASTER_PRODUCT_IMPORT_MAX_ROWS", 12000)),
                canonical_enabled=is_canonical(upload.parser_version),
            )
            rows = parsed.rows
            parsed_meta = {
                "sheet_name": parsed.sheet_name,
                "header_row_number": parsed.header_row_number,
                "detected_columns": parsed.detected_columns,
                "rows_without_name": parsed.rows_without_name,
            }
            upload.rows_total = len(rows) + parsed.rows_without_name
            upload.save(update_fields=["rows_total", "updated_at"])
        batch_size = _import_batch_size()
        all_rows = []
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            all_rows.extend(resolve_master_product_import_rows(owner=owner, rows=batch))
            upload.rows_done = len(all_rows)
            upload.save(update_fields=["rows_done", "updated_at"])

        needs_attention_count = sum(1 for row in all_rows if row.needs_attention)
        payload = {
            **parsed_meta,
            "total_rows": len(all_rows) + parsed_meta["rows_without_name"],
            "auto_resolved_count": len(all_rows) - needs_attention_count,
            "needs_attention_count": needs_attention_count,
            "rows": all_rows,
        }
        serialized = MasterProductImportPreviewResponseSerializer(payload).data
        json_safe_result = json.loads(JSONRenderer().render(serialized))

        with transaction.atomic():
            current_upload = MasterProductImportUpload.objects.select_for_update().get(pk=upload_id)
            if current_upload.status != MasterProductImportUploadStatus.PROCESSING:
                return {"upload_id": str(upload_id), "skipped": True, "status": current_upload.status}
            session = persist_catalog_import_session(
                owner=owner,
                domain=CatalogImportDomain.MASTER,
                source_file_name=upload.source_file_name or "",
                rows=json_safe_result["rows"],
                parsed_rows=rows,
                import_file=upload.import_file,
                meta={
                    key: parsed_meta[key]
                    for key in ("sheet_name", "header_row_number", "detected_columns", "rows_without_name")
                },
            )
            json_safe_result["session_id"] = str(session.id)

            finished = MasterProductImportUpload.objects.filter(
                pk=upload_id, status=MasterProductImportUploadStatus.PROCESSING
            ).update(
                status=MasterProductImportUploadStatus.READY,
                result=json_safe_result,
                processed_at=timezone.now(),
                rows_done=upload.rows_total,
                updated_at=timezone.now(),
            )
        if not finished:
            logger.warning(
                "master_product_import.process.finish_skipped upload_id=%s reason=no_longer_processing", upload_id
            )
        return {"upload_id": str(upload_id), "status": MasterProductImportUploadStatus.READY}
    except Exception as error:
        logger.exception("master_product_import.process.failed upload_id=%s error=%s", upload_id, error)
        MasterProductImportUpload.objects.filter(
            pk=upload_id, status=MasterProductImportUploadStatus.PROCESSING
        ).update(
            status=MasterProductImportUploadStatus.FAILED,
            error_message=_readable_error_message(error),
            processed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        raise
    finally:
        close_old_connections()


def _venue_batch_size():
    try:
        value = int(getattr(settings, "VENUE_IMPORT_BATCH_SIZE", 500) or 500)
    except (TypeError, ValueError):
        return 500
    return value if value > 0 else 500


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    name="catalog.process_venue_import_session",
    time_limit=getattr(settings, "VENUE_IMPORT_TASK_TIME_LIMIT", 3600),
    soft_time_limit=getattr(settings, "VENUE_IMPORT_TASK_SOFT_TIME_LIMIT", 3300),
)
@exclusive_import_task("venue", "session_id")
def process_venue_import_session_task(self, *, session_id):
    """Файловый preview Справочника заведения в фоне: файл читается из хранилища, строки создаются пачками
    (по VENUE_IMPORT_BATCH_SIZE), между пачками обновляется rows_done. Повтор безопасен: уже созданные строки
    пропускаются по row_number."""
    from catalog.models import CatalogImportProcessingStatus, CatalogImportSession

    close_old_connections()
    try:
        session = CatalogImportSession.objects.select_related("venue", "import_file").get(pk=session_id)
        if session.processing_status not in (
            CatalogImportProcessingStatus.PENDING,
            CatalogImportProcessingStatus.PROCESSING,
        ):
            return {"session_id": str(session_id), "skipped": True, "status": session.processing_status}
        CatalogImportSession.objects.filter(pk=session.pk).update(
            processing_status=CatalogImportProcessingStatus.PROCESSING, updated_at=timezone.now()
        )
        try:
            if session.import_file is None:
                raise ImportFileUnavailable("Исходный файл не сохранён.")
            parsed = parse_catalog_file(
                import_file_as_upload(session.import_file),
                max_rows=int(getattr(settings, "VENUE_IMPORT_MAX_ROWS", 12000)),
                canonical_enabled=is_canonical(session.parser_version),
            )
        except ImportFileUnavailable:
            CatalogImportSession.objects.filter(pk=session.pk).update(
                processing_status=CatalogImportProcessingStatus.FAILED,
                error_message="Исходный файл недоступен: срок хранения истёк. Загрузите файл ещё раз.",
                updated_at=timezone.now(),
            )
            return {"session_id": str(session_id), "status": "failed"}
        except ValidationError as error:
            CatalogImportSession.objects.filter(pk=session.pk).update(
                processing_status=CatalogImportProcessingStatus.FAILED,
                error_message=_readable_error_message(error),
                updated_at=timezone.now(),
            )
            return {"session_id": str(session_id), "status": "failed"}

        session.rows_total = len(parsed.rows) + parsed.rows_without_name
        session.meta = {
            "sheet_name": parsed.sheet_name,
            "header_row_number": parsed.header_row_number,
            "detected_columns": parsed.detected_columns,
            "rows_without_name": parsed.rows_without_name,
        }
        session.save(update_fields=["rows_total", "meta", "updated_at"])
        done = set(session.items.values_list("row_number", flat=True))
        pending_rows = [row for row in parsed.rows if row["row_number"] not in done]
        batch_size = _venue_batch_size()
        for start in range(0, len(pending_rows), batch_size):
            batch = pending_rows[start : start + batch_size]
            matches = resolve_venue_import_rows(
                venue=session.venue,
                raw_names=[row["name"] for row in batch],
                unit_labels=[row.get("unit") for row in batch],
                **({"allow_unit_llm": False} if any(row.get("_parsed") for row in batch) else {}),
            )
            rows = VenueImportFilePreviewRowSerializer(build_venue_preview_rows(batch, matches), many=True).data
            with transaction.atomic():
                create_catalog_import_items(session, rows, start_index=len(done) + start + 1, parsed_rows=batch)
                CatalogImportSession.objects.filter(pk=session.pk).update(
                    rows_done=session.items.count(), updated_at=timezone.now()
                )
        CatalogImportSession.objects.filter(
            pk=session.pk, processing_status=CatalogImportProcessingStatus.PROCESSING
        ).update(
            processing_status=CatalogImportProcessingStatus.READY,
            rows_done=session.rows_total,
            updated_at=timezone.now(),
        )
        return {"session_id": str(session_id), "status": "ready"}
    except Exception as error:
        logger.exception("venue_import.process.failed session_id=%s error=%s", session_id, error)
        CatalogImportSession.objects.filter(
            pk=session_id,
            processing_status__in=(CatalogImportProcessingStatus.PENDING, CatalogImportProcessingStatus.PROCESSING),
        ).update(processing_status=CatalogImportProcessingStatus.FAILED, error_message="Не удалось обработать файл.")
        raise
    finally:
        close_old_connections()
