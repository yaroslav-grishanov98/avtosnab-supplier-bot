"""Legacy price upload normalization and AI cleanup: start, run, progress, queue limits."""

from contextlib import contextmanager

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.v1.suppliers.normalization import (
    NORMALIZATION_PROCESSING_VERSION,
    NORMALIZATION_SCHEMA_VERSION,
    _needs_ai_fallback,
    active_unit_codes_and_names,
    build_normalized_payload,
    normalize_products_with_ai,
    parse_supplier_price_row,
    unit_code_aliases_from_units,
)
from suppliers.models import (
    SourceChannel,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    UploadItemStatus,
    UploadStatus,
)

from ._shared import _json_safe, _rows_for_upload, logger
from .price_apply import apply_supplier_price_ai_result
from .price_uploads import SupplierPriceUploadConflict, SupplierPriceUploadEnqueueError, SupplierPriceUploadLoadShedding

AI_CLEANUP_SCOPE_ALL = "all"


AI_CLEANUP_SCOPE_AMBIGUOUS = "ambiguous"


AI_CLEANUP_SCOPE_NEEDS_REVIEW = "needs_review"


AI_CLEANUP_SCOPES = {AI_CLEANUP_SCOPE_ALL, AI_CLEANUP_SCOPE_AMBIGUOUS, AI_CLEANUP_SCOPE_NEEDS_REVIEW}


AI_CLEANUP_STATUS_QUEUED = "queued"


AI_CLEANUP_STATUS_PROCESSING = "processing"


AI_CLEANUP_STATUS_APPLIED = "applied"


AI_CLEANUP_STATUS_NEEDS_REVIEW = "needs_review"


AI_CLEANUP_STATUS_FAILED = "failed"


AI_CLEANUP_ACTIVE_STATUSES = {AI_CLEANUP_STATUS_QUEUED, AI_CLEANUP_STATUS_PROCESSING}


def _update_upload_normalization_payload(upload, payload):
    raw_payload = upload.raw_payload or {}
    raw_payload["normalization"] = {
        **raw_payload.get("normalization", {}),
        **_json_safe(payload),
    }
    upload.raw_payload = raw_payload
    upload.save(update_fields=["raw_payload", "updated_at"])


def _normalization_payload(upload):
    raw_payload = upload.raw_payload or {}
    normalization = raw_payload.get("normalization")
    return normalization if isinstance(normalization, dict) else {}


def _ai_cleanup_payload(upload):
    cleanup = _normalization_payload(upload).get("ai_cleanup")
    return cleanup if isinstance(cleanup, dict) else {}


def _update_upload_ai_cleanup_payload(upload, payload):
    raw_payload = upload.raw_payload or {}
    normalization = raw_payload.get("normalization")
    if not isinstance(normalization, dict):
        normalization = {}
    cleanup = normalization.get("ai_cleanup")
    if not isinstance(cleanup, dict):
        cleanup = {}
    normalization["ai_cleanup"] = {
        **cleanup,
        **_json_safe(payload),
    }
    raw_payload["normalization"] = normalization
    upload.raw_payload = raw_payload
    upload.save(update_fields=["raw_payload", "updated_at"])


def _cached_normalize_response(upload, force=False):
    if force:
        return None
    raw_payload = upload.raw_payload or {}
    cached_result = (raw_payload.get("normalization") or {}).get("result")
    if not (upload.status in {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW} and cached_result):
        return None
    logger.info(
        "supplier_price_upload.normalize.cached upload_id=%s supplier_id=%s status=%s",
        upload.id,
        upload.supplier_id,
        upload.status,
    )
    items_total = cached_result.get("items_total", 0)
    cached_overrides = {
        "items_created": 0,
        "products_created": 0,
        "products_updated": 0,
        "prices_created": 0,
        "items_matched": items_total,
    }
    raw_payload["normalization"]["result"] = {**cached_result, **cached_overrides}
    upload.raw_payload = raw_payload
    upload.save(update_fields=["raw_payload"])
    return {
        **cached_result,
        **cached_overrides,
        "supplier_id": upload.supplier_id,
        "supplier_name": upload.supplier.display_name,
        "normalization": raw_payload.get("normalization", {}),
        "cached": True,
    }


