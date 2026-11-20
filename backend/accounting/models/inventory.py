from django.db import models
from django.utils import timezone

from accounting.models.connection import AccountingConnection
from accounting.models.sync import AccountingSyncJob
from core.models import UUIDTimestampedModel

# Tenant isolation (docs/adr/0020): every stock, snapshot and consumption row
# carries connection_id and venue_id, and composite foreign keys added in
# accounting/migrations/0003_composite_fks.py (RunSQL; Django 5.1 cannot declare
# them) keep those equal to the warehouse's own and keep a warehouse/department
# venue linked to its connection. The UniqueConstraints named *_id_conn_* below
# exist only as targets for those keys. Which venue owns a warehouse is the
# department assignment service's decision; the database does not check it.
# The keys are MATCH SIMPLE: a NULL venue skips the check, which is how a
# warehouse or department waits for its venue to be assigned.


class SnapshotStatus(models.TextChoices):
    COMPLETE = "complete", "Complete"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class Reliability(models.TextChoices):
    OK = "ok", "OK"
    UNIT_UNKNOWN = "unit_unknown", "Unit unknown"
    SOURCE_ERROR = "source_error", "Source error"


class CoverageStatus(models.TextChoices):
    COMPLETE = "complete", "Complete"
    PARTIAL = "partial", "Partial"


class AccountingWarehouse(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="warehouses")
    venue = models.ForeignKey(
        "core.Venue",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accounting_warehouses",
        help_text="Venue of the warehouse's department; empty until the department is assigned.",
    )
    external_id = models.CharField(max_length=64, help_text="Store id in the accounting system.")
    name = models.CharField(max_length=255)
    external_department_id = models.CharField(
        max_length=64, blank=True, default="", help_text="Parent department id in the accounting system, if any."
    )
    # Free text: the set of store types belongs to the external system.
    store_type = models.CharField(max_length=32, blank=True, default="")
    is_present_in_source = models.BooleanField(
        default=True, help_text="False once the store disappears from the source; the row is kept."
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounting_warehouses"
        constraints = [
            models.UniqueConstraint(fields=["connection", "external_id"], name="accounting_warehouses_conn_ext_uniq"),
            models.UniqueConstraint(
                fields=["id", "connection", "venue"], name="accounting_warehouses_id_conn_venue_uniq"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class AccountingProduct(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="products")
    external_id = models.CharField(max_length=64, help_text="Product id in the accounting system.")
    name = models.CharField(max_length=255)
    article = models.CharField(max_length=64, blank=True, default="", help_text="Product code in the system.")
    group_name = models.CharField(max_length=255, blank=True, default="")
    # Free text: the set of product types belongs to the external system.
    product_type = models.CharField(max_length=32, blank=True, default="")
    unit_external_id = models.CharField(max_length=64, blank=True, default="")
    unit_name = models.CharField(max_length=64, blank=True, default="", help_text="Main unit of the product.")
    is_deleted = models.BooleanField(default=False, help_text="Deleted or gone from the source; the row is kept.")
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounting_products"
        constraints = [
            models.UniqueConstraint(fields=["connection", "external_id"], name="accounting_products_conn_ext_uniq"),
            models.UniqueConstraint(fields=["id", "connection"], name="accounting_products_id_conn_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class AccountingStockSnapshot(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="stock_snapshots")
    venue = models.ForeignKey("core.Venue", on_delete=models.PROTECT, related_name="accounting_stock_snapshots")
    warehouse = models.ForeignKey(AccountingWarehouse, on_delete=models.PROTECT, related_name="snapshots")
    # Snapshots are the stock history; pruning old jobs must not take them along.
    job = models.ForeignKey(
        AccountingSyncJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_snapshots"
    )
    taken_at = models.DateTimeField(help_text="Moment the balances refer to.")
    synced_at = models.DateTimeField(default=timezone.now, help_text="Moment the balances were stored.")
    status = models.CharField(max_length=16, choices=SnapshotStatus.choices)
    item_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default="", help_text="Error code, empty if none.")
    source_version = models.CharField(max_length=64, blank=True, default="", help_text="Server version at read time.")

    class Meta:
        db_table = "accounting_stock_snapshots"
        indexes = [
            models.Index(fields=["warehouse", "-taken_at"], name="acc_snap_wh_taken_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["id", "connection", "venue"], name="accounting_stock_snapshots_id_conn_venue_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.warehouse_id} @ {self.taken_at} ({self.status})"


class AccountingStockLevel(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="stock_levels")
    venue = models.ForeignKey("core.Venue", on_delete=models.PROTECT, related_name="accounting_stock_levels")
    warehouse = models.ForeignKey(AccountingWarehouse, on_delete=models.PROTECT, related_name="stock_levels")
    product = models.ForeignKey(AccountingProduct, on_delete=models.PROTECT, related_name="stock_levels")
    snapshot = models.ForeignKey(AccountingStockSnapshot, on_delete=models.PROTECT, related_name="levels")
    stock_qty = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Balance in source_uom; empty when the source could not report it (zero is a real zero).",
    )
    source_uom = models.CharField(max_length=64, blank=True, default="", help_text="Unit of stock_qty as in source.")
    taken_at = models.DateTimeField(help_text="Moment the balance refers to.")
    synced_at = models.DateTimeField(default=timezone.now)
    reliability = models.CharField(max_length=16, choices=Reliability.choices, default=Reliability.OK)

    class Meta:
        db_table = "accounting_stock_levels"
        constraints = [
            models.UniqueConstraint(fields=["warehouse", "product"], name="accounting_stock_levels_wh_product_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.product_id} @ {self.warehouse_id}: {self.stock_qty}"


class AccountingConsumptionCoverage(UUIDTimestampedModel):
    """The warehouse's consumption for the day was fetched; no daily row then means zero."""

    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="consumption_coverage")
    venue = models.ForeignKey("core.Venue", on_delete=models.PROTECT, related_name="accounting_consumption_coverage")
    warehouse = models.ForeignKey(AccountingWarehouse, on_delete=models.PROTECT, related_name="consumption_coverage")
    day = models.DateField()
    status = models.CharField(max_length=16, choices=CoverageStatus.choices)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounting_consumption_coverage"
        constraints = [
            models.UniqueConstraint(fields=["warehouse", "day"], name="accounting_consumption_coverage_wh_day_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.warehouse_id} {self.day} ({self.status})"


class AccountingConsumptionDaily(UUIDTimestampedModel):
    connection = models.ForeignKey(AccountingConnection, on_delete=models.PROTECT, related_name="consumption_daily")
    venue = models.ForeignKey("core.Venue", on_delete=models.PROTECT, related_name="accounting_consumption_daily")
    warehouse = models.ForeignKey(AccountingWarehouse, on_delete=models.PROTECT, related_name="consumption_daily")
    product = models.ForeignKey(AccountingProduct, on_delete=models.PROTECT, related_name="consumption_daily")
    day = models.DateField()
    # Stored as the source names it; which types count as consumption is the reader's choice (plan D5).
    transaction_type = models.CharField(max_length=64, help_text="Transaction type as in the accounting system.")
    qty = models.DecimalField(max_digits=18, decimal_places=6, help_text="Daily total in source_uom.")
    source_uom = models.CharField(max_length=64, blank=True, default="", help_text="Unit of qty as in source.")

    class Meta:
        db_table = "accounting_consumption_daily"
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "product", "day", "transaction_type"],
                name="accounting_consumption_daily_wh_prod_day_type_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product_id} @ {self.warehouse_id} {self.day} {self.transaction_type}: {self.qty}"
