import uuid

from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.models import EntityStatus, ExternalSystem, SuggestionSource, UUIDTimestampedModel, VenueAreaRole


class EmbeddingProvider(models.TextChoices):
    OPENAI = "openai", "OpenAI"
    LOCAL = "local", "Local"
    QDRANT_MIRROR = "qdrant_mirror", "Qdrant mirror"


class MappingStatus(models.TextChoices):
    SUGGESTED = "suggested", "Suggested"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"
    ARCHIVED = "archived", "Archived"


class MappingScope(models.TextChoices):
    GLOBAL = "global", "Global"
    VENUE = "venue", "Venue"
    IIKO = "iiko", "iiko"


class GoogleSheetSyncStatus(models.TextChoices):
    STARTED = "started", "Started"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"


class GoogleSheetSyncTrigger(models.TextChoices):
    UPLOAD = "upload", "Upload"
    SCHEDULED = "scheduled", "Scheduled"
    MANUAL = "manual", "Manual"
    COMMAND = "command", "Command"


class VenueProductMatrixItemSource(models.TextChoices):
    ACCOUNTING_SYSTEM = "accounting_system", "Учётная система"
    MANUAL = "manual", "Вручную"
    MASTER_CATALOG = "master_catalog", "Мастер-справочник"


class VectorField(models.Field):
    description = "PostgreSQL pgvector column"

    def __init__(self, dimensions: int = 1536, *args, **kwargs):
        self.dimensions = dimensions
        super().__init__(*args, **kwargs)

    def db_type(self, connection):
        return f"vector({self.dimensions})"

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["dimensions"] = self.dimensions
        return name, path, args, kwargs


class Brand(UUIDTimestampedModel):
    canonical_name = models.TextField()
    normalized_name = models.TextField(unique=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "brands"

    def __str__(self) -> str:
        return self.canonical_name


class ProductCategory(UUIDTimestampedModel):
    parent = models.ForeignKey(
        "catalog.ProductCategory",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="children",
    )
    name = models.TextField()
    slug = models.TextField(unique=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "product_categories"
        indexes = [
            models.Index(fields=["parent"], name="product_categories_parent_idx"),
        ]

    def __str__(self) -> str:
        return self.name


class Product(UUIDTimestampedModel):
    category = models.ForeignKey(
        "catalog.ProductCategory",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="products",
    )
    brand = models.ForeignKey(
        "catalog.Brand",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="products",
    )
    base_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="base_products",
    )
    canonical_name = models.TextField()
    normalized_name = models.TextField()
    enriched_text = models.TextField(default="", blank=True)
    volume_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    volume_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="products_by_volume",
    )
    weight_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    weight_unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="products_by_weight",
    )
    package = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "products"
        constraints = [
            models.UniqueConstraint(
                fields=["normalized_name", "brand", "category", "package"],
                name="products_normalized_brand_category_package_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["category", "status"], name="products_category_status_idx"),
            models.Index(fields=["brand"], name="products_brand_idx"),
        ]

    def __str__(self) -> str:
        return self.canonical_name


class ProductEmbedding(UUIDTimestampedModel):
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="embeddings")
    provider = models.CharField(max_length=32, choices=EmbeddingProvider.choices)
    model = models.TextField()
    source_text = models.TextField()
    embedding = VectorField(dimensions=1536, blank=True, null=True)
    external_vector_id = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "product_embeddings"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "provider", "model"], name="product_embeddings_product_provider_model_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["product"], name="product_embeddings_product_idx"),
        ]


class VenueSupplierProduct(UUIDTimestampedModel):
    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="venue_supplier_products")
    # 2026-08-11: was RESTRICT -> CASCADE (Витя wanted DELETE to actually
    # delete). 2026-09-23: reverted back to RESTRICT (backlog#task-69, ТЗ
    # «Карточка товара поставщика» §32) — DELETE is archive again, favorites
    # shouldn't silently disappear through this FK anymore.
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.RESTRICT,
        related_name="venue_supplier_products",
    )
    role = models.CharField(max_length=16, choices=VenueAreaRole.choices, blank=True, null=True)
    is_favorite = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    custom_name = models.TextField(blank=True, null=True)
    last_ordered_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "venue_supplier_products"
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "supplier_product"], name="venue_supplier_products_venue_supplier_product_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["venue", "is_favorite", "status"], name="vsp_venue_favorite_idx"),
            models.Index(fields=["supplier_product"], name="vsp_supplier_product_idx"),
        ]