@transaction.atomic
def _prepare_upload_for_normalization(*, upload_id, force=False):
    locked_upload = SupplierPriceUpload.objects.select_for_update().select_related("supplier").get(pk=upload_id)
    cached_response = _cached_normalize_response(locked_upload, force=force)
    if cached_response:
        return locked_upload, cached_response

    if locked_upload.status == UploadStatus.PROCESSING:
        raise SupplierPriceUploadConflict("Supplier price upload is already processing.")
    if locked_upload.status == UploadStatus.ARCHIVED:
        raise ValidationError({"status": "Archived upload cannot be normalized."})
    if locked_upload.status == UploadStatus.FAILED and not force:
        reason = locked_upload.error_message or "unknown error"
        raise ValidationError(
            {"status": f"Failed upload requires force=true to restart normalization (last error: {reason})."}
        )
    if locked_upload.status in {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW} and not force:
        raise ValidationError({"status": "Already processed upload requires force=true when cached result is missing."})
    if locked_upload.status not in {
        UploadStatus.RECEIVED,
        UploadStatus.PARSED,
        UploadStatus.FAILED,
        UploadStatus.APPLIED,
        UploadStatus.NEEDS_REVIEW,
    }:
        raise ValidationError({"status": f"Upload status {locked_upload.status} cannot be normalized."})

    locked_upload.status = UploadStatus.PROCESSING
    locked_upload.error_message = None
    raw_payload = locked_upload.raw_payload or {}
    raw_payload["normalization"] = {
        "status": UploadStatus.PROCESSING,
        "started_at": timezone.now().isoformat(),
    }
    locked_upload.raw_payload = raw_payload
    locked_upload.save(update_fields=["status", "error_message", "raw_payload", "updated_at"])
    return locked_upload, None


@transaction.atomic
def _mark_upload_failed(upload, message):
    locked_upload = SupplierPriceUpload.objects.select_for_update().get(pk=upload.pk)
    locked_upload.status = UploadStatus.FAILED
    locked_upload.error_message = message
    raw_payload = locked_upload.raw_payload or {}
    raw_payload["normalization"] = {
        **raw_payload.get("normalization", {}),
        "status": UploadStatus.FAILED,
        "finished_at": timezone.now().isoformat(),
        "error": message,
    }
    locked_upload.raw_payload = raw_payload
    locked_upload.save(update_fields=["status", "error_message", "raw_payload", "updated_at"])
    return locked_upload


