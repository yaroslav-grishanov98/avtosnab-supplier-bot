"""Stock sync: one snapshot header per warehouse and an upsert of its current levels.

Only warehouses from the last catalogue sync are read: present in the source,
with a venue and an ACTIVE link of that venue to the connection. Each warehouse
is stored in its own transaction, so a failed warehouse never rolls back the
others. Snapshot status:

* FAILED — the adapter could not read the warehouse (``ok=False``, also
  ``warehouse_not_found``), the read was aborted before it, or storing it
  failed. Its levels are left exactly as they were.
* PARTIAL — read, but with item errors (or items we could not store), or an
  empty answer for a warehouse that has non-zero levels (``empty_answer``:
  restricted rights or a server fault look exactly like that). What came is
  upserted; nothing is filled with zeros.
* COMPLETE — read as a whole. Only then, and only when the connection's
  ``capabilities["omits_zero_balances"] is True``, the warehouse's known levels
  missing from the answer become 0. A product the warehouse never reported
  keeps having no row: "no data" is not zero (docs/adr/0020).

``stock_qty`` stays Decimal or None; None is stored as NULL with
``Reliability.SOURCE_ERROR`` and never turns into 0. A product repeated in one
warehouse's answer is stored the same way: we cannot tell which row is right.

Warehouse and product ids from the adapter are matched to the stored ones
case-insensitively (``sync.keys.id_key``): iiko GUID case may differ between
methods. Stored ``external_id`` values are never rewritten.

``signals.stock_snapshot_ready`` is sent on commit, once per venue that got at
least one stored (complete or partial) snapshot — registered in a ``finally``,
so warehouses already committed are announced even if the sync is cut short.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any, NamedTuple
from uuid import UUID

from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

from accounting.adapters.base import AccountingAdapter
from accounting.dto import AdapterError, ItemErrorDTO, StockItemDTO, StockSnapshotDTO
from accounting.models import (
    AccountingConnection,
    AccountingProduct,
    AccountingStockLevel,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    LinkStatus,
    Reliability,
    SnapshotStatus,
    SyncKind,
)
from accounting.signals import stock_snapshot_ready
from accounting.sync.keys import id_key

logger = logging.getLogger(__name__)

WAREHOUSE_FAILED = "warehouse_failed"
WAREHOUSE_NOT_RETURNED = "warehouse_not_returned"
PRODUCT_NOT_IN_CATALOG = "product_not_in_catalog"
DUPLICATE_ITEM = "duplicate_item"
QUANTITY_MISSING = "quantity_missing"
STORE_FAILED = "store_failed"
INTERNAL_ERROR = "internal_error"
EMPTY_ANSWER = "empty_answer"

_MESSAGES = {
    EMPTY_ANSWER: (
        "Учётная система вернула пустой ответ по складу, на котором были ненулевые остатки; "
        "остатки не обнулены и оставлены прежними."
    ),
    WAREHOUSE_FAILED: "Остатки склада не прочитаны.",
    WAREHOUSE_NOT_RETURNED: "Учётная система не вернула остатки склада.",
    PRODUCT_NOT_IN_CATALOG: "Товара нет в сохранённом каталоге; остаток не сохранён до следующей синхронизации каталога.",
    DUPLICATE_ITEM: "Товар повторяется в ответе по складу; остаток сохранён пустым.",
    QUANTITY_MISSING: "Учётная система не сообщила количество остатка по товару.",
    STORE_FAILED: "Остатки склада не удалось сохранить.",
    INTERNAL_ERROR: "Внутренняя ошибка при чтении остатков; склад не обновлён.",
}
# Keeps one INSERT ... ON CONFLICT well below PostgreSQL's 65535 bind parameters.
BULK_BATCH_SIZE = 1000
_SOURCE_UOM_LENGTH = AccountingStockLevel._meta.get_field("source_uom").max_length
_SOURCE_VERSION_LENGTH = AccountingStockSnapshot._meta.get_field("source_version").max_length
_ERROR_CODE_LENGTH = AccountingStockSnapshot._meta.get_field("error_code").max_length


class ProductRef(NamedTuple):
    product_id: UUID
    unit_name: str


@dataclass
class StockStats:
    warehouses: int = 0
    complete: int = 0
    partial: int = 0
    failed: int = 0
    items: int = 0
    zero_filled: int = 0
    # {"external_warehouse_id", "status", "code", "message"} for every warehouse that is not COMPLETE.
    errors: list[dict[str, str]] = field(default_factory=list)
    # Set when the adapter aborted the read; the warehouses it did not return are FAILED.
    error: AdapterError | None = None

    @property
    def stored(self) -> int:
        return self.complete + self.partial

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": SyncKind.STOCK.value,
            "warehouses": self.warehouses,
            "complete": self.complete,
            "partial": self.partial,
            "failed": self.failed,
            "items": self.items,
            "zero_filled": self.zero_filled,
            "errors": list(self.errors),
        }
        if self.error is not None:
            data["error"] = {"code": self.error.code, "message": self.error.message}
        return data


@dataclass
class _Stored:
    snapshot: AccountingStockSnapshot
    items: int = 0
    zero_filled: int = 0
    message: str = ""


def readable_warehouses(connection: AccountingConnection) -> QuerySet[AccountingWarehouse]:
    """Warehouses the stock sync reads: from the last catalogue, with a venue linked ACTIVE to the connection."""
    live_link = AccountingVenueLink.objects.filter(
        connection=connection, venue=OuterRef("venue"), status=LinkStatus.ACTIVE
    )
    return (
        AccountingWarehouse.objects.filter(connection=connection, venue__isnull=False, is_present_in_source=True)
        .filter(Exists(live_link))
        .order_by("external_id")
    )


def load_products(connection: AccountingConnection) -> dict[str, ProductRef]:
    """The connection's stored catalogue by ``id_key`` of the external id: one query per stock job."""
    products: dict[str, ProductRef] = {}
    rows = (
        AccountingProduct.objects.filter(connection=connection)
        .order_by("external_id")
        .values_list("external_id", "id", "unit_name")
    )
    for external_id, product_id, unit_name in rows:
        products.setdefault(id_key(external_id), ProductRef(product_id=product_id, unit_name=unit_name))
    return products


