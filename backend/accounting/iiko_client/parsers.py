"""Format detection and tolerant parsing of iiko responses (JSON or XML), ported from iiko_diagnostics.

The real format is detected from the body/Content-Type, never assumed. Field names are unverified, so parsers
report what they could not find instead of guessing, and expose only keys (never values) when a schema does
not match. Unlike the prototype, JSON numbers with a fraction and balance amounts are ``Decimal``.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .exceptions import ErrorKind, IikoError

_LIST_WRAPPER_KEYS = ("response", "items", "data", "result", "products", "list", "records")
_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


# -- format detection -----------------------------------------------------
def detect_format(content_type: str | None, body: bytes) -> str:
    """Return json | xml | html | empty | text."""
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:200]
    if not head:
        return "empty"
    low = head.lower()
    if low.startswith((b"<!doctype html", b"<html")):
        return "html"
    if head[:1] in (b"{", b"["):
        return "json"
    if head[:1] == b"<":
        return "xml"
    ct = (content_type or "").lower()
    if "json" in ct:
        return "json"
    if "xml" in ct:
        return "xml"
    return "text"


def parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8-sig"), parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise IikoError(ErrorKind.INVALID_RESPONSE, "Ответ iiko не является корректным JSON") from None


def parse_xml(body: bytes) -> ET.Element:
    # DOCTYPE/ENTITY are refused outright, so entity expansion attacks never reach the parser.
    if _DOCTYPE_RE.search(body[:4096]):
        raise IikoError(ErrorKind.INVALID_RESPONSE, "XML с DOCTYPE/ENTITY отклонён из соображений безопасности")
    try:
        return ET.fromstring(body.lstrip(b"\xef\xbb\xbf \t\r\n"))
    except ET.ParseError:
        raise IikoError(ErrorKind.INVALID_RESPONSE, "Ответ iiko не является корректным XML") from None


def parse_body(content_type: str | None, body: bytes) -> tuple[str, Any]:
    """Detect the format and parse. Raises INVALID_RESPONSE for html/empty/broken bodies."""
    fmt = detect_format(content_type, body)
    if fmt == "json":
        return fmt, parse_json(body)
    if fmt == "xml":
        return fmt, parse_xml(body)
    if fmt == "html":
        raise IikoError(ErrorKind.INVALID_RESPONSE, "Получена HTML-страница вместо данных API iiko")
    if fmt == "empty":
        raise IikoError(ErrorKind.INVALID_RESPONSE, "Пустой ответ сервера iiko")
    raise IikoError(ErrorKind.INVALID_RESPONSE, "Не удалось определить формат ответа iiko (ни JSON, ни XML)")


def describe_shape(data: Any, depth: int = 4) -> str:
    """Structure of a JSON value (types and key names only, never values)."""
    if isinstance(data, dict):
        if depth == 0:
            return "object{...}"
        keys = list(data)[:12]
        inner = ", ".join(f"{k}: {describe_shape(data[k], depth - 1)}" for k in keys)
        more = ", ..." if len(data) > len(keys) else ""
        return f"object{{{inner}{more}}}"
    if isinstance(data, list):
        if not data:
            return "array[0]"
        kinds = sorted({type(i).__name__ for i in data})
        first = describe_shape(data[0], depth - 1) if depth else "..."
        return f"array[{len(data)}] of {'/'.join(kinds)}; first: {first}"
    if isinstance(data, str):
        return f"string(len={len(data)})"
    return type(data).__name__


# -- catalogue records ------------------------------------------------------
@dataclass
class ParsedCatalog:
    fmt: str
    records: list[dict]  # all valid records (id + name present)
    invalid_records: int = 0
    observed_keys: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.records and self.invalid_records == 0


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _normalise(raw: dict[str, Any]) -> dict | None:
    rid, name = _text(raw.get("id")), _text(raw.get("name"))
    if rid is None or name is None:
        return None
    return {
        "id": rid,
        "name": name,
        "code": _text(raw.get("code")),
        "type": _text(raw.get("type")),
        "deleted": _bool(raw.get("deleted")),
        "parent_id": _text(raw.get("parentid")),
        "main_unit": _text(raw.get("mainunit")),
    }


def _json_items(data: Any) -> list | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_WRAPPER_KEYS:
            if isinstance(data.get(key), list):
                return data[key]
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return None


def parse_catalog(content_type: str | None, body: bytes) -> ParsedCatalog:
    """Extract id/name records from a JSON or XML catalogue response.

    Raises INVALID_RESPONSE for unparsable bodies and SCHEMA_MISMATCH when records exist but none carry both
    an id and a name.
    """
    fmt, data = parse_body(content_type, body)
    candidates: list[dict[str, Any]] = []
    observed: set[str] = set()

    if fmt == "json":
        items = _json_items(data)
        if items is None:
            raise IikoError(
                ErrorKind.SCHEMA_MISMATCH,
                "JSON не содержит списка записей (ожидался массив или объект со списком). Структура: "
                + describe_shape(data),
            )
        for item in items:
            if isinstance(item, dict):
                lowered = {str(k).lower(): v for k, v in item.items()}
                candidates.append(lowered)
                observed.update(lowered)
            else:
                candidates.append({})
    else:
        for element in data.iter():
            children = {_local(c.tag): (c.text or "").strip() for c in element if len(c) == 0}
            if "id" in children or "name" in children:
                candidates.append(children)
                observed.update(children)

    records: list[dict] = []
    invalid = 0
    for cand in candidates:
        rec = _normalise(cand)
        if rec is None:
            invalid += 1
        else:
            records.append(rec)

    if candidates and not records:
        raise IikoError(
            ErrorKind.SCHEMA_MISMATCH,
            "Записи получены, но ни в одной нет пары id+name. Найденные поля: "
            + (", ".join(sorted(observed)[:20]) or "нет")
            + (". Структура: " + describe_shape(data) if fmt == "json" else ""),
        )
    return ParsedCatalog(fmt, records, invalid, sorted(observed))


def parse_records(content_type: str | None, body: bytes) -> list[dict]:
    """The object records of a JSON list response (bare or wrapped, e.g. OLAP ``{"data": [...]}``), keys as sent.

    Unlike ``parse_catalog`` nothing is normalised away (products need ``num``, ``parent``, ``mainUnit``).
    Raises INVALID_RESPONSE for unparsable bodies and SCHEMA_MISMATCH when there is no list of records.
    """
    fmt, data = parse_body(content_type, body)
    items = _json_items(data) if fmt == "json" else None
    if items is None:
        raise IikoError(
            ErrorKind.SCHEMA_MISMATCH,
            "Ответ iiko не содержит списка записей JSON. Структура: "
            + (describe_shape(data) if fmt == "json" else fmt),
        )
    return [item for item in items if isinstance(item, dict)]


# -- balances ---------------------------------------------------------------
_STORE_KEYS = ("store", "storeid", "store_id")
_PRODUCT_KEYS = ("product", "productid", "product_id")
_AMOUNT_KEYS = ("amount", "quantity", "balance")


def _ref_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    return _text(value)


def _first(lowered: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def decimal_or_none(value: Any) -> Decimal | None:
    """Exact quantity from a JSON number or XML text; None for missing, boolean or non-finite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