def _run_supplier_price_upload_normalization(*, upload, use_ai=True, ai_mode=None, confirm_full=False):
    rows = _rows_for_upload(upload)
    logger.info(
        "supplier_price_upload.normalize.rows_loaded upload_id=%s supplier_id=%s rows=%s",
        upload.id,
        upload.supplier_id,
        len(rows),
    )

    _update_upload_normalization_payload(
        upload, {"rows_total": len(rows), "progress": {"chunks_done": 0, "chunks_total": 0}}
    )

    if not rows:
        _mark_upload_failed(upload, "No price rows were extracted from upload.")
        logger.warning(
            "supplier_price_upload.normalize.failed upload_id=%s supplier_id=%s error=%s",
            upload.id,
            upload.supplier_id,
            "No price rows were extracted from upload.",
        )
        return {
            "upload_id": upload.id,
            "supplier_id": upload.supplier_id,
            "supplier_name": upload.supplier.display_name,
            "status": UploadStatus.FAILED,
            "items_total": 0,
            "items_created": 0,
            "products_created": 0,
            "products_updated": 0,
            "prices_created": 0,
            "items_matched": 0,
            "needs_review": 0,
            "normalization": {
                "rows_total": 0,
                "rows_ai_requested": 0,
                "rows_ai_resolved": 0,
                "error": "No price rows were extracted from upload.",
            },
        }

    try:
        payload, diagnostics = build_normalized_payload(
            upload=upload,
            rows=rows,
            use_ai=use_ai,
            ai_mode=ai_mode,
            confirm_full=confirm_full,
            progress_callback=_normalization_progress_callback(upload.id),
        )
        logger.info(
            "supplier_price_upload.normalize.apply_start upload_id=%s supplier_id=%s rows=%s ai_mode=%s",
            upload.id,
            upload.supplier_id,
            len(payload["products"]),
            diagnostics["ai_mode"],
        )
        with _apply_progress_reporter(upload.id) as progress_callback, transaction.atomic():
            SupplierPriceUploadItem.objects.filter(upload=upload).delete()
            result = apply_supplier_price_ai_result(
                upload_id=upload.id, data=payload, progress_callback=progress_callback
            )
    except ValueError as error:
        _mark_upload_failed(upload, str(error))
        logger.warning(
            "supplier_price_upload.normalize.rejected upload_id=%s supplier_id=%s error=%s",
            upload.id,
            upload.supplier_id,
            error,
        )
        raise ValidationError({"normalization": str(error)}) from error
    except Exception as error:
        _mark_upload_failed(upload, str(error))
        logger.exception(
            "supplier_price_upload.normalize.failed upload_id=%s supplier_id=%s error=%s",
            upload.id,
            upload.supplier_id,
            error,
        )
        raise

    refreshed_upload = SupplierPriceUpload.objects.get(pk=upload.id)
    _update_upload_normalization_payload(
        refreshed_upload,
        {
            **diagnostics,
            "status": refreshed_upload.status,
            "finished_at": timezone.now().isoformat(),
            "result": result,
        },
    )
    logger.info(
        "supplier_price_upload.normalize.finish upload_id=%s supplier_id=%s status=%s items_total=%s items_created=%s products_created=%s products_updated=%s prices_created=%s items_matched=%s needs_review=%s ai_mode=%s",
        refreshed_upload.id,
        refreshed_upload.supplier_id,
        refreshed_upload.status,
        result["items_total"],
        result["items_created"],
        result["products_created"],
        result["products_updated"],
        result["prices_created"],
        result["items_matched"],
        result["needs_review"],
        diagnostics["ai_mode"],
    )
    if refreshed_upload.source_channel == SourceChannel.WEB:
        from api.v1.suppliers.bot_events import notify_linked_supplier_chats

        notify_linked_supplier_chats(
            supplier=refreshed_upload.supplier,
            text=(
                "✅ Прайс с сайта обработан\n\n"
                f"Компания: «{refreshed_upload.supplier.display_name}»\n"
                f"Строк: {result['items_total']}; новых товаров: {result['products_created']}; "
                f"обновлено: {result['products_updated']}; требуют проверки: {result['needs_review']}."
            ),
        )
    return {
        **result,
        "supplier_id": refreshed_upload.supplier_id,
        "supplier_name": refreshed_upload.supplier.display_name,
        "normalization": diagnostics,
    }


def normalize_supplier_price_upload(*, upload_id, use_ai=True, ai_mode=None, force=False, confirm_full=False):
    upload = get_object_or_404(
        SupplierPriceUpload.objects.select_related("supplier"),
        pk=upload_id,
    )

    logger.info(
        "supplier_price_upload.normalize.start upload_id=%s supplier_id=%s status=%s use_ai=%s ai_mode=%s force=%s confirm_full=%s",
        upload.id,
        upload.supplier_id,
        upload.status,
        use_ai,
        ai_mode or "default",
        force,
        confirm_full,
    )
    upload, cached_response = _prepare_upload_for_normalization(upload_id=upload.id, force=force)
    if cached_response:
        return cached_response

    return _run_supplier_price_upload_normalization(
        upload=upload,
        use_ai=use_ai,
        ai_mode=ai_mode,
        confirm_full=confirm_full,
    )


