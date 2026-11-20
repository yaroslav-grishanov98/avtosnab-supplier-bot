"""The only non-GET request the client can send: one narrow, validated OLAP TRANSACTIONS report.

POST /api/v2/reports/olap builds a report and changes nothing on the server. It is allowed only for a period of
at most one day, a single store filter (up to ``Settings.olap_max_store_values`` stores, one in the prototype),
and without the balance fields (StartBalance.* / FinalBalance.*) that make iiko sum the whole database history.
The guard below is what keeps "read-only" true; the client calls it before sending.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .exceptions import ErrorKind, IikoError

OLAP_REPORT_PATH = "/api/v2/reports/olap"
OLAP_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
DATE_FIELDS = ("DateTime.DateTyped", "DateTime.Typed")
GROUP_FIELDS = ("Account.Name", "Product.Num", "Product.Name", "Product.MeasureUnit", "TransactionType")
AGGREGATE_FIELDS = ("Amount", "Amount.In", "Amount.Out")
ALLOWED_KEYS = frozenset(
    {"reportType", "buildSummary", "groupByRowFields", "groupByColFields", "aggregateFields", "filters"}
)
FIELD_LIST_KEYS = ("groupByRowFields", "groupByColFields", "aggregateFields")
FORBIDDEN_PREFIXES = ("StartBalance", "FinalBalance")
# The fields a store filter may use (the store_field values the adapter sends). Any other field would let a
# one-day report pass as "one store" while not narrowing it to a store at all.
STORE_FILTER_FIELDS = ("Account.Name", "Store", "Store.Name", "Store.Id")
MAX_LISTED_FIELDS = 32


def _violation(message: str) -> IikoError:
    return IikoError(ErrorKind.READ_ONLY_VIOLATION, "Запрос OLAP не разрешён: " + message)


def _allowed(columns: Mapping[str, Any], name: str, flag: str) -> bool:
    return bool((columns.get(name) or {}).get(flag))


def build_transactions_body(
    columns: Mapping[str, Any], *, day_from: str, day_to: str, store_field: str, store_value: str
) -> dict | None:
    """Per-product movements of one store for [day_from, day_to). None when the report lacks the needed fields."""
    groups = [f for f in GROUP_FIELDS if _allowed(columns, f, "groupingAllowed")]
    aggregates = [f for f in AGGREGATE_FIELDS if _allowed(columns, f, "aggregationAllowed")]
    date_field = next((f for f in DATE_FIELDS if _allowed(columns, f, "filteringAllowed")), None)
    has_product = "Product.Num" in groups or "Product.Name" in groups
    if not (date_field and has_product and "TransactionType" in groups and "Amount" in aggregates):
        return None
    if store_field not in STORE_FILTER_FIELDS or not _allowed(columns, store_field, "filteringAllowed"):
        return None
    return {
        "reportType": "TRANSACTIONS",
        "buildSummary": "false",
        "groupByRowFields": groups,
        "groupByColFields": [],
        "aggregateFields": aggregates,
        "filters": {
            date_field: {"filterType": "DateRange", "periodType": "CUSTOM", "from": day_from, "to": day_to},
            store_field: {"filterType": "IncludeValues", "values": [store_value]},
        },
    }


def validate_olap_body(body: Mapping[str, Any], *, max_store_values: int = 1) -> None:
    """Raise READ_ONLY_VIOLATION unless the body is a <=1-day TRANSACTIONS report of the allowed stores."""
    if not isinstance(body, Mapping):
        raise _violation("тело запроса должно быть объектом")
    if not set(body) <= ALLOWED_KEYS:
        raise _violation("лишние ключи тела: " + ", ".join(sorted(map(str, set(body) - ALLOWED_KEYS))))
    if body.get("reportType") != "TRANSACTIONS":
        raise _violation("разрешён только reportType=TRANSACTIONS")

    filters = body.get("filters") or {}
    if not isinstance(filters, Mapping):
        raise _violation("filters должен быть объектом")
    listed: list[Any] = list(filters)
    for key in FIELD_LIST_KEYS:
        value = body.get(key) or []
        if not isinstance(value, list):
            raise _violation(f"{key} должен быть списком")
        listed.extend(value)
    if len(listed) > MAX_LISTED_FIELDS:
        raise _violation("слишком много полей")
    if any(not isinstance(name, str) for name in listed):
        raise _violation("имена полей должны быть строками")
    forbidden = tuple(prefix.lower() for prefix in FORBIDDEN_PREFIXES)
    if any(name.lower().startswith(forbidden) for name in listed):
        raise _violation("поля начальных и конечных остатков считают всю историю базы")

    date_filters = [name for name in filters if name in DATE_FIELDS]
    if len(date_filters) != 1:
        raise _violation("нужен ровно один фильтр по дате (DateTime.DateTyped)")
    date_filter = filters[date_filters[0]]
    if not isinstance(date_filter, Mapping):
        raise _violation("фильтр даты должен быть объектом")
    if date_filter.get("filterType") != "DateRange" or date_filter.get("periodType") != "CUSTOM":
        raise _violation("фильтр даты должен быть DateRange/CUSTOM")
    try:
        start = datetime.strptime(str(date_filter.get("from")), OLAP_DATE_FORMAT)
        end = datetime.strptime(str(date_filter.get("to")), OLAP_DATE_FORMAT)
    except ValueError:
        raise _violation("даты в формате yyyy-MM-ddTHH:mm:ss.SSS") from None
    if not timedelta(0) < end - start <= timedelta(days=1):
        raise _violation("период должен быть не длиннее одних суток")

    others = {name: spec for name, spec in filters.items() if name not in DATE_FIELDS}
    if len(others) != 1:
        raise _violation("нужен ровно один фильтр по складу")
    ((store_field, store_filter),) = others.items()
    if store_field not in STORE_FILTER_FIELDS:
        raise _violation("фильтр склада допускается только по полям: " + ", ".join(STORE_FILTER_FIELDS))
    if not isinstance(store_filter, Mapping):
        raise _violation("фильтр склада должен быть объектом")
    values = store_filter.get("values")
    if (
        store_filter.get("filterType") != "IncludeValues"
        or not isinstance(values, list)
        or not 1 <= len(values) <= max_store_values
        or not all(isinstance(v, str) and v for v in values)
    ):
        raise _violation(f"фильтр склада — IncludeValues, от 1 до {max_store_values} складов")