class MasterProduct(UUIDTimestampedModel):
    """2026-08-31 design (docs/superpowers/specs/2026-08-31-master-catalog-design.md
    §3.2): per-owner "master catalog" item — independent of the
    platform-wide catalog.Product (that one is for AI-matching supplier
    listings across the whole platform; this one is a single owner's own
    grouping of their venues' product-matrix rows). Owner is a core.User,
    not a new Account/Organization table (see spec §3.1): "the owner's
    companies" = venues where that user currently has an active
    VenueUserRole.OWNER row."""

    owner = models.ForeignKey("core.User", on_delete=models.RESTRICT, related_name="master_products")
    canonical_name = models.TextField()
    unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="master_products",
    )
    net_value = models.DecimalField(max_digits=14, decimal_places=4, blank=True, null=True)
    note = models.TextField(blank=True, null=True)
    embedding = VectorField(dimensions=384, blank=True, null=True)
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "master_products"
        indexes = [
            models.Index(fields=["owner", "status"], name="mp_owner_status_idx"),
            GinIndex(fields=["canonical_name"], name="mp_canonical_name_trgm_gin", opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self) -> str:
        return f"{self.canonical_name} ({self.owner_id})"


class MasterProductImportUploadStatus(models.TextChoices):
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class CatalogImportDomain(models.TextChoices):
    MASTER = "master", "Мастер-справочник"
    VENUE = "venue", "Справочник заведения"


class CatalogImportProcessingStatus(models.TextChoices):
    """Фоновая обработка сессии (тот же словарь, что у Supplier-импорта): pending -> processing -> ready | failed.
    Сессии, созданные синхронно (маленький файл), сразу ready."""

    PENDING = "pending", "В очереди"
    PROCESSING = "processing", "Обрабатывается"
    READY = "ready", "Готово"
    FAILED = "failed", "Ошибка"


class CatalogImportSessionStatus(models.TextChoices):
    OPEN = "open", "Открыта"
    COMMITTED = "committed", "Завершена"


class CatalogImportItemAction(models.TextChoices):
    REVIEW = "review", "Требует проверки"
    CREATE_NEW = "create_new", "Создать новый"
    UPDATE_EXISTING = "update_existing", "Обновить существующий"
    REPLACE = "replace", "Заменить существующий"
    SKIP = "skip", "Не добавлять"


class CatalogImportSession(UUIDTimestampedModel):
    parser_version = models.CharField(max_length=64, blank=True, default="")

    """Persisted review state shared by master and venue imports.

    The domain-specific apply services remain the source of truth for writes;
    this model only stores the extracted row snapshot and user decisions so a
    preview can be reviewed and committed independently.
    """

    owner = models.ForeignKey("core.User", on_delete=models.RESTRICT, related_name="catalog_import_sessions")
    venue = models.ForeignKey(
        "core.Venue", on_delete=models.CASCADE, blank=True, null=True, related_name="catalog_import_sessions"
    )
    domain = models.CharField(max_length=16, choices=CatalogImportDomain.choices)
    recovery_attempts = models.PositiveSmallIntegerField(default=0)
    source_file_name = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=CatalogImportSessionStatus.choices, default=CatalogImportSessionStatus.OPEN
    )
    processing_status = models.CharField(
        max_length=16, choices=CatalogImportProcessingStatus.choices, default=CatalogImportProcessingStatus.READY
    )
    rows_total = models.PositiveIntegerField(default=0)
    rows_done = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    # sheet_name / header_row_number / detected_columns / rows_without_name из разбора файла -- нужны, когда ответ
    # preview формируется не в запросе, а фоновой задачей.
    meta = models.JSONField(default=dict, blank=True)
    import_file = models.ForeignKey(
        "file_imports.ImportFile", on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )

    class Meta:
        db_table = "catalog_import_sessions"
        indexes = [
            models.Index(fields=["owner", "domain", "status"], name="cis_owner_domain_status_idx"),
            models.Index(fields=["venue", "status"], name="cis_venue_status_idx"),
        ]


