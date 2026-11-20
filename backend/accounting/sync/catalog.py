"""Catalogue sync: departments, warehouses and products of one connection.

Everything is read from the adapter first, so an ``AdapterError`` leaves the
stored catalogue untouched; the writes then run in one transaction. Rows are
keyed by ``(connection, external_id)``: identical ids of another connection are
other rows (Review Focus 1). Ids from the source are matched to stored ones
case-insensitively (``sync.keys.id_key``); stored spellings never change. Nothing is deleted: a warehouse gone from the
source gets ``is_present_in_source=False``, a product gets ``is_deleted=True``.
An empty list while we hold live rows changes no flags and is reported as
``catalog_empty_answer`` (the job is PARTIAL). The connection row is updated
with a queryset update of only the columns this sync owns.

A department's ``venue`` is the admin's assignment (Task 13) and is never
written here. A warehouse takes its venue from its department; moving it to
another venue fails in the database once stock or consumption rows point at
it (composite foreign keys, migration 0003). Such a warehouse keeps its venue
and the failure is reported in ``CatalogStats.errors`` instead of failing the
whole sync.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from accounting.adapters.base import AccountingAdapter
from accounting.dto import DepartmentDTO, ProductDTO, WarehouseDTO
from accounting.models import (
    AccountingConnection,
    AccountingDepartment,
    AccountingProduct,
    AccountingSyncJob,
    AccountingWarehouse,
    SyncKind,
)
from accounting.sync.keys import id_key

logger = logging.getLogger(__name__)

VENUE_CHANGE_BLOCKED = "warehouse_venue_change_blocked"
_VENUE_CHANGE_BLOCKED_MESSAGE = (
    "Склад не перенесён в другое заведение: по нему уже есть остатки или расход в прежнем заведении."
)
CATALOG_EMPTY_ANSWER = "catalog_empty_answer"
_EMPTY_ANSWER_MESSAGES = {
    "warehouses": "Учётная система вернула пустой список складов; отметки об отсутствии складов не изменены.",
    "products": "Учётная система вернула пустой список товаров; отметки об удалении товаров не изменены.",
}
# Keeps one INSERT ... ON CONFLICT well below PostgreSQL's 65535 bind parameters (a product row has ~12).
BULK_BATCH_SIZE = 1000

DTO = TypeVar("DTO", DepartmentDTO, WarehouseDTO, ProductDTO)


@dataclass
class CatalogStats:
    departments: int = 0
    warehouses: int = 0
    warehouses_missing: int = 0
    products: int = 0
    products_missing: int = 0
    # Source rows without a usable external id (empty or longer than the column).
    skipped: int = 0
    # {"external_id", "code", "message"}: per warehouse whose venue could not be changed, and one entry with an
    # empty external_id per empty list (warehouses, products) that left the stored flags unchanged.
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": SyncKind.CATALOG.value,
            "departments": self.departments,
            "warehouses": self.warehouses,
            "warehouses_missing": self.warehouses_missing,
            "products": self.products,
            "products_missing": self.products_missing,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


def merge_capabilities(stored: Any, fresh: dict[str, Any]) -> dict[str, Any]:
    """The adapter's capabilities, except that a recorded ``read_*: false`` stays false.

    A downgrade is recorded when the source turned out unable to give something (e.g. consumption); the
    adapter's constant would switch it on again at the next catalogue sync. Replacing the credentials
    re-checks the source and stores fresh capabilities, which is how an admin lifts a downgrade."""
    merged = dict(fresh)
    if isinstance(stored, dict):
        for name, value in stored.items():
            if name.startswith("read_") and value is False:
                merged[name] = False
    return merged


def sync_catalog(adapter: AccountingAdapter, connection: AccountingConnection, job: AccountingSyncJob) -> CatalogStats:
    departments = adapter.list_departments()
    warehouses = adapter.list_warehouses()
    products = adapter.list_products()
    capabilities = adapter.capabilities()

    now = timezone.now()
    stats = CatalogStats()
    with transaction.atomic():
        departments_by_id = _by_external_id(departments, AccountingDepartment, connection, stats)
        _upsert_departments(connection, departments_by_id, now, stats)
        warehouses_by_id = _by_external_id(warehouses, AccountingWarehouse, connection, stats)
        _upsert_warehouses(connection, warehouses_by_id, now, stats)
        products_by_id = _by_external_id(products, AccountingProduct, connection, stats)
        _upsert_products(connection, products_by_id, now, stats)

        # A queryset update of exactly the columns this sync owns: save() would re-derive host from a base_url
        # read when the job started, and an admin may change the address or status meanwhile.
        stored_capabilities = (
            AccountingConnection.objects.filter(pk=connection.pk).values_list("capabilities", flat=True).first()
        )
        values: dict[str, Any] = {
            "catalog_synced_at": now,
            "capabilities": merge_capabilities(stored_capabilities, capabilities.to_json()),
            "updated_at": now,
        }
        if capabilities.source_version:
            values["server_version"] = _fit(AccountingConnection, "server_version", capabilities.source_version)
        AccountingConnection.objects.filter(pk=connection.pk).update(**values)
        for name, value in values.items():
            setattr(connection, name, value)

    logger.info(
        "Каталог подключения %s синхронизирован (задача %s): складов %d, товаров %d, ошибок %d",
        connection.id,
        job.job_id,
        stats.warehouses,
        stats.products,
        len(stats.errors),
    )
    return stats


def _fit(model: type[models.Model], field_name: str, value: str | None) -> str:
    """The source's text cut to the column, so one long name cannot fail the whole catalogue."""
    max_length = model._meta.get_field(field_name).max_length
    return (value or "")[:max_length]


