"""Canonical, database-independent extraction with auditable field candidates.

Adapters own API representation and business defaults. Matching never writes here.
"""

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from core.countries import normalize_country
from file_imports.barcode import gs1_country, normalize_barcode
from file_imports.country import country_from_heading, find_country_in_text
from file_imports.versions import CANONICAL_V2

PARSER_VERSION = CANONICAL_V2


class FieldStatus(StrEnum):
    PARSED = "parsed"
    INFERRED = "inferred"
    DEFAULTED = "defaulted"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class FieldCandidate:
    value: object
    raw_value: object = None
    source: str = "unknown"
    method: str = "unknown"
    evidence: str | None = None
    source_column: str | None = None
    confidence: float | None = None
    status: str = FieldStatus.PARSED
    span: tuple[int, int] | None = None


@dataclass
class ParsedField:
    method: str = "unknown"
    reliability: str = "unknown"
    value: object = None
    raw_value: object = None
    source: str = "unknown"
    status: str = FieldStatus.MISSING
    evidence: str | None = None
    source_column: str | None = None
    confidence: float | None = None
    candidates: list[FieldCandidate] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)


@dataclass
class ParsedProductRow:
    row_number: int
    raw: dict
    mapping: dict
    fields: dict[str, ParsedField]
    row_type: str = "product"
    parse_issues: list[dict] = field(default_factory=list)
    parser_version: str = PARSER_VERSION

    def snapshot(self):
        return json_values(asdict(self))


def json_values(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): json_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_values(v) for v in value]
    return value


UNITS = {
    "кг": "кг",
    "kg": "кг",
    "г": "г",
    "гр": "г",
    "g": "г",
    "л": "л",
    "l": "л",
    "литр": "л",
    "литра": "л",
    "литров": "л",
    "мл": "мл",
    "ml": "мл",
    "шт": "шт",
    "pcs": "шт",
    "упак": "упак",
    "уп": "упак",
    "упаковка": "упак",
    "кор": "кор",
}
NUMERIC = {"price", "net_value", "delivery_multiple", "min_order_qty", "pack_item_value", "pack_items_count"}
UNIT_FIELDS = {"unit_label", "net_unit_label", "pack_item_unit_label", "pack_type_label"}
PACKAGE = re.compile(
    r"(?<![\w/.,])(?P<count>\d+)\s*(?:шт\.?\s*)?(?:[/xх×*]|по)\s*(?P<value>\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(?P<unit>кг|kg|мл|ml|гр|г|g|л|l)\.?(?!\w)",
    re.I,
)
MEASURE = re.compile(
    r"(?<![\w.,])(?<![A-Za-zА-Яа-я]-)(?P<value>\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(?P<unit>кг|kg|мл|ml|литр(?:а|ов)?|гр|г|g|л|l)\.?(?:(?!\w)|(?=\d+/\d))",
    re.I,
)
# «180г/6шт», «200гр х 15 пл/бут»: the mass of one item first, then how many items the pack holds.
PACKAGE_MASS_FIRST = re.compile(
    r"(?<![\w/.,])(?<![A-Za-zА-Яа-я]-)(?P<value>\d+(?:[  ]\d{3})*(?:[.,]\d+)?)\s*"
    r"(?P<unit>кг|kg|мл|ml|литр(?:а|ов)?|гр|г|g|л|l)\.?\s*"
    r"(?:/\s*(?P<count>\d+)\s*шт|[х×x*]\s*(?P<count2>\d+)\s*(?:шт|пл/бут|пл|бут|уп|пак))(?!\w)",
    re.I,
)
# MEASURE above accepts a case ratio glued to the unit ("1000 мл1/12"); any other word character after it is not a measure.
# "(цена за 1 кг)": the quantity the price refers to. Other measures in such a name (a case weight) do not describe it.
# A number right after a designation («№2 800г», «сорт 2 500 г») names the product, not a thousands group of the weight.
DESIGNATION_BEFORE = re.compile(r"(?:№|#|сорт|категория|кат\.?|калибр|арт\.?|артикул)\s*$", re.I)
PRICE_BASIS = re.compile(
    r"(?<!\w)цена\s+за\s+(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>кг|kg|мл|ml|гр|г|g|л|l)\.?(?!\w)", re.I
)
SALE_UNIT = re.compile(r",\s*(?P<unit>упак|шт|кг|л|г|мл|кор)\.?\s*$", re.I)
WEIGHT_WORD = re.compile(r"(?<!\w)вес(?:ов\w*)?(?!\w)", re.I)
PACK_TYPE = re.compile(r"(?<!\w)(?:в\s+)?(?P<type>ведре|ведро|вед\.|коробке|коробка|кор\.|кор)(?!\w)", re.I)
PACK_TYPES = {
    "ведре": "ведро",
    "ведро": "ведро",
    "вед.": "ведро",
    "коробке": "кор",
    "коробка": "кор",
    "кор.": "кор",
    "кор": "кор",
}