def omits_zero_balances(connection: AccountingConnection) -> bool:
    # Only a stored JSON true counts: a missing, null or "true" value must never zero real stock.
    capabilities = connection.capabilities
    return isinstance(capabilities, dict) and capabilities.get("omits_zero_balances") is True


def sync_stock(
    adapter: AccountingAdapter,
    connection: AccountingConnection,
    job: AccountingSyncJob,
    *,
    at: datetime | None = None,
    products: Mapping[str, ProductRef] | None = None,
) -> StockStats:
    """Read and store the balances of every readable warehouse at ``at`` (now by default, one moment per sync).

    ``products`` is ``load_products(connection)``; the runner passes the map it already built for the adapter,
    direct callers may leave it out. An ``AdapterError`` raised while reading is not propagated: it ends up in
    ``StockStats.error``, the warehouses already stored stay stored and the rest get FAILED snapshots. Any other
    exception while storing one warehouse fails only that warehouse; one raised by the adapter's read fails the
    warehouses not read yet and is re-raised.
    """
    at = at or timezone.now()
    warehouses = list(readable_warehouses(connection))
    stats = StockStats(warehouses=len(warehouses))
    if not warehouses:
        return stats

    by_key: dict[str, AccountingWarehouse] = {}
    for warehouse in warehouses:
        by_key.setdefault(id_key(warehouse.external_id), warehouse)
    if products is None:
        products = load_products(connection)
    zero_fill = omits_zero_balances(connection)
    ready: dict[UUID, list[tuple[UUID, UUID]]] = defaultdict(list)
    done: set[UUID] = set()

    def record(warehouse: AccountingWarehouse, stored: _Stored) -> None:
        snapshot = stored.snapshot
        stats.items += stored.items
        stats.zero_filled += stored.zero_filled
        if snapshot.status == SnapshotStatus.COMPLETE:
            stats.complete += 1
        else:
            stats.errors.append(
                {
                    "external_warehouse_id": warehouse.external_id,
                    "status": snapshot.status,
                    "code": snapshot.error_code,
                    "message": stored.message,
                }
            )
            if snapshot.status == SnapshotStatus.PARTIAL:
                stats.partial += 1
            else:
                stats.failed += 1
        if snapshot.status != SnapshotStatus.FAILED:
            ready[warehouse.venue_id].append((warehouse.id, snapshot.id))

    try:
        crash: Exception | None = None
        try:
            for dto in adapter.read_stock([warehouse.external_id for warehouse in warehouses], at):
                warehouse = by_key.get(id_key(dto.external_warehouse_id))
                if warehouse is None or warehouse.id in done:
                    logger.warning(
                        "Остатки подключения %s: адаптер вернул лишний или повторный склад %s, пропущен",
                        connection.id,
                        dto.external_warehouse_id,
                    )
                    continue
                done.add(warehouse.id)
                try:
                    stored = _store_warehouse(connection, warehouse, dto, job, at, products, zero_fill)
                except Exception as exc:
                    # Type only: the text of an unexpected error may carry data that must not reach the log.
                    logger.error(
                        "Остатки подключения %s: склад %s не сохранён (%s)",
                        connection.id,
                        warehouse.external_id,
                        type(exc).__name__,
                    )
                    stored = _store_failed(
                        connection, warehouse, job, at, "", STORE_FAILED, _MESSAGES[STORE_FAILED], error_count=1
                    )
                record(warehouse, stored)
        except AdapterError as exc:
            logger.warning("Остатки подключения %s: чтение прервано (%s)", connection.id, exc.code)
            stats.error = exc
        except Exception as exc:
            logger.error(
                "Остатки подключения %s: чтение упало с внутренней ошибкой %s", connection.id, type(exc).__name__
            )
            crash = exc

        for warehouse in warehouses:
            if warehouse.id in done:
                continue
            if stats.error is not None:
                code, message = stats.error.code, stats.error.message
            elif crash is not None:
                code, message = INTERNAL_ERROR, _MESSAGES[INTERNAL_ERROR]
            else:
                code, message = WAREHOUSE_NOT_RETURNED, _MESSAGES[WAREHOUSE_NOT_RETURNED]
            record(warehouse, _store_failed(connection, warehouse, job, at, "", code, message, error_count=1))
        if crash is not None:
            raise crash
    finally:
        # Warehouses already committed are announced whatever happens next (also on KeyboardInterrupt).
        _announce(connection, ready, at)
    return stats