def start_supplier_price_upload_normalization(
    *,
    upload_id,
    use_ai=True,
    ai_mode=None,
    force=False,
    confirm_full=False,
    run_ai_cleanup=False,
    ai_cleanup_scope=AI_CLEANUP_SCOPE_ALL,
):
    upload = get_object_or_404(
        SupplierPriceUpload.objects.select_related("supplier"),
        pk=upload_id,
    )

    logger.info(
        "supplier_price_upload.normalize.enqueue upload_id=%s supplier_id=%s status=%s use_ai=%s ai_mode=%s force=%s confirm_full=%s",
        upload.id,
        upload.supplier_id,
        upload.status,
        use_ai,
        ai_mode or "default",
        force,
        confirm_full,
    )
    upload, cached_response = _prepare_upload_for_normalization(upload_id=upload.id, force=force)
    if cached_response:
        return {**cached_response, "accepted": False, "run_async": True}

    try:
        from api.v1.suppliers.tasks import normalize_supplier_price_upload_task

        task_result = normalize_supplier_price_upload_task.delay(
            upload_id=str(upload.id),
            use_ai=use_ai,
            ai_mode=ai_mode,
            confirm_full=confirm_full,
            run_ai_cleanup=run_ai_cleanup,
            ai_cleanup_scope=ai_cleanup_scope,
        )
    except Exception as error:
        _mark_upload_failed(upload, f"Failed to enqueue Celery normalization task: {error}")
        logger.exception(
            "supplier_price_upload.normalize.enqueue_failed upload_id=%s supplier_id=%s error=%s",
            upload.id,
            upload.supplier_id,
            error,
        )
        raise SupplierPriceUploadEnqueueError(f"Failed to enqueue Celery normalization task: {error}") from error

    _update_upload_normalization_payload(
        upload,
        {
            "celery_task_id": task_result.id,
            "queued_at": timezone.now().isoformat(),
            "run_async": True,
            "run_ai_cleanup": run_ai_cleanup,
            "ai_cleanup_scope": ai_cleanup_scope,
        },
    )
    return {
        "upload_id": upload.id,
        "supplier_id": upload.supplier_id,
        "supplier_name": upload.supplier.display_name,
        "status": UploadStatus.PROCESSING,
        "accepted": True,
        "run_async": True,
        "celery_task_id": task_result.id,
        "normalization": (upload.raw_payload or {}).get("normalization", {}),
    }


def _ai_cleanup_max_in_flight():
    try:
        return max(int(getattr(settings, "AI_CLEANUP_MAX_IN_FLIGHT", 2)), 0)
    except (TypeError, ValueError):
        return 2


def _ai_cleanup_queue():
    return str(getattr(settings, "AI_CLEANUP_QUEUE", "ai_normalization") or "ai_normalization")


def _ai_cleanup_in_flight_count(*, exclude_upload_id=None):
    queryset = SupplierPriceUpload.objects.filter(
        raw_payload__normalization__ai_cleanup__status__in=list(AI_CLEANUP_ACTIVE_STATUSES)
    )
    if exclude_upload_id:
        queryset = queryset.exclude(pk=exclude_upload_id)
    return queryset.count()


def _selected_ai_cleanup_products(*, upload, rows, scope):
    unit_aliases = unit_code_aliases_from_units(active_unit_codes_and_names())
    parsed_products = [parse_supplier_price_row(row, unit_aliases=unit_aliases) for row in rows]
    item_by_row = {
        item.row_number: item
        for item in SupplierPriceUploadItem.objects.select_related("supplier_product").filter(upload=upload)
        if item.row_number is not None
    }

    selected = []
    for product in parsed_products:
        row_number = int(product["row_number"])
        upload_item = item_by_row.get(row_number)
        if upload_item and upload_item.supplier_product_id:
            product["supplier_product_id"] = upload_item.supplier_product_id
        if scope == AI_CLEANUP_SCOPE_ALL:
            selected.append(product)
        elif scope == AI_CLEANUP_SCOPE_AMBIGUOUS and _needs_ai_fallback(product):
            selected.append(product)
        elif (
            scope == AI_CLEANUP_SCOPE_NEEDS_REVIEW
            and upload_item
            and upload_item.status == UploadItemStatus.NEEDS_REVIEW
        ):
            selected.append(product)
    return parsed_products, selected


@transaction.atomic
def _prepare_upload_for_ai_cleanup(*, upload_id, scope, force=False):
    locked_upload = SupplierPriceUpload.objects.select_for_update().select_related("supplier").get(pk=upload_id)

    if locked_upload.status == UploadStatus.PROCESSING:
        raise SupplierPriceUploadConflict("Primary supplier price upload normalization is still processing.")
    if locked_upload.status not in {UploadStatus.APPLIED, UploadStatus.NEEDS_REVIEW}:
        raise ValidationError({"status": "AI cleanup requires primary normalization to finish first."})

    cleanup = _ai_cleanup_payload(locked_upload)
    cleanup_status = cleanup.get("status")
    if cleanup_status in AI_CLEANUP_ACTIVE_STATUSES:
        raise SupplierPriceUploadConflict("Supplier price upload AI cleanup is already queued or processing.")
    if cleanup_status in {AI_CLEANUP_STATUS_APPLIED, AI_CLEANUP_STATUS_NEEDS_REVIEW} and not force:
        return locked_upload, {
            "upload_id": locked_upload.id,
            "supplier_id": locked_upload.supplier_id,
            "supplier_name": locked_upload.supplier.display_name,
            "accepted": False,
            "cached": True,
            "ai_cleanup": cleanup,
        }

    max_in_flight = _ai_cleanup_max_in_flight()
    if max_in_flight and _ai_cleanup_in_flight_count(exclude_upload_id=locked_upload.id) >= max_in_flight:
        cleanup = {
            "status": "rejected_overload",
            "scope": scope,
            "rejected_at": timezone.now().isoformat(),
            "max_in_flight": max_in_flight,
        }
        _update_upload_ai_cleanup_payload(locked_upload, cleanup)
        return locked_upload, {
            "upload_id": locked_upload.id,
            "supplier_id": locked_upload.supplier_id,
            "supplier_name": locked_upload.supplier.display_name,
            "accepted": False,
            "overloaded": True,
            "ai_cleanup": cleanup,
        }

    _update_upload_ai_cleanup_payload(
        locked_upload,
        {
            "status": AI_CLEANUP_STATUS_QUEUED,
            "scope": scope,
            "queued_at": timezone.now().isoformat(),
            "force": force,
            "max_in_flight": max_in_flight,
        },
    )
    locked_upload.refresh_from_db()
    return locked_upload, None