class CatalogImportItem(UUIDTimestampedModel):
    session = models.ForeignKey("catalog.CatalogImportSession", on_delete=models.CASCADE, related_name="items")
    row_number = models.PositiveIntegerField()
    raw_values = models.JSONField(default=dict)
    normalized_values = models.JSONField(default=dict)
    parsed_snapshot = models.JSONField(default=dict, blank=True)
    field_sources = models.JSONField(default=dict, blank=True)
    candidates = models.JSONField(default=list, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    action = models.CharField(
        max_length=24, choices=CatalogImportItemAction.choices, default=CatalogImportItemAction.REVIEW
    )
    target_id = models.UUIDField(blank=True, null=True)
    target_expected_updated_at = models.DateTimeField(blank=True, null=True)
    applied_at = models.DateTimeField(blank=True, null=True)
    apply_result = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "catalog_import_items"
        ordering = ["row_number"]
        indexes = [models.Index(fields=["session", "action"], name="cii_session_action_idx")]


class MasterProductImportUpload(UUIDTimestampedModel):
    parser_version = models.CharField(max_length=64, blank=True, default="")
    parsed_rows = models.JSONField(default=list, blank=True)

    """TASK-112 (ТЗ §48): background processing record for a Master Catalog
    import file too large to preview synchronously (>500 rows — see
    api/v1/catalog/views.py's MasterProductImportPreviewView). Holds only
    the parsed/processed result, never the uploaded file itself (same
    choice as SupplierPriceUpload — raw binary isn't kept in permanent
    storage)."""

    owner = models.ForeignKey("core.User", on_delete=models.RESTRICT, related_name="master_product_import_uploads")
    recovery_attempts = models.PositiveSmallIntegerField(default=0)
    source_file_name = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=16,
        choices=MasterProductImportUploadStatus.choices,
        default=MasterProductImportUploadStatus.PROCESSING,
    )
    rows_total = models.PositiveIntegerField(default=0)
    rows_done = models.PositiveIntegerField(default=0)
    result = models.JSONField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    import_file = models.ForeignKey(
        "file_imports.ImportFile", on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )

    class Meta:
        db_table = "master_product_import_uploads"
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="mpiu_owner_created_idx"),
            models.Index(fields=["status"], name="mpiu_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.id} ({self.status})"


class VenueProductMatrixItem(UUIDTimestampedModel):
    """2026-08-26 sync call + prototype screenshot: "товарная матрица" — a
    venue's own product reference list, filled in once a season. First 3
    columns (requested_name/requested_unit_label/note) are typed by the user
    by hand; supplier_product is attached afterwards ("на каждый товар юзер
    прикрепляет товар с маркетплейса") — a single specific supplier's listing,
    not an abstract catalog.Product: Pavel said the price shown is "минимальная
    цена от ЭТОГО поставщика", i.e. that one SupplierProduct's own
    current_price, not an aggregate across every supplier carrying the same
    product. requested_unit_label is free text, not an FK to core.Unit — the
    mockup shows values like "банка стекло 1кг" that aren't a controlled unit
    code."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="product_matrix_items")
    # 2026-09-17, по прямой просьбе пользователя: подразделение, к которому
    # относится эта строка справочника — тот же паттерн, что уже есть на
    # VenueCartItem/OrderItem (core.models), опционально (не у каждой
    # компании есть подразделения / не каждая строка справочника к ним
    # привязана).
    subdivision = models.ForeignKey(
        "core.VenueSubdivision",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="product_matrix_items",
    )
    requested_name = models.TextField()
    requested_unit_label = models.TextField(blank=True, null=True)
    # TASK-122 п.1: optional hint alongside requested_unit_label, never a
    # replacement for it — requested_unit_label stays free text (real data
    # includes composite packaging strings like "банка стекло 1кг" that
    # don't map to a single core.Unit). Populated at apply time from
    # resolve_venue_import_rows' closed-list match or unit-LLM suggestion;
    # existing rows are left NULL, no backfill.
    unit = models.ForeignKey(
        "core.Unit",
        on_delete=models.RESTRICT,
        blank=True,
        null=True,
        related_name="venue_matrix_items",
    )
    note = models.TextField(blank=True, null=True)
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="venue_matrix_items",
    )
    supplier_product_deleted = models.BooleanField(
        default=False,
    )
    comment = models.TextField(blank=True, null=True)
    master_product = models.ForeignKey(
        "catalog.MasterProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="matrix_items",
    )
    lists = models.ManyToManyField("catalog.VenueProductList", related_name="items", blank=True)
    source = models.CharField(
        max_length=32, choices=VenueProductMatrixItemSource.choices, default=VenueProductMatrixItemSource.MANUAL
    )
    needs_review = models.BooleanField(default=False)
    missing_from_latest_import = models.BooleanField(
        default=False,
        help_text="§30 raw ТЗ: true when this accounting_system-sourced item was not present in the "
        "venue's most recent price-list import batch. Never auto-archived — informational only.",
    )
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "venue_product_matrix_items"
        constraints = [
            models.UniqueConstraint(
                fields=["master_product", "venue"],
                condition=models.Q(master_product__isnull=False),
                name="vpmi_master_product_venue_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["venue", "status"], name="vpmi_venue_status_idx"),
            models.Index(fields=["supplier_product"], name="vpmi_supplier_product_idx"),
            models.Index(fields=["master_product"], name="vpmi_master_product_idx"),
            GinIndex(fields=["requested_name"], name="vpmi_requested_name_trgm_gin", opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self) -> str:
        return f"{self.requested_name} ({self.venue})"


class SupplierMatchStatus(models.TextChoices):
    SEARCHING = "searching", "Подбираем..."
    FOUND = "found", "Нашли N вариантов"
    NOT_FOUND = "not_found", "Не нашли"
    ERROR = "error", "Ошибка подбора"


class VenueProductMatrixSupplierMatch(UUIDTimestampedModel):
    """2026-10-04, TASK-148 (массовый подбор товаров поставщиков для справочника): сохранённый
    автоматический поиск для строки без «Товара поставщика». Хранятся только параметры поиска
    и количество — сами товары каталога грузятся при открытии строки (актуальные цены/наличие).
    Нет записи — подбор не запускался. Bulk-запуск пишет сюда; при открытии каталога
    result_count пересчитывается, при выборе товара запись удаляется."""

    item = models.OneToOneField(
        "catalog.VenueProductMatrixItem", on_delete=models.CASCADE, related_name="supplier_match"
    )
    status = models.CharField(max_length=16, choices=SupplierMatchStatus.choices)
    # Стартовое состояние каталога: q — поисковая строка, filters — параметры GET /catalog/products,
    # определённые однозначно (category_id, brand_id, …). Поставщик и наличие сюда не ставятся (ТЗ §5).
    query = models.TextField(blank=True, default="")
    filters = models.JSONField(default=dict, blank=True)
    result_count = models.PositiveIntegerField(blank=True, null=True)
    error = models.TextField(blank=True, null=True)
    searched_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "venue_product_matrix_supplier_matches"

    def __str__(self) -> str:
        return f"{self.item_id}: {self.status}"


class VenueProductImportAlias(UUIDTimestampedModel):
    """2026-08-31 design (docs/superpowers/specs/2026-08-31-master-catalog-design.md
    §3.7): cache of a venue's manually-confirmed decisions when resolving an
    import row's raw name against that venue's own product-matrix list, so the
    same raw name is never re-asked about on a later import. Written on EITHER
    manual resolution ("update existing" or "add as new" — after the new
    VenueProductMatrixItem is created) but never for "don't import" (spec
    §3.7: no resulting matrix_item to point at)."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="product_import_aliases")
    raw_name = models.TextField()
    matrix_item = models.ForeignKey(
        "catalog.VenueProductMatrixItem", on_delete=models.CASCADE, related_name="import_aliases"
    )
    confirmed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="confirmed_venue_product_import_aliases",
    )

    class Meta:
        db_table = "venue_product_import_aliases"
        constraints = [
            models.UniqueConstraint(fields=["venue", "raw_name"], name="venue_product_import_alias_venue_raw_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.raw_name} -> {self.matrix_item_id} ({self.venue_id})"


class SupplierProductVenuePriceSourceType(models.TextChoices):
    MANUAL = "manual", "Вручную"
    TENDER = "tender", "Тендер"


class SupplierProductVenuePriceStatus(models.TextChoices):
    ACTIVE = "active", "Активна"
    ARCHIVED = "archived", "Архивна"


class SupplierProductVenuePrice(UUIDTimestampedModel):
    """2026-08-31 design (docs/superpowers/specs/2026-08-31-master-catalog-design.md
    §3.5): individual (per-venue) price for a supplier's product, overriding
    SupplierProduct.current_price for the venue on this row while active —
    read-time resolution only (see resolve_supplier_product_price in
    api/v1/catalog/selectors.py), never denormalized onto SupplierProduct.

    2026-09-07 (Павел's "Поставщик Изменения цен - ТЗ", §12/§46-47): one
    venue per row (was a `venues` M2M) + `group_id` linking rows created by
    one batch "assign to several venues" operation — replaces the M2M's
    documented flaw where superseding a conflict on one venue silently
    archived the whole shared row's *other* venues too. `group_id` is purely
    a display/undo convenience (§12: "later the supplier can change just
    Кафе 2" — the group auto-splits since each venue already has its own
    row); no uniqueness or FK is enforced on it, several unrelated groups
    can coincidentally never collide since it's a fresh uuid4 per batch.
    source_type/source_reference are a deliberate seam for the future
    Tenders module (spec §3.6): a winning tender bid creates a row here with
    source_type=TENDER."""

    # 2026-09-23: RESTRICT (backlog#task-69, ТЗ «Карточка товара поставщика»
    # §32) — individual price history must survive a product "delete" (which
    # is archive again, see delete_supplier_product()); wasn't part of the
    # original 2026-08-11 CASCADE batch (this model shipped later) but wiped
    # the same history, so it gets the same fix.
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.RESTRICT,
        related_name="venue_prices",
    )
    venue = models.ForeignKey(
        "core.Venue",
        on_delete=models.CASCADE,
        related_name="supplier_product_prices",
    )
    group_id = models.UUIDField(
        default=uuid.uuid4,
        db_index=True,
        help_text="Связывает строки, созданные одной операцией назначения цены нескольким заведениям.",
    )
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(blank=True, null=True)
    source_type = models.CharField(
        max_length=16,
        choices=SupplierProductVenuePriceSourceType.choices,
        default=SupplierProductVenuePriceSourceType.MANUAL,
    )
    source_reference = models.TextField(blank=True, null=True)
    source_tender_offer = models.ForeignKey(
        "catalog.TenderOffer",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="applied_venue_prices",
        help_text="§23 ТЗ «Карточка товара поставщика»: структурная ссылка на тендер вместо/вместе с "
        "текстовым source_reference — делает «Тендер №...» кликабельным. SET_NULL: удаление предложения "
        "не должно стирать историю уже назначенной цены, только связь на переход.",
    )
    comment = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="supplier_product_venue_prices",
    )
    status = models.CharField(
        max_length=16, choices=SupplierProductVenuePriceStatus.choices, default=SupplierProductVenuePriceStatus.ACTIVE
    )

    class Meta:
        db_table = "supplier_product_venue_prices"
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name="sp_venue_prices_positive"),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=models.F("valid_from")),
                name="sp_venue_prices_period",
            ),
        ]
        indexes = [
            models.Index(fields=["supplier_product", "status"], name="spvp_product_status_idx"),
            models.Index(fields=["supplier_product", "venue", "status"], name="spvp_product_venue_status_idx"),
            models.Index(fields=["group_id"], name="spvp_group_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_product_id} — {self.price} {self.currency}"


class VenueProductList(UUIDTimestampedModel):
    """2026-08-31 design (docs/superpowers/specs/2026-08-31-master-catalog-design.md
    §3.4): «Списки» — a venue's own named groupings of its
    VenueProductMatrixItem rows (e.g. "Для бара"/"Для кухни"), purely for
    the venue's own organization. Not a separate product catalog — a
    VenueProductMatrixItem can belong to several lists at once (M2M lives
    on VenueProductMatrixItem.lists, see below), and archiving a list must
    never touch the matrix items themselves (see this plan's Global
    Constraints — deliberate deviation from the Wishlist/WishlistItem
    cascade-archive pattern this model otherwise mirrors)."""

    venue = models.ForeignKey("core.Venue", on_delete=models.CASCADE, related_name="product_lists")
    name = models.TextField()
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "venue_product_lists"
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "name"],
                condition=models.Q(status="active"),
                name="venue_product_lists_venue_name_active_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["venue", "status"], name="vpl_venue_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.venue})"


