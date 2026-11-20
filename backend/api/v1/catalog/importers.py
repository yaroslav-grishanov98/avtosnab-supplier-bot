import re
from dataclasses import dataclass

from django.conf import settings
from rest_framework.exceptions import ValidationError

from file_imports.extraction import FileExtractionError, UnreadableSpreadsheetError, extract_rows_from_file

MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
_FIELD_ALIASES = {
    "name": {
        "название",
        "название товара",
        "наименование",
        "наименование товара",
        "товар",
        "номенклатура",
        "мастер товар",
        "product",
        "product name",
        "master product",
        "item",
        "item name",
        "name",
    },
    "unit": {
        "ед",
        "ед изм",
        "единица",
        "единица измерения",
        "единица изм",
        "unit",
        "uom",
    },
    "net": {
        "нетто",
        "вес объем нетто",
        "вес нетто",
        "объем нетто",
        "net",
        "net value",
        "net weight",
    },
    "note": {
        "примечание",
        "описание",
        "заметка",
        "note",
        "notes",
        "description",
    },
}


def _normalize_header(value):
    text = str(value or "").strip().casefold().replace("ё", "е").replace("_", " ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


_NORMALIZED_ALIASES = {
    field: {_normalize_header(alias) for alias in aliases} for field, aliases in _FIELD_ALIASES.items()
}

# Accounting-system (УС) exports commonly qualify column headers with a
# trailing "в УС" (e.g. "Наименование товара в УС"), which no alias above
# matches verbatim. Stripping just this one recognized suffix before
# comparing against the alias set — rather than switching to substring
# matching everywhere — keeps the false-positive risk of header detection
# unchanged for every other header shape.
_SOURCE_SYSTEM_SUFFIX_RE = re.compile(r"\s+в\s+ус$")

_FILE_IMPORT_HEADER_ALIASES = (
    set(_FIELD_ALIASES["name"]),
    set(_FIELD_ALIASES["unit"]),
    set(_FIELD_ALIASES["net"]),
    set(_FIELD_ALIASES["note"]),
)


def _strip_source_system_suffix(normalized_header):
    return _SOURCE_SYSTEM_SUFFIX_RE.sub("", normalized_header)


@dataclass(frozen=True)
class ParsedCatalogFile:
    rows: list[dict]
    detected_columns: dict[str, str]
    header_row_number: int | None
    sheet_name: str | None = None
    rows_without_name: int = 0


def _field_for_header(header):
    normalized = _strip_source_system_suffix(_normalize_header(header))
    if normalized == "text":
        return "name"
    matches = [
        (len(alias), field)
        for field, aliases in _NORMALIZED_ALIASES.items()
        for alias in aliases
        if normalized == alias or normalized.startswith(alias + " ")
    ]
    return max(matches)[1] if matches else None


def parse_catalog_file(uploaded_file, *, max_rows=MAX_IMPORT_ROWS, canonical_enabled=None):
    if canonical_enabled is None:
        canonical_enabled = getattr(settings, "CANONICAL_MASTER_PARSER_ENABLED", False)
    try:
        raw_rows = extract_rows_from_file(uploaded_file, header_alias_groups=_FILE_IMPORT_HEADER_ALIASES)
    except (FileExtractionError, UnreadableSpreadsheetError) as error:
        raise ValidationError({"file": str(error)}) from error
    except Exception as error:
        raise ValidationError({"file": "Не удалось прочитать файл. Проверьте формат и целостность файла."}) from error
    metadata = getattr(raw_rows, "metadata", {})
    display = {}
    for key, label in metadata.get("header_columns", {}).items():
        field = _field_for_header(key)
        if field:
            display.setdefault(field, label)
    rows_without_name = metadata.get("blank_rows", 0)
    parsed = []
    llm_budget = [20]
    mapping_cache = {}
    for row in raw_rows:
        if canonical_enabled:
            from file_imports.adapters import catalog_row
            from file_imports.canonical import parse_product_row

            def canonical_header(key):
                mapped = _field_for_header(key)
                return {"unit": "unit_label", "net": "net_value"}.get(mapped, mapped)

            from file_imports.llm_candidates import apply_mapping_provenance, enrich_candidates, mapping_candidates

            llm_mapping = mapping_candidates(row, canonical_header, mapping_cache)
            canonical = parse_product_row(row, lambda key: canonical_header(key) or llm_mapping.get(key))
            canonical = apply_mapping_provenance(canonical, llm_mapping)
            canonical = enrich_candidates(canonical, request_budget=llm_budget)
            if canonical.row_type not in ("product", "possible_product"):
                rows_without_name += 1
                continue
            parsed.append(catalog_row(canonical))
            if len(parsed) > max_rows:
                raise ValidationError({"file": f"Слишком много строк. Максимум — {max_rows}."})
            continue
        item = {"row_number": row.row_number}
        for key, value in row.cells.items():
            field = _field_for_header(key)
            if field:
                item.setdefault(field, value)
        if row.source.get("headerless"):
            key = row.source.get("fallback_name_column", "column_1")
            if row.cells.get(key):
                item.setdefault("name", row.cells[key])
                display.setdefault("name", key)
        if not item.get("name"):
            rows_without_name += 1
            continue
        parsed.append(item)
        if len(parsed) > max_rows:
            raise ValidationError({"file": f"Слишком много строк. Максимум — {max_rows}."})
    if not parsed:
        raise ValidationError({"file": "Не найдено ни одной строки с названием товара."})
    from file_imports.diagnostics import log_canonical_rows

    log_canonical_rows(parsed)
    return ParsedCatalogFile(
        rows=parsed,
        detected_columns=display,
        header_row_number=metadata.get("header_row_number"),
        sheet_name=metadata.get("sheet_name"),
        rows_without_name=rows_without_name,
    )
