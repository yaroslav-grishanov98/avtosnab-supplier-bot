"""Bounded evidence-backed LLM fallback using the existing connector.

No network calls unless CANONICAL_IMPORT_LLM_ENABLED is explicitly enabled.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, get_args

from django.conf import settings
from pydantic import BaseModel, Field, field_validator

from api.llm import LLMConnector, LLMConnectorError
from api.v1.suppliers.product_import_hints import has_country_hint
from file_imports.canonical import UNITS, FieldCandidate, FieldStatus, decimal_value, resolve_field

logger = logging.getLogger("autosnab.import.candidates")


class SuggestedField(BaseModel):
    field: str
    value: str
    evidence: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class CandidateResponse(BaseModel):
    candidates: list[SuggestedField] = Field(default_factory=list, max_length=16)


MappingField = Literal[
    "name",
    "price",
    "unit_label",
    "net_value",
    "net_unit_label",
    "supplier_sku",
    "availability_label",
    "package_composition",
    "country_of_origin",
    "delivery_multiple",
    "min_order_qty",
    "pack_items_count",
]


class MappingEntry(BaseModel):
    column: str
    field: MappingField
    evidence: str


class MappingResponse(BaseModel):
    mapping: list[MappingEntry] = Field(default_factory=list, max_length=32)

    @field_validator("mapping", mode="before")
    @classmethod
    def _drop_unknown_fields(cls, value):
        # Providers without strict json_schema may return out-of-enum fields; drop those entries only.
        if not isinstance(value, list):
            return value
        known = set(get_args(MappingField))
        return [
            item
            for item in value
            if isinstance(item, MappingEntry) or (isinstance(item, dict) and item.get("field") in known)
        ]


_ALLOWED = {
    "unit_label",
    "country_of_origin",
    "pack_items_count",
    "pack_item_value",
    "pack_item_unit_label",
    "pack_type_label",
}
_PROMPT = (
    "Предложи только значения, явно подтверждённые переданным исходным текстом. "
    "Evidence — точная непрерывная цитата из источника. Не придумывай отсутствующие данные. "
    "Единица фасовки не доказывает единицу продажи. Возвращай кандидатов, а не окончательные значения. "
    "payload.rows — список строк прайса; у каждой есть свои row_number, requested_fields и cells "
    "(словарь «колонка -> значение» этой строки). Для КАЖДОЙ строки верни один объект с ТЕМ ЖЕ row_number и "
    "кандидатами только для requested_fields этой строки. Никогда не смешивай данные разных строк; если cells "
    "строки ничего не подтверждают, пропусти строку или верни пустой список candidates. "
    "Колонки с ценой, номером строки, штрихкодом, "
    "остатком или количеством никогда не содержат фасовку (pack_*) или страну происхождения. "
    "Фасовку и тару ищи только в названии товара, страну — только если она названа явно."
)


_MAPPING_PROMPT = (
    "Сопоставь колонки файла поставщика с каноническими полями. "
    "Поле (field) должно быть строго одним из перечисленных ниже кодов, а не названием колонки:\n"
    "- name: наименование товара\n"
    "- price: цена за единицу продажи\n"
    "- unit_label: единица продажи (шт, упак, кг и т.п.)\n"
    "- net_value: вес или объём нетто одной единицы товара\n"
    "- net_unit_label: единица измерения нетто (кг, г, л, мл)\n"
    "- supplier_sku: артикул или код товара у поставщика\n"
    "- availability_label: наличие или остаток на складе\n"
    "- package_composition: состав фасовки (например, 1/5 кг, 6х1 л)\n"
    "- country_of_origin: страна происхождения\n"
    "- delivery_multiple: кратность поставки\n"
    "- min_order_qty: минимальное количество для заказа\n"
    "- pack_items_count: количество штук в упаковке\n"
    "Evidence должно совпадать с заголовком колонки. Правило: колонки без подходящего поля не включать. Не угадывай."
)


def mapping_candidates(raw, header_mapper, cache):
    """One bounded request per header fingerprint, never the entire file."""
    fingerprint = tuple(raw.cells)
    if fingerprint in cache:
        return cache[fingerprint]
    mapping = {}
    cache[fingerprint] = mapping
    if not getattr(settings, "CANONICAL_IMPORT_LLM_ENABLED", False):
        return mapping
    if all(header_mapper(key) is not None for key in raw.cells) or raw.source.get("headerless") or len(cache) > 8:
        return mapping
    try:
        response = LLMConnector().structured(
            scenario="canonical_import_mapping",
            system_prompt=_MAPPING_PROMPT,
            payload={"columns": list(raw.cells)[:32], "sample": {k: v[:200] for k, v in list(raw.cells.items())[:32]}},
            response_model=MappingResponse,
        )
    except LLMConnectorError:
        logger.warning("canonical.mapping_fallback_failed")
        return mapping
    allowed = set(get_args(MappingField))
    for entry in response.mapping:
        # Only columns the header rules could not place: a column the rules already know must not be flagged
        # "needs confirmation" on every row just because the model listed it too.
        if (
            entry.column in raw.cells
            and header_mapper(entry.column) is None
            and entry.evidence == entry.column
            and entry.field in allowed
        ):
            mapping[entry.column] = entry.field
    return mapping


def _name_cell(parsed) -> str:
    cells = (parsed.raw or {}).get("cells") or {}
    column = parsed.fields["name"].source_column
    if column in cells:
        return str(cells[column])
    if "name" in cells:
        return str(cells["name"])
    return str(next(iter(cells.values()), ""))


def _name_column(parsed):
    cells = (parsed.raw or {}).get("cells") or {}
    column = parsed.fields["name"].source_column
    if column in cells:
        return column
    if "name" in cells:
        return "name"
    return next(iter(cells), None)


_PACK_FIELDS = {"pack_items_count", "pack_item_value", "pack_item_unit_label"}
_PACK_HINT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:кг|kg|мл|ml|гр|г|g|л|l|шт)(?!\w)|\d+\s*[/xх×*]\s*\d+", re.I)
_PACK_TYPE_HINT_RE = re.compile(
    r"(?<!\w)(?:ведр\w*|вед\.|короб\w*|кор\.?|банк\w*|пакет\w*|бутыл\w*|пюр-пак|мешок|мешк\w*|лоток|лотк\w*|"
    r"туб\w*|стакан\w*|контейнер\w*|канистр\w*|бочк\w*|флакон\w*|пласт\.?)(?!\w)",
    re.I,
)
_FIELD_ORDER = (
    "unit_label",
    "country_of_origin",
    "pack_items_count",
    "pack_item_value",
    "pack_item_unit_label",
    "pack_type_label",
)


def requested_fields(parsed) -> list[str]:
    """Fields worth asking the model about: missing/ambiguous and with a signal in the product name."""
    name = _name_cell(parsed)
    result = []
    for key in _FIELD_ORDER:
        allowed = ("missing", "ambiguous", "defaulted") if key == "unit_label" else ("missing", "ambiguous")
        if parsed.fields[key].status not in allowed:
            continue
        if key == "country_of_origin" and not has_country_hint(name):
            continue
        if key in _PACK_FIELDS and not _PACK_HINT_RE.search(name):
            continue
        if key == "pack_type_label" and not _PACK_TYPE_HINT_RE.search(name):
            continue
        result.append(key)
    return result


_MEASURE_UNIT = r"(?:кг|kg|мл|ml|гр|г|g|л|l)(?!\w)"
_QUANTITY_RE = re.compile(rf"\d\s*{_MEASURE_UNIT}", re.I)
_NUMBER_TOKEN_RE = re.compile(r"(?<!\d)(?<!\d[.,])(\d+(?:[.,]\d+)?)(?!\d)(?![.,]\d)")
_FOLLOWING_MEASURE_RE = re.compile(rf"\s*{_MEASURE_UNIT}", re.I)


def _whole_number_in(value, evidence, *, reject_measure_unit=False) -> bool:
    """True if value equals a whole numeric token of the evidence (not a digit substring of a longer number)."""
    target = decimal_value(value)
    if target is None:
        return False
    for match in _NUMBER_TOKEN_RE.finditer(evidence):
        if decimal_value(match.group(1)) != target:
            continue
        if reject_measure_unit and _FOLLOWING_MEASURE_RE.match(evidence, match.end()):
            continue
        return True
    return False


def candidate_is_plausible(field, value, evidence, parsed) -> bool:
    """Reject model values that merely occur in some cell: prices, row numbers, barcodes, regions."""
    value = str(value).strip()
    name = _name_cell(parsed)
    if field in _PACK_FIELDS or field == "pack_type_label":
        if evidence not in name:
            return False
    if field == "pack_items_count":
        # Look a few characters past the evidence too: evidence "25" in "Мука 25 кг" is a weight, not a count.
        start = name.find(evidence)
        context = name[start : start + len(evidence) + 4] if start >= 0 else evidence
        if not value.isdigit() or not _whole_number_in(value, context, reject_measure_unit=True):
            return False
    if field == "pack_item_value":
        number = decimal_value(value)
        if number is None or number <= 0 or not _whole_number_in(value, evidence):
            return False
        name_column = _name_column(parsed)
        for column, cell in ((parsed.raw or {}).get("cells") or {}).items():
            if column == name_column:
                continue
            other = decimal_value(str(cell))
            if other is not None and other == number:
                return False
    if field == "country_of_origin":
        if decimal_value(value) is not None or not has_country_hint(value):
            return False
    if field in ("unit_label", "pack_item_unit_label"):
        if value.casefold().strip(" .") not in UNITS:
            return False
        if field == "unit_label" and _QUANTITY_RE.search(evidence):
            # The sale unit is never derived from a quantity ("10 кг" is a size, not "sold per kg").
            return False
    return True


class RowCandidates(BaseModel):
    row_number: int
    candidates: list[SuggestedField] = Field(default_factory=list, max_length=16)


class BatchCandidateResponse(BaseModel):
    rows: list[RowCandidates] = Field(default_factory=list)


def _row_cells(parsed) -> dict[str, str]:
    return {str(column): str(text)[:500] for column, text in list((parsed.raw["cells"]).items())[:16]}


def _apply_suggestion(parsed, suggestion, requested, cells) -> bool:
    """Accept one model suggestion into `parsed` only if it is evidence-backed and plausible."""
    if (
        suggestion.field not in requested
        or not suggestion.evidence
        or not any(suggestion.evidence in x for x in cells.values())
    ):
        return False
    # Critical values must be lexical evidence, not an unsupported model guess.
    if suggestion.value.casefold() not in suggestion.evidence.casefold():
        return False
    if not candidate_is_plausible(suggestion.field, suggestion.value, suggestion.evidence, parsed):
        return False
    original = parsed.fields[suggestion.field]
    candidate = FieldCandidate(
        suggestion.value,
        source="llm",
        method="evidence-backed-fallback",
        evidence=suggestion.evidence,
        status=FieldStatus.INFERRED,
        confidence=None,
    )
    parsed.fields[suggestion.field] = resolve_field(suggestion.field, original.candidates + [candidate])
    if not any(i.get("code") == "llm_value_review" and i.get("field") == suggestion.field for i in parsed.parse_issues):
        parsed.parse_issues.append(
            {
                "code": "llm_value_review",
                "field": suggestion.field,
                "level": "warning",
                "review_required": True,
                "message": f"Значение поля {suggestion.field} предложено моделью и требует подтверждения.",
            }
        )
    return True


def _chunks(rows, batch_size):
    """Chunks of at most batch_size rows; a row_number never repeats within a chunk."""
    chunk, numbers = [], set()
    for row in rows:
        if len(chunk) >= batch_size or row.row_number in numbers:
            yield chunk
            chunk, numbers = [], set()
        chunk.append(row)
        numbers.add(row.row_number)
    if chunk:
        yield chunk


def _max_tokens(rows: int) -> int:
    """Room for the answer of one chunk: up to ~6 candidates per row; the default 1200 cut 12-row answers in half."""
    return max(500, min(4096, rows * 250 + 300))


def _max_workers() -> int:
    try:
        value = int(getattr(settings, "CANONICAL_IMPORT_LLM_MAX_WORKERS", 8))
    except (TypeError, ValueError):
        return 8
    return value if value > 0 else 8


def enrich_candidates_batch(parsed_rows, *, request_budget, batch_size) -> None:
    """One structured request per chunk of rows that still need fields; rows beyond the budget stay untouched."""
    if not getattr(settings, "CANONICAL_IMPORT_LLM_ENABLED", False):
        return
    wanted = [(row, requested_fields(row)) for row in parsed_rows]
    wanted = [(row, fields) for row, fields in wanted if fields]
    requested_by_row = {id(row): fields for row, fields in wanted}
    # The budget is spent up front so that the requests can run side by side; rows beyond it stay untouched.
    chunks = []
    for chunk in _chunks([row for row, _ in wanted], max(1, batch_size)):
        if request_budget[0] <= 0:
            break
        request_budget[0] -= 1
        chunks.append(chunk)
    if not chunks:
        return
    cells_by_chunk = [{row.row_number: _row_cells(row) for row in chunk} for chunk in chunks]

    def request(index):
        chunk, cells_by_row = chunks[index], cells_by_chunk[index]
        try:
            return LLMConnector().structured(
                scenario="canonical_import_field_candidates",
                system_prompt=_PROMPT,
                payload={
                    "rows": [
                        {
                            "row_number": row.row_number,
                            "requested_fields": requested_by_row[id(row)],
                            "cells": cells_by_row[row.row_number],
                        }
                        for row in chunk
                    ]
                },
                response_model=BatchCandidateResponse,
                max_tokens=_max_tokens(len(chunk)),
            )
        except LLMConnectorError:
            logger.warning("canonical.field_fallback_failed rows=%s", [row.row_number for row in chunk])
            return None

    with ThreadPoolExecutor(max_workers=min(_max_workers(), len(chunks))) as executor:
        responses = list(executor.map(request, range(len(chunks))))
    # Suggestions are applied here, one thread, so rows are never mutated concurrently.
    for chunk, cells_by_row, response in zip(chunks, cells_by_chunk, responses):
        if response is None:
            continue
        by_number = {row.row_number: row for row in chunk}
        response_rows = getattr(response, "rows", None)
        if response_rows is None and len(chunk) == 1:
            # Exists only for single-row legacy test mocks that return a flat CandidateResponse.
            response_rows = [RowCandidates(row_number=chunk[0].row_number, candidates=response.candidates)]
        for row_candidates in response_rows or []:
            parsed = by_number.get(row_candidates.row_number)
            if parsed is None:
                continue
            for suggestion in row_candidates.candidates:
                _apply_suggestion(parsed, suggestion, requested_by_row[id(parsed)], cells_by_row[parsed.row_number])


def enrich_candidates(parsed, *, request_budget):
    enrich_candidates_batch([parsed], request_budget=request_budget, batch_size=1)
    return parsed


def apply_mapping_provenance(parsed, mapping):
    for column, key in mapping.items():
        entries = parsed.mapping.get(key, [])
        for entry in entries:
            if entry["column"] == column:
                entry["source"] = "llm"
        if key in parsed.fields:
            parsed.fields[key].status = FieldStatus.AMBIGUOUS
            parsed.parse_issues.append(
                {
                    "code": "llm_mapping_review",
                    "field": key,
                    "level": "warning",
                    "review_required": True,
                    "message": f"Смысл колонки {column} требует подтверждения.",
                }
            )
    return parsed
