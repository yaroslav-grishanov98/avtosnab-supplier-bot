import json
import logging
import os
import re
import socket
import time
import urllib.error
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings

from api.v1.suppliers.package_composition import parse_package_composition
from file_imports import extraction as shared_extraction

NORMALIZATION_SCHEMA_VERSION = "supplier_price_ai.v1"
NORMALIZATION_PROCESSING_VERSION = "backend-normalizer-2026-06-18"
PARSER_VERSION = "rules-2026-06-18"
AI_CONFIDENCE_THRESHOLD = Decimal("0.8000")
HEADER_DETECTION_SCAN_ROWS = 10

AI_NORMALIZATION_URL = "https://api.artemox.com/v1/chat/completions"
AI_NORMALIZATION_MODEL = "gpt-4o-mini"
AI_CHUNK_SIZE = 20
AI_TIMEOUT_SECONDS = 110
AI_MAX_RETRIES = 0
AI_RETRY_BACKOFF_SECONDS = 0.5

AI_MODE_FULL = "full"
AI_MODE_AMBIGUOUS = "ambiguous"
AI_MODE_OFF = "off"
AI_MODES = {AI_MODE_FULL, AI_MODE_AMBIGUOUS, AI_MODE_OFF}

AI_ERROR_CREDENTIALS_NOT_CONFIGURED = "credentials_not_configured"
AI_ERROR_UNAUTHORIZED_401 = "unauthorized_401"
AI_ERROR_FORBIDDEN_403 = "forbidden_403"
AI_ERROR_BAD_REQUEST_400 = "bad_request_400"
AI_ERROR_RATE_LIMIT_429 = "rate_limit_429"
AI_ERROR_TIMEOUT = "timeout"
AI_ERROR_NETWORK = "network_error"
AI_ERROR_INVALID_JSON = "invalid_json"
AI_ERROR_EMPTY_RESPONSE = "empty_response"

logger = logging.getLogger("autosnab.suppliers.normalization")


NAME_ALIASES = {
    "name",
    "product",
    "product_name",
    "товар",
    "наименование",
    "название",
    "номенклатура",
    "описание",
}
PRODUCT_NAME_ALIASES = {
    "name",
    "product_name",
    "наименование товара",
    "название товара",
    "номенклатура",
    "описание",
}
CATEGORY_ALIASES = {
    "вид товар",
    "вид товара",
    "группа товар",
    "группа товара",
    "подгруппа товар",
    "подгруппа товара",
    "category",
    "category_name",
}
# 2026-08-14: per-level breakdown of CATEGORY_ALIASES, in canonical
# группа -> подгруппа -> вид order — used only by _category_path() below to
# always emit category_path in this fixed order, regardless of which column
# order a supplier's source file happens to use. Generic "category"/
# "category_name" (no level of their own) are treated as the most specific
# (вид) level, since a single flat category column is usually the leaf.
CATEGORY_LEVEL_ALIASES = (
    {"группа товар", "группа товара"},
    {"подгруппа товар", "подгруппа товара"},
    {"вид товар", "вид товара", "category", "category_name"},
)
PRICE_ALIASES = {"price", "цена", "стоимость", "прайс", "руб", "руб.", "цена руб", "цена, руб"}
UNIT_ALIASES = {"unit", "ед", "ед.", "единица", "единица измерения", "изм", "uom"}
SKU_ALIASES = {"sku", "артикул", "код", "код товара", "supplier_sku"}
MIN_ORDER_ALIASES = {"мин заказ", "минимальный заказ", "min_order", "min order", "min_order_qty"}
MULTIPLICITY_ALIASES = {"кратность", "multiplicity", "кол-во в коробке", "количество в коробке"}
# TASK-111: «Состав упаковки» — «8 шт. × 2 кг = 16 кг/кор.», см. package_composition.py.
PACKAGE_COMPOSITION_ALIASES = {"состав упаковки", "состав уп", "package_composition"}
HEADER_ALIAS_GROUPS = (
    NAME_ALIASES,
    PRICE_ALIASES,
    UNIT_ALIASES,
    SKU_ALIASES,
    MIN_ORDER_ALIASES,
    MULTIPLICITY_ALIASES,
    PACKAGE_COMPOSITION_ALIASES,
)

UNIT_ALIASES_MAP = {
    "шт": "шт",
    "шт.": "шт",
    "штука": "шт",
    "штуки": "шт",
    "pcs": "шт",
    "pc": "шт",
    "кг": "кг",
    "kg": "кг",
    "килограмм": "кг",
    "килограммы": "кг",
    "г": "г",
    "гр": "г",
    "гр.": "г",
    "g": "г",
    "gram": "г",
    "л": "л",
    "l": "л",
    "литр": "л",
    "мл": "мл",
    "ml": "мл",
    "упак": "уп",
    "упак.": "уп",
}

UNIT_PATTERN = r"кг|kg|г|гр\.?|g|л|l|мл|ml|шт\.?|pcs|pc"
SIZE_PATTERN = re.compile(rf"(?<!\w)(\d+(?:[.,]\d+)?)\s*({UNIT_PATTERN})(?!\w)", re.IGNORECASE)
RATIO_PATTERN = re.compile(r"(?<!\d)(\d{1,4})\s*/\s*(\d{1,4})(?!\d)")
BRAND_PATTERN = re.compile(r'(?:\bТМ\b|\bTM\b|бренд)\s*["«]?([^",;»]+)["»]?', re.IGNORECASE)
PRICE_WITH_CURRENCY_PATTERN = re.compile(
    r"(?<!\d)(\d{1,9}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:рубл[а-яё]*\b|руб\.?\b|₽|rub\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExtractedRow:
    row_number: int
    cells: dict[str, str]
    source: dict = dataclass_field(default_factory=dict)


def _clean_text(value, *, max_length=None):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:max_length]