def _announce(connection: AccountingConnection, ready: dict[UUID, list[tuple[UUID, UUID]]], at: datetime) -> None:
    for venue_id, entries in ready.items():
        transaction.on_commit(
            partial(
                _send_ready,
                connection_id=connection.id,
                venue_id=venue_id,
                warehouse_ids=[warehouse_id for warehouse_id, _ in entries],
                snapshot_ids=[snapshot_id for _, snapshot_id in entries],
                taken_at=at,
            ),
            robust=True,
        )


def _store_warehouse(
    connection: AccountingConnection,
    warehouse: AccountingWarehouse,
    dto: StockSnapshotDTO,
    job: AccountingSyncJob,
    at: datetime,
    products: Mapping[str, ProductRef],
    zero_fill: bool,
) -> _Stored:
    """Header and levels of one warehouse in one transaction; an exception rolls back both (the caller logs it)."""
    version = (dto.source_version or "")[:_SOURCE_VERSION_LENGTH]
    if not dto.ok:
        first = dto.errors[0] if dto.errors else None
        code = first.code if first else WAREHOUSE_FAILED
        message = first.message if first else _MESSAGES[WAREHOUSE_FAILED]
        return _store_failed(connection, warehouse, job, at, version, code, message, error_count=len(dto.errors) or 1)

    items, errors = _map_items(dto, products)
    if not dto.items and _has_non_zero_levels(connection, warehouse):
        # An empty answer may come from restricted rights or a server fault, not from an emptied warehouse:
        # with known non-zero balances it must never zero the whole warehouse.
        errors.append(_own_error(None, EMPTY_ANSWER))
    status = SnapshotStatus.PARTIAL if errors else SnapshotStatus.COMPLETE
    synced_at = timezone.now()
    with transaction.atomic():
        snapshot = AccountingStockSnapshot.objects.create(
            connection=connection,
            venue_id=warehouse.venue_id,
            warehouse=warehouse,
            job=job,
            taken_at=at,
            synced_at=synced_at,
            status=status,
            item_count=len(items),
            error_count=len(errors),
            error_code=errors[0].code[:_ERROR_CODE_LENGTH] if errors else "",
            source_version=version,
        )
        AccountingStockLevel.objects.bulk_create(
            [
                AccountingStockLevel(
                    connection=connection,
                    venue_id=warehouse.venue_id,
                    warehouse=warehouse,
                    product_id=product_id,
                    snapshot=snapshot,
                    stock_qty=item.stock_qty,
                    source_uom=(item.source_uom or "")[:_SOURCE_UOM_LENGTH],
                    taken_at=at,
                    synced_at=synced_at,
                    reliability=item.reliability,
                )
                for product_id, item in items
            ],
            batch_size=BULK_BATCH_SIZE,
            update_conflicts=True,
            unique_fields=["warehouse", "product"],
            update_fields=[
                "snapshot",
                "stock_qty",
                "source_uom",
                "taken_at",
                "synced_at",
                "reliability",
                "updated_at",
            ],
        )
        zero_filled = 0
        if status == SnapshotStatus.COMPLETE and zero_fill:
            # Every level in the answer now points at this snapshot; the rest of the warehouse's known
            # levels were left out by a source that omits zero balances.
            zero_filled = (
                AccountingStockLevel.objects.filter(connection=connection, warehouse=warehouse)
                .exclude(snapshot=snapshot)
                .update(
                    stock_qty=Decimal("0"),
                    reliability=Reliability.OK,
                    snapshot=snapshot,
                    taken_at=at,
                    synced_at=synced_at,
                    updated_at=synced_at,
                )
            )
    message = errors[0].message if errors else ""
    if len(errors) > 1:
        message = f"{message} Ошибок по складу: {len(errors)}."
    return _Stored(snapshot=snapshot, items=len(items), zero_filled=zero_filled, message=message)


