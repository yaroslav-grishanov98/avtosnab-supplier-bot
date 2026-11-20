"""Builders shared by the sync tests (accounting/test_sync_*.py); fictional ids and names only."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from accounting.dto import DepartmentDTO, ProductDTO, StockItemDTO, WarehouseDTO
from accounting.models import (
    AccountingConnection,
    AccountingSyncJob,
    AccountingVenueLink,
    LinkStatus,
    Provider,
    Reliability,
    SyncKind,
    SyncTrigger,
)
from core.models import Venue


def make_venue(name: str) -> Venue:
    return Venue.objects.create(display_name=name)


def make_connection(base_url: str = "https://a.iiko.example/resto", **fields: Any) -> AccountingConnection:
    return AccountingConnection.objects.create(provider=Provider.IIKO, base_url=base_url, **fields)


def link(connection: AccountingConnection, venue: Venue, status: str = LinkStatus.ACTIVE) -> AccountingVenueLink:
    return AccountingVenueLink.objects.create(connection=connection, venue=venue, status=status)


def make_job(connection: AccountingConnection, kind: str = SyncKind.STOCK) -> AccountingSyncJob:
    return AccountingSyncJob.objects.create(connection=connection, kind=kind, trigger=SyncTrigger.MANUAL)


def department(external_id: str, name: str = "Подразделение") -> DepartmentDTO:
    return DepartmentDTO(external_id=external_id, name=name, department_type="DEPARTMENT")


def warehouse(external_id: str, department_id: str | None, name: str = "Склад") -> WarehouseDTO:
    return WarehouseDTO(external_id=external_id, name=name, external_department_id=department_id, store_type="STORE")


def product(external_id: str, unit: str = "кг", is_deleted: bool = False) -> ProductDTO:
    return ProductDTO(
        external_id=external_id,
        name=f"Товар {external_id}",
        article=external_id[-4:],
        group_name="Группа",
        product_type="GOODS",
        unit_external_id=f"unit-{unit}" if unit else "",
        unit_name=unit,
        is_deleted=is_deleted,
    )


def item(product_id: str, qty: str | None, uom: str = "кг", reliability: str = Reliability.OK) -> StockItemDTO:
    return StockItemDTO(
        external_product_id=product_id,
        stock_qty=None if qty is None else Decimal(qty),
        source_uom=uom,
        reliability=Reliability(reliability),
    )


def adapter_factory(adapter: Any, calls: list | None = None) -> Callable[..., Any]:
    """A stand-in for ``get_adapter`` that yields ``adapter`` and records the options it was called with."""

    @contextmanager
    def fake_get_adapter(connection: AccountingConnection, **options: Any) -> Iterator[Any]:
        if calls is not None:
            calls.append({"connection_id": connection.id, **options})
        yield adapter

    return fake_get_adapter