def start_supplier_price_upload_ai_cleanup(*, upload_id, scope=AI_CLEANUP_SCOPE_ALL, force=False):
    if scope not in AI_CLEANUP_SCOPES:
        raise ValidationError({"scope": "Unsupported AI cleanup scope."})

    upload = get_object_or_404(
        SupplierPriceUpload.objects.select_related("supplier"),
        pk=upload_id,
    )
    logger.info(
        "supplier_price_upload.ai_cleanup.enqueue upload_id=%s supplier_id=%s status=%s scope=%s force=%s",
        upload.id,
        upload.supplier_id,
        upload.status,
        scope,
        force,
    )

    upload, cached_response = _prepare_upload_for_ai_cleanup(upload_id=upload.id, scope=scope, force=force)
    if cached_response:
        logger.info(
            "supplier_price_upload.ai_cleanup.cached upload_id=%s supplier_id=%s status=%s",
            upload.id,
            upload.supplier_id,
            cached_response["ai_cleanup"].get("status"),
        )
        if cached_response.get("overloaded"):
            raise SupplierPriceUploadLoadShedding(
                f"AI cleanup queue is overloaded; max in-flight jobs is {cached_response['ai_cleanup'].get('max_in_flight')}."
            )
        return cached_response

    try:
        from api.v1.suppliers.tasks import ai_cleanup_supplier_price_upload_task

        task_result = ai_cleanup_supplier_price_upload_task.apply_async(
            kwargs={
                "upload_id": str(upload.id),
                "scope": scope,
                "force": force,
            },
            queue=_ai_cleanup_queue(),
        )
    except Exception as error:
        _update_upload_ai_cleanup_payload(
            upload,
            {
                "status": AI_CLEANUP_STATUS_FAILED,
                "finished_at": timezone.now().isoformat(),
                "error": f"Failed to enqueue Celery AI cleanup task: {error}",
            },
        )
        logger.exception(
            "supplier_price_upload.ai_cleanup.enqueue_failed upload_id=%s supplier_id=%s error=%s",
            upload.id,
            upload.supplier_id,
            error,
        )
        raise SupplierPriceUploadEnqueueError(f"Failed to enqueue Celery AI cleanup task: {error}") from error

    _update_upload_ai_cleanup_payload(
        upload,
        {
            "celery_task_id": task_result.id,
            "queue": _ai_cleanup_queue(),
        },
    )
    upload.refresh_from_db()
    cleanup = _ai_cleanup_payload(upload)
    return {
        "upload_id": upload.id,
        "supplier_id": upload.supplier_id,
        "supplier_name": upload.supplier.display_name,
        "accepted": True,
        "run_async": True,
        "celery_task_id": task_result.id,
        "ai_cleanup": cleanup,
    }


@transaction.atomic
def _mark_ai_cleanup_failed(upload, message):
    locked_upload = SupplierPriceUpload.objects.select_for_update().get(pk=upload.pk)
    _update_upload_ai_cleanup_payload(
        locked_upload,
        {
            "status": AI_CLEANUP_STATUS_FAILED,
            "finished_at": timezone.now().isoformat(),
            "error": str(message),
        },
    )
    return locked_upload


