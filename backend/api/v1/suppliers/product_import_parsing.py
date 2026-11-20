"""Supplier domain mapping after shared deterministic file extraction (ТЗ ANALYZE)."""

from __future__ import annotations

import re

from django.conf import settings

from api.v1.suppliers.product_import_net import net_from_name
from file_imports.extraction import (
    FileExtractionError,
    UnreadableSpreadsheetError,
    extract_rows_from_file,
)

_COLUMN_ALIASES = {
    "name": [
        "название",
        "наименование",
        "номенклатура",
        "товар",
        "name",
        "product name",
        "product",
        "item name",
        "item",
        "master product",
    ],
    "country_of_origin": ["страна", "country"],
    "unit_label": ["ед. изм", "единица", "ед изм", "unit", "uom"],
    "price": ["цена", "price"],
    "delivery_multiple": ["кратность"],
    "net_value": ["вес", "объём", "объем", "нетто", "net", "net weight", "net value"],
    "net_unit_label": ["ед. нетто", "единица нетто"],
    "min_order_qty": ["мин", "минимальное количество"],
    "availability_label": ["наличие", "статус"],
    "supplier_sku": ["артикул", "sku"],
    "barcode": ["штрихкод", "штрих код", "шк", "ean", "баркод", "barcode"],
    "pack_items_count": ["кол в упаковке", "кол во в упаковке", "количество в упаковке"],
    "package_composition": ["состав упаковки", "состав уп"],
}


# «Артикул производителя» (MPN) -- код изготовителя, не поставщика: одинаков у всех поставщиков товара, а
# supplier_sku -- ключ повторного импорта (replace по артикулу). Такую колонку не распознаём как supplier_sku.
_MANUFACTURER_CODE_MARKERS = ("производител", "изготовител", "manufacturer", "mpn", "завод")

# Заголовок из одного слова («Ед.») нельзя искать подстрокой ("ед" встретится в «среднее») --
# только целиком.
_EXACT_HEADER_ALIASES = {"ед": "unit_label", "изм": "unit_label"}

# 1С отдаёт «Название, кг» -- единица в хвосте названия; только коды, которые есть в core.Unit.
_TRAILING_UNIT = re.compile(r",\s*(кг|шт|л|г|мл)\.?\s*$", re.IGNORECASE)


class SupplierProductImportParseError(Exception):
    pass


def _max_import_rows():
    return int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_MAX_ROWS", 12000) or 12000)


_HEADER_ALIAS_GROUPS = tuple(set(aliases) for aliases in _COLUMN_ALIASES.values())


def _field_for_header_key(header_key):
    header_key = re.sub(r"[^\w]+", " ", str(header_key).casefold().replace("_", " ")).strip()
    if header_key in _EXACT_HEADER_ALIASES:
        return _EXACT_HEADER_ALIASES[header_key]
    if header_key in ("text", "name", "product name"):
        return "name"
    matches = [
        (len(normalized_alias), field)
        for field, aliases in _COLUMN_ALIASES.items()
        for alias in aliases
        if header_key == (normalized_alias := re.sub(r"[^\w]+", " ", alias.casefold()).strip())
        or header_key.startswith(normalized_alias + " ")
    ]
    if matches:
        field = max(matches)[1]
        if field == "supplier_sku" and any(marker in header_key for marker in _MANUFACTURER_CODE_MARKERS):
            return None
        return field
    return None