def _map_items(
    dto: StockSnapshotDTO, products: Mapping[str, ProductRef]
) -> tuple[list[tuple[UUID, StockItemDTO]], list[ItemErrorDTO]]:
    """Items keyed by our product id, and every reason the stored snapshot is not the complete answer.

    A missing quantity is always NULL with SOURCE_ERROR (whatever reliability the item claims); a product
    repeated in the answer is stored the same way, since we cannot tell which of its rows is right.
    """
    errors = list(dto.errors)
    reported = {id_key(error.external_product_id) for error in dto.errors if error.external_product_id}
    items: dict[UUID, StockItemDTO] = {}
    duplicated: set[UUID] = set()
    for item in dto.items:
        key = id_key(item.external_product_id)
        product = products.get(key)
        if product is None:
            errors.append(_own_error(item.external_product_id, PRODUCT_NOT_IN_CATALOG))
            continue
        product_id = product.product_id
        if product_id in items:
            if product_id not in duplicated:
                duplicated.add(product_id)
                errors.append(_own_error(item.external_product_id, DUPLICATE_ITEM))
                items[product_id] = replace(items[product_id], stock_qty=None, reliability=Reliability.SOURCE_ERROR)
            continue
        if item.stock_qty is None:
            if key not in reported:
                # A quantity the source did not give is never a complete answer, even if the adapter forgot to say so.
                errors.append(_own_error(item.external_product_id, QUANTITY_MISSING))
            item = replace(item, reliability=Reliability.SOURCE_ERROR)
        items[product_id] = item
    return list(items.items()), errors


def _own_error(external_product_id: str | None, code: str) -> ItemErrorDTO:
    return ItemErrorDTO(external_product_id=external_product_id, code=code, message=_MESSAGES[code])


def _has_non_zero_levels(connection: AccountingConnection, warehouse: AccountingWarehouse) -> bool:
    return (
        AccountingStockLevel.objects.filter(connection=connection, warehouse=warehouse, stock_qty__isnull=False)
        .exclude(stock_qty=0)
        .exists()
    )


def _store_failed(
    connection: AccountingConnection,
    warehouse: AccountingWarehouse,
    job: AccountingSyncJob,
    at: datetime,
    version: str,
    code: str,
    message: str,
    *,
    error_count: int,
) -> _Stored:
    """A FAILED header only: the warehouse's levels are not touched."""
    with transaction.atomic():
        snapshot = AccountingStockSnapshot.objects.create(
            connection=connection,
            venue_id=warehouse.venue_id,
            warehouse=warehouse,
            job=job,
            taken_at=at,
            status=SnapshotStatus.FAILED,
            item_count=0,
            error_count=error_count,
            error_code=code[:_ERROR_CODE_LENGTH],
            source_version=version,
        )
    return _Stored(snapshot=snapshot, message=message)


def _receiver_name(receiver: Callable[..., Any]) -> str:
    return getattr(receiver, "__qualname__", None) or type(receiver).__name__


def _send_ready(**kwargs: Any) -> None:
    for receiver, response in stock_snapshot_ready.send_robust(sender=AccountingStockSnapshot, **kwargs):
        if isinstance(response, Exception):
            # Our line carries the exception type only. Note that Django's Signal._log_robust_failure has already
            # logged the receiver's exception text with its traceback to the "django.dispatch" logger, so
            # receivers must not put secrets in exception messages (see accounting/signals.py).
            logger.error(
                "Получатель сигнала stock_snapshot_ready %s завершился ошибкой %s (заведение %s)",
                _receiver_name(receiver),
                type(response).__name__,
                kwargs.get("venue_id"),
            )