class ProductMapping(UUIDTimestampedModel):
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="product_mappings",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="product_mappings",
    )
    venue = models.ForeignKey(
        "core.Venue",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="product_mappings",
    )
    external_system = models.CharField(max_length=32, choices=ExternalSystem.choices, blank=True, null=True)
    external_product_id = models.TextField(blank=True, null=True)
    external_product_name = models.TextField(blank=True, null=True)
    scope = models.CharField(max_length=16, choices=MappingScope.choices, default=MappingScope.GLOBAL)
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        blank=True,
        null=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    status = models.CharField(max_length=16, choices=MappingStatus.choices, default=MappingStatus.SUGGESTED)
    suggested_by = models.CharField(max_length=16, choices=SuggestionSource.choices, blank=True, null=True)
    confirmed_by_user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="confirmed_product_mappings",
    )
    confirmed_at = models.DateTimeField(blank=True, null=True)
    source_upload_item = models.ForeignKey(
        "suppliers.SupplierPriceUploadItem",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="product_mappings",
    )

    class Meta:
        db_table = "product_mappings"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confidence__isnull=True)
                | (models.Q(confidence__gte=0) & models.Q(confidence__lte=1)),
                name="product_mappings_confidence_range",
            ),
            models.CheckConstraint(
                condition=models.Q(product__isnull=False) | models.Q(external_product_id__isnull=False),
                name="product_mappings_has_target",
            ),
        ]
        indexes = [
            models.Index(fields=["supplier_product", "status"], name="pm_supplier_product_status_idx"),
            models.Index(fields=["product"], name="product_mappings_product_idx"),
            models.Index(fields=["venue", "external_system", "external_product_id"], name="pm_venue_external_idx"),
        ]