def decimal_value(value):
    text = re.sub(r"\s+", "", str(value)).replace(",", ".")
    text = re.sub(r"(?:₽|руб\.?|р\.|RUB)$", "", text, flags=re.I)
    try:
        result = Decimal(text)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def normalize_candidate(name, candidate):
    value = candidate.value
    status = candidate.status
    if name in NUMERIC:
        value = decimal_value(value)
        if value is None or value < 0 or (name != "price" and value <= 0):
            status = FieldStatus.INVALID
        if value is not None and (abs(value) >= Decimal("1e12") or (name != "price" and abs(value) >= Decimal("1e10"))):
            status = FieldStatus.INVALID
        if name == "pack_items_count" and value is not None:
            if value != value.to_integral_value():
                status = FieldStatus.INVALID
            else:
                value = int(value)
                if value > 2147483647:
                    status = FieldStatus.INVALID
    elif name == "currency":
        text = str(value).strip().upper()
        value = "RUB" if text in {"РУБ", "РУБ.", "₽", "RUB"} else text
        if not re.fullmatch(r"[A-Z]{3}", value):
            status = FieldStatus.INVALID
    elif name == "price_basis":
        value = {"упаковку": "упак", "коробку": "кор"}.get(str(value).casefold(), str(value).casefold())
    elif name in UNIT_FIELDS:
        value = UNITS.get(str(value).strip().casefold().rstrip("."), str(value).strip().casefold())
    elif name == "country_of_origin":
        text = str(value).strip() if value is not None else ""
        if text:
            known = normalize_country(text)
            if known:
                value = known
            elif candidate.source != "user":
                # «Россия, Краснодарский край», «РФ/Китай»: not silently canonicalized, the operator decides (a manual edit is final).
                status = FieldStatus.AMBIGUOUS
    changes = {"value": value, "status": status}
    if name == "country_of_origin" and candidate.source == "column" and status != FieldStatus.AMBIGUOUS:
        # Full confidence only for a recognized country (or a blank cell); unknown text stays for review.
        changes["confidence"] = 1.0
    return FieldCandidate(**(asdict(candidate) | changes))


# Highest priority first; a source missing from the order (unknown, default, rule) ranks last.
COUNTRY_SOURCE_ORDER = (
    "user",
    "column",
    "name",
    "section",
    "catalog_barcode",
    "catalog_name",
    "brand",
    "gs1_prefix",
    "llm",
)
# Hints, not evidence: they never override an explicit source and only fill the field when nothing explicit exists.
ADVISORY_COUNTRY_SOURCES = frozenset({"section", "catalog_barcode", "catalog_name", "brand", "gs1_prefix"})
# A disagreeing candidate from these sources is reported as a conflict but does not make an explicit value ambiguous.
_NON_BLOCKING_COUNTRY_SOURCES = ADVISORY_COUNTRY_SOURCES | {"llm"}


def _country_rank(candidate):
    try:
        return COUNTRY_SOURCE_ORDER.index(candidate.source)
    except ValueError:
        return len(COUNTRY_SOURCE_ORDER)


def _conflicts_with(chosen, candidates):
    return [
        {"code": "conflicting_candidates", "values": json_values([chosen.value, c.value])}
        for c in candidates
        if c.value != chosen.value and c.status != FieldStatus.INVALID
    ]


_CHOSEN_VALUE = object()


def _build_field(chosen, normalized, conflicts, status, *, reliability=None, value=_CHOSEN_VALUE):
    if reliability is None:
        reliability = (
            "requires_review"
            if status in (FieldStatus.AMBIGUOUS, FieldStatus.INVALID)
            else (
                "explicit"
                if chosen.source in ("column", "user")
                else "deterministic"
                if chosen.source in ("name", "package_text", "rule", "calculated")
                else "unverified"
            )
        )
    return ParsedField(
        method=chosen.method,
        reliability=reliability,
        value=chosen.value if value is _CHOSEN_VALUE else value,
        raw_value=chosen.raw_value,
        source=chosen.source,
        status=status,
        evidence=chosen.evidence,
        source_column=chosen.source_column,
        confidence=chosen.confidence,
        candidates=normalized,
        conflicts=conflicts,
    )