def _ai_cleanup_progress_callback(upload_id):
    def _callback(*, chunk_index, chunks_total, stats, resolved):
        try:
            with transaction.atomic():
                locked_upload = SupplierPriceUpload.objects.select_for_update().get(pk=upload_id)
                _update_upload_ai_cleanup_payload(
                    locked_upload,
                    {
                        "status": AI_CLEANUP_STATUS_PROCESSING,
                        "chunks_done": chunk_index,
                        "chunks_total": chunks_total,
                        "rows_resolved": resolved,
                        "ai_chunks_succeeded": stats["ai_chunks_succeeded"],
                        "ai_chunks_failed": stats["ai_chunks_failed"],
                        "ai_retries": stats["ai_retries"],
                        "ai_error_codes": stats["ai_error_codes"],
                        "last_progress_at": timezone.now().isoformat(),
                    },
                )
        except Exception:
            logger.exception("supplier_price_upload.ai_cleanup.progress_failed upload_id=%s", upload_id)

    return _callback


def _normalization_progress_callback(upload_id):
    def _callback(*, chunk_index, chunks_total, stats, resolved):
        try:
            with transaction.atomic():
                locked = SupplierPriceUpload.objects.select_for_update().get(pk=upload_id)
                _update_upload_normalization_payload(
                    locked,
                    {
                        "progress": {
                            "chunks_done": chunk_index,
                            "chunks_total": chunks_total,
                            "rows_done": resolved,
                            "ai_chunks_succeeded": stats["ai_chunks_succeeded"],
                            "ai_chunks_failed": stats["ai_chunks_failed"],
                            "last_progress_at": timezone.now().isoformat(),
                        }
                    },
                )
        except Exception:
            logger.exception("supplier_price_upload.normalize.progress_failed upload_id=%s", upload_id)

    return _callback


@contextmanager
def _apply_progress_reporter(upload_id):
    """Progress reporting for apply_supplier_price_ai_result's per-product
    loop — the loop itself runs inside ONE @transaction.atomic() for the
    whole batch (see that function's docstring for why it no longer holds a
    row lock, but it's still one open transaction for hundreds of writes).
    Unlike _normalization_progress_callback/_ai_cleanup_progress_callback
    above, a plain `with transaction.atomic(): ...` here would just open a
    SAVEPOINT inside that same outer transaction — invisible to any other
    connection (e.g. the request serving GET /uploads/{id}) until the whole
    apply() finishes, which defeats the entire point of incremental
    progress. This opens one independent, autocommitting connection for the
    duration of the call (reused across updates, not reopened per row —
    each fresh connection pays a real TLS handshake, see
    docs/wiki/logs/2026-08-14-suppliers-products-slow-query-root-cause.md)
    so writes commit immediately regardless of the outer transaction's
    fate. 2026-08-17: added because a real 834-row upload spent several
    minutes with the progress bar stuck at 0% — normalize()'s AI-chunking
    phase (build_normalized_payload, reports progress fine via the callback
    above) had already finished; all the time was this function's own
    per-row DB writes, which reported nothing at all."""
    import psycopg
    from django.db import connections

    conn = psycopg.connect(**connections["default"].get_connection_params(), autocommit=True)

    def _callback(*, rows_done, rows_total):
        try:
            with conn.cursor() as cursor:
                # jsonb_set's create_missing only creates the FINAL key of
                # the path — it does NOT build missing intermediate levels
                # (a well-known Postgres gotcha), so a naive
                # jsonb_set(..., '{normalization,progress,rows_done}', ...,
                # true) is a silent no-op whenever "normalization" isn't
                # already an object (caught by
                # ApplyProgressReporterTests before shipping). In practice
                # _run_supplier_price_upload_normalization always seeds
                # raw_payload.normalization.progress before calling apply(),
                # but building it up one COALESCE'd level at a time here
                # doesn't depend on that — correct regardless of what's
                # already there, and preserves sibling keys (chunks_done/
                # chunks_total) at the same level via ||.
                cursor.execute(
                    """
                    UPDATE supplier_price_uploads
                    SET raw_payload = jsonb_set(
                        coalesce(raw_payload, '{}'::jsonb),
                        '{normalization}',
                        coalesce(raw_payload -> 'normalization', '{}'::jsonb) || jsonb_build_object(
                            'progress',
                            coalesce(raw_payload -> 'normalization' -> 'progress', '{}'::jsonb)
                                || jsonb_build_object('rows_done', %s::int, 'rows_total', %s::int)
                        ),
                        true
                    )
                    WHERE id = %s
                    """,
                    [rows_done, rows_total, str(upload_id)],
                )
        except Exception:
            logger.exception("supplier_price_upload.normalize.apply_progress_failed upload_id=%s", upload_id)

    try:
        yield _callback
    finally:
        conn.close()

    return _callback