class GoogleSheetSyncLog(UUIDTimestampedModel):
    spreadsheet_id = models.TextField()
    sheet_name = models.TextField(default="Товары от поставщиков")
    status = models.CharField(max_length=16, choices=GoogleSheetSyncStatus.choices)
    trigger = models.CharField(
        max_length=16, choices=GoogleSheetSyncTrigger.choices, default=GoogleSheetSyncTrigger.MANUAL
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="google_sheet_syncs",
    )
    upload = models.ForeignKey(
        "suppliers.SupplierPriceUpload",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="google_sheet_syncs",
    )
    n8n_execution_id = models.TextField(blank=True, null=True)
    rows_read = models.IntegerField(default=0)
    rows_written = models.IntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "google_sheet_sync_logs"
        indexes = [
            models.Index(fields=["status", "-started_at"], name="gssl_status_started_idx"),
            models.Index(fields=["upload"], name="gssl_upload_idx"),
            models.Index(fields=["n8n_execution_id"], name="gssl_n8n_execution_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.sheet_name} {self.status} {self.started_at:%Y-%m-%d %H:%M:%S}"


class Wishlist(UUIDTimestampedModel):
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="wishlists")
    name = models.TextField()
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "wishlists"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"],
                condition=models.Q(status="active"),
                name="wishlists_user_name_active_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="wishlists_user_status_idx"),
        ]

    def __str__(self) -> str:
        return self.name


