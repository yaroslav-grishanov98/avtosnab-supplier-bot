"""Read interface of the accounting data for BE-1 (and the status API): everything is scoped to one venue.

Every function takes a ``venue_id`` and filters by it; nothing here lists a model without that filter. The database
keeps rows consistent with their warehouse's connection and venue, but it does not look at the link's status, so
the reading side does: by default only data of a connection that is ACTIVE *and* has an ACTIVE link to the venue is
returned. A disconnected connection (``include_disconnected=False``) therefore yields nothing, and data of the same
external product id in two connections is never mixed (rows are tied to their own connection's warehouse).

Quantity conventions (docs/wiki/integrations/iiko-capability-matrix.md, Step 0):

* ``stock_qty`` 0 is a real zero and ``None`` is "no number from the source"; a product the warehouse never reported
  has no row at all ("no data").
* Consumption ``qty`` is positive when stock left the warehouse. For a (warehouse, day) covered as COMPLETE, a missing
  row is a real zero; no coverage is "no data" (``None``); PARTIAL coverage means what is shown may be incomplete.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

from accounting.models import (
    AccountingConnection,
    AccountingConsumptionCoverage,
    AccountingConsumptionDaily,
    AccountingStockLevel,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionStatus,
    CoverageStatus,
    LinkStatus,
)

CONSUMPTION_OK = "ok"
CONSUMPTION_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class WarehouseView:
    warehouse_id: UUID
    external_id: str
    name: str
    connection_id: UUID
    connection_status: str
    is_present_in_source: bool


@dataclass(frozen=True)
class StockLevelView:
    warehouse_id: UUID
    external_product_id: str
    stock_qty: Decimal | None
    source_uom: str
    taken_at: datetime
    synced_at: datetime
    is_stale: bool
    reliability: str
    connection_status: str


@dataclass(frozen=True)
class DayValue:
    day: date
    # Decimal("0") for a covered day without rows of the requested kinds, None for a day without coverage.
    qty: Decimal | None
    # True when the day's coverage is PARTIAL: the quantity may be incomplete (known incomplete).
    partial: bool = False


@dataclass(frozen=True)
class ConsumptionView:
    status: str  # "ok" | "unsupported"
    available_kinds: list[str]
    # (warehouse id, external product id) -> one DayValue per day of the window, oldest first. Only products with
    # rows of the requested kinds appear; for the others read ``coverage``: a covered day is a zero.
    series: dict[tuple[UUID, str], list[DayValue]]
    # warehouse id -> {day: "complete" | "partial"} for the days of the window that have coverage.
    coverage: dict[UUID, dict[date, str]] = field(default_factory=dict)


def _live(queryset: QuerySet, venue_id: UUID, include_disconnected: bool) -> QuerySet:
    """Rows of this venue; unless asked otherwise only of an ACTIVE connection with an ACTIVE link to the venue."""
    queryset = queryset.filter(venue_id=venue_id)
    if include_disconnected:
        return queryset
    live_link = AccountingVenueLink.objects.filter(
        connection_id=OuterRef("connection_id"), venue_id=venue_id, status=LinkStatus.ACTIVE
    )
    return queryset.filter(connection__status=ConnectionStatus.ACTIVE).filter(Exists(live_link))


def list_venue_warehouses(venue_id: UUID, *, include_disconnected: bool = False) -> list[WarehouseView]:
    rows = _live(AccountingWarehouse.objects.select_related("connection"), venue_id, include_disconnected).order_by(
        "name", "external_id"
    )
    return [
        WarehouseView(
            warehouse_id=row.id,
            external_id=row.external_id,
            name=row.name,
            connection_id=row.connection_id,
            connection_status=row.connection.status,
            is_present_in_source=row.is_present_in_source,
        )
        for row in rows
    ]


def get_venue_stock(
    venue_id: UUID, *, now: datetime | None = None, include_disconnected: bool = False
) -> list[StockLevelView]:
    now = now or timezone.now()
    stale_after = timedelta(minutes=settings.ACCOUNTING_STOCK_STALE_AFTER_MINUTES)
    rows = _live(
        AccountingStockLevel.objects.select_related("connection", "product"), venue_id, include_disconnected
    ).order_by("warehouse_id", "product__external_id")
    return [
        StockLevelView(
            warehouse_id=row.warehouse_id,
            external_product_id=row.product.external_id,
            stock_qty=row.stock_qty,
            source_uom=row.source_uom,
            taken_at=row.taken_at,
            synced_at=row.synced_at,
            is_stale=now - row.taken_at > stale_after,
            reliability=row.reliability,
            connection_status=row.connection.status,
        )
        for row in rows
    ]


def _venue_connections(venue_id: UUID, include_disconnected: bool) -> QuerySet:
    links = AccountingVenueLink.objects.filter(connection_id=OuterRef("pk"), venue_id=venue_id)
    if not include_disconnected:
        return AccountingConnection.objects.filter(status=ConnectionStatus.ACTIVE).filter(
            Exists(links.filter(status=LinkStatus.ACTIVE))
        )
    return AccountingConnection.objects.filter(Exists(links))


def get_venue_consumption(
    venue_id: UUID,
    *,
    kinds: Collection[str],
    days: int = 7,
    end_day: date | None = None,
    include_disconnected: bool = False,
) -> ConsumptionView:
    """Consumption of the venue's warehouses per product and day, summed over the requested transaction types.

    ``kinds`` is required and has no default: which transaction types count as consumption is BE-1's formula
    (ТЗ 3.3), not ours. ``end_day`` defaults to yesterday; the window is ``days`` days ending on it."""
    kinds = {str(kind) for kind in kinds}
    if not kinds:
        raise ValueError("kinds must name at least one transaction type")
    if days < 1:
        raise ValueError("days must be at least 1")
    end_day = end_day or timezone.localdate() - timedelta(days=1)
    window = [end_day - timedelta(days=offset) for offset in range(days - 1, -1, -1)]

    connections = list(_venue_connections(venue_id, include_disconnected))
    if connections and not any(_reads_consumption(c) for c in connections):
        return ConsumptionView(status=CONSUMPTION_UNSUPPORTED, available_kinds=[], series={})

    rows = _live(AccountingConsumptionDaily.objects.select_related("product"), venue_id, include_disconnected).filter(
        day__range=(window[0], window[-1])
    )
    available: set[str] = set()
    sums: dict[tuple[UUID, str], dict[date, Decimal]] = {}
    for row in rows:
        available.add(row.transaction_type)
        if row.transaction_type in kinds:
            per_day = sums.setdefault((row.warehouse_id, row.product.external_id), {})
            per_day[row.day] = per_day.get(row.day, Decimal(0)) + row.qty

    coverage: dict[UUID, dict[date, str]] = {}
    for item in _live(
        AccountingConsumptionCoverage.objects.filter(venue_id=venue_id), venue_id, include_disconnected
    ).filter(day__range=(window[0], window[-1])):
        coverage.setdefault(item.warehouse_id, {})[item.day] = item.status

    series = {
        pair: [
            DayValue(
                day=day,
                qty=per_day[day] if day in per_day else (Decimal(0) if day in coverage.get(pair[0], {}) else None),
                partial=coverage.get(pair[0], {}).get(day) == CoverageStatus.PARTIAL,
            )
            for day in window
        ]
        for pair, per_day in sorted(sums.items(), key=lambda item: (str(item[0][0]), item[0][1]))
    }
    return ConsumptionView(status=CONSUMPTION_OK, available_kinds=sorted(available), series=series, coverage=coverage)


def _reads_consumption(connection: AccountingConnection) -> bool:
    capabilities = connection.capabilities
    return isinstance(capabilities, dict) and capabilities.get("read_consumption") is True
