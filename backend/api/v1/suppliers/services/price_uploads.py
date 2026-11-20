"""Legacy supplier price uploads: create, preview, column mapping, corrections, rollback."""

import hashlib
from decimal import Decimal

from django.db import OperationalError, connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from api.v1.suppliers.normalization import (
    MIN_ORDER_ALIASES,
    MULTIPLICITY_ALIASES,
    NAME_ALIASES,
    PACKAGE_COMPOSITION_ALIASES,
    PRICE_ALIASES,
    PRODUCT_NAME_ALIASES,
    SKU_ALIASES,
    UNIT_ALIASES,
    _matches_any_alias,
    active_unit_codes_and_names,
    extract_price_rows_from_file,
    extracted_rows_from_payload,
    extracted_rows_from_text,
    extracted_rows_to_payload,
    extractor_name_for_file,
    parse_supplier_price_row,
    unit_code_aliases_from_units,
)
from api.v1.suppliers.storage import (
    persist_supplier_price_source_file,
)
from file_imports.extraction import FileExtractionError, UnreadableSpreadsheetError
from suppliers.models import (
    PriceStatus,
    PriceType,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductPrice,
    UploadItemStatus,
    UploadStatus,
)
from suppliers.services import sync_supplier_product_price_snapshot

from ._shared import _currency_value, _json_safe, _resolve_ai_unit, _rows_for_upload, logger
from .products import archive_supplier_product


class SupplierPriceUploadConflict(APIException):
    status_code = 409
    default_code = "supplier_price_upload_conflict"
    default_detail = "Supplier price upload cannot be processed right now."


class SupplierPriceUploadEnqueueError(APIException):
    status_code = 503
    default_code = "supplier_price_upload_enqueue_failed"
    default_detail = "Supplier price upload normalization could not be queued."


class SupplierPriceUploadLoadShedding(APIException):
    status_code = 503
    default_code = "supplier_price_upload_ai_cleanup_overloaded"
    default_detail = "Supplier price upload AI cleanup queue is overloaded."


def _integration_metadata(data):
    metadata = {}
    for field in ("telegram_message_id", "n8n_execution_id"):
        value = data.get(field)
        if value:
            metadata[field] = value
    return metadata


def _acquire_upload_dedup_lock(*, supplier_id, source_channel, kind):
    # Postgres advisory lock, keyed by (supplier, channel, kind) — the
    # select_for_update() inside _get_existing_upload_for_execution/
    # _get_existing_upload_for_fingerprint only locks rows that already
    # exist, so it locks nothing on the *first* delivery of a given
    # execution_id/fingerprint. Two near-simultaneous calls (n8n retry
    # racing the original, duplicate webhook delivery) then both pass the
    # "no existing upload" checks and both create one — confirmed
    # empirically under real concurrency. Serializing on this coarser key
    # (not the specific fingerprint) is simpler and correct; it just means
    # truly concurrent *different* uploads for the same supplier/channel
    # briefly queue instead of racing, which is fine for this integration
    # path's volume.
    raw_key = f"supplier_price_upload:{supplier_id}:{source_channel}:{kind}"
    lock_key = int(hashlib.sha256(raw_key.encode()).hexdigest()[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])


def _locked_or_unlocked_first(queryset):
    # 2026-08-16: plain select_for_update() here used to block for as long as
    # apply_supplier_price_ai_result() (Celery, run_async=True by default
    # since 2026-08-15) holds its own select_for_update() on the SAME upload
    # row for the ENTIRE per-row normalization loop — hundreds of rows, easily
    # minutes. A synchronous intake request (this dedup check runs inside
    # POST /uploads, still bound by gunicorn --timeout 120) re-uploading a
    # byte-identical file while the first is still processing would hang
    # until the worker got SIGABRT/SIGKILLed — live incident, see
    # docs/wiki/logs/2026-08-16-normalize-async-default-and-dedup-lock-hang.md.
    # nowait=True fails fast instead of blocking; falling back to an unlocked
    # read is safe here because callers only use the result to decide
    # "reuse existing upload" vs "create new" — the one write that follows
    # (_ensure_upload_source_file) is a no-op whenever source_file_uri is
    # already set, which it always is by the time another transaction is
    # deep enough into processing to be holding this lock.
    # A failed nowait lock marks the whole enclosing Postgres transaction
    # "aborted" — any further command on it (including the unlocked retry
    # below) would fail with "current transaction is aborted" unless the
    # attempt runs inside its own SAVEPOINT. transaction.atomic() used as a
    # nested context manager here creates exactly that (both call sites are
    # already inside an outer @transaction.atomic), and rolls back to it
    # automatically when OperationalError propagates out.
    try:
        with transaction.atomic():
            return queryset.select_for_update(nowait=True).first()
    except OperationalError:
        return queryset.first()