_AI_CLEANUP_PROTECTED_FIELDS = ("price", "min_order_qty", "multiplicity", "availability_status")


def _restore_ai_cleanup_protected_fields(*, ai_products, parsed_products):
    """2026-09-29 (unified-import-architecture spec §9.7): ai_cleanup -- второй,
    ревью-проход поверх уже распарсенных данных -- не должен иметь возможности
    менять price/min_order_qty/multiplicity/availability_status, что бы ни
    вернула модель. Системный промпт (_system_prompt) уже просит её этого не
    делать, но схема ответа (ParsedSupplierProductRow/_json_schema_prompt) эти
    поля всё равно содержит -- она общая с phase="normalization", где они
    нужны легитимно (чтение цены из сырого текста прайса). Поэтому гарантия
    здесь, а не в схеме: что бы ни предложила модель для этих 4 полей, они
    принудительно возвращаются к значению первичного Python-разбора
    (parsed_products) перед тем, как результат уйдёт в apply_supplier_price_ai_result."""
    parsed_by_row = {int(product["row_number"]): product for product in parsed_products}
    for product in ai_products:
        source = parsed_by_row.get(int(product["row_number"]))
        if source is None:
            continue
        for field in _AI_CLEANUP_PROTECTED_FIELDS:
            product[field] = source.get(field)


