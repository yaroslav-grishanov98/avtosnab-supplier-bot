"""Provider-neutral data transfer objects for accounting-system adapters.

Adapters translate a provider's answers into these frozen dataclasses; nothing
above the adapter layer sees provider-specific shapes. Quantities are Decimal.
A missing quantity is ``None`` and is never the same thing as a zero balance.
"""

import enum
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from accounting.models.inventory import Reliability


class OperationKind(enum.StrEnum):
    """What a stock movement means for demand, as far as the adapter could tell."""

    CONSUMPTION = "consumption"
    TRANSFER = "transfer"
    RECEIPT = "receipt"
    ADJUSTMENT = "adjustment"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DepartmentDTO:
    external_id: str
    name: str
    department_type: str


@dataclass(frozen=True)
class WarehouseDTO:
    external_id: str
    name: str
    external_department_id: str | None
    store_type: str


@dataclass(frozen=True)
class ProductDTO:
    external_id: str
    name: str
    article: str
    group_name: str
    product_type: str
    unit_external_id: str
    unit_name: str
    is_deleted: bool


@dataclass(frozen=True)
class StockItemDTO:
    external_product_id: str
    # None: the source could not report the balance. Decimal("0"): a real zero.
    stock_qty: Decimal | None
    source_uom: str
    reliability: Reliability


@dataclass(frozen=True)
class ItemErrorDTO:
    external_product_id: str | None
    code: str
    message: str


@dataclass(frozen=True)
class StockSnapshotDTO:
    external_warehouse_id: str
    taken_at: datetime
    items: list[StockItemDTO]
    errors: list[ItemErrorDTO]
    source_version: str
    # False when the warehouse could not be read as a whole (also for a warehouse the source does not know:
    # "warehouse_not_found"). ok=True with non-empty errors is PARTIAL for the caller: store what came, but
    # never fill missing products with zeros.
    ok: bool


@dataclass(frozen=True)
class ConsumptionEventDTO:
    external_product_id: str
    external_warehouse_id: str
    occurred_at: datetime
    qty: Decimal
    source_uom: str
    # The provider's own transaction type, kept as is.
    transaction_type: str
    kind: OperationKind


@dataclass(frozen=True)
class ConsumptionResult:
    events: list[ConsumptionEventDTO]
    # (external warehouse id, day) -> the day was fetched for the warehouse as a whole.
    # A covered day without events is a real zero; a missing or False pair means "no data".
    covered: dict[tuple[str, date], bool]
    # Set when the provider stopped reading early (rate limit, per-session report cap, request budget), so the
    # uncovered pairs are "not asked yet", not failed days; retry_after tells when to come back, if known.
    stopped: "AdapterError | None" = None
    # Pairs read as a whole but with rows that could not be stored (e.g. a product missing from the catalogue):
    # their events are returned, ``covered`` is False. Told apart from failed and not-asked pairs, so the caller
    # can mark them "known incomplete" instead of re-reading them in every session.
    partial: frozenset[tuple[str, date]] = frozenset()


@dataclass(frozen=True)
class Capabilities:
    read_products: bool
    read_warehouses: bool
    read_departments: bool
    read_stock: bool
    read_consumption: bool
    write_receipt: bool = False
    # None: not known (yet) whether the source leaves zero balances out of its answers.
    omits_zero_balances: bool | None = None
    source_version: str = ""
    notes: str = ""

    @classmethod
    def unsupported(cls) -> "Capabilities":
        return cls(
            read_products=False,
            read_warehouses=False,
            read_departments=False,
            read_stock=False,
            read_consumption=False,
        )

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, data: object) -> "Capabilities":
        """Inverse of to_json(); missing keys fall back to defaults so an old stored value never crashes."""
        if not isinstance(data, dict):
            return cls.unsupported()
        omits = data.get("omits_zero_balances")
        # Only a real JSON true counts: bool("false") is True, and a capability must never switch on by accident.
        return cls(
            read_products=data.get("read_products") is True,
            read_warehouses=data.get("read_warehouses") is True,
            read_departments=data.get("read_departments") is True,
            read_stock=data.get("read_stock") is True,
            read_consumption=data.get("read_consumption") is True,
            write_receipt=data.get("write_receipt") is True,
            omits_zero_balances=omits if isinstance(omits, bool) else None,
            source_version=str(data.get("source_version") or ""),
            notes=str(data.get("notes") or ""),
        )


class AdapterError(Exception):
    """A provider call failed. ``message`` is user-facing Russian text without secrets or tracebacks."""

    def __init__(self, code: str, message: str, retryable: bool = False, retry_after: float | None = None):
        # All fields go to args so the exception survives pickling (Celery result backends).
        super().__init__(code, message, retryable, retry_after)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.message


AccessStepStatus = Literal["ok", "failed", "skipped"]
_ACCESS_STEP_STATUSES = ("ok", "failed", "skipped")


@dataclass(frozen=True)
class AccessStep:
    """One stage of an access check, e.g. "login" or "read stores". Never carries secrets."""

    name: str
    status: AccessStepStatus
    message: str

    def __post_init__(self) -> None:
        if self.status not in _ACCESS_STEP_STATUSES:
            raise ValueError(f"Unknown access step status: {self.status!r}")


@dataclass(frozen=True)
class AccessReport:
    ok: bool
    steps: list[AccessStep] = field(default_factory=list)
    error_code: str | None = None
    # Russian advice on what to do about a failure; never carries secrets.
    hint: str | None = None