def _get_existing_upload_for_execution(*, data, kind):
    n8n_execution_id = data.get("n8n_execution_id")
    if not n8n_execution_id:
        return None

    return _locked_or_unlocked_first(
        SupplierPriceUpload.objects.filter(
            supplier=data["supplier"],
            source_channel=data["source_channel"],
            raw_payload__kind=kind,
            raw_payload__metadata__n8n_execution_id=n8n_execution_id,
        ).order_by("-created_at", "-id")
    )


def _get_existing_upload_for_fingerprint(*, supplier, source_channel, kind, fingerprint_field, fingerprint):
    if not fingerprint:
        return None

    return _locked_or_unlocked_first(
        SupplierPriceUpload.objects.filter(
            supplier=supplier,
            source_channel=source_channel,
            raw_payload__kind=kind,
            **{f"raw_payload__metadata__{fingerprint_field}": fingerprint},
        ).order_by("-created_at", "-id")
    )


def _file_sha256(source_file):
    source_file.seek(0)
    digest = hashlib.sha256(source_file.read()).hexdigest()
    source_file.seek(0)
    return digest


def _ensure_upload_source_file(*, upload, source_file, source_file_name, content_sha256):
    if upload.source_file_uri:
        return upload.source_file_uri

    source_file_uri = persist_supplier_price_source_file(
        source_file=source_file,
        supplier_id=upload.supplier_id,
        content_sha256=content_sha256,
        source_file_name=source_file_name,
    )
    raw_payload = upload.raw_payload or {}
    metadata = raw_payload.get("metadata") if isinstance(raw_payload.get("metadata"), dict) else {}
    metadata = {
        **metadata,
        "content_sha256": content_sha256,
        "source_file_uri": source_file_uri,
    }
    raw_payload["metadata"] = metadata
    upload.source_file_uri = source_file_uri
    upload.raw_payload = raw_payload
    upload.save(update_fields=["source_file_uri", "raw_payload", "updated_at"])
    return source_file_uri