def _resolve_country(normalized):
    chosen = min(normalized, key=_country_rank)
    conflicts = _conflicts_with(chosen, normalized)
    if chosen.source in ADVISORY_COUNTRY_SOURCES:
        # Advisory hints that disagree with each other (even two values of one source) cannot pick a country.
        disagree = any(
            c.source in ADVISORY_COUNTRY_SOURCES and c.value != chosen.value and c.status != FieldStatus.INVALID
            for c in normalized
        )
        if disagree:
            return _build_field(
                chosen, normalized, conflicts, FieldStatus.AMBIGUOUS, reliability="requires_review", value=None
            )
        return _build_field(chosen, normalized, conflicts, chosen.status, reliability="requires_review")
    status = chosen.status
    # Only an explicit winner ignores weaker hints; an llm/unknown winner keeps the generic "any disagreement" rule.
    explicit = chosen.source in ("user", "column", "name")
    blocking = [c for c in normalized if not explicit or c.source not in _NON_BLOCKING_COUNTRY_SOURCES]
    if _conflicts_with(chosen, blocking) and chosen.source != "user":
        status = FieldStatus.AMBIGUOUS
    if chosen.source == "llm" and not chosen.evidence:
        status = FieldStatus.AMBIGUOUS
    # A manual edit is final: the operator already settled the disagreement.
    reliability = "requires_review" if conflicts and chosen.source != "user" else None
    return _build_field(chosen, normalized, conflicts, status, reliability=reliability)


def resolve_field(name, candidates):
    normalized = [normalize_candidate(name, c) for c in candidates]
    if not normalized:
        return ParsedField()
    if name == "country_of_origin":
        return _resolve_country(normalized)
    priority = {
        "user": 0,
        "column": 1,
        "package_text": 2,
        "name": 3,
        "rule": 4,
        "calculated": 5,
        "llm": 6,
        "default": 7,
        "unknown": 8,
    }
    chosen = min(normalized, key=lambda c: priority.get(c.source, 8))
    conflicts = _conflicts_with(chosen, normalized)
    status = chosen.status
    if conflicts and chosen.source != "user":
        status = FieldStatus.AMBIGUOUS
    if chosen.source == "llm" and not chosen.evidence:
        status = FieldStatus.AMBIGUOUS
    return _build_field(chosen, normalized, conflicts, status)


# «27 шт в кор», «30 шт/кор»: how many pieces a box holds (the item itself is the only measure of the name).
COUNT_IN_BOX = re.compile(
    r"(?<![\w/.,])(?P<count>\d+)\s*шт\.?\s*(?:в\s+|/\s*)(?:кор(?:обк\w*)?|уп(?:ак\w*)?|ящ\w*)(?!\w)", re.I
)


# D11: «1/12», «(1/5)», «1//50» with no unit after N is «N pieces in a box» (not «1/0,5 л», not «1/4кг»).
CASE_RATIO = re.compile(
    r"(?<![\w/.,])1\s*/{1,2}\s*(?P<count>\d+)(?![\w.]|,\d|\s*(?:кг|kg|гр|г|g|мл|ml|литр|л|l)(?!\w))", re.I
)


def iter_measures(text):
    """MEASURE matches, with a leading designation number split off the weight."""
    matches = list(MEASURE.finditer(text))
    for match in matches:
        # «2L» next to a real weight («2L ... 1кг») is a size grade of the product, not two litres.
        if match["unit"] == "L" and any(m["unit"].casefold() in {"кг", "kg", "г", "гр", "g"} for m in matches):
            continue
        group_end = re.match(r"\d+[  ](?=\d{3}(?!\d))", match["value"])
        if group_end and DESIGNATION_BEFORE.search(text[: match.start()]):
            tail = MEASURE.match(text, match.start("value") + group_end.end())
            if tail:
                match = tail
        yield match


def _default_piece_unit_enabled():
    from django.conf import settings

    return bool(getattr(settings, "CANONICAL_DEFAULT_PIECE_UNIT", False))


def _country_hints_enabled():
    """Advisory country hints (section heading, GS1 prefix) stay off until their precision is measured."""
    from django.conf import settings

    return bool(getattr(settings, "CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED", False))