def _by_external_id(
    rows: Iterable[DTO], model: type[models.Model], connection: AccountingConnection, stats: CatalogStats
) -> dict[str, DTO]:
    """Source rows keyed by the ``external_id`` to write.

    Ids are matched case-insensitively (``id_key``): a row we already have keeps its stored spelling, so a
    GUID that comes back in another letter case updates that row instead of creating a twin. Ids repeated
    in the answer (in any case) keep their last row, since one upsert may touch a row only once.
    """
    max_length = model._meta.get_field("external_id").max_length
    stored: dict[str, str] = {}
    for external_id in (
        model.objects.filter(connection=connection).order_by("external_id").values_list("external_id", flat=True)
    ):
        stored.setdefault(id_key(external_id), external_id)
    by_key: dict[str, tuple[str, DTO]] = {}
    skipped = 0
    for row in rows:
        external_id = row.external_id
        if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > max_length:
            skipped += 1
            continue
        key = id_key(external_id)
        by_key[key] = (stored.get(key, external_id), row)
    if skipped:
        logger.warning("Каталог (%s): пропущено строк без пригодного внешнего id: %d", model.__name__, skipped)
        stats.skipped += skipped
    return dict(by_key.values())


def _upsert_departments(
    connection: AccountingConnection, departments: dict[str, DepartmentDTO], now: datetime, stats: CatalogStats
) -> None:
    rows = [
        AccountingDepartment(
            connection=connection,
            external_id=external_id,
            name=_fit(AccountingDepartment, "name", dto.name),
            department_type=_fit(AccountingDepartment, "department_type", dto.department_type),
            last_seen_at=now,
        )
        for external_id, dto in departments.items()
    ]
    # "venue" is deliberately not among update_fields: an existing department keeps the admin's assignment,
    # a new one starts without a venue.
    AccountingDepartment.objects.bulk_create(
        rows,
        batch_size=BULK_BATCH_SIZE,
        update_conflicts=True,
        unique_fields=["connection", "external_id"],
        update_fields=["name", "department_type", "last_seen_at", "updated_at"],
    )
    stats.departments = len(rows)