@dataclass
class ParsedBalances:
    fmt: str
    # {"product_id", "store_id", "amount": Decimal | None}; None: the amount field holds no usable number.
    rows: list[dict]
    invalid_rows: int = 0
    observed_keys: list[str] = field(default_factory=list)


def parse_balances(content_type: str | None, body: bytes) -> ParsedBalances:
    fmt, data = parse_body(content_type, body)
    candidates: list[dict[str, Any]] = []
    if fmt == "json":
        items = _json_items(data)
        if items is None:
            raise IikoError(ErrorKind.SCHEMA_MISMATCH, "JSON остатков не содержит списка записей")
        candidates = [{str(k).lower(): v for k, v in i.items()} for i in items if isinstance(i, dict)]
        skipped = len(items) - len(candidates)
    else:
        skipped = 0
        for element in data.iter():
            children = {_local(c.tag): (c.text or "").strip() for c in element if len(c) == 0}
            if _first(children, _PRODUCT_KEYS) is not None and _first(children, _AMOUNT_KEYS) is not None:
                candidates.append(children)

    observed = sorted({k for c in candidates for k in c})
    rows: list[dict] = []
    invalid = skipped
    for cand in candidates:
        product_id = _ref_id(_first(cand, _PRODUCT_KEYS))
        store_id = _ref_id(_first(cand, _STORE_KEYS))
        if product_id is None or not any(key in cand for key in _AMOUNT_KEYS):
            invalid += 1
            continue
        # A present amount without a usable number stays None: "the server gave no number" is not a zero.
        amount = decimal_or_none(_first(cand, _AMOUNT_KEYS))
        rows.append({"product_id": product_id, "store_id": store_id, "amount": amount})

    # No usable amount at all is a changed schema, not "unknown" for every product: it must not null all levels.
    if ((candidates or skipped) and not rows) or (rows and all(row["amount"] is None for row in rows)):
        raise IikoError(
            ErrorKind.SCHEMA_MISMATCH,
            "Записи остатков получены, но поля товара/количества не распознаны. Найденные поля: "
            + ", ".join(observed[:20]),
        )
    return ParsedBalances(fmt, rows, invalid, observed)


# -- server info ------------------------------------------------------------
def parse_server_info(content_type: str | None, body: bytes) -> dict[str, str]:
    """Return only non-identifying fields (version, edition)."""
    fmt, data = parse_body(content_type, body)
    wanted = {"version", "edition"}
    found: dict[str, str] = {}
    if fmt == "json" and isinstance(data, dict):
        for k, v in data.items():
            text = _text(v)
            if str(k).lower() in wanted and text:
                found[str(k).lower()] = text
    elif fmt == "xml":
        for element in data.iter():
            name = _local(element.tag)
            if name in wanted and (element.text or "").strip():
                found.setdefault(name, element.text.strip())
    return found