def name_candidates(name, *, source="name"):
    candidates = {}

    def add(key, value, match, method):
        candidates.setdefault(key, []).append(
            FieldCandidate(
                value=value,
                raw_value=match.group(),
                source=source,
                status=FieldStatus.INFERRED,
                method=method,
                evidence=match.group(),
                span=match.span(),
            )
        )

    packages = [(match, match["count"], "package-expression") for match in PACKAGE.finditer(name)]
    for match in PACKAGE_MASS_FIRST.finditer(name):
        if not any(p.start() < match.end() and match.start() < p.end() for p, _, _ in packages):
            packages.append((match, match["count"] or match["count2"], "package-mass-first"))
    for match, count, method in packages:
        # D2: «1/5кг» is one container of 5 kg; a pack of one item is not a composition, only the net is kept --
        # unless the name says what the container is («в ведре»), then 1 x 5 kg carries the container type.
        if int(count) != 1 or PACK_TYPE.search(name):
            add("pack_items_count", count, match, method)
            add("pack_item_value", match["value"], match, method)
            add("pack_item_unit_label", match["unit"], match, method)
        # Unit mass/volume is independent of the sale unit and order conditions.
        add("net_value", match["value"], match, method)
        add("net_unit_label", match["unit"], match, method)
    if any(int(count) == 1 for _, count, _ in packages):
        # «1/25 кг, (5 кг)»: another weight next to a single container contradicts it, so both stay as candidates.
        spans = [match.span() for match, _, _ in packages]
        for match in iter_measures(name):
            if not any(start < match.end() and match.start() < end for start, end in spans):
                add("net_value", match["value"], match, "measure-expression")
                add("net_unit_label", match["unit"], match, "measure-expression")
    if not packages:
        basis = PRICE_BASIS.search(name)
        if basis:
            add("net_value", basis["value"], basis, "price-basis")
            add("net_unit_label", basis["unit"], basis, "price-basis")
        else:
            for match in iter_measures(name):
                add("net_value", match["value"], match, "measure-expression")
                add("net_unit_label", match["unit"], match, "measure-expression")
    in_box = COUNT_IN_BOX.search(name)
    if in_box and not packages and int(in_box["count"]) != 1:
        measures = {(m["value"], m["unit"].casefold()): m for m in iter_measures(name)}
        if len(measures) == 1:
            item = next(iter(measures.values()))
            for key, value, evidence in (
                ("pack_items_count", in_box["count"], in_box.group()),
                ("pack_item_value", item["value"], item.group()),
                ("pack_item_unit_label", item["unit"], item.group()),
            ):
                candidates.setdefault(key, []).append(
                    FieldCandidate(
                        value=value,
                        raw_value=evidence,
                        source=source,
                        status=FieldStatus.INFERRED,
                        method="count-in-box",
                        evidence=evidence,
                    )
                )
    ratio = CASE_RATIO.search(name)
    if ratio and not packages and not in_box and int(ratio["count"]) > 1:
        measures = {(m["value"], m["unit"].casefold()): m for m in iter_measures(name)}
        parts = [("pack_items_count", ratio["count"], ratio.group())]
        if len(measures) == 1:
            item = next(iter(measures.values()))
            parts += [
                ("pack_item_value", item["value"], item.group()),
                ("pack_item_unit_label", item["unit"], item.group()),
            ]
        for key, value, evidence in parts:
            candidates.setdefault(key, []).append(
                FieldCandidate(
                    value=value,
                    raw_value=evidence,
                    source=source,
                    status=FieldStatus.INFERRED,
                    method="case-ratio",
                    evidence=evidence,
                )
            )
    # D5: a country named inside the row itself is evidence (see file_imports.country for the matching rules).
    country = find_country_in_text(name)
    if country:
        candidates.setdefault("country_of_origin", []).append(
            FieldCandidate(
                value=country.value,
                raw_value=country.evidence,
                source=source,
                status=FieldStatus.PARSED,
                method="country-in-row",
                evidence=country.evidence,
                confidence=0.95,
                span=country.span,
            )
        )
    sale = SALE_UNIT.search(name)
    if sale:
        add("unit_label", sale["unit"], sale, "explicit-sale-suffix")
    for match in PACK_TYPE.finditer(name):
        add("pack_type_label", PACK_TYPES[match["type"].casefold()], match, "package-type")
    return candidates


def classify_row(raw, mapped):
    name = mapped.get("name", "").strip()
    if not any(raw.cells.values()):
        return "empty"
    if not name:
        return "unknown"
    if re.fullmatch(r"итого(?:\s+.*)?|всего(?:\s+.*)?|total(?:\s+.*)?", name, re.I):
        return "total"
    if re.fullmatch(r"наименование(?: товара)?|название(?: товара)?|номенклатура|product name|name", name, re.I):
        return "header"
    return "product"