def _run_supplier_price_upload_ai_cleanup(*, upload, scope=AI_CLEANUP_SCOPE_ALL, force=False):
    if getattr(settings, "CANONICAL_LEGACY_PARSER_ENABLED", False):
        return _run_supplier_price_upload_normalization(upload=upload, use_ai=True, ai_mode="ambiguous")
    logger.info(
        "supplier_price_upload.ai_cleanup.start upload_id=%s supplier_id=%s status=%s scope=%s force=%s",
        upload.id,
        upload.supplier_id,
        upload.status,
        scope,
        force,
    )

    rows = _rows_for_upload(upload)
    logger.info(
        "supplier_price_upload.ai_cleanup.rows_loaded upload_id=%s supplier_id=%s rows=%s scope=%s",
        upload.id,
        upload.supplier_id,
        len(rows),
        scope,
    )
    if not rows:
        _mark_ai_cleanup_failed(upload, "No price rows were extracted from upload.")
        return {
            "upload_id": upload.id,
            "status": AI_CLEANUP_STATUS_FAILED,
            "error": "No price rows were extracted from upload.",
        }

    try:
        parsed_products, products_for_ai = _selected_ai_cleanup_products(upload=upload, rows=rows, scope=scope)
        _update_upload_ai_cleanup_payload(
            upload,
            {
                "status": AI_CLEANUP_STATUS_PROCESSING,
                "started_at": timezone.now().isoformat(),
                "scope": scope,
                "rows_total": len(rows),
                "rows_rules_parsed": len(parsed_products),
                "rows_requested": len(products_for_ai),
                "rows_resolved": 0,
                "chunks_done": 0,
                "chunks_total": 0,
                "ai_errors": [],
                "ai_error_codes": [],
            },
        )
        if not products_for_ai:
            result = {
                "upload_id": upload.id,
                "status": upload.status,
                "items_total": 0,
                "items_created": 0,
                "products_created": 0,
                "products_updated": 0,
                "prices_created": 0,
                "items_matched": 0,
                "needs_review": SupplierPriceUploadItem.objects.filter(
                    upload=upload,
                    status=UploadItemStatus.NEEDS_REVIEW,
                ).count(),
            }
            _update_upload_ai_cleanup_payload(
                upload,
                {
                    "status": AI_CLEANUP_STATUS_APPLIED,
                    "finished_at": timezone.now().isoformat(),
                    "rows_resolved": 0,
                    "result": result,
                },
            )
            logger.info(
                "supplier_price_upload.ai_cleanup.noop upload_id=%s supplier_id=%s scope=%s",
                upload.id,
                upload.supplier_id,
                scope,
            )
            return result

        unit_rows = active_unit_codes_and_names()
        ai_products, ai_stats = normalize_products_with_ai(
            upload=upload,
            rows=rows,
            products=products_for_ai,
            parser_source="ai_cleanup",
            phase="ai_cleanup",
            unit_codes=[code for code, _name in unit_rows],
            unit_aliases=unit_code_aliases_from_units(unit_rows),
            progress_callback=_ai_cleanup_progress_callback(upload.id),
        )
        _restore_ai_cleanup_protected_fields(ai_products=ai_products, parsed_products=parsed_products)

        products_by_row = {int(product["row_number"]): product for product in parsed_products}
        for product in ai_products:
            products_by_row[int(product["row_number"])] = product

        requested_rows = {int(product["row_number"]) for product in products_for_ai}
        if ai_stats["ai_errors"]:
            for row_number in requested_rows - ai_stats["resolved_row_numbers"]:
                product = products_by_row[row_number]
                warnings = product.setdefault("warnings", [])
                warnings.append("ai_cleanup_failed")
                product["comment"] = "; ".join([product.get("comment") or "", *ai_stats["ai_errors"]]).strip("; ")

        cleanup_products = [products_by_row[row.row_number] for row in rows if row.row_number in requested_rows]
        payload = {
            "schema_version": NORMALIZATION_SCHEMA_VERSION,
            "processing_version": NORMALIZATION_PROCESSING_VERSION,
            "supplier_id": upload.supplier_id,
            "upload_id": upload.id,
            "source_channel": upload.source_channel,
            "supplier_name": upload.supplier.display_name,
            "products": cleanup_products,
        }
        logger.info(
            "supplier_price_upload.ai_cleanup.apply_start upload_id=%s supplier_id=%s rows=%s chunks_total=%s chunks_failed=%s",
            upload.id,
            upload.supplier_id,
            len(cleanup_products),
            ai_stats["ai_chunks_total"],
            ai_stats["ai_chunks_failed"],
        )
        result = apply_supplier_price_ai_result(upload_id=upload.id, data=payload)
    except Exception as error:
        _mark_ai_cleanup_failed(upload, str(error))
        logger.exception(
            "supplier_price_upload.ai_cleanup.failed upload_id=%s supplier_id=%s error=%s",
            upload.id,
            upload.supplier_id,
            error,
        )
        raise

    refreshed_upload = SupplierPriceUpload.objects.get(pk=upload.id)
    final_status = AI_CLEANUP_STATUS_NEEDS_REVIEW if result["needs_review"] else AI_CLEANUP_STATUS_APPLIED
    _update_upload_ai_cleanup_payload(
        refreshed_upload,
        {
            "status": final_status,
            "finished_at": timezone.now().isoformat(),
            "rows_total": len(rows),
            "rows_rules_parsed": len(parsed_products),
            "rows_requested": len(products_for_ai),
            "rows_resolved": len(ai_products),
            "rows_failed": max(len(products_for_ai) - len(ai_products), 0),
            "ai_chunk_size": getattr(settings, "AI_NORMALIZATION_CHUNK_SIZE", 20),
            "ai_chunks_total": ai_stats["ai_chunks_total"],
            "ai_chunks_succeeded": ai_stats["ai_chunks_succeeded"],
            "ai_chunks_failed": ai_stats["ai_chunks_failed"],
            "ai_retries": ai_stats["ai_retries"],
            "ai_errors": ai_stats["ai_errors"],
            "ai_error_codes": ai_stats["ai_error_codes"],
            "result": result,
        },
    )
    logger.info(
        "supplier_price_upload.ai_cleanup.finish upload_id=%s supplier_id=%s status=%s rows_requested=%s rows_resolved=%s products_updated=%s prices_created=%s needs_review=%s chunks_failed=%s",
        refreshed_upload.id,
        refreshed_upload.supplier_id,
        final_status,
        len(products_for_ai),
        len(ai_products),
        result["products_updated"],
        result["prices_created"],
        result["needs_review"],
        ai_stats["ai_chunks_failed"],
    )
    return {
        **result,
        "supplier_id": refreshed_upload.supplier_id,
        "supplier_name": refreshed_upload.supplier.display_name,
        "ai_cleanup": _ai_cleanup_payload(refreshed_upload),
    }