TENDER_NUMBER_SEQUENCE = "tenders_number_seq"


class TenderAccess(models.TextChoices):
    OPEN = "open", "Открытый"
    INVITE_ONLY = "invite_only", "По приглашению"


class TenderStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    ACTIVE = "active", "Активен"
    COMPLETED = "completed", "Завершён"


class TenderOfferAutofillMatchStatus(models.TextChoices):
    """Not a model field — response-only enum for GET .../offer-autofill
    (TASK-67, raw ТЗ §62-64). Kept alongside the other Tender* choices for
    consistency and so drf-spectacular gets a real named enum."""

    FOUND = "found", "Найден"
    NOT_FOUND = "not_found", "Не найден"


class SupplierTenderMyWork(models.TextChoices):
    """Not a model field — the «Моя работа» filter of supplier tender lists."""

    NOT_STARTED = "not_started", "Не начинал"
    IN_PROGRESS = "in_progress", "В работе"
    DONE = "done", "Все обработано"
    PARTICIPATING = "participating", "Участвую"


class TenderVenuePriceSource(models.TextChoices):
    """Not a model field — response-only enum for the supplier's price per
    venue of a tender position (raw ТЗ §58-60)."""

    INDIVIDUAL = "individual", "Индивидуальная"
    BASE = "base", "Базовая"


class Tender(UUIDTimestampedModel):
    """Raw ТЗ "Тендеры - LLM Wiki" (project_raw/inbox/autosnab-tenders-and-
    individual-price-tz-2026-09-06.md, §3.1/§6/§13-15) — first slice only:
    the tender shell (create + list), per Дмитрий Подкопаев's stated
    priority 2026-09-14 (docs/wiki/logs/2026-09-14-sync-call-podkopaev-
    onboarding.md): "в тендерах надо ручка на создание тендера ...
    ручка на получение тендеров". Позиции/предложения/распределение
    (§3.2-3.4, §9+) intentionally NOT built yet — separate, later asks.

    owner is a core.User, same account-wide model as MasterProduct.owner
    above (§2: "Покупатель — пользователь учётной записи АвтоСнаб") — a
    tender isn't scoped to one Venue, items will later pick venues
    per-item (§10-11).

    status defaults to draft (§14) — publish (§15, creates the public link,
    opens supplier access) is a separate not-yet-built action, so there is
    no `published_at` field yet either (avoiding a dead column ahead of
    that endpoint existing). accepts_offers_until=None means "без срока"
    (§7) — the two-way choice in the raw ТЗ doesn't need its own flag
    field, null already says everything a boolean would.

    Auto-transition to `completed` once accepts_offers_until passes (§7)
    is NOT implemented yet — deliberately out of scope until publish/
    activate exists; listed as a known gap in the wiki writeback."""

    created_by = models.ForeignKey("core.User", on_delete=models.RESTRICT, related_name="tenders")
    # docs/adr/0012: platform-wide «Тендер №15», assigned by the database on insert.
    number = models.PositiveIntegerField(
        unique=True,
        editable=False,
        db_default=models.Func(
            models.Value(TENDER_NUMBER_SEQUENCE), function="nextval", output_field=models.BigIntegerField()
        ),
    )
    name = models.TextField()
    description = models.TextField(blank=True, null=True)
    accepts_offers_until = models.DateTimeField(blank=True, null=True)
    # docs/adr/0018: set when the draft is published; null for a draft and for tenders published before history existed.
    published_at = models.DateTimeField(blank=True, null=True)
    access = models.CharField(max_length=16, choices=TenderAccess.choices)
    invited_suppliers = models.ManyToManyField("suppliers.Supplier", related_name="tender_invites", blank=True)
    status = models.CharField(max_length=16, choices=TenderStatus.choices, default=TenderStatus.DRAFT)

    class Meta:
        db_table = "tenders"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["created_by", "-created_at"], name="tenders_created_by_idx"),
        ]

    def __str__(self) -> str:
        return self.name


