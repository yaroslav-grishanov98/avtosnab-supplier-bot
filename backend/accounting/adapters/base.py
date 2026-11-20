from collections.abc import Iterator, Sequence
from datetime import date, datetime
from typing import Protocol

from accounting.dto import (
    AccessReport,
    Capabilities,
    ConsumptionResult,
    DepartmentDTO,
    ProductDTO,
    StockSnapshotDTO,
    WarehouseDTO,
)


class AccountingAdapter(Protocol):
    """What the sync code needs from an accounting system, whatever the provider.

    Every method raises AdapterError when the provider cannot serve the call.
    """

    provider: str

    def capabilities(self) -> Capabilities: ...

    def check_access(self) -> AccessReport: ...

    def list_departments(self) -> list[DepartmentDTO]: ...

    def list_warehouses(self) -> list[WarehouseDTO]: ...

    def list_products(self) -> list[ProductDTO]: ...

    def read_stock(self, warehouse_ids: Sequence[str], at: datetime) -> Iterator[StockSnapshotDTO]:
        """One snapshot per requested warehouse; a warehouse that fails comes back with ok=False.

        A warehouse the source does not know (deleted, never there) is ok=False with a ``warehouse_not_found``
        error, never an empty ok snapshot. ok=True with non-empty ``errors`` means PARTIAL: the caller stores the
        items it got but must not fill missing products with zeros (``omits_zero_balances``).
        """
        ...

    def read_consumption(self, warehouse_ids: Sequence[str], day_from: date, day_to: date) -> ConsumptionResult: ...
