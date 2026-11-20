"""Общая запись строк preview в CatalogImportSession (Master и Venue). Вынесено из views.py, чтобы фоновые
Celery-задачи использовали ровно тот же код, что и синхронные ветки."""

from django.conf import settings
from django.utils.dateparse import parse_datetime

from catalog.models import CatalogImportItem, CatalogImportItemAction, CatalogImportSession
from file_imports.extraction import extractor_name_for_file
from file_imports.services import import_file_storage_enabled
from file_imports.versions import parser_version_for


def catalog_import_always_async():
    """True -- preview Мастера/заведения идёт в воркере при любом размере файла (флаг + сохранение файла)."""
    return bool(getattr(settings, "CATALOG_IMPORT_ALWAYS_ASYNC", False)) and import_file_storage_enabled()


def catalog_import_async_extraction(uploaded_file):
    return import_file_storage_enabled() and (
        catalog_import_always_async()
        or (
            getattr(settings, "CATALOG_IMPORT_ASYNC_EXTRACTION_ENABLED", False)
            and extractor_name_for_file(getattr(uploaded_file, "name", ""))
            in {"docling_pdf", "docx", "txt", "image_ocr"}
        )
    )


def _expected_updated_at(row):
    value = row.get("matrix_item_updated_at") or row.get("master_product_updated_at")
    return parse_datetime(value) if isinstance(value, str) else value


def create_catalog_import_items(session, rows, *, start_index=1, parsed_rows=None):
    snapshots = {row["row_number"]: row["_parsed"] for row in (parsed_rows or []) if row.get("_parsed")}
    items = [
        CatalogImportItem(
            session=session,
            row_number=row.get("row_number") or index,
            raw_values=dict(row),
            parsed_snapshot=snapshots.get(row.get("row_number"), {}),
            normalized_values=dict(row),
            candidates=row.get("candidates") or [],
            action=row.get("action") or CatalogImportItemAction.REVIEW,
            target_id=row.get("matrix_item_id") or row.get("master_product_id"),
            target_expected_updated_at=_expected_updated_at(row),
        )
        for index, row in enumerate(rows, start=start_index)
    ]
    CatalogImportItem.objects.bulk_create(items)
    return items


def persist_catalog_import_session(
    *, owner, domain, source_file_name, rows, venue=None, import_file=None, meta=None, parsed_rows=None
):
    """Сохраняет строки preview, не меняя записи справочника."""
    session = CatalogImportSession.objects.create(
        owner=owner,
        venue=venue,
        domain=domain,
        source_file_name=source_file_name or "",
        import_file=import_file,
        meta=meta or {},
        parser_version=parser_version_for(
            getattr(settings, "CANONICAL_" + ("MASTER" if domain == "master" else "VENUE") + "_PARSER_ENABLED", False)
        ),
    )
    create_catalog_import_items(session, rows, parsed_rows=parsed_rows)
    return session


def build_venue_preview_rows(parsed_rows, matches):
    return [
        {
            "row_number": source_row["row_number"],
            "raw_name": source_row["name"],
            "requested_unit_label": source_row.get("unit"),
            "unit_id": match.unit_id,
            "unit_unrecognized": match.unit_unrecognized,
            "unit_source": match.unit_source,
            "note": source_row.get("note"),
            "action": match.action,
            "candidates": match.candidates,
            "matrix_item_id": match.matrix_item_id,
            "matrix_item_requested_name": match.matrix_item_requested_name,
            "matrix_item_updated_at": match.matrix_item_updated_at,
            "auto_resolved": match.auto_resolved,
        }
        for source_row, match in zip(parsed_rows, matches, strict=True)
    ]


def prepare_venue_import_matching(session):
    """Batch embeddings before the commit locks; apply rechecks candidate versions."""
    from django.db import connection

    from api.v1.catalog.embeddings import create_embeddings
    from api.v1.catalog.selectors import _ensure_master_product_embeddings, _venue_sole_active_owner_id
    from core.models import User

    owner_id = _venue_sole_active_owner_id(session.venue_id)
    if owner_id is None:
        return None
    names = list(
        dict.fromkeys(
            (item.normalized_values or item.raw_values).get("raw_name", "")
            for item in session.items.filter(action="create_new", applied_at__isnull=True)
        )
    )
    if not names:
        return None
    _ensure_master_product_embeddings(User.objects.get(pk=owner_id))
    vectors = create_embeddings(names)
    prepared = {
        "owner_id": owner_id,
        "rows": {name: {"vector": vector, "candidates": []} for name, vector in zip(names, vectors, strict=True)},
        "created": [],
    }
    literals = ["[" + ",".join(str(value) for value in vector) + "]" for vector in vectors]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT q.name, m.id, m.updated_at, m.score "
            "FROM unnest(%s::text[], %s::text[]) AS q(name, vector) CROSS JOIN LATERAL ("
            "SELECT id, updated_at, 1 - (embedding <=> q.vector::vector) AS score FROM master_products "
            "WHERE owner_id=%s AND status='active' AND embedding IS NOT NULL "
            "ORDER BY embedding <=> q.vector::vector, id LIMIT 5) AS m",
            [names, literals, owner_id],
        )
        for name, identifier, updated_at, score in cursor.fetchall():
            prepared["rows"][name]["candidates"].append((identifier, updated_at, score))
    return prepared


def record_catalog_receipt(session, item):
    from file_imports.models import ImportRowReceipt

    if not item.parsed_snapshot or item.action == "skip":
        return
    target_id = (
        item.apply_result.get("master_product_id")
        if session.domain == "master"
        else item.apply_result.get("matrix_item_id")
    )
    target_id = target_id or item.target_id
    if target_id is None:
        return
    ImportRowReceipt.objects.get_or_create(
        source_item_id=item.id,
        defaults={
            "source_session_id": session.id,
            "scope_type": "venue" if session.venue_id else "owner",
            "scope_id": session.venue_id or session.owner_id,
            "target_type": "master_product" if session.domain == "master" else "venue_matrix_item",
            "target_id": target_id,
            "parser_version": item.parsed_snapshot["parser_version"],
            "snapshot": item.parsed_snapshot,
        },
    )