class TenderItem(UUIDTimestampedModel):
    """Raw ТЗ "Тендеры - LLM Wiki" §3.2/§9-11 — one Мастер-товар inside a
    tender. master_product uses PROTECT (not the SET_NULL the rest of the
    catalog uses for MasterProduct references) — deleting a master product
    that's referenced by a tender item would silently break the tender's
    core meaning ("a position IS a master product"), and this incidentally
    satisfies the ORIGINAL 57-section ТЗ's §51 ("блокировка удаления при
    активном тендере"), which was explicitly deferred pending a Tender
    model existing at all (docs/wiki/master-catalog-tenders-tz-compliance-
    2026-09-04.md §51).

    product_kind_label is free text (§10: "Вид товара" — "Покупатель /
    справочник"), not an FK to catalog.ProductCategory — that model is
    scoped to the platform-wide catalog.Product, not MasterProduct, and the
    raw ТЗ doesn't require a controlled vocabulary here. Matches the
    project's existing precedent for this kind of field
    (VenueProductMatrixItem.requested_unit_label is free text for the same
    reason).

    unit is NOT stored on this model — §10 says its source is "Мастер-
    справочник", i.e. read at display time from master_product.unit,
    never duplicated (same reasoning MasterProduct.net_value uses for not
    syncing from SupplierProduct.net_value)."""

    tender = models.ForeignKey("catalog.Tender", on_delete=models.CASCADE, related_name="items")
    master_product = models.ForeignKey("catalog.MasterProduct", on_delete=models.PROTECT, related_name="tender_items")
    product_kind_label = models.TextField(blank=True, null=True)
    comment = models.TextField(blank=True, null=True)
    venues = models.ManyToManyField("core.Venue", related_name="tender_items", blank=True)
    published = models.BooleanField(
        default=True,
        help_text="TASK-59 (raw ТЗ §18/§75): колонка «Публикация» в таблице товаров карточки тендера. "
        "Позиция создаётся уже опубликованной. Снятие с публикации — способ скрыть позицию от "
        "поставщиков без физического удаления (§75, актуально при наличии истории предложений).",
    )

    class Meta:
        db_table = "tender_items"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tender", "master_product"], name="tender_items_tender_master_product_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.master_product.canonical_name} ({self.tender_id})"