def _text_sha256(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


@transaction.atomic
def create_supplier_price_upload_from_file(*, data):
    _acquire_upload_dedup_lock(supplier_id=data["supplier"].id, source_channel=data["source_channel"], kind="file")

    source_file = data["source_file"]
    source_file_name = data.get("source_file_name") or source_file.name
    content_sha256 = _file_sha256(source_file)

    existing_upload = _get_existing_upload_for_execution(data=data, kind="file")
    if existing_upload:
        _ensure_upload_source_file(
            upload=existing_upload,
            source_file=source_file,
            source_file_name=source_file_name,
            content_sha256=content_sha256,
        )
        logger.info(
            "supplier_price_upload.file_intake.reused_by_execution upload_id=%s supplier_id=%s source_channel=%s",
            existing_upload.id,
            existing_upload.supplier_id,
            existing_upload.source_channel,
        )
        return existing_upload

    existing_upload = _get_existing_upload_for_fingerprint(
        supplier=data["supplier"],
        source_channel=data["source_channel"],
        kind="file",
        fingerprint_field="content_sha256",
        fingerprint=content_sha256,
    )
    if existing_upload:
        _ensure_upload_source_file(
            upload=existing_upload,
            source_file=source_file,
            source_file_name=source_file_name,
            content_sha256=content_sha256,
        )
        logger.info(
            "supplier_price_upload.file_intake.reused_by_fingerprint upload_id=%s supplier_id=%s source_channel=%s file_name=%s",
            existing_upload.id,
            existing_upload.supplier_id,
            existing_upload.source_channel,
            source_file_name,
        )
        return existing_upload

    logger.info(
        "supplier_price_upload.file_intake.start supplier_id=%s source_channel=%s file_name=%s size=%s",
        data["supplier"].id,
        data["source_channel"],
        source_file_name,
        source_file.size,
    )
    source_file_uri = persist_supplier_price_source_file(
        source_file=source_file,
        supplier_id=data["supplier"].id,
        content_sha256=content_sha256,
        source_file_name=source_file_name,
    )
    extractor = extractor_name_for_file(source_file_name)
    deferred_extraction = extractor in {"docling_pdf", "image_ocr"}
    extracted_rows = []
    if not deferred_extraction:
        try:
            extracted_rows = extract_price_rows_from_file(
                source_file,
                source_file_uri=source_file_uri,
                source_file_name=source_file_name,
            )
        except (FileExtractionError, UnreadableSpreadsheetError) as error:
            raise ValidationError({"source_file": str(error)}) from error
    raw_payload = {
        "kind": "file",
        "metadata": {
            "original_file_name": source_file.name,
            "source_file_name": source_file_name,
            "content_type": getattr(source_file, "content_type", ""),
            "size": source_file.size,
            "content_sha256": content_sha256,
            "source_file_uri": source_file_uri,
            **_integration_metadata(data),
        },
        "extracted_rows": extracted_rows_to_payload(extracted_rows),
        "extraction": {
            "rows_total": len(extracted_rows),
            "source": "backend",
            "extractor": extractor,
            "deferred": deferred_extraction,
        },
    }

    upload = SupplierPriceUpload.objects.create(
        supplier=data["supplier"],
        source_channel=data["source_channel"],
        source_file_name=source_file_name,
        source_file_uri=source_file_uri,
        status=UploadStatus.RECEIVED,
        raw_payload=raw_payload,
    )
    logger.info(
        "supplier_price_upload.file_intake.finish upload_id=%s supplier_id=%s file_name=%s rows_extracted=%s",
        upload.id,
        upload.supplier_id,
        source_file_name,
        len(extracted_rows),
    )
    return upload


@transaction.atomic
def create_supplier_price_upload_from_text(*, data):
    _acquire_upload_dedup_lock(supplier_id=data["supplier"].id, source_channel=data["source_channel"], kind="text")

    existing_upload = _get_existing_upload_for_execution(data=data, kind="text")
    if existing_upload:
        logger.info(
            "supplier_price_upload.text_intake.reused_by_execution upload_id=%s supplier_id=%s source_channel=%s",
            existing_upload.id,
            existing_upload.supplier_id,
            existing_upload.source_channel,
        )
        return existing_upload

    text_sha256 = _text_sha256(data["text"])
    existing_upload = _get_existing_upload_for_fingerprint(
        supplier=data["supplier"],
        source_channel=data["source_channel"],
        kind="text",
        fingerprint_field="text_sha256",
        fingerprint=text_sha256,
    )
    if existing_upload:
        logger.info(
            "supplier_price_upload.text_intake.reused_by_fingerprint upload_id=%s supplier_id=%s source_channel=%s",
            existing_upload.id,
            existing_upload.supplier_id,
            existing_upload.source_channel,
        )
        return existing_upload

    extracted_rows = extracted_rows_from_text(data["text"])
    raw_payload = {
        "kind": "text",
        "text": data["text"],
        "extracted_rows": extracted_rows_to_payload(extracted_rows),
        "extraction": {
            "rows_total": len(extracted_rows),
            "source": "backend",
            "extractor": "text",
            "deferred": False,
        },
        "metadata": {
            "text_sha256": text_sha256,
            **_integration_metadata(data),
        },
    }

    upload = SupplierPriceUpload.objects.create(
        supplier=data["supplier"],
        source_channel=data["source_channel"],
        source_file_name=data.get("source_file_name") or None,
        status=UploadStatus.RECEIVED,
        raw_payload=raw_payload,
    )
    logger.info(
        "supplier_price_upload.text_intake.finish upload_id=%s supplier_id=%s source_channel=%s text_chars=%s",
        upload.id,
        upload.supplier_id,
        upload.source_channel,
        len(data["text"]),
    )
    return upload


def get_supplier_price_upload_preview(*, upload, limit=20):
    rows = _rows_for_upload(upload)
    unit_aliases = unit_code_aliases_from_units(active_unit_codes_and_names())
    preview_rows = []
    for row in rows[:limit]:
        product = parse_supplier_price_row(row, unit_aliases=unit_aliases)
        warnings = list(product.get("warnings") or [])
        preview_rows.append(
            {
                "row_number": row.row_number,
                "cells": row.cells,
                "name": product.get("raw_name") or "",
                "normalized_name": product.get("normalized_name") or "",
                "price": str(product["price"]) if product.get("price") is not None else None,
                "unit": product.get("unit_code") or None,
                "warnings": warnings,
                "valid": not warnings,
            }
        )

    required_fields = {
        "name": bool(preview_rows)
        and all(
            row["name"]
            and "raw_name_unknown" not in row["warnings"]
            and "normalized_name_unknown" not in row["warnings"]
            for row in preview_rows
        ),
        "price": bool(preview_rows) and all(row["price"] is not None for row in preview_rows),
        "unit": bool(preview_rows) and all(row["unit"] for row in preview_rows),
    }
    columns = sorted({str(key) for row in rows[:limit] for key in row.cells})
    aliases_by_field = {
        "name": PRODUCT_NAME_ALIASES | NAME_ALIASES,
        "price": PRICE_ALIASES,
        "unit": UNIT_ALIASES,
        "sku": SKU_ALIASES,
        "min_order": MIN_ORDER_ALIASES,
        "multiplicity": MULTIPLICITY_ALIASES,
        "package_composition": PACKAGE_COMPOSITION_ALIASES,
    }
    detected_mapping = {
        canonical: next((column for column in columns if _matches_any_alias(column, aliases)), None)
        for canonical, aliases in aliases_by_field.items()
    }
    detected_mapping = {key: value for key, value in detected_mapping.items() if value}
    return {
        "upload_id": upload.id,
        "file_name": upload.source_file_name,
        "rows_total": len(rows),
        "preview_count": len(preview_rows),
        "columns": columns,
        "detected_column_mapping": detected_mapping,
        "column_mapping": (upload.raw_payload or {}).get("column_mapping") or {},
        "required_fields": required_fields,
        "missing_required_fields": [key for key, present in required_fields.items() if not present],
        "ready_for_processing": bool(preview_rows) and all(row["valid"] for row in preview_rows),
        "rows": preview_rows,
    }


_COLUMN_MAPPING_DESTINATIONS = {
    "name": "name",
    "price": "price",
    "unit": "unit",
    "sku": "supplier_sku",
    "min_order": "min_order_qty",
    "multiplicity": "multiplicity",
    "package_composition": "package_composition",
}


@transaction.atomic
def set_supplier_price_upload_column_mapping(*, upload_id, mapping):
    upload = get_object_or_404(SupplierPriceUpload.objects.select_for_update(), pk=upload_id)
    if upload.status not in {UploadStatus.RECEIVED, UploadStatus.PARSED, UploadStatus.FAILED}:
        raise ValidationError(
            {
                "status": (
                    f"Column mapping cannot be changed for {upload.status} upload. "
                    "Rollback an applied upload or correct individual review rows instead."
                )
            }
        )

    raw_payload = upload.raw_payload or {}
    original_payload = raw_payload.get("original_extracted_rows")
    if not isinstance(original_payload, list):
        original_payload = raw_payload.get("extracted_rows") or []
        raw_payload["original_extracted_rows"] = original_payload
    rows = extracted_rows_from_payload(original_payload)
    columns = {str(key) for row in rows for key in row.cells}
    missing_columns = sorted(set(mapping.values()) - columns)
    if missing_columns:
        raise ValidationError({"mapping": f"Columns not found: {', '.join(missing_columns)}."})

    remapped_rows = []
    for row in rows:
        cells = dict(row.cells)
        for canonical, source_column in mapping.items():
            cells[_COLUMN_MAPPING_DESTINATIONS[canonical]] = cells.get(source_column, "")
        remapped_rows.append(type(row)(row_number=row.row_number, cells=cells))

    raw_payload["extracted_rows"] = extracted_rows_to_payload(remapped_rows)
    raw_payload["column_mapping"] = dict(mapping)
    raw_payload.pop("normalization", None)
    upload.raw_payload = raw_payload
    upload.status = UploadStatus.RECEIVED
    upload.processed_at = None
    upload.error_message = None
    upload.save(update_fields=["raw_payload", "status", "processed_at", "error_message", "updated_at"])
    upload.items.all().delete()
    return upload


def get_supplier_price_upload_change_plan(*, upload):
    rows = _rows_for_upload(upload)
    unit_aliases = unit_code_aliases_from_units(active_unit_codes_and_names())
    counts = {"create": 0, "update": 0, "unchanged": 0, "reject": 0, "duplicate": 0}
    problems = []
    seen_keys = set()

    for row in rows:
        product = parse_supplier_price_row(row, unit_aliases=unit_aliases)
        warnings = list(product.get("warnings") or [])
        key = (
            (
                "sku",
                str(product.get("supplier_sku") or "").strip().casefold(),
            )
            if product.get("supplier_sku")
            else (
                "name",
                str(product.get("normalized_name") or "").strip().casefold(),
                str(product.get("package_info") or "").strip().casefold(),
            )
        )
        if key in seen_keys:
            counts["duplicate"] += 1
            problems.append({"row_number": row.row_number, "reason": "duplicate_in_file", "warnings": warnings})
            continue
        seen_keys.add(key)

        if warnings:
            counts["reject"] += 1
            problems.append({"row_number": row.row_number, "reason": "invalid_required_fields", "warnings": warnings})
            continue

        existing = None
        if product.get("supplier_sku"):
            existing = SupplierProduct.objects.filter(
                supplier=upload.supplier, supplier_sku=product["supplier_sku"]
            ).first()
        if existing is None:
            existing = SupplierProduct.objects.filter(
                supplier=upload.supplier, supplier_name=product.get("raw_name")
            ).first()
        if existing is None:
            counts["create"] += 1
            continue

        active_price = _current_active_price_readonly(existing.id)
        same_price = _price_matches_payload(active_price=active_price, product_data=product)
        same_name = existing.normalized_name == product.get("normalized_name")
        same_unit = not product.get("unit_code") or (
            existing.unit is not None and existing.unit.code == product.get("unit_code")
        )
        counts["unchanged" if same_price and same_name and same_unit else "update"] += 1

    actionable = counts["create"] + counts["update"]
    return {
        "upload_id": upload.id,
        "rows_total": len(rows),
        **counts,
        "actionable": actionable,
        "requires_confirmation": actionable > 0,
        "can_publish": bool(rows) and counts["reject"] == 0 and counts["duplicate"] == 0,
        "problems": problems[:100],
    }


@transaction.atomic
def correct_supplier_price_upload_item(*, upload_id, item_id, data):
    upload = get_object_or_404(SupplierPriceUpload.objects.select_for_update(), pk=upload_id)
    item = get_object_or_404(
        SupplierPriceUploadItem.objects.select_for_update(of=("self",)).select_related("parsed_unit"),
        pk=item_id,
        upload=upload,
    )
    if item.status not in {UploadItemStatus.NEEDS_REVIEW, UploadItemStatus.REJECTED}:
        raise ValidationError({"status": "Only needs_review or rejected rows can be corrected."})
    if item.row_number is None:
        raise ValidationError({"row_number": "Upload item is not linked to an extracted source row."})

    rows = _rows_for_upload(upload)
    source_row = next((row for row in rows if row.row_number == item.row_number), None)
    if source_row is None:
        raise ValidationError({"row_number": "Source row was not found in the upload payload."})

    cells = dict(source_row.cells)
    if "name" in data:
        cells = {
            key: value
            for key, value in cells.items()
            if not _matches_any_alias(key, PRODUCT_NAME_ALIASES | NAME_ALIASES)
        }
        cells["name"] = data["name"]
    if "price" in data:
        cells = {key: value for key, value in cells.items() if not _matches_any_alias(key, PRICE_ALIASES)}
        cells["price"] = str(data["price"])
    if "unit" in data:
        cells = {key: value for key, value in cells.items() if not _matches_any_alias(key, UNIT_ALIASES)}
        cells["unit"] = data["unit"].code

    original_canonical = ((item.raw_payload or {}).get("raw_payload") or {}).get("canonical")
    correction_source = dict(next(row.source for row in rows if row.row_number == item.row_number))
    if original_canonical:
        from file_imports.canonical import override_snapshot

        keys = {"unit": "unit_label"}
        correction_source["canonical_initial"] = override_snapshot(
            original_canonical,
            {keys.get(key, key): str(value) for key, value in data.items()},
            at=timezone.now().isoformat(),
        )
    corrected_rows = [
        type(row)(
            row_number=row.row_number,
            cells=cells if row.row_number == item.row_number else row.cells,
            source=correction_source if row.row_number == item.row_number else row.source,
        )
        for row in rows
    ]
    corrected_row = next(row for row in corrected_rows if row.row_number == item.row_number)
    unit_aliases = unit_code_aliases_from_units(active_unit_codes_and_names())
    parsed = parse_supplier_price_row(corrected_row, unit_aliases=unit_aliases)
    parsed_unit = data.get("unit") or _resolve_ai_unit(parsed)
    parsed_payload = _json_safe(
        {
            **parsed,
            "manual_correction": {
                "corrected_at": timezone.now().isoformat(),
                "fields": sorted(data.keys()),
            },
        }
    )

    item.raw_name = parsed.get("raw_name") or ""
    item.raw_unit = parsed.get("unit_code") or ""
    item.raw_price = str(parsed["price"]) if parsed.get("price") is not None else ""
    item.raw_payload = parsed_payload
    item.normalized_name = parsed.get("normalized_name") or ""
    item.parsed_price = parsed.get("price")
    item.parsed_unit = parsed_unit
    item.status = UploadItemStatus.NEEDS_REVIEW
    item.review_comment = "Исправлено вручную. Запустите повторную обработку загрузки."
    item.save(
        update_fields=[
            "raw_name",
            "raw_unit",
            "raw_price",
            "raw_payload",
            "normalized_name",
            "parsed_price",
            "parsed_unit",
            "status",
            "review_comment",
            "updated_at",
        ]
    )

    raw_payload = upload.raw_payload or {}
    raw_payload["extracted_rows"] = extracted_rows_to_payload(corrected_rows)
    normalization = raw_payload.get("normalization")
    if not isinstance(normalization, dict):
        normalization = {}
    normalization.pop("result", None)
    normalization["manual_corrections_pending"] = True
    raw_payload["normalization"] = normalization
    upload.raw_payload = raw_payload
    upload.status = UploadStatus.RECEIVED
    upload.processed_at = None
    upload.error_message = None
    upload.save(update_fields=["raw_payload", "status", "processed_at", "error_message", "updated_at"])
    return item


def _current_active_price_readonly(supplier_product_id):
    return (
        SupplierProductPrice.objects.filter(
            supplier_product_id=supplier_product_id,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        )
        .order_by("-valid_from", "-created_at")
        .first()
    )


def _price_matches_payload(*, active_price, product_data):
    if not active_price or product_data.get("price") is None:
        return False
    return active_price.price == product_data.get("price") and active_price.currency == _currency_value(product_data)


@transaction.atomic
def rollback_supplier_price_upload(*, upload_id):
    upload = get_object_or_404(
        SupplierPriceUpload.objects.select_for_update(),
        pk=upload_id,
    )

    if upload.status == UploadStatus.ARCHIVED:
        return {
            "upload_id": upload.id,
            "status": upload.status,
            "already_rolled_back": True,
            "products_archived": 0,
            "prices_reverted": 0,
            "skipped": 0,
        }

    products_archived = 0
    prices_reverted = 0
    skipped = 0
    now = timezone.now()

    items = (
        SupplierPriceUploadItem.objects.select_for_update()
        .select_related("supplier_product")
        .filter(
            upload=upload,
            status__in=[UploadItemStatus.NEW, UploadItemStatus.UPDATED],
            supplier_product__isnull=False,
        )
    )
    for item in items:
        supplier_product = item.supplier_product
        # A later upload may have since touched the same product; only this
        # upload's own changes should be undone, not anything applied after it.
        if supplier_product.last_upload_id != upload.id:
            skipped += 1
            continue

        if item.status == UploadItemStatus.NEW:
            archive_supplier_product(supplier_product_id=supplier_product.id)
            products_archived += 1
            continue

        old_price_raw = (item.raw_payload or {}).get("old_price")
        current_active = (
            SupplierProductPrice.objects.select_for_update()
            .filter(supplier_product=supplier_product, status=PriceStatus.ACTIVE, valid_to__isnull=True)
            .order_by("-valid_from", "-created_at")
            .first()
        )
        currency = current_active.currency if current_active else "RUB"
        SupplierProductPrice.objects.filter(
            supplier_product=supplier_product,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        ).update(status=PriceStatus.CANCELLED, valid_to=now)
        if old_price_raw is not None:
            SupplierProductPrice.objects.create(
                supplier_product=supplier_product,
                price_type=PriceType.PRICE_LIST,
                price=Decimal(old_price_raw),
                currency=currency,
                status=PriceStatus.ACTIVE,
                valid_from=now,
            )
            prices_reverted += 1
        sync_supplier_product_price_snapshot(supplier_product.id)

    upload.status = UploadStatus.ARCHIVED
    upload.processed_at = now
    upload.save(update_fields=["status", "processed_at", "updated_at"])

    return {
        "upload_id": upload.id,
        "status": upload.status,
        "already_rolled_back": False,
        "products_archived": products_archived,
        "prices_reverted": prices_reverted,
        "skipped": skipped,
    }