def clean_name(original, fields):
    spans = set()
    for key in (
        "unit_label",
        "pack_items_count",
        "pack_item_value",
        "pack_item_unit_label",
        "pack_type_label",
        "net_value",
        "net_unit_label",
    ):
        parsed = fields.get(key)
        if not parsed or parsed.status in {FieldStatus.INVALID, FieldStatus.AMBIGUOUS}:
            continue
        for candidate in parsed.candidates:
            if (
                candidate.source == "name"
                and candidate.span
                and (
                    candidate.value == parsed.value or (key in ("net_value", "net_unit_label") and not parsed.conflicts)
                )
            ):
                spans.add(tuple(candidate.span))
    # Do not remove only part of an ambiguous package expression.
    blocked = {
        tuple(c.span)
        for p in fields.values()
        if p.status in {FieldStatus.AMBIGUOUS, FieldStatus.INVALID}
        for c in p.candidates
        if c.source == "name" and c.span
    }
    for ratio in re.finditer(r"(?<![\w/])\d+\s*/\s*\d+(?![\w.,])", original):
        if not any(start <= ratio.start() and ratio.end() <= end for start, end in spans):
            blocked.add(ratio.span())
    spans -= blocked
    cleaned = original
    for start, end in sorted(spans, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    return tidy_name(cleaned) or original


def tidy_name(text: str) -> str:
    """Remove punctuation orphaned by cutting package/unit expressions out of a name."""
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"\s+([,;])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"(?<=\s)\.(?=[^\W\d_])", " ", text)
    text = re.sub(r"(?<=\w)/(?=[\s,]|$)", "", text)
    text = re.sub(r"(?<=\s)[*×](?=\d)", "", text)
    return " ".join(text.split()).strip(" ,;-")


def name_with_measure(raw_name: str) -> str:
    """Original name without a trailing «, шт»-style sale unit, tidied; keeps weight/volume."""
    return tidy_name(SALE_UNIT.sub("", raw_name or "")) or (raw_name or "").strip()


def _is_caliber_range(text: str) -> bool:
    """«100/150»: a size/caliber range (both parts >= 10, left < right), not a pack count."""
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
    return bool(match) and 10 <= int(match[1]) < int(match[2])


def barcode_candidates(column_candidates):
    """Штрихкоды из ячеек колонки «штрихкод» и страны по их GS1 Prefix.

    Возвращает (кандидаты поля barcode, кандидаты country_of_origin). Один код ячейки -- один кандидат.
    Вид кода (ean13, upca, ean8, gtin14) виден в `method`: `barcode-column:<вид>`. Значение -- `Barcode.value`
    как есть (для gtin14 это цифры 2-14). Страна по префиксу -- слабая подсказка (источник `gs1_prefix`)."""
    codes, countries = [], []
    for cell in column_candidates:
        for barcode in normalize_barcode(cell.value):
            codes.append(
                FieldCandidate(
                    value=barcode.value,
                    raw_value=cell.raw_value,
                    source="column",
                    method=f"barcode-column:{barcode.kind}",
                    evidence=str(cell.raw_value),
                    source_column=cell.source_column,
                    status=FieldStatus.PARSED if barcode.valid else FieldStatus.INVALID,
                )
            )
            country = gs1_country(barcode)
            if country:
                countries.append(
                    FieldCandidate(
                        value=country,
                        raw_value=barcode.value,
                        source="gs1_prefix",
                        method="gs1-prefix",
                        status=FieldStatus.INFERRED,
                        confidence=0.5,
                        evidence=barcode.value[:3],
                        source_column=cell.source_column,
                    )
                )
    return codes, countries


def section_country_candidate(section_path):
    """Страна из самого вложенного заголовка раздела, который целиком называет страну."""
    for heading in reversed(section_path or []):
        country = country_from_heading(heading)
        if country:
            return FieldCandidate(
                value=country,
                raw_value=heading,
                source="section",
                method="section-heading",
                status=FieldStatus.INFERRED,
                confidence=0.8,
                evidence=heading,
            )
    return None


def parse_product_row(raw, header_mapper):
    candidates = {}
    mapping = {}
    mapped = {}
    for column, value in raw.cells.items():
        key = header_mapper(column)
        if not key:
            continue
        mapping.setdefault(key, []).append({"column": column, "source": "alias", "evidence": column})
        mapped.setdefault(key, value)
        candidates.setdefault(key, []).append(
            FieldCandidate(
                value=value,
                raw_value=value,
                source="column",
                method="column-mapping",
                evidence=str(value),
                source_column=column,
            )
        )
    if raw.source.get("headerless") and "name" not in candidates:
        column = raw.source.get("fallback_name_column", "column_1")
        if raw.cells.get(column):
            mapped["name"] = raw.cells[column]
            candidates["name"] = [
                FieldCandidate(
                    value=raw.cells[column],
                    raw_value=raw.cells[column],
                    source="column",
                    method="headerless-first-column",
                    evidence=raw.cells[column],
                    source_column=column,
                )
            ]
            mapping["name"] = [{"column": column, "source": "heuristic", "evidence": column}]
    # Explicit price semantics are independent from package and sale-unit inference.
    for column, text in raw.cells.items():
        key = header_mapper(column)
        if key == "price":
            header = str(column).casefold()
            basis = re.search(r"(?:за|/)\s*(кг|шт|упак|упаковку|коробку|л)(?!\w)", header)
            if basis:
                candidates.setdefault("price_basis", []).append(
                    FieldCandidate(
                        basis[1], source="column", method="price-header", evidence=column, source_column=column
                    )
                )
            if re.search(r"₽|\bруб\.?|\bRUB\b", str(text), re.I) or re.search(r"руб|rub|₽", header):
                candidates.setdefault("currency", []).append(
                    FieldCandidate(
                        "RUB",
                        source="column",
                        method="explicit-currency",
                        evidence=str(text) if re.search(r"₽|руб|rub", str(text), re.I) else column,
                        source_column=column,
                    )
                )
        elif str(column).casefold() in ("валюта", "currency"):
            candidates.setdefault("currency", []).append(
                FieldCandidate(
                    text, raw_value=text, source="column", method="currency-column", evidence=text, source_column=column
                )
            )
    original = mapped.get("name", "")
    for key, values in name_candidates(original).items():
        candidates.setdefault(key, []).extend(values)
    if mapped.get("package_composition"):
        for key, values in name_candidates(mapped["package_composition"], source="package_text").items():
            candidates.setdefault(key, []).extend(values)
    barcodes, gs1_countries = barcode_candidates(candidates.pop("barcode", []))
    if barcodes:
        candidates["barcode"] = barcodes
    if _country_hints_enabled():
        if gs1_countries:
            candidates.setdefault("country_of_origin", []).extend(gs1_countries)
        section_country = section_country_candidate(raw.source.get("section_path"))
        if section_country:
            candidates.setdefault("country_of_origin", []).append(section_country)
    # Net columns may explicitly contain a quantity and its unit.
    net_candidates = []
    for candidate in candidates.get("net_value", []):
        match = MEASURE.fullmatch(str(candidate.value).strip()) if candidate.source == "column" else None
        if match:
            net_candidates.append(
                FieldCandidate(
                    value=match["value"],
                    raw_value=candidate.raw_value,
                    source="column",
                    method="measure-column",
                    evidence=candidate.evidence,
                    source_column=candidate.source_column,
                )
            )
            candidates.setdefault("net_unit_label", []).append(
                FieldCandidate(
                    value=match["unit"],
                    raw_value=candidate.raw_value,
                    source="column",
                    method="measure-column",
                    evidence=candidate.evidence,
                    source_column=candidate.source_column,
                )
            )
        else:
            net_candidates.append(candidate)
    if net_candidates:
        candidates["net_value"] = net_candidates
    column_count = any(c.source == "column" for c in candidates.get("pack_items_count", []))
    name_nets = [c for c in candidates.get("net_value", []) if c.source == "name"]
    if column_count and not candidates.get("pack_item_value") and len(name_nets) == 1:
        net = name_nets[0]
        net_units = [
            c for c in candidates.get("net_unit_label", []) if c.source == "name" and c.evidence == net.evidence
        ]
        if net_units:
            for key, source in (("pack_item_value", net), ("pack_item_unit_label", net_units[0])):
                candidates.setdefault(key, []).append(
                    FieldCandidate(
                        value=source.value,
                        raw_value=source.raw_value,
                        source="calculated",
                        status=FieldStatus.INFERRED,
                        method="column-count-with-name-measure",
                        evidence=source.evidence,
                        span=source.span,
                    )
                )
    # Rows without an explicit sale unit: weight words imply kg, otherwise priced rows default to pieces.
    if "unit_label" in candidates:
        candidates["unit_label"] = [c for c in candidates["unit_label"] if str(c.value or "").strip()]
        if not candidates["unit_label"]:
            del candidates["unit_label"]
    if not candidates.get("unit_label"):
        weight_word = WEIGHT_WORD.search(original)
        if weight_word:
            candidates["unit_label"] = [
                FieldCandidate(
                    value="кг",
                    raw_value=weight_word.group(),
                    source="rule",
                    method="weight-word",
                    evidence=weight_word.group(),
                    status=FieldStatus.INFERRED,
                    span=weight_word.span(),
                )
            ]
        elif _default_piece_unit_enabled() and any(str(c.value or "").strip() for c in candidates.get("price", [])):
            # D12: off by default. The spec asks for a manual choice when the unit is missing; switch this on
            # (CANONICAL_DEFAULT_PIECE_UNIT) to bring back the earlier "no unit, a price -> шт" rule.
            candidates["unit_label"] = [
                FieldCandidate(value="шт", source="default", method="default-piece", status=FieldStatus.DEFAULTED)
            ]
    # D1/D10: sold by the kilogram means the net of one unit is 1 kg. A weight in the name (a brick of ~3кг) is only
    # a reference weight of the piece; it is kept as a separate reference field and never decides the net.
    if candidates.get("unit_label"):
        sale_unit = resolve_field("unit_label", candidates["unit_label"])
        explicit_net = any(c.source == "column" and str(c.value or "").strip() for c in candidates.get("net_value", []))
        if (
            sale_unit.value == "кг"
            and sale_unit.status in (FieldStatus.PARSED, FieldStatus.INFERRED)
            and not explicit_net
        ):
            name_nets = [c for c in candidates.get("net_value", []) if c.source == "name" and c.value]
            name_units = {c.evidence: c.value for c in candidates.get("net_unit_label", []) if c.source == "name"}
            for net in name_nets:
                unit = UNITS.get(str(name_units.get(net.evidence, "")).casefold())
                factor = {"кг": Decimal(1), "г": Decimal("0.001")}.get(unit)
                weight = decimal_value(net.value)
                if factor and weight and "estimated_piece_weight" not in candidates:
                    candidates["estimated_piece_weight"] = [
                        FieldCandidate(
                            value=weight * factor,
                            raw_value=net.raw_value,
                            source="name",
                            status=FieldStatus.INFERRED,
                            method="piece-weight-reference",
                            evidence=net.evidence,
                            span=net.span,
                        )
                    ]
            for key in ("net_value", "net_unit_label"):
                candidates[key] = [
                    c for c in candidates.get(key, []) if c.source != "name" or c.method == "price-basis"
                ]
            # A case composition of a kg-priced item ("~3кг 1/12, кг") says nothing about what one kg is.
            for key in ("pack_items_count", "pack_item_value", "pack_item_unit_label"):
                kept = [c for c in candidates.get(key, []) if c.method != "case-ratio"]
                if kept:
                    candidates[key] = kept
                else:
                    candidates.pop(key, None)
    # A kg/l sale unit means the net quantity of one unit is exactly one of that unit; so does a piece (шт) when
    # nothing in the row gives a weight or volume ("манго, шт" is one piece).
    if candidates.get("unit_label") and not any(str(c.value or "").strip() for c in candidates.get("net_value", [])):
        sale_unit = resolve_field("unit_label", candidates["unit_label"])
        if sale_unit.value in ("кг", "л", "шт") and sale_unit.status != FieldStatus.INVALID:
            for key, value in (("net_value", "1"), ("net_unit_label", sale_unit.value)):
                candidates.setdefault(key, []).append(
                    FieldCandidate(
                        value=value,
                        source="rule",
                        method="price-per-unit",
                        evidence=sale_unit.evidence,
                        status=FieldStatus.INFERRED,
                    )
                )
    fields = {key: resolve_field(key, values) for key, values in candidates.items()}
    for key in (
        "name",
        "unit_label",
        "price",
        "currency",
        "price_basis",
        "country_of_origin",
        "net_value",
        "net_unit_label",
        "delivery_multiple",
        "min_order_qty",
        "availability_label",
        "supplier_sku",
        "barcode",
        "pack_items_count",
        "pack_item_value",
        "pack_item_unit_label",
        "pack_type_label",
    ):
        fields.setdefault(key, ParsedField())
    recognized_packages = list(PACKAGE.finditer(original))
    for ratio in re.finditer(r"(?<![\w/])\d+\s*/\s*\d+(?![\w.,])", original):
        if (
            column_count
            or _is_caliber_range(ratio.group())
            or any(c.method == "case-ratio" for c in fields["pack_items_count"].candidates)
        ):
            continue
        if not any(match.start() <= ratio.start() and ratio.end() <= match.end() for match in recognized_packages):
            fields["pack_items_count"].status = FieldStatus.AMBIGUOUS
            fields["pack_items_count"].evidence = ratio.group()
    # Compare net quantities in equivalent measure units rather than raw numbers.
    nv, nu = fields["net_value"], fields["net_unit_label"]
    if nv.status == FieldStatus.AMBIGUOUS and nu.candidates:
        factors = {"г": Decimal(".001"), "кг": Decimal(1), "мл": Decimal(".001"), "л": Decimal(1)}
        measured = []
        for c in nv.candidates:
            units = [u for u in nu.candidates if u.source == c.source and u.evidence == c.evidence]
            if c.value is not None and units and units[0].value in factors:
                family = "mass" if units[0].value in ("г", "кг") else "volume"
                measured.append((family, c.value * factors[units[0].value]))
        if len(measured) == len(nv.candidates) and len(set(measured)) == 1:
            nv.status = nv.candidates[0].status
            nv.conflicts = []
            nu.status = nu.candidates[0].status
            nu.conflicts = []
    # A weight word implies "sold per kg"; a non-unit net quantity in the name contradicts it, so ask for review.
    unit_field = fields["unit_label"]
    if unit_field.candidates and all(c.method == "weight-word" for c in unit_field.candidates):
        unit_factors = {"г": Decimal(".001"), "кг": Decimal(1), "мл": Decimal(".001"), "л": Decimal(1)}
        for net in (c for c in fields["net_value"].candidates if c.source == "name" and c.value is not None):
            units = [
                u for u in fields["net_unit_label"].candidates if u.source == "name" and u.evidence == net.evidence
            ]
            if units and units[0].value in unit_factors and net.value * unit_factors[units[0].value] != 1:
                unit_field.status = FieldStatus.AMBIGUOUS
                break
    row = ParsedProductRow(
        raw.row_number,
        {"row_number": raw.row_number, "cells": dict(raw.cells), "source": dict(raw.source)},
        mapping,
        fields,
        row_type=classify_row(raw, mapped),
    )
    for key, parsed in fields.items():
        # The barcode is a hint kept in the snapshot only (not persisted), so a bad code must not block the row.
        if key != "barcode" and parsed.status in (FieldStatus.AMBIGUOUS, FieldStatus.INVALID):
            row.parse_issues.append(
                {
                    "code": str(parsed.status),
                    "field": key,
                    "level": "error",
                    "review_required": True,
                    "message": f"Поле {key}: {parsed.status}.",
                }
            )
    if row.row_type == "unknown":
        row.parse_issues.append(
            {
                "code": "unknown_row",
                "field": "name",
                "level": "warning",
                "review_required": True,
                "message": "Не определён тип строки.",
            }
        )
    if re.search(r"[~≈]\s*\d|\d+\s*[-–]\s*\d+\s*(?:кг|г|л|мл)", original):
        for key in ("net_value", "net_unit_label", "pack_item_value", "pack_item_unit_label", "pack_items_count"):
            if any(c.source == "name" for c in fields[key].candidates):
                fields[key].status = FieldStatus.AMBIGUOUS
                row.parse_issues.append(
                    {
                        "code": "approximate_package",
                        "field": key,
                        "level": "warning",
                        "review_required": True,
                        "message": "Приблизительная фасовка требует проверки.",
                    }
                )
    if original and fields["name"].status != FieldStatus.AMBIGUOUS:
        fields["name"].value = clean_name(original, fields)
    return row


def override_snapshot(snapshot, updates, *, at=None, actor_id=None):
    """Keep original parsing immutable; store manual values and explicit field resolution."""
    from copy import deepcopy

    result = deepcopy(snapshot or {})
    result.setdefault("overrides", [])
    for key, value in updates.items():
        normalized = normalize_candidate(
            key,
            FieldCandidate(
                value,
                raw_value=json_values(value),
                source="user",
                method="manual-edit",
                status=FieldStatus.MISSING if value is None else FieldStatus.PARSED,
            ),
        )
        if value is None:
            normalized = FieldCandidate(None, source="user", method="manual-edit", status=FieldStatus.MISSING)
        result["overrides"].append(
            {
                "field": key,
                "value": json_values(normalized.value),
                "status": str(normalized.status),
                "source": "user",
                "at": at,
                "actor_id": str(actor_id) if actor_id else None,
            }
        )
    return result


# Pack composition is optional (ТЗ: not a required field, not shown as editable): when it cannot be determined
# the row simply gets no composition. The doubt stays in the snapshot for diagnostics but never blocks the row.
_NON_BLOCKING_ISSUE_FIELDS = frozenset(
    {"pack_items_count", "pack_item_value", "pack_item_unit_label", "pack_type_label"}
)


def effective_issues(snapshot):
    overrides = {x["field"]: x for x in (snapshot or {}).get("overrides", [])}
    return [
        i
        for i in (snapshot or {}).get("parse_issues", [])
        if i.get("review_required")
        and i.get("field") not in _NON_BLOCKING_ISSUE_FIELDS
        and (i.get("field") not in overrides or overrides[i["field"]]["status"] in ("invalid", "ambiguous"))
    ]


_ACCEPTABLE_ISSUE_CODES = frozenset({"ambiguous", "approximate_package", "llm_mapping_review", "llm_value_review"})


def accept_issues(snapshot, *, at=None, actor_id=None):
    """Confirm parser warnings by overriding each flagged field with its current value.

    Issues with code ``invalid`` are never accepted; fields already overridden by the user are skipped
    because ``effective_issues`` does not report them.
    """
    updates = {}
    fields = (snapshot or {}).get("fields", {})
    for issue in effective_issues(snapshot):
        field = issue.get("field")
        if issue.get("code") in _ACCEPTABLE_ISSUE_CODES and field and field not in updates:
            updates[field] = fields.get(field, {}).get("value")
    if not updates:
        return snapshot
    return override_snapshot(snapshot, updates, at=at, actor_id=actor_id)