class TenderOffer(UUIDTimestampedModel):
    """Raw ТЗ §3.3: one supplier's current answer to one tender position —
    "1 поставщик + 1 позиция тендера = 1 текущее предложение", enforced by
    the unique constraint below. supplier_product uses SET_NULL (not
    RESTRICT/PROTECT) to match the project's existing precedent for
    "a deleted supplier product shouldn't destroy history"
    (VenueProductMatrixItem.supplier_product does the same) — this minimal
    slice deliberately does NOT add a supplier_product_deleted flag the way
    TASK-35/37 did for that model; that's an accepted, documented gap for
    this first version (see the plan's Scope & Deferred Items), not an
    oversight.

    venues here means "готов поставлять" (§21/§27) — the supplier's own
    subset of the position's venues, separate from and validated against
    TenderItem.venues at submission time (see submit_tender_offer)."""

    tender_item = models.ForeignKey("catalog.TenderItem", on_delete=models.CASCADE, related_name="offers")
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.RESTRICT, related_name="tender_offers")
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="tender_offers",
    )
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    venues = models.ManyToManyField("core.Venue", related_name="tender_offer_availability", blank=True)
    comment = models.TextField(blank=True, null=True)
    withdrawn_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="TASK-90 (raw ТЗ §67, docs/adr/0006): поставщик снял предложение («Не предлагаем»). "
        "Запись остаётся; повторная подача обнуляет поле.",
    )

    class Meta:
        db_table = "tender_offers"
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["tender_item", "supplier"], name="tender_offers_item_supplier_uniq"),
            models.CheckConstraint(condition=models.Q(price__gte=0), name="tender_offers_price_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_id} -> {self.tender_item_id}"


class TenderChangeType(models.TextChoices):
    CREATED = "created", "Создан"
    PUBLISHED = "published", "Опубликован"
    UPDATED = "updated", "Изменён"
    ITEM_ADDED = "item_added", "Добавлен товар"
    ITEM_UPDATED = "item_updated", "Изменён товар"
    ITEM_PUBLISHED = "item_published", "Товар опубликован"
    ITEM_UNPUBLISHED = "item_unpublished", "Товар снят с публикации"
    FILE_ADDED = "file_added", "Добавлен файл"
    FILE_REMOVED = "file_removed", "Удалён файл"
    OFFER_SUBMITTED = "offer_submitted", "Отправлено предложение"
    OFFER_UPDATED = "offer_updated", "Изменено предложение"
    OFFER_WITHDRAWN = "offer_withdrawn", "Предложение снято"
    OFFER_RETURNED = "offer_returned", "Предложение возвращено"
    COMPLETED = "completed", "Завершён"


class TenderChangedField(models.TextChoices):
    NAME = "name", "Название"
    DESCRIPTION = "description", "Описание"
    ACCEPTS_OFFERS_UNTIL = "accepts_offers_until", "Срок приёма предложений"
    INVITED_SUPPLIERS = "invited_suppliers", "Приглашённые поставщики"
    COMMENT = "comment", "Комментарий"
    PRODUCT_KIND_LABEL = "product_kind_label", "Вид товара"
    VENUES = "venues", "Заведения"
    SUPPLIER_PRODUCT = "supplier_product", "Товар поставщика"
    PRICE = "price", "Цена"


class TenderChange(UUIDTimestampedModel):
    """Raw ТЗ §16/§76, docs/adr/0010: one row per lifecycle event or per
    changed field, append-only. Values are display strings snapshotted at
    write time (ISO 8601 for the deadline, names joined for lists), so a
    later rename does not rewrite history. actor is null for completion by
    deadline; occurred_at is then the deadline itself."""

    tender = models.ForeignKey("catalog.Tender", on_delete=models.CASCADE, related_name="changes")
    tender_item = models.ForeignKey(
        "catalog.TenderItem", on_delete=models.SET_NULL, blank=True, null=True, related_name="changes"
    )
    tender_offer = models.ForeignKey(
        "catalog.TenderOffer", on_delete=models.SET_NULL, blank=True, null=True, related_name="changes"
    )
    change_type = models.CharField(max_length=32, choices=TenderChangeType.choices)
    changed_field = models.CharField(max_length=32, choices=TenderChangedField.choices, blank=True, null=True)
    old_value = models.TextField(blank=True, null=True)
    new_value = models.TextField(blank=True, null=True)
    actor = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, blank=True, null=True, related_name="tender_changes"
    )
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "tender_changes"
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["tender", "-occurred_at"], name="tender_changes_tender_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.tender_id} {self.change_type} {self.changed_field or ''}".rstrip()


class TenderFile(UUIDTimestampedModel):
    """Raw ТЗ §6/§16, docs/adr/0011: a buyer's document attached to a tender.
    Stored in the project's default storage under storage_path; the public
    URL is built from it at read time. name is the uploaded file name."""

    tender = models.ForeignKey("catalog.Tender", on_delete=models.CASCADE, related_name="files")
    name = models.TextField()
    storage_path = models.CharField(max_length=500)
    size = models.PositiveBigIntegerField()
    uploaded_by = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, blank=True, null=True, related_name="tender_files"
    )

    class Meta:
        db_table = "tender_files"
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return self.name


class WishlistItem(UUIDTimestampedModel):
    wishlist = models.ForeignKey("catalog.Wishlist", on_delete=models.CASCADE, related_name="items")
    # 2026-08-11: was RESTRICT -> CASCADE, same reasoning as
    # VenueSupplierProduct.supplier_product above. 2026-09-23: reverted back
    # to RESTRICT along with it (backlog#task-69).
    supplier_product = models.ForeignKey(
        "suppliers.SupplierProduct",
        on_delete=models.RESTRICT,
        related_name="wishlist_items",
    )
    status = models.CharField(max_length=16, choices=EntityStatus.choices, default=EntityStatus.ACTIVE)

    class Meta:
        db_table = "wishlist_items"
        constraints = [
            models.UniqueConstraint(
                fields=["wishlist", "supplier_product"], name="wishlist_items_wishlist_product_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["wishlist", "status"], name="wi_wishlist_status_idx"),
            models.Index(fields=["supplier_product"], name="wi_supplier_product_idx"),
        ]


# Create your models here.
