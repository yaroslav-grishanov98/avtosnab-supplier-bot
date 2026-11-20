from collections.abc import Iterator, Sequence
from datetime import date, datetime
from typing import NoReturn

from accounting.dto import (
    AccessReport,
    AdapterError,
    Capabilities,
    ConsumptionResult,
    DepartmentDTO,
    ProductDTO,
    StockSnapshotDTO,
    WarehouseDTO,
)


def _unsupported() -> NoReturn:
    raise AdapterError(
        code="api_not_supported",
        message="StoreHouse не предоставляет API для автоматического чтения данных.",
        retryable=False,
    )


class StoreHouseAdapter:
    """StoreHouse has no API we can read from; every operation says so instead of returning empty data.

    The methods are plain functions, not generators, so read_stock raises on the call itself.
    """

    provider = "storehouse"

    def capabilities(self) -> Capabilities:
        return Capabilities.unsupported()

    def check_access(self) -> AccessReport:
        _unsupported()

    def list_departments(self) -> list[DepartmentDTO]:
        _unsupported()

    def list_warehouses(self) -> list[WarehouseDTO]:
        _unsupported()

    def list_products(self) -> list[ProductDTO]:
        _unsupported()

    def read_stock(self, warehouse_ids: Sequence[str], at: datetime) -> Iterator[StockSnapshotDTO]:
        _unsupported()

    def read_consumption(self, warehouse_ids: Sequence[str], day_from: date, day_to: date) -> ConsumptionResult:
        _unsupported()