def _normalize_key(value):
    value = _clean_text(value, max_length=120).lower().replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" :;.,")


def _detect_header(non_empty_rows):
    return shared_extraction._detect_header(
        non_empty_rows, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extractor_name_for_file(file_name):
    return shared_extraction.extractor_name_for_file(file_name)


def extract_price_rows_from_file(source_file, *, source_file_uri=None, source_file_name=None):
    return shared_extraction.extract_rows_from_file(
        source_file,
        header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS,
        source_file_uri=source_file_uri,
        source_file_name=source_file_name,
    )


def extract_price_rows_from_stored_file(*, source_file_uri, source_file_name=None):
    return shared_extraction.extract_rows_from_stored_file(
        source_file_uri=source_file_uri,
        source_file_name=source_file_name,
        header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS,
    )


def extract_price_rows_from_csv(content):
    return shared_extraction.extract_rows_from_csv(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extract_price_rows_from_xlsx(content):
    return shared_extraction.extract_rows_from_xlsx(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extract_price_rows_from_docx(content):
    return shared_extraction.extract_rows_from_docx(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extract_price_rows_from_txt(content):
    return shared_extraction.extract_rows_from_txt(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extract_price_rows_from_markdown_text(content):
    return shared_extraction.extract_rows_from_markdown_text(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extract_price_rows_from_pdf_with_pdfplumber(content):
    return shared_extraction.extract_rows_from_pdf_with_pdfplumber(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS
    )


def extract_price_rows_from_pdf(content, *, source_file_uri=None):
    return shared_extraction.extract_rows_from_pdf(
        content, header_alias_groups=shared_extraction.COMMON_HEADER_ALIAS_GROUPS, source_file_uri=source_file_uri
    )


def extract_price_rows_from_image(content):
    return shared_extraction.extract_rows_from_image(content)


def extracted_rows_to_payload(rows):
    return shared_extraction.raw_rows_to_payload(rows)


def extracted_rows_from_payload(payload_rows):
    rows = shared_extraction.raw_rows_from_payload(payload_rows)
    # Old snapshots may contain a header serialized as column_N. Newly
    # extracted rows already have common headers and explicit provenance.
    return rows if any(row.source for row in rows) else _remap_legacy_column_rows(rows)


def _remap_legacy_column_rows(rows):
    if not rows:
        return rows
    if not all(_is_column_key(key) for key in rows[0].cells):
        return rows

    scan_limit = min(HEADER_DETECTION_SCAN_ROWS, len(rows))
    header_index, headers = None, None
    for row_index in range(scan_limit):
        values = _ordered_column_values(rows[row_index].cells)
        has_header = any(
            _matches_any_alias(cell, aliases) for cell in values if cell for aliases in HEADER_ALIAS_GROUPS
        )
        if has_header:
            header_index = row_index
            headers = shared_extraction._dedupe_headers(
                [_normalize_key(cell) or f"column_{index + 1}" for index, cell in enumerate(values)]
            )
            break

    if header_index is None:
        return rows

    remapped = []
    for row in rows[header_index + 1 :]:
        values = _ordered_column_values(row.cells)
        cells = {}
        for index, header in enumerate(headers):
            value = _clean_text(values[index] if index < len(values) else "")
            if value:
                cells[header] = value
        if cells:
            remapped.append(ExtractedRow(row_number=row.row_number, cells=cells))
    return remapped


def _is_column_key(value):
    return bool(re.fullmatch(r"column_\d+", _normalize_key(value)))


def _ordered_column_values(cells):
    def column_index(item):
        key, _value = item
        match = re.fullmatch(r"column_(\d+)", _normalize_key(key))
        return int(match.group(1)) if match else 0

    return [value for _key, value in sorted(cells.items(), key=column_index)]


def extracted_rows_from_text(text):
    rows = []
    for index, line in enumerate(str(text or "").splitlines(), start=1):
        clean_line = _clean_text(line, max_length=1000)
        if clean_line:
            rows.append(ExtractedRow(row_number=index, cells={"text": clean_line}))
    if not rows and _clean_text(text):
        rows.append(ExtractedRow(row_number=1, cells={"text": _clean_text(text, max_length=1000)}))
    return rows


def _resolve_ai_mode(*, use_ai, ai_mode):
    if not use_ai or not _ai_enabled():
        return AI_MODE_OFF
    if not ai_mode:
        ai_mode = _ai_default_mode()
    if ai_mode not in AI_MODES:
        raise ValueError(f"Unsupported ai_mode: {ai_mode}")
    return ai_mode


def _empty_ai_stats():
    return {
        "ai_chunks_total": 0,
        "ai_chunks_succeeded": 0,
        "ai_chunks_failed": 0,
        "ai_retries": 0,
        "ai_errors": [],
        "ai_error_codes": [],
        "resolved_row_numbers": set(),
    }


def build_normalized_payload(*, upload, rows, use_ai=True, ai_mode=None, confirm_full=False, progress_callback=None):
    ai_mode = _resolve_ai_mode(use_ai=use_ai, ai_mode=ai_mode)
    if getattr(settings, "CANONICAL_LEGACY_PARSER_ENABLED", False):
        # Canonical field fallback is bounded and evidence-checked inside its adapter.
        ai_mode = AI_MODE_OFF
    logger.info(
        "supplier_price_normalization.python_parse.start upload_id=%s supplier_id=%s rows=%s ai_mode=%s",
        upload.id,
        upload.supplier_id,
        len(rows),
        ai_mode,
    )
    unit_rows = active_unit_codes_and_names()
    unit_aliases = unit_code_aliases_from_units(unit_rows)
    if getattr(settings, "CANONICAL_LEGACY_PARSER_ENABLED", False):
        from file_imports.legacy_adapter import parse_legacy_row

        llm_budget = [20] if use_ai else [0]
        parsed_products = [parse_legacy_row(row, unit_aliases=unit_aliases, llm_budget=llm_budget) for row in rows]
    else:
        parsed_products = [parse_supplier_price_row(row, unit_aliases=unit_aliases) for row in rows]
    ambiguous_products = [product for product in parsed_products if _needs_ai_fallback(product)]
    logger.info(
        "supplier_price_normalization.python_parse.finish upload_id=%s rows_rules_parsed=%s rows_ambiguous=%s",
        upload.id,
        len(parsed_products),
        len(ambiguous_products),
    )

    products_for_ai = []
    parser_source = "rules"
    if ai_mode == AI_MODE_FULL:
        products_for_ai = parsed_products
        parser_source = "ai_full_pass"
    elif ai_mode == AI_MODE_AMBIGUOUS:
        products_for_ai = ambiguous_products
        parser_source = "ai_fallback"

    _validate_ai_workload(
        ai_mode=ai_mode, rows_total=len(rows), rows_ai_requested=len(products_for_ai), confirm_full=confirm_full
    )

    ai_products = []
    ai_stats = _empty_ai_stats()

    if ai_mode != AI_MODE_OFF and products_for_ai:
        ai_products, ai_stats = normalize_products_with_ai(
            upload=upload,
            rows=rows,
            products=products_for_ai,
            parser_source=parser_source,
            unit_codes=[code for code, _name in unit_rows],
            unit_aliases=unit_aliases,
            progress_callback=progress_callback,
        )

    products_by_row = {int(product["row_number"]): product for product in parsed_products}
    for product in ai_products:
        row_number = int(product["row_number"])
        products_by_row[row_number] = product

    if ai_stats["ai_errors"]:
        requested_row_numbers = {int(product["row_number"]) for product in products_for_ai}
        warning_code = "ai_full_pass_failed" if ai_mode == AI_MODE_FULL else "ai_fallback_failed"
        for row_number, product in products_by_row.items():
            if row_number in requested_row_numbers and row_number not in ai_stats["resolved_row_numbers"]:
                warnings = product.setdefault("warnings", [])
                warnings.append(warning_code)
                product["comment"] = "; ".join([product.get("comment") or "", *ai_stats["ai_errors"]]).strip("; ")

    products = [products_by_row[row.row_number] for row in rows if row.row_number in products_by_row]
    diagnostics = {
        "ai_mode": ai_mode,
        "parser_version": PARSER_VERSION,
        "processing_version": NORMALIZATION_PROCESSING_VERSION,
        "rows_total": len(rows),
        "rows_rules_parsed": len(parsed_products),
        "rows_ambiguous": len(ambiguous_products),
        "rows_ai_requested": len(products_for_ai),
        "rows_ai_resolved": len(ai_products),
        "rows_ai_failed": max(len(products_for_ai) - len(ai_products), 0),
        "ai_chunk_size": _ai_chunk_size(),
        "ai_chunks_total": ai_stats["ai_chunks_total"],
        "ai_chunks_succeeded": ai_stats["ai_chunks_succeeded"],
        "ai_chunks_failed": ai_stats["ai_chunks_failed"],
        "ai_retries": ai_stats["ai_retries"],
        "ai_errors": ai_stats["ai_errors"],
        "ai_error_codes": ai_stats["ai_error_codes"],
    }
    logger.info(
        "supplier_price_normalization.payload.finish upload_id=%s rows_total=%s ai_mode=%s rows_ai_requested=%s rows_ai_resolved=%s ai_chunks_total=%s ai_chunks_failed=%s",
        upload.id,
        diagnostics["rows_total"],
        ai_mode,
        diagnostics["rows_ai_requested"],
        diagnostics["rows_ai_resolved"],
        diagnostics["ai_chunks_total"],
        diagnostics["ai_chunks_failed"],
    )
    return {
        "schema_version": NORMALIZATION_SCHEMA_VERSION,
        "processing_version": NORMALIZATION_PROCESSING_VERSION,
        "supplier_id": upload.supplier_id,
        "upload_id": upload.id,
        "source_channel": upload.source_channel,
        "supplier_name": upload.supplier.display_name,
        "products": products,
    }, diagnostics


def parse_supplier_price_row(row, *, unit_aliases=None):
    if getattr(settings, "CANONICAL_LEGACY_PARSER_ENABLED", False):
        from file_imports.legacy_adapter import parse_legacy_row

        return parse_legacy_row(row, unit_aliases=unit_aliases)
    cells = {_normalize_key(key): _clean_text(value) for key, value in row.cells.items() if _clean_text(value)}
    line = _build_line(cells)
    name_value = (
        _first_alias_value(cells, PRODUCT_NAME_ALIASES)
        or _first_alias_value(cells, NAME_ALIASES, excluded_keys=CATEGORY_ALIASES)
        or _infer_name_from_cells(cells)
    )
    raw_name = name_value or line

    explicit_price = _normalize_money(_first_alias_value(cells, PRICE_ALIASES))
    price = explicit_price if explicit_price is not None else _extract_price_from_text(line)
    explicit_unit = _first_alias_value(cells, UNIT_ALIASES)
    unit_code = (
        normalize_unit_code(explicit_unit, aliases=unit_aliases)
        or _extract_sale_unit(raw_name)
        or _extract_sale_unit(line)
    )
    supplier_sku = _first_alias_value(cells, SKU_ALIASES)
    brand = _extract_brand(raw_name)
    package_info, weight, weight_unit, volume, volume_unit = _extract_package(raw_name)
    ratio_raw, multiplicity_from_ratio = _extract_multiplicity_ratio(raw_name)
    if ratio_raw:
        package_info = "; ".join(part for part in (package_info, ratio_raw) if part)
    min_order_qty = _normalize_quantity(_first_alias_value(cells, MIN_ORDER_ALIASES))
    multiplicity = _normalize_quantity(_first_alias_value(cells, MULTIPLICITY_ALIASES)) or multiplicity_from_ratio
    package_composition_raw = _first_alias_value(cells, PACKAGE_COMPOSITION_ALIASES)
    pack = (
        parse_package_composition(
            package_composition_raw,
            unit_aliases=unit_aliases or UNIT_ALIASES_MAP,
            normalize_unit_code=normalize_unit_code,
        )
        if package_composition_raw
        else None
    ) or {}

    normalized_name = _normalize_product_name(
        raw_name,
        brand=brand,
        package_info=package_info,
        ratio_raw=ratio_raw,
        unit_code=unit_code,
        price=price,
    )
    warnings = _warnings_for_product(
        raw_name=raw_name,
        normalized_name=normalized_name,
        price=price,
        unit_code=unit_code,
    )
    confidence = Decimal("0.9500") if not warnings else Decimal("0.6500")
    if warnings == ["price_unknown"]:
        confidence = Decimal("0.7600")

    raw_payload = {
        "row_number": row.row_number,
        "cells": cells,
        "parser_source": "rules",
        "parser_version": PARSER_VERSION,
    }
    if ratio_raw:
        raw_payload["package_ratio_raw"] = ratio_raw
    if package_composition_raw:
        raw_payload["package_composition_raw"] = package_composition_raw

    product = {
        "row_number": row.row_number,
        "raw_name": raw_name,
        "normalized_name": normalized_name,
        "supplier_sku": supplier_sku or None,
        "price": price,
        "currency": "RUB",
        "unit_code": unit_code or "",
        "package_info": package_info or None,
        "brand": brand or None,
        "volume": volume,
        "volume_unit": volume_unit,
        "weight": weight,
        "weight_unit": weight_unit,
        "min_order_qty": min_order_qty,
        "multiplicity": multiplicity,
        "pack_items_count": pack.get("pack_items_count"),
        "pack_item_value": pack.get("pack_item_value"),
        "pack_item_unit_code": pack.get("pack_item_unit_code"),
        "pack_type_code": pack.get("pack_type_code"),
        "category_path": _category_path(cells),
        "comment": None,
        "availability_status": "available",
        "confidence": confidence,
        "warnings": warnings,
        "raw_payload": raw_payload,
    }
    return product


def normalize_unit_code(value, *, aliases=None):
    raw = _normalize_key(value)
    if not raw:
        return ""
    return (aliases or UNIT_ALIASES_MAP).get(raw, "")


def unit_code_aliases_from_units(units):
    """Merge (code, name) pairs from the live Unit reference table under
    UNIT_ALIASES_MAP — the fixed map additionally covers English/plural
    spellings for the 5 core measurement units that the DB's own `name`
    column doesn't (e.g. "kg", "pcs", "штуки")."""
    aliases = dict(UNIT_ALIASES_MAP)
    for code, name in units:
        normalized_code = _normalize_key(code)
        if normalized_code:
            aliases.setdefault(normalized_code, code)
        normalized_name = _normalize_key(name)
        if normalized_name:
            aliases.setdefault(normalized_name, code)
    return aliases


def active_unit_codes_and_names():
    """Live (code, name) pairs for every ACTIVE Unit. Lazy import — this
    module keeps no module-level ORM dependency. Call once per file/batch,
    not per row."""
    from core.models import EntityStatus, Unit

    return list(Unit.objects.filter(status=EntityStatus.ACTIVE).values_list("code", "name"))


def _first_alias_value(cells, aliases, *, excluded_keys=None):
    excluded_keys = excluded_keys or set()
    for key, value in cells.items():
        if _matches_any_alias(key, excluded_keys):
            continue
        if _matches_any_alias(key, aliases) and value:
            return value
    return ""


def _matches_any_alias(value, aliases):
    normalized = _normalize_key(value)
    if not normalized:
        return False
    normalized_compact = normalized.replace("_", " ")
    for alias in aliases:
        alias_normalized = _normalize_key(alias).replace("_", " ")
        if not alias_normalized:
            continue
        if normalized_compact == alias_normalized:
            return True
        if re.search(rf"(?<!\w){re.escape(alias_normalized)}(?!\w)", normalized_compact):
            return True
    return False


def _build_line(cells):
    return "; ".join(value for value in cells.values() if value)


def _infer_name_from_cells(cells):
    skipped_aliases = (
        PRICE_ALIASES
        | UNIT_ALIASES
        | SKU_ALIASES
        | MIN_ORDER_ALIASES
        | MULTIPLICITY_ALIASES
        | PACKAGE_COMPOSITION_ALIASES
        | CATEGORY_ALIASES
    )
    candidates = [
        value
        for key, value in cells.items()
        if not _matches_any_alias(key, skipped_aliases) and value and not _looks_like_price_only(value)
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _category_path(cells):
    # 2026-08-14: раньше просто append()'ил значения в порядке колонок
    # исходного файла — если поставщик расположил "Вид товара" раньше
    # "Группы товара", или у него всего 2 из 3 колонок,
    # _resolve_category_path() (services.py) всё равно слепо трактует
    # элемент [0] как группу, [1] как подгруппу — без проверки семантики.
    # На проде это дало ~60 категорий верхнего уровня, которые на деле были
    # видом/подгруппой без родителя (see
    # docs/wiki/logs/2026-08-14-category-taxonomy-cleanup.md). Теперь
    # порядок всегда группа -> подгруппа -> вид, независимо от порядка
    # колонок в файле.
    levels = [None] * len(CATEGORY_LEVEL_ALIASES)
    for key, value in cells.items():
        if not value:
            continue
        for level, aliases in enumerate(CATEGORY_LEVEL_ALIASES):
            if levels[level] is None and _matches_any_alias(key, aliases):
                levels[level] = value
                break
    return [value for value in levels if value is not None]


def _looks_like_price_only(value):
    value = str(value or "").replace(" ", "").replace(",", ".")
    try:
        Decimal(value)
        return True
    except InvalidOperation:
        return False


def _normalize_money(value):
    if value in (None, ""):
        return None
    normalized = re.sub(r"[^\d,.\-]", "", str(value).replace("\u00a0", ""))
    if not normalized:
        return None
    normalized = normalized.replace(",", ".")
    if normalized.count(".") > 1:
        parts = normalized.split(".")
        normalized = "".join(parts[:-1]) + "." + parts[-1]
    try:
        value_decimal = Decimal(normalized)
    except InvalidOperation:
        return None
    if value_decimal < 0:
        return None
    return value_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_quantity(value):
    if value in (None, ""):
        return None
    normalized = re.sub(r"[^\d,.]", "", str(value).replace("\u00a0", ""))
    if not normalized:
        return None
    normalized = normalized.replace(",", ".")
    try:
        value_decimal = Decimal(normalized)
    except InvalidOperation:
        return None
    if value_decimal < 0:
        return None
    return value_decimal.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _extract_price_from_text(value):
    match = PRICE_WITH_CURRENCY_PATTERN.search(value or "")
    if not match:
        return None
    return _normalize_money(match.group(1))


def _extract_sale_unit(value):
    tokens = re.findall(rf"(?:^|[\s,;])({UNIT_PATTERN})(?:$|[\s,.;])", value or "", flags=re.IGNORECASE)
    for token in reversed(tokens):
        unit = normalize_unit_code(token)
        if unit in {"шт", "кг", "г", "л", "мл"}:
            return unit
    return ""


def _extract_brand(value):
    match = BRAND_PATTERN.search(value or "")
    return _clean_text(match.group(1), max_length=120) if match else ""


def _extract_package(value):
    package_parts = []
    weight = weight_unit = volume = volume_unit = None
    for match in SIZE_PATTERN.finditer(value or ""):
        raw_value, raw_unit = match.groups()
        unit_code = normalize_unit_code(raw_unit)
        quantity = _normalize_quantity(raw_value)
        if not quantity or not unit_code:
            continue
        package_parts.append(f"{_format_decimal(quantity)} {unit_code}")
        if unit_code in {"кг", "г"} and weight is None:
            weight = quantity
            weight_unit = unit_code
        if unit_code in {"л", "мл"} and volume is None:
            volume = quantity
            volume_unit = unit_code
    package_info = "; ".join(dict.fromkeys(package_parts))
    return package_info, weight, weight_unit, volume, volume_unit


def _format_decimal(value):
    if value is None:
        return ""
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def _extract_multiplicity_ratio(value):
    match = RATIO_PATTERN.search(value or "")
    if not match:
        return "", None
    left, right = match.groups()
    multiplicity = _normalize_quantity(right) if left == "1" else None
    return f"{left}/{right}", multiplicity


def _normalize_product_name(raw_name, *, brand, package_info, ratio_raw, unit_code, price):
    value = raw_name or ""
    value = BRAND_PATTERN.sub("", value)
    value = SIZE_PATTERN.sub("", value)
    value = RATIO_PATTERN.sub("", value)
    if price is not None:
        value = PRICE_WITH_CURRENCY_PATTERN.sub("", value)
    if unit_code:
        value = re.sub(rf"(?:^|[\s,;]){re.escape(unit_code)}\.?(?:$|[\s,.;])", " ", value, flags=re.IGNORECASE)
    value = re.sub(r'["«»]', " ", value)
    value = re.sub(r"[\s,;:/\\-]+", " ", value)
    return value.strip().lower()


def _warnings_for_product(*, raw_name, normalized_name, price, unit_code):
    warnings = []
    if not raw_name:
        warnings.append("raw_name_unknown")
    if not normalized_name:
        warnings.append("normalized_name_unknown")
    if price is None:
        warnings.append("price_unknown")
    if not unit_code:
        warnings.append("unit_unknown")
    return warnings


def _needs_ai_fallback(product):
    return Decimal(product.get("confidence") or 0) < AI_CONFIDENCE_THRESHOLD or bool(product.get("warnings"))


class SupplierPriceAIError(Exception):
    def __init__(self, message, *, code=AI_ERROR_NETWORK, http_status=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable

    @property
    def system_error(self):
        return self.code in {
            AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
            AI_ERROR_UNAUTHORIZED_401,
            AI_ERROR_FORBIDDEN_403,
            AI_ERROR_BAD_REQUEST_400,
            AI_ERROR_RATE_LIMIT_429,
            AI_ERROR_TIMEOUT,
            AI_ERROR_NETWORK,
            AI_ERROR_INVALID_JSON,
            AI_ERROR_EMPTY_RESPONSE,
        }


def normalize_ambiguous_rows_with_ai(*, upload, rows, ambiguous_products):
    resolved, stats = normalize_products_with_ai(
        upload=upload,
        rows=rows,
        products=ambiguous_products,
        parser_source="ai_fallback",
    )
    if stats["ai_errors"]:
        raise SupplierPriceAIError("; ".join(stats["ai_errors"]))
    return resolved


def normalize_products_with_ai(
    *,
    upload,
    rows,
    products,
    parser_source,
    phase="normalization",
    progress_callback=None,
    unit_codes=None,
    unit_aliases=None,
):
    rows_by_number = {row.row_number: row for row in rows}
    resolved = []
    stats = _empty_ai_stats()
    chunk_size = _ai_chunk_size()
    chunks_total = (len(products) + chunk_size - 1) // chunk_size
    stats["ai_chunks_total"] = chunks_total

    logger.info(
        "supplier_price_normalization.ai.start upload_id=%s supplier_id=%s parser_source=%s rows=%s chunk_size=%s chunks=%s",
        upload.id,
        upload.supplier_id,
        parser_source,
        len(products),
        chunk_size,
        chunks_total,
    )

    for chunk_index, start in enumerate(range(0, len(products), chunk_size), start=1):
        chunk = products[start : start + chunk_size]
        started_at = time.perf_counter()
        row_numbers = [int(product["row_number"]) for product in chunk]
        logger.info(
            "supplier_price_normalization.ai_chunk.start upload_id=%s chunk=%s/%s rows=%s first_row=%s last_row=%s",
            upload.id,
            chunk_index,
            chunks_total,
            len(chunk),
            row_numbers[0] if row_numbers else None,
            row_numbers[-1] if row_numbers else None,
        )
        logger.debug(
            "supplier_price_normalization.ai_chunk.row_numbers upload_id=%s chunk=%s/%s row_numbers=%s",
            upload.id,
            chunk_index,
            chunks_total,
            row_numbers,
        )
        try:
            ai_payload, retries = _request_ai_normalization_with_retries(
                upload=upload,
                rows_by_number=rows_by_number,
                products=chunk,
                chunk_index=chunk_index,
                chunks_total=chunks_total,
                phase=phase,
                unit_codes=unit_codes,
            )
            chunk_products = _products_from_ai_payload(
                ai_payload,
                fallback_products=chunk,
                parser_source=parser_source,
                unit_aliases=unit_aliases,
            )
        except SupplierPriceAIError as error:
            stats["ai_chunks_failed"] += 1
            message = f"chunk {chunk_index}/{chunks_total}: {error}"
            stats["ai_errors"].append(message)
            stats["ai_error_codes"].append(error.code)
            logger.warning(
                "supplier_price_normalization.ai_chunk.failed upload_id=%s chunk=%s/%s rows=%s elapsed_ms=%s error=%s",
                upload.id,
                chunk_index,
                chunks_total,
                len(chunk),
                int((time.perf_counter() - started_at) * 1000),
                error,
            )
            if _should_stop_ai_pipeline(error):
                logger.warning(
                    "supplier_price_normalization.ai.stop_system_error upload_id=%s chunk=%s/%s error_code=%s error=%s",
                    upload.id,
                    chunk_index,
                    chunks_total,
                    error.code,
                    error,
                )
                break
            if progress_callback:
                progress_callback(
                    chunk_index=chunk_index,
                    chunks_total=chunks_total,
                    stats=stats,
                    resolved=len(resolved),
                )
            continue

        stats["ai_chunks_succeeded"] += 1
        stats["ai_retries"] += retries
        resolved.extend(chunk_products)
        resolved_row_numbers = {int(product["row_number"]) for product in chunk_products}
        stats["resolved_row_numbers"].update(resolved_row_numbers)
        missing_row_numbers = sorted(set(row_numbers) - resolved_row_numbers)
        if missing_row_numbers:
            message = f"chunk {chunk_index}/{chunks_total}: AI response missed rows {missing_row_numbers}"
            stats["ai_errors"].append(message)
            logger.warning(
                "supplier_price_normalization.ai_chunk.partial upload_id=%s chunk=%s/%s missing_row_numbers=%s",
                upload.id,
                chunk_index,
                chunks_total,
                missing_row_numbers,
            )
        logger.info(
            "supplier_price_normalization.ai_chunk.finish upload_id=%s chunk=%s/%s rows=%s resolved=%s retries=%s elapsed_ms=%s",
            upload.id,
            chunk_index,
            chunks_total,
            len(chunk),
            len(chunk_products),
            retries,
            int((time.perf_counter() - started_at) * 1000),
        )
        if progress_callback:
            progress_callback(
                chunk_index=chunk_index,
                chunks_total=chunks_total,
                stats=stats,
                resolved=len(resolved),
            )

    logger.info(
        "supplier_price_normalization.ai.finish upload_id=%s rows_requested=%s rows_resolved=%s chunks_succeeded=%s chunks_failed=%s retries=%s",
        upload.id,
        len(products),
        len(resolved),
        stats["ai_chunks_succeeded"],
        stats["ai_chunks_failed"],
        stats["ai_retries"],
    )
    return resolved, stats


def _is_non_retryable_ai_error(error):
    return not getattr(error, "retryable", False)


def _should_stop_ai_pipeline(error):
    if error.code in {
        AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
        AI_ERROR_UNAUTHORIZED_401,
        AI_ERROR_FORBIDDEN_403,
        AI_ERROR_BAD_REQUEST_400,
    }:
        return True
    return _ai_stop_on_system_error() and error.system_error


def _request_ai_normalization_with_retries(
    *, upload, rows_by_number, products, chunk_index, chunks_total, phase="normalization", unit_codes=None
):
    max_retries = _ai_max_retries()
    retries = 0
    while True:
        try:
            return (
                _request_ai_normalization(
                    upload=upload,
                    rows_by_number=rows_by_number,
                    products=products,
                    phase=phase,
                    unit_codes=unit_codes,
                ),
                retries,
            )
        except SupplierPriceAIError as error:
            if _is_non_retryable_ai_error(error) or retries >= max_retries:
                raise
            retries += 1
            logger.warning(
                "supplier_price_normalization.ai_chunk.retry upload_id=%s chunk=%s/%s retry=%s/%s error_code=%s",
                upload.id,
                chunk_index,
                chunks_total,
                retries,
                max_retries,
                error.code,
            )
            time.sleep(AI_RETRY_BACKOFF_SECONDS * retries)


def _product_for_ai_prompt(product):
    prompt_product = {}
    for field in (
        "row_number",
        "raw_name",
        "normalized_name",
        "supplier_sku",
        "price",
        "currency",
        "unit_code",
        "package_info",
        "brand",
        "volume",
        "volume_unit",
        "weight",
        "weight_unit",
        "min_order_qty",
        "multiplicity",
        "category_path",
        "confidence",
        "warnings",
    ):
        value = product.get(field)
        if isinstance(value, Decimal):
            value = str(value)
        prompt_product[field] = value
    return prompt_product


def _settings_int(name, default, *, minimum=0):
    value = getattr(settings, name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


def _settings_bool(name, default):
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _ai_enabled():
    return _settings_bool("AI_NORMALIZATION_ENABLED", True)


def _ai_default_mode():
    mode = getattr(settings, "AI_NORMALIZATION_DEFAULT_MODE", AI_MODE_AMBIGUOUS)
    return mode if mode in AI_MODES else AI_MODE_AMBIGUOUS


def _ai_url():
    return getattr(settings, "AI_NORMALIZATION_URL", AI_NORMALIZATION_URL)


def _ai_model():
    return getattr(settings, "AI_NORMALIZATION_MODEL", AI_NORMALIZATION_MODEL)


def _openai_model():
    return getattr(settings, "OPENAI_NORMALIZATION_MODEL", _ai_model())


def _openai_base_url():
    base_url = str(getattr(settings, "OPENAI_BASE_URL", "") or "").strip()
    if not base_url:
        legacy_url = str(os.environ.get("AI_NORMALIZATION_URL", "") or "").strip()
        if legacy_url:
            base_url = legacy_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    return base_url or None


def _openai_api_key():
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        api_key = str(getattr(settings, "AI_NORMALIZATION_API_KEY", "") or "").strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    if not api_key:
        raise SupplierPriceAIError(
            "OpenAI normalization credentials are not configured.",
            code=AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
            retryable=False,
        )
    return api_key


def _ai_chunk_size():
    return _settings_int("AI_NORMALIZATION_CHUNK_SIZE", AI_CHUNK_SIZE, minimum=1)


def _ai_timeout_seconds():
    return _settings_int("AI_NORMALIZATION_TIMEOUT_SECONDS", AI_TIMEOUT_SECONDS, minimum=1)


def _openai_timeout_seconds():
    return _settings_int("OPENAI_NORMALIZATION_TIMEOUT_SECONDS", _ai_timeout_seconds(), minimum=1)


def _ai_max_retries():
    return _settings_int("AI_NORMALIZATION_MAX_RETRIES", AI_MAX_RETRIES, minimum=0)


def _ai_full_max_rows():
    return _settings_int("AI_NORMALIZATION_FULL_MAX_ROWS", 200, minimum=1)


def _ai_local_max_rows():
    return _settings_int("AI_NORMALIZATION_LOCAL_MAX_ROWS", 200, minimum=1)


def _ai_stop_on_system_error():
    return _settings_bool("AI_NORMALIZATION_STOP_ON_SYSTEM_ERROR", True)


def _validate_ai_workload(*, ai_mode, rows_total, rows_ai_requested, confirm_full):
    if ai_mode == AI_MODE_OFF or rows_ai_requested == 0:
        return
    if ai_mode == AI_MODE_FULL and not confirm_full:
        raise ValueError("Full AI normalization requires confirm_full=true.")
    if ai_mode == AI_MODE_FULL and rows_ai_requested > _ai_full_max_rows():
        raise ValueError(
            f"Full AI normalization is limited to {_ai_full_max_rows()} rows; requested {rows_ai_requested}."
        )
    if getattr(settings, "DEBUG", False) and rows_ai_requested > _ai_local_max_rows():
        raise ValueError(
            f"Local AI normalization is limited to {_ai_local_max_rows()} AI rows in DEBUG; requested {rows_ai_requested} of {rows_total}."
        )


def _ai_authorization_header():
    authorization = str(getattr(settings, "AI_NORMALIZATION_AUTHORIZATION", "") or "").strip()
    if authorization and authorization.lower() != "bearer":
        return authorization
    api_key = str(getattr(settings, "AI_NORMALIZATION_API_KEY", "") or "").strip()
    if api_key:
        if api_key.lower().startswith("bearer "):
            return api_key
        return f"Bearer {api_key}"
    raise SupplierPriceAIError(
        "AI normalization credentials are not configured.",
        code=AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
        retryable=False,
    )


def _http_error_code(status):
    if status == 400:
        return AI_ERROR_BAD_REQUEST_400
    if status == 401:
        return AI_ERROR_UNAUTHORIZED_401
    if status == 403:
        return AI_ERROR_FORBIDDEN_403
    if status == 429:
        return AI_ERROR_RATE_LIMIT_429
    return AI_ERROR_NETWORK


def _http_error_retryable(status):
    return status == 429 or status >= 500


def _read_http_error_body(error):
    try:
        body = error.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    return body[:1000]


def _network_error_from_url_error(error):
    reason = getattr(error, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return SupplierPriceAIError(
            f"AI normalization request timed out: {reason}",
            code=AI_ERROR_TIMEOUT,
            retryable=True,
        )
    message = str(error)
    if "timed out" in message.lower():
        return SupplierPriceAIError(message, code=AI_ERROR_TIMEOUT, retryable=True)
    return SupplierPriceAIError(
        f"AI normalization network error: {message}",
        code=AI_ERROR_NETWORK,
        retryable=True,
    )


def _request_ai_normalization(*, upload, rows_by_number, products, phase="normalization", unit_codes=None):
    input_rows = []
    for product in products:
        row = rows_by_number.get(int(product["row_number"]))
        cells = row.cells if row else {}
        input_rows.append(
            {
                "row_number": int(product["row_number"]),
                "cells": cells,
                "python_parse": _product_for_ai_prompt(product),
            }
        )

    try:
        from api.llm.runtime import llm_primary_provider_configured
        from api.v1.suppliers.ai_client import request_ai_normalization_payload

        connector_self_sufficient = _settings_bool("LLM_CONNECTOR_ENABLED", False) and llm_primary_provider_configured()
        if connector_self_sufficient:
            # None of these four reach LLMConnector() in this branch — it reads
            # LLM_PRIMARY_*/LLM_FALLBACK_* itself via get_llm_runtimes(), and
            # connector.structured() below never receives timeout_seconds either.
            api_key = base_url = model = timeout_seconds = None
        else:
            api_key = _openai_api_key()
            base_url = _openai_base_url()
            model = _openai_model()
            timeout_seconds = _openai_timeout_seconds()

        return request_ai_normalization_payload(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            upload=upload,
            input_rows=input_rows,
            phase=phase,
            unit_codes=unit_codes,
        )
    except SupplierPriceAIError:
        raise
    except Exception as error:
        raise _ai_error_from_openai_exception(error) from error


def _ai_error_from_openai_exception(error):
    class_name = error.__class__.__name__
    status_code = getattr(error, "status_code", None) or getattr(error, "code", None)
    message = str(error)
    if isinstance(error, urllib.error.HTTPError):
        body = _read_http_error_body(error)
        if body:
            message = f"{message}. {body}"
    if class_name in {"AuthenticationError"} or status_code == 401:
        return SupplierPriceAIError(
            f"AI normalization HTTP 401 Unauthorized. {message}",
            code=AI_ERROR_UNAUTHORIZED_401,
            http_status=401,
            retryable=False,
        )
    if class_name in {"PermissionDeniedError"} or status_code == 403:
        return SupplierPriceAIError(
            f"AI normalization HTTP 403 Forbidden. {message}",
            code=AI_ERROR_FORBIDDEN_403,
            http_status=403,
            retryable=False,
        )
    if class_name in {"BadRequestError"} or status_code == 400:
        return SupplierPriceAIError(
            f"AI normalization HTTP 400 Bad Request. {message}",
            code=AI_ERROR_BAD_REQUEST_400,
            http_status=400,
            retryable=False,
        )
    if class_name in {"RateLimitError"} or status_code == 429:
        return SupplierPriceAIError(
            f"OpenAI normalization rate limit. {message}",
            code=AI_ERROR_RATE_LIMIT_429,
            http_status=429,
            retryable=True,
        )
    if class_name in {"APITimeoutError", "Timeout"} or isinstance(error, (TimeoutError, socket.timeout)):
        return SupplierPriceAIError(
            f"AI normalization request timed out: {message}",
            code=AI_ERROR_TIMEOUT,
            retryable=True,
        )
    if isinstance(error, urllib.error.URLError):
        return _network_error_from_url_error(error)
    if class_name in {"APIConnectionError", "APIError"}:
        return SupplierPriceAIError(
            f"AI normalization network error: {message}",
            code=AI_ERROR_NETWORK,
            retryable=True,
        )
    if (
        isinstance(error, json.JSONDecodeError)
        or "validation" in class_name.lower()
        or "instructor" in class_name.lower()
    ):
        return SupplierPriceAIError(
            f"AI normalization response failed schema validation: {message}",
            code=AI_ERROR_INVALID_JSON,
            retryable=False,
        )
    return SupplierPriceAIError(
        f"OpenAI normalization error: {message}",
        code=AI_ERROR_NETWORK,
        retryable=True,
    )


def _products_from_ai_payload(ai_payload, *, fallback_products, parser_source, unit_aliases=None):
    fallback_by_row = {int(product["row_number"]): product for product in fallback_products}
    products = []
    for ai_product in ai_payload.get("products", []):
        try:
            row_number = int(ai_product.get("row_number"))
        except (TypeError, ValueError):
            continue
        fallback = fallback_by_row.get(row_number)
        if not fallback:
            continue
        merged = {**fallback}
        for field in (
            "raw_name",
            "normalized_name",
            "supplier_sku",
            "currency",
            "unit_code",
            "package_info",
            "brand",
            "volume_unit",
            "weight_unit",
            "category_path",
            "comment",
            "availability_status",
            "warnings",
        ):
            if field in ai_product:
                merged[field] = ai_product[field]
        for field in ("price", "volume", "weight", "min_order_qty", "multiplicity"):
            if field in ai_product:
                if field == "price":
                    merged[field] = _normalize_money(ai_product[field])
                else:
                    merged[field] = _normalize_quantity(ai_product[field])
        merged["unit_code"] = normalize_unit_code(merged.get("unit_code"), aliases=unit_aliases)
        merged["confidence"] = _normalize_confidence(ai_product.get("confidence"), fallback=fallback["confidence"])
        merged["warnings"] = ai_product.get("warnings") if isinstance(ai_product.get("warnings"), list) else []
        if _warnings_for_product(
            raw_name=merged.get("raw_name"),
            normalized_name=merged.get("normalized_name"),
            price=merged.get("price"),
            unit_code=merged.get("unit_code"),
        ):
            merged["warnings"] = list(
                dict.fromkeys(
                    [
                        *merged.get("warnings", []),
                        *_warnings_for_product(
                            raw_name=merged.get("raw_name"),
                            normalized_name=merged.get("normalized_name"),
                            price=merged.get("price"),
                            unit_code=merged.get("unit_code"),
                        ),
                    ]
                )
            )
        merged["raw_payload"] = {
            **merged.get("raw_payload", {}),
            "parser_source": parser_source,
            "parser_version": PARSER_VERSION,
        }
        products.append(merged)
    return products


def _normalize_confidence(value, *, fallback):
    try:
        confidence = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return fallback
    if confidence < 0:
        return Decimal("0.0000")
    if confidence > 1:
        return Decimal("1.0000")
    return confidence.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
