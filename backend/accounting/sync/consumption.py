"""Consumption sync: daily totals per warehouse, product and transaction type, with explicit coverage.

For every complete past day of the window (today is never read) and every warehouse that is linked and present in
the source, one OLAP report is asked from the adapter. The result is stored per (warehouse, day) in its own
transaction: the daily rows (replaced on a re-read) and a coverage row that says how the day was read:

* COMPLETE — read as a whole. A day without rows is a real zero ("no data" is the absence of a coverage row).
* PARTIAL — read, but some rows could not be stored (a product missing from the stored catalogue, or the adapter
  said so). What could be mapped is stored. Such a pair is *known incomplete*: it is asked again only after the
  catalogue changed, so it never burns the per-session report cap again and again.

A pair that failed or was not reached has no coverage row and is asked in a later session. Pairs that were read
before the day settled (ACCOUNTING_CONSUMPTION_SETTLE_HOURS after its end) are read once more: a late shift
closing or a back-dated document may still add rows.

Which transaction types count as consumption is the reader's choice (BE-1, ``kinds``); only the types the spec
says to leave out (ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES: receipts, revenue, discounts) are not stored. The stored
quantity is positive when stock left the warehouse. Zero totals are not stored.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounting.adapters.base import AccountingAdapter
from accounting.dto import AdapterError, ConsumptionEventDTO
from accounting.models import (
    AccountingConnection,
    AccountingConsumptionCoverage,
    AccountingConsumptionDaily,
    AccountingProduct,
    AccountingSyncJob,
    AccountingWarehouse,
    CoverageStatus,
    SyncKind,
)
from accounting.sync.keys import id_key
from accounting.sync.stock import ProductRef, load_products, readable_warehouses

logger = logging.getLogger(__name__)

CONSUMPTION_UNSUPPORTED = "consumption_unsupported"
DAY_NOT_READ = "day_not_read"
# The adapter says the source cannot give consumption at all: the capability is switched off for good.
UNSUPPORTED_CODES = frozenset({"api_not_supported", "endpoint_not_found"})
BULK_BATCH_SIZE = 1000
QUANTITY_STEP = Decimal("0.000001")  # AccountingConsumptionDaily.qty has six decimal places

_MESSAGES = {
    DAY_NOT_READ: "Расход склада за день не прочитан; запрос будет повторён в следующей синхронизации.",
}


@dataclass
class ConsumptionStats:
    unsupported: bool = False
    warehouses: int = 0
    # Counted in (warehouse, day) pairs.
    complete: int = 0
    partial: int = 0
    failed: int = 0  # asked, but the report was not read
    remaining: int = 0  # not asked yet (the provider stopped early); read in a later session
    rows: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    stopped: AdapterError | None = None

    @property
    def stored(self) -> int:
        return self.complete + self.partial

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": SyncKind.CONSUMPTION.value,
            "unsupported": self.unsupported,
            "warehouses": self.warehouses,
            "complete": self.complete,
            "partial": self.partial,
            "failed": self.failed,
            "remaining": self.remaining,
            "rows": self.rows,
            "errors": list(self.errors),
        }
        if self.stopped is not None:
            data["stopped"] = {"code": self.stopped.code, "message": self.stopped.message}
        return data


def consumption_enabled(connection: AccountingConnection) -> bool:
    """Only an explicit JSON true counts: a missing, null or "true" value never starts OLAP reports."""
    capabilities = connection.capabilities
    return isinstance(capabilities, dict) and capabilities.get("read_consumption") is True


def load_product_catalog(connection: AccountingConnection) -> dict[str, tuple[str, str]]:
    """Article -> (external product id, main unit) of the stored catalogue, for the adapter's ``product_catalog``.

    Articles shared by two products are left out (the OLAP report is grouped by article, so they cannot be told
    apart); deleted products are left out too."""
    rows = list(
        AccountingProduct.objects.filter(connection=connection, is_deleted=False)
        .exclude(article="")
        .values_list("article", "external_id", "unit_name")
    )
    shared = {article for article, count in _count_by_article(rows).items() if count > 1}
    return {article: (external_id, unit) for article, external_id, unit in rows if article not in shared}


def _count_by_article(rows: list[tuple[str, str, str]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for article, _, _ in rows:
        counts[article] += 1
    return counts


def _settled_at(day: date) -> datetime:
    """From this moment on a read of ``day`` is final: its end plus ACCOUNTING_CONSUMPTION_SETTLE_HOURS."""
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.get_default_timezone())
    return end + timedelta(hours=settings.ACCOUNTING_CONSUMPTION_SETTLE_HOURS)


def _needs_reading(
    coverage: AccountingConsumptionCoverage | None, day: date, catalog_synced_at: datetime | None
) -> bool:
    if coverage is None:
        return True
    if coverage.fetched_at < _settled_at(day):
        return True  # read before the day settled
    if coverage.status == CoverageStatus.PARTIAL:
        # Known incomplete: only a newer catalogue may map the products that were missing.
        return catalog_synced_at is not None and catalog_synced_at > coverage.fetched_at
    return False


def sync_consumption(
    adapter: AccountingAdapter,
    connection: AccountingConnection,
    job: AccountingSyncJob,
    *,
    today: date,
    window_days: int | None = None,
) -> ConsumptionStats:
    stats = ConsumptionStats()
    if not consumption_enabled(connection):
        stats.unsupported = True
        return stats
    warehouses = list(readable_warehouses(connection))
    stats.warehouses = len(warehouses)
    if not warehouses:
        return stats

    window = window_days or settings.ACCOUNTING_CONSUMPTION_WINDOW_DAYS
    days = [today - timedelta(days=offset) for offset in range(1, window + 1)]  # newest first; today is excluded
    coverage = {
        (row.warehouse_id, row.day): row
        for row in AccountingConsumptionCoverage.objects.filter(connection=connection, day__in=days)
    }
    pending = [
        (day, [w for w in warehouses if _needs_reading(coverage.get((w.id, day)), day, connection.catalog_synced_at)])
        for day in days
    ]
    pending = [(day, wanted) for day, wanted in pending if wanted]
    if not pending:
        return stats

    products = load_products(connection)
    excluded = {str(kind).upper() for kind in settings.ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES}
    for index, (day, wanted) in enumerate(pending):
        try:
            result = adapter.read_consumption([w.external_id for w in wanted], day, day)
        except AdapterError as exc:
            if exc.code in UNSUPPORTED_CODES:
                _switch_off_consumption(connection)
                stats.unsupported = True
                return stats
            raise
        events: dict[str, list[ConsumptionEventDTO]] = defaultdict(list)
        for event in result.events:
            events[id_key(event.external_warehouse_id)].append(event)
        for warehouse in wanted:
            pair = (warehouse.external_id, day)
            if result.covered.get(pair):
                status = CoverageStatus.COMPLETE
            elif pair in result.partial:
                status = CoverageStatus.PARTIAL
            else:
                if result.stopped is None:
                    stats.failed += 1
                    stats.errors.append(_error(warehouse, day, DAY_NOT_READ))
                else:
                    stats.remaining += 1
                continue
            stored_status, rows = _store_pair(
                connection, warehouse, day, events.get(id_key(warehouse.external_id), []), status, products, excluded
            )
            stats.rows += rows
            if stored_status == CoverageStatus.COMPLETE:
                stats.complete += 1
            else:
                stats.partial += 1
        if result.stopped is not None:
            stats.stopped = result.stopped
            stats.remaining += sum(len(later) for _, later in pending[index + 1 :])
            break
    return stats


def _error(warehouse: AccountingWarehouse, day: date, code: str) -> dict[str, str]:
    return {
        "external_warehouse_id": warehouse.external_id,
        "day": day.isoformat(),
        "code": code,
        "message": _MESSAGES[code],
    }


def _store_pair(
    connection: AccountingConnection,
    warehouse: AccountingWarehouse,
    day: date,
    events: list[ConsumptionEventDTO],
    status: str,
    products: dict[str, ProductRef],
    excluded: set[str],
) -> tuple[str, int]:
    """Replace the daily rows of (warehouse, day) and write its coverage; returns the stored status and row count."""
    totals: dict[tuple[Any, str], list[Any]] = {}
    unknown_products = 0
    for event in events:
        if event.transaction_type.upper() in excluded:
            continue
        product = products.get(id_key(event.external_product_id))
        if product is None:
            unknown_products += 1
            continue
        entry = totals.setdefault((product.product_id, event.transaction_type[:64]), [Decimal(0), ""])
        entry[0] += event.qty
        entry[1] = (event.source_uom or entry[1])[:64]
    if unknown_products:
        status = CoverageStatus.PARTIAL
    rows = [
        AccountingConsumptionDaily(
            connection=connection,
            venue_id=warehouse.venue_id,
            warehouse=warehouse,
            product_id=product_id,
            day=day,
            transaction_type=transaction_type,
            qty=quantity,
            source_uom=unit,
        )
        for (product_id, transaction_type), (total, unit) in totals.items()
        if (quantity := total.quantize(QUANTITY_STEP)) != 0
    ]
    with transaction.atomic():
        AccountingConsumptionDaily.objects.filter(warehouse=warehouse, day=day).delete()
        AccountingConsumptionDaily.objects.bulk_create(rows, batch_size=BULK_BATCH_SIZE)
        AccountingConsumptionCoverage.objects.update_or_create(
            warehouse=warehouse,
            day=day,
            defaults={
                "connection": connection,
                "venue_id": warehouse.venue_id,
                "status": status,
                "fetched_at": timezone.now(),
            },
        )
    return status, len(rows)


def _switch_off_consumption(connection: AccountingConnection) -> None:
    """Record that the source cannot give consumption; a catalogue sync keeps it (merge_capabilities)."""
    capabilities = dict(connection.capabilities) if isinstance(connection.capabilities, dict) else {}
    capabilities["read_consumption"] = False
    now = timezone.now()
    AccountingConnection.objects.filter(pk=connection.pk).update(capabilities=capabilities, updated_at=now)
    connection.capabilities, connection.updated_at = capabilities, now
    logger.warning("Подключение %s: расход недоступен на сервере, чтение расхода выключено", connection.pk)