def _rows_from_raw_import_rows(raw_import_rows, *, canonical_enabled=None):
    """Map raw cells to supplier fields without AI or database writes."""
    if canonical_enabled is None:
        canonical_enabled = getattr(settings, "CANONICAL_SUPPLIER_PARSER_ENABLED", False)
    rows = []
    max_rows = _max_import_rows()
    mapping_cache = {}
    parsed_rows = []
    for raw_row in raw_import_rows:
        if canonical_enabled:
            from file_imports.canonical import parse_product_row
            from file_imports.llm_candidates import apply_mapping_provenance, mapping_candidates

            # Phase 1: map and parse every row (no per-row LLM calls).
            llm_mapping = mapping_candidates(raw_row, _field_for_header_key, mapping_cache)
            parsed = parse_product_row(
                raw_row, lambda key, llm_mapping=llm_mapping: _field_for_header_key(key) or llm_mapping.get(key)
            )
            parsed_rows.append(apply_mapping_provenance(parsed, llm_mapping))
            continue
        by_field = {}
        for header_key, value in raw_row.cells.items():
            field = _field_for_header_key(header_key)
            if field and field not in by_field:
                by_field[field] = value
        if raw_row.source.get("headerless"):
            by_field.setdefault("name", raw_row.cells.get(raw_row.source.get("fallback_name_column", "column_1")))
        name = by_field.get("name")
        if not name:
            continue
        if not by_field.get("unit_label"):
            trailing_unit = _TRAILING_UNIT.search(name)
            if trailing_unit:
                by_field["unit_label"] = trailing_unit.group(1).lower()
        if not by_field.get("net_value") and not by_field.get("net_unit_label"):
            net = net_from_name(name)
            if net is not None:
                # format(..., "f") -- без экспоненты: Decimal("250").normalize() печатается как "2.5E+2"
                by_field["net_value"] = format(net[0].normalize(), "f")
                by_field["net_unit_label"] = net[1]
        rows.append(
            {
                "row_number": raw_row.row_number,
                "name": name,
                "country_of_origin": by_field.get("country_of_origin"),
                "unit_label": by_field.get("unit_label"),
                "price": by_field.get("price"),
                "delivery_multiple": by_field.get("delivery_multiple"),
                "net_value": by_field.get("net_value"),
                "net_unit_label": by_field.get("net_unit_label"),
                "min_order_qty": by_field.get("min_order_qty"),
                "availability_label": by_field.get("availability_label"),
                "supplier_sku": by_field.get("supplier_sku"),
                "package_composition": by_field.get("package_composition"),
            }
        )
    if parsed_rows:
        from file_imports.adapters import supplier_row
        from file_imports.llm_candidates import enrich_candidates_batch

        # Phase 2: one bounded batch pass over all rows that still need fields.
        product_rows = [parsed for parsed in parsed_rows if parsed.row_type in ("product", "possible_product")]
        enrich_candidates_batch(
            product_rows,
            request_budget=[getattr(settings, "CANONICAL_IMPORT_LLM_MAX_REQUESTS", 40)],
            batch_size=getattr(settings, "CANONICAL_IMPORT_LLM_BATCH_SIZE", 12),
        )
        # Phase 3: supplier rows for product-like rows.
        rows.extend(supplier_row(parsed) for parsed in product_rows)
    if len(rows) > max_rows:
        raise SupplierProductImportParseError(f"Слишком много строк. Максимум — {max_rows}.")
    disambiguate_duplicate_names(rows)
    from file_imports.diagnostics import log_canonical_rows

    log_canonical_rows(rows)
    return rows


def disambiguate_duplicate_names(rows):
    """Cleaned names that collide while the source names differ get their measure back (canonical rows only)."""
    groups = {}
    for row in rows:
        if "_raw_name" in row:
            groups.setdefault(row["name"].casefold(), []).append(row)
    if all(len(group) < 2 for group in groups.values()):
        return
    from file_imports.canonical import name_with_measure

    for group in groups.values():
        if len({row["_raw_name"] for row in group}) > 1:
            for row in group:
                row["name"] = name_with_measure(row["_raw_name"])


class ParsedImportRows(list):
    """Parsed supplier rows plus file-level warnings from extraction (e.g. skipped sheets)."""

    def __init__(self, rows=(), *, warnings=()):
        super().__init__(rows)
        self.warnings = list(warnings)


def parse_supplier_product_import_file(uploaded_file, *, canonical_enabled=None):
    name = (getattr(uploaded_file, "name", "") or "").lower()
    try:
        raw_import_rows = extract_rows_from_file(
            uploaded_file, header_alias_groups=_HEADER_ALIAS_GROUPS, source_file_name=name
        )
    except (FileExtractionError, UnreadableSpreadsheetError) as error:
        raise SupplierProductImportParseError(str(error)) from error
    except Exception as error:
        raise SupplierProductImportParseError(
            "Не удалось прочитать файл. Проверьте формат и целостность файла."
        ) from error
    if not raw_import_rows:
        raise SupplierProductImportParseError("Файл пуст.")
    rows = _rows_from_raw_import_rows(raw_import_rows, canonical_enabled=canonical_enabled)
    if not rows:
        raise SupplierProductImportParseError("Не найдена колонка с названием товара.")
    return ParsedImportRows(rows, warnings=getattr(raw_import_rows, "metadata", {}).get("warnings", []))