def _upsert_warehouses(
    connection: AccountingConnection, warehouses: dict[str, WarehouseDTO], now: datetime, stats: CatalogStats
) -> None:
    # The store's parentId may differ in case from the department's own id: match by id_key.
    department_venues = {
        id_key(external_id): venue_id
        for external_id, venue_id in AccountingDepartment.objects.filter(connection=connection).values_list(
            "external_id", "venue_id"
        )
    }
    # Keys are already the stored spellings (_by_external_id), so an exact lookup is right here.
    existing = dict(AccountingWarehouse.objects.filter(connection=connection).values_list("external_id", "venue_id"))
    rows = []
    moves: list[tuple[str, Any]] = []
    for external_id, dto in warehouses.items():
        department_id = dto.external_department_id or ""
        venue_id = department_venues.get(id_key(department_id)) if id_key(department_id) else None
        if external_id in existing and existing[external_id] != venue_id:
            moves.append((external_id, venue_id))
        rows.append(
            AccountingWarehouse(
                connection=connection,
                # A new warehouse is inserted with its venue; an existing one changes venue below, one by one.
                venue_id=existing.get(external_id, venue_id),
                external_id=external_id,
                name=_fit(AccountingWarehouse, "name", dto.name),
                external_department_id=_fit(AccountingWarehouse, "external_department_id", department_id),
                store_type=_fit(AccountingWarehouse, "store_type", dto.store_type),
                is_present_in_source=True,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    AccountingWarehouse.objects.bulk_create(
        rows,
        batch_size=BULK_BATCH_SIZE,
        update_conflicts=True,
        unique_fields=["connection", "external_id"],
        update_fields=[
            "name",
            "external_department_id",
            "store_type",
            "is_present_in_source",
            "last_seen_at",
            "updated_at",
        ],
    )
    stats.warehouses = len(rows)

    for external_id, venue_id in moves:
        try:
            # Own savepoint: a blocked move must not abort the catalogue transaction.
            with transaction.atomic():
                AccountingWarehouse.objects.filter(connection=connection, external_id=external_id).update(
                    venue_id=venue_id, updated_at=now
                )
        except IntegrityError:
            logger.warning(
                "Каталог подключения %s: склад %s не перенесён в другое заведение (есть остатки или расход)",
                connection.id,
                external_id,
            )
            stats.errors.append(
                {"external_id": external_id, "code": VENUE_CHANGE_BLOCKED, "message": _VENUE_CHANGE_BLOCKED_MESSAGE}
            )

    # Every row the source still has was stamped with last_seen_at=now above.
    still_present = AccountingWarehouse.objects.filter(connection=connection, is_present_in_source=True)
    if _empty_answer(warehouses, still_present, "warehouses", connection, stats):
        return
    stats.warehouses_missing = still_present.exclude(last_seen_at=now).update(
        is_present_in_source=False, updated_at=now
    )


def _empty_answer(
    rows: dict[str, Any],
    still_flagged_live: models.QuerySet,
    entity: str,
    connection: AccountingConnection,
    stats: CatalogStats,
) -> bool:
    """True when the source sent no usable rows although we hold live ones: flags stay, the sync is PARTIAL.

    An empty list may mean restricted rights or a server fault; marking the whole catalogue gone on it would
    hide every warehouse from the stock sync (or every product from BE-1).
    """
    if rows or not still_flagged_live.exists():
        return False
    logger.warning("Каталог подключения %s: пустой список (%s), отметки не изменены", connection.id, entity)
    stats.errors.append({"external_id": "", "code": CATALOG_EMPTY_ANSWER, "message": _EMPTY_ANSWER_MESSAGES[entity]})
    return True


def _upsert_products(
    connection: AccountingConnection, products: dict[str, ProductDTO], now: datetime, stats: CatalogStats
) -> None:
    rows = [
        AccountingProduct(
            connection=connection,
            external_id=external_id,
            name=_fit(AccountingProduct, "name", dto.name),
            article=_fit(AccountingProduct, "article", dto.article),
            group_name=_fit(AccountingProduct, "group_name", dto.group_name),
            product_type=_fit(AccountingProduct, "product_type", dto.product_type),
            unit_external_id=_fit(AccountingProduct, "unit_external_id", dto.unit_external_id),
            unit_name=_fit(AccountingProduct, "unit_name", dto.unit_name),
            is_deleted=bool(dto.is_deleted),
            last_seen_at=now,
        )
        for external_id, dto in products.items()
    ]
    AccountingProduct.objects.bulk_create(
        rows,
        batch_size=BULK_BATCH_SIZE,
        update_conflicts=True,
        unique_fields=["connection", "external_id"],
        update_fields=[
            "name",
            "article",
            "group_name",
            "product_type",
            "unit_external_id",
            "unit_name",
            "is_deleted",
            "last_seen_at",
            "updated_at",
        ],
    )
    stats.products = len(rows)
    still_live = AccountingProduct.objects.filter(connection=connection, is_deleted=False)
    if _empty_answer(products, still_live, "products", connection, stats):
        return
    stats.products_missing = still_live.exclude(last_seen_at=now).update(is_deleted=True, updated_at=now)
