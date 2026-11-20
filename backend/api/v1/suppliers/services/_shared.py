"""Helpers shared by several supplier service modules."""

import logging
from decimal import Decimal
from uuid import UUID

from django.utils import timezone

from api.v1.suppliers.normalization import (
    extract_price_rows_from_stored_file,
    extracted_rows_from_payload,
    extracted_rows_from_text,
    extracted_rows_to_payload,
    extractor_name_for_file,
)
from core.models import (
    EntityStatus,
    Unit,
)

logger = logging.getLogger("autosnab.suppliers.services")


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value


def _rows_for_upload(upload):
    raw_payload = upload.raw_payload or {}
    kind = raw_payload.get("kind")
    if kind == "file":
        rows = extracted_rows_from_payload(raw_payload.get("extracted_rows", []))
        if rows:
            return rows
        source_file_uri = upload.source_file_uri or (raw_payload.get("metadata") or {}).get("source_file_uri")
        if not source_file_uri:
            return []
        rows = extract_price_rows_from_stored_file(
            source_file_uri=source_file_uri,
            source_file_name=upload.source_file_name,
        )
        _store_extracted_rows_for_upload(upload=upload, rows=rows, source_file_uri=source_file_uri)
        return rows
    if kind == "text":
        rows = extracted_rows_from_payload(raw_payload.get("extracted_rows", []))
        return rows or extracted_rows_from_text(raw_payload.get("text", ""))
    return []


def _store_extracted_rows_for_upload(*, upload, rows, source_file_uri):
    raw_payload = upload.raw_payload or {}
    raw_payload["extracted_rows"] = extracted_rows_to_payload(rows)
    raw_payload["extraction"] = {
        **(raw_payload.get("extraction") or {}),
        "rows_total": len(rows),
        "source": "backend",
        "extractor": extractor_name_for_file(upload.source_file_name or source_file_uri),
        "deferred": False,
        "extracted_at": timezone.now().isoformat(),
    }
    metadata = raw_payload.get("metadata") if isinstance(raw_payload.get("metadata"), dict) else {}
    metadata["source_file_uri"] = source_file_uri
    raw_payload["metadata"] = metadata
    upload.raw_payload = raw_payload
    upload.save(update_fields=["raw_payload", "updated_at"])


def _resolve_ai_unit(product_data):
    unit_id = product_data.get("unit_id")
    if unit_id:
        return Unit.objects.filter(id=unit_id, status=EntityStatus.ACTIVE).first()

    unit_code = product_data.get("unit_code")
    if unit_code:
        return Unit.objects.filter(code=unit_code, status=EntityStatus.ACTIVE).first()

    return None


def _currency_value(product_data):
    currency = (product_data.get("currency") or "RUB").upper()
    return currency[:3]
