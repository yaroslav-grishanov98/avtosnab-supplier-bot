"""Applying a normalized price upload to the catalog: row classification, caches, bulk writes."""

import hashlib
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.models import (
    EntityStatus,
    Unit,
    UnitKind,
)
from suppliers.models import (
    PriceStatus,
    PriceType,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductPrice,
    UploadItemStatus,
    UploadStatus,
)

from ._shared import _currency_value, _json_safe, _resolve_ai_unit, _rows_for_upload, logger
from .product_refs import _resolve_brand, _resolve_category_path, _system_sku_stream

AI_CONFIDENCE_REVIEW_THRESHOLD = Decimal("0.8000")


# apply_supplier_price_ai_result's per-row progress reporting (see
# _apply_progress_reporter) — every Nth row, not every row: each report is
# its own out-of-band SQL round-trip (~47ms measured live), so reporting
# every single row would meaningfully add to the total runtime for a large
# upload instead of just observing it.
APPLY_PROGRESS_UPDATE_EVERY = 25


def _catalog_qdrant_sync_schedule_status():
    if not settings.CATALOG_QDRANT_AUTO_SYNC_ENABLED:
        return {"scheduled": False, "reason": "disabled"}
    if not settings.QDRANT_URL or not settings.QDRANT_API_KEY:
        return {"scheduled": False, "reason": "qdrant_not_configured"}
    return {"scheduled": True}


def _enqueue_supplier_catalog_qdrant_sync(*, supplier_id, upload_id):
    from api.v1.suppliers.tasks import sync_supplier_catalog_to_qdrant_task

    try:
        task_result = sync_supplier_catalog_to_qdrant_task.delay(
            supplier_id=str(supplier_id),
            upload_id=str(upload_id),
        )
    except Exception as error:
        logger.exception(
            "supplier_catalog.qdrant_sync.enqueue_failed upload_id=%s supplier_id=%s error=%s",
            upload_id,
            supplier_id,
            error,
        )
        return {"accepted": False, "reason": "enqueue_failed", "error": str(error)}

    logger.info(
        "supplier_catalog.qdrant_sync.enqueued upload_id=%s supplier_id=%s task_id=%s",
        upload_id,
        supplier_id,
        task_result.id,
    )
    return {"accepted": True, "task_id": task_result.id}


def _acquire_upload_apply_lock(*, upload_id):
    # Same advisory-lock mechanism as _acquire_upload_dedup_lock above, but
    # keyed on the specific upload_id — serializes concurrent
    # apply_supplier_price_ai_result() calls for the SAME upload (the guard
    # it needs — SupplierPriceAIApplyView calls it directly with no upstream
    # status gate) without holding a lock on the SupplierPriceUpload row
    # itself, unlike the select_for_update() this replaced. See the comment
    # at apply_supplier_price_ai_result's call site for why that mattered.
    raw_key = f"supplier_price_upload_apply:{upload_id}"
    lock_key = int(hashlib.sha256(raw_key.encode()).hexdigest()[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])


def _resolve_unit_code(unit_code):
    if not unit_code:
        return None
    return Unit.objects.filter(code=unit_code, status=EntityStatus.ACTIVE).first()


def _resolve_quantity_unit(product_data, field_name, *, expected_kind):
    unit = _resolve_unit_code(product_data.get(field_name))
    if unit and (expected_kind is None or unit.kind == expected_kind):
        return unit
    return None


def _cached_resolve_ai_unit(product_data, cache):
    """Memoized _resolve_ai_unit — apply_supplier_price_ai_result's
    bulk-write path (2026-08-18) calls this once per row, and real uploads
    overwhelmingly reuse a small handful of unit codes ("шт", "кг", ...)
    across hundreds of rows; unlike SupplierProduct/SupplierPriceUploadItem/
    SupplierProductPrice, Unit isn't something this function ever creates,
    so a plain per-run dict cache is enough — no bulk-prefetch needed."""
    key = (
        ("id", product_data.get("unit_id")) if product_data.get("unit_id") else ("code", product_data.get("unit_code"))
    )
    if key not in cache:
        cache[key] = _resolve_ai_unit(product_data)
    return cache[key]


def _cached_resolve_quantity_unit(product_data, field_name, *, expected_kind, cache):
    key = (field_name, expected_kind, product_data.get(field_name))
    if key not in cache:
        cache[key] = _resolve_quantity_unit(product_data, field_name, expected_kind=expected_kind)
    return cache[key]


def _cached_resolve_pack(product_data, cache):
    """TASK-111: состав упаковки из строки прайса -> значения полей SupplierProduct,
    или None, если строка его не несёт или единица не подходит (тогда состав товара
    не трогаем). Единица содержимого — вес/объём/штуки, тара — только Unit вида package."""
    if product_data.get("pack_items_count") is None:
        return None
    item_unit = _cached_resolve_quantity_unit(product_data, "pack_item_unit_code", expected_kind=None, cache=cache)
    if item_unit is None or item_unit.kind == UnitKind.PACKAGE or product_data.get("pack_item_value") is None:
        return None
    pack_type = None
    if product_data.get("pack_type_code"):
        pack_type = _cached_resolve_quantity_unit(
            product_data, "pack_type_code", expected_kind=UnitKind.PACKAGE, cache=cache
        )
    return {
        "pack_items_count": int(product_data["pack_items_count"]),
        "pack_item_value": Decimal(str(product_data["pack_item_value"])),
        "pack_item_unit": item_unit,
        "pack_type": pack_type,
    }


def _ai_item_needs_review(product_data, unit):
    snapshot = (product_data.get("raw_payload") or {}).get("canonical", {})
    if snapshot:
        from file_imports.canonical import effective_issues

        if effective_issues(snapshot):
            return True
        availability = snapshot.get("fields", {}).get("availability_label", {})
        overrides = {value["field"] for value in snapshot.get("overrides", [])}
        if availability.get("status") in ("missing", "defaulted") and "availability_label" not in overrides:
            return True
    confidence = product_data.get("confidence")
    if confidence is not None and confidence < AI_CONFIDENCE_REVIEW_THRESHOLD:
        return True

    return not all(
        [
            product_data.get("raw_name"),
            product_data.get("normalized_name"),
            product_data.get("price") is not None,
            unit,
        ]
    )


def _package_value(product_data):
    return product_data.get("package") or product_data.get("package_info") or None


def _availability_status_value(product_data):
    value = (product_data.get("availability_status") or "available").strip().lower()
    if value in {"archived", "deleted", "removed"}:
        return EntityStatus.ARCHIVED
    if value in {"unavailable", "out_of_stock", "inactive", "not_available"}:
        return EntityStatus.INACTIVE
    return EntityStatus.ACTIVE


def _get_existing_supplier_product(*, upload, product_data):
    supplier_product_id = product_data.get("supplier_product_id")
    if supplier_product_id:
        supplier_product = (
            SupplierProduct.objects.select_for_update().filter(id=supplier_product_id, supplier=upload.supplier).first()
        )
        if supplier_product:
            return supplier_product

    supplier_sku = product_data.get("supplier_sku")
    if supplier_sku:
        found = (
            SupplierProduct.objects.select_for_update()
            .filter(supplier=upload.supplier, supplier_sku=supplier_sku)
            .first()
        )
        if found:
            return found

    raw_name = product_data.get("raw_name")
    if raw_name:
        found = (
            SupplierProduct.objects.select_for_update().filter(supplier=upload.supplier, supplier_name=raw_name).first()
        )
        if found:
            return found

    normalized_name = product_data.get("normalized_name")
    if not normalized_name:
        return None

    candidate = (
        SupplierProduct.objects.select_for_update()
        .filter(
            supplier=upload.supplier,
            normalized_name=normalized_name,
            package=_package_value(product_data),
        )
        .first()
    )
    if candidate and (not raw_name or candidate.supplier_name == raw_name):
        return candidate
    return None


def _prefetch_existing_supplier_products(*, upload, products):
    """Bulk equivalent of _get_existing_supplier_product for
    apply_supplier_price_ai_result's per-row loop — 2026-08-17, real 834-row
    upload measured at ~1.3s/row, apply()'s own docstring on
    APPLY_PROGRESS_UPDATE_EVERY has the fuller story. _get_existing_supplier_product
    ran up to 4 sequential SELECTs per row (id, sku, raw_name,
    normalized_name+package), each paying the ~47ms DB round-trip floor
    measured in the 2026-08-15/16 latency investigations — almost all of
    them misses for a first-time upload, since there's nothing to match yet.
    4 bulk queries (one per lookup key, IN a batch) replace up to 4×N.

    Same lookup priority as _get_existing_supplier_product: id, then sku,
    then raw_name, then normalized_name+package — see
    _resolve_supplier_product_from_cache, which every row consults instead
    of hitting the DB directly. Newly created SupplierProducts get folded
    back into the cache by the loop (see _remember_supplier_product_in_cache)
    so a later row in the SAME batch referencing the same raw_name/sku/id
    still finds it — matching what the old per-row queries saw naturally
    (a just-.create()'d row is visible to later SELECTs in the same
    transaction)."""
    ids = {p["supplier_product_id"] for p in products if p.get("supplier_product_id")}
    skus = {p["supplier_sku"] for p in products if p.get("supplier_sku")}
    raw_names = {p["raw_name"] for p in products if p.get("raw_name")}
    normalized_names = {p["normalized_name"] for p in products if p.get("normalized_name")}

    cache = {"by_id": {}, "by_sku": {}, "by_raw_name": {}, "by_normalized_and_package": {}}
    if ids:
        for sp in SupplierProduct.objects.select_for_update().filter(supplier=upload.supplier, id__in=ids):
            cache["by_id"][sp.id] = sp
    if skus:
        for sp in SupplierProduct.objects.select_for_update().filter(supplier=upload.supplier, supplier_sku__in=skus):
            cache["by_sku"][sp.supplier_sku] = sp
    if raw_names:
        for sp in SupplierProduct.objects.select_for_update().filter(
            supplier=upload.supplier, supplier_name__in=raw_names
        ):
            cache["by_raw_name"][sp.supplier_name] = sp
    if normalized_names:
        for sp in SupplierProduct.objects.select_for_update().filter(
            supplier=upload.supplier, normalized_name__in=normalized_names
        ):
            cache["by_normalized_and_package"].setdefault((sp.normalized_name, sp.package), sp)
    return cache


def _resolve_supplier_product_from_cache(*, cache, product_data):
    supplier_product_id = product_data.get("supplier_product_id")
    if supplier_product_id and supplier_product_id in cache["by_id"]:
        return cache["by_id"][supplier_product_id]

    supplier_sku = product_data.get("supplier_sku")
    if supplier_sku and supplier_sku in cache["by_sku"]:
        return cache["by_sku"][supplier_sku]

    raw_name = product_data.get("raw_name")
    if raw_name and raw_name in cache["by_raw_name"]:
        return cache["by_raw_name"][raw_name]

    normalized_name = product_data.get("normalized_name")
    if not normalized_name:
        return None

    candidate = cache["by_normalized_and_package"].get((normalized_name, _package_value(product_data)))
    if candidate and (not raw_name or candidate.supplier_name == raw_name):
        return candidate
    return None


def _remember_supplier_product_in_cache(*, cache, supplier_product):
    cache["by_id"][supplier_product.id] = supplier_product
    if supplier_product.supplier_sku:
        cache["by_sku"][supplier_product.supplier_sku] = supplier_product
    cache["by_raw_name"][supplier_product.supplier_name] = supplier_product
    cache["by_normalized_and_package"][(supplier_product.normalized_name, supplier_product.package)] = supplier_product


def _prefetch_active_prices_by_product(*, product_ids):
    """Bulk lookup of the current active price for
    apply_supplier_price_ai_result's bulk-write path (2026-08-18) — one
    query for every product apply() *might* touch (see the caller: the full
    union of all 4 prefetch-cache dicts, a safe over-fetch, same pattern as
    _next_system_sku_batch) instead of one per row. Returns ALL active rows
    per product (not just the first), same as _apply_price's own list(...)
    query — needed to replicate its "exactly one active row and it matches"
    no-op check; ordered by (-valid_from,
    -created_at) so callers wanting just the latest can take index 0."""
    if not product_ids:
        return {}
    prices_by_product = {}
    for price in (
        SupplierProductPrice.objects.select_for_update()
        .filter(supplier_product_id__in=product_ids, status=PriceStatus.ACTIVE, valid_to__isnull=True)
        .order_by("supplier_product_id", "-valid_from", "-created_at")
    ):
        prices_by_product.setdefault(price.supplier_product_id, []).append(price)
    return prices_by_product


def _prefetch_existing_upload_items_by_row(*, upload, row_numbers):
    """Bulk equivalent of _upsert_upload_item's existing-item lookup, for
    the same reason as _prefetch_active_prices_by_product above — one query
    for the whole batch instead of one per row."""
    if not row_numbers:
        return {}
    return {
        item.row_number: item
        for item in SupplierPriceUploadItem.objects.select_for_update().filter(
            upload=upload, row_number__in=row_numbers
        )
    }


def _bulk_sync_supplier_product_price_snapshots(product_ids):
    """Set-based equivalent of suppliers/services.py's
    sync_supplier_product_price_snapshot(), for callers that write
    SupplierProductPrice via bulk_create()/queryset .update() — neither
    fires the post_save signal (suppliers/signals.py) that normally keeps
    SupplierProduct.current_price/previous_price/price_changed_at in sync.
    Same correlated-subquery shape as bulk_restore_supplier_products
    elsewhere in this file — one UPDATE for any number of product ids."""
    if not product_ids:
        return
    previous_price_subquery = SupplierProductPrice.objects.filter(
        supplier_product_id=OuterRef("pk"),
        price_type=PriceType.PRICE_LIST,
        status=PriceStatus.SUPERSEDED,
    ).order_by("-valid_to", "-created_at")
    SupplierProduct.objects.filter(pk__in=product_ids).update(
        current_price=Subquery(
            SupplierProductPrice.objects.filter(
                supplier_product_id=OuterRef("pk"),
                price_type=PriceType.PRICE_LIST,
                status=PriceStatus.ACTIVE,
                valid_to__isnull=True,
            )
            .order_by("-valid_from", "-created_at")
            .values("price")[:1]
        ),
        previous_price=Subquery(previous_price_subquery.values("price")[:1]),
        price_changed_at=Subquery(previous_price_subquery.values("valid_to")[:1]),
    )


def _same_decimal(left, right):
    if left is None or right is None:
        return left is None and right is None
    return Decimal(left) == Decimal(right)


def _same_optional_id(left, right):
    left_id = getattr(left, "id", left)
    right_id = getattr(right, "id", right)
    return left_id == right_id


def _resolve_net_measure(product_data, *, weight_unit, volume_unit):
    # 2026-08-15: SupplierProduct.weight_value/volume_value объединены в
    # net_value/net_unit (см. миграцию 0039) — raw-парсинг (_extract_package в
    # normalization.py) по-прежнему детектит вес и объём независимо (некоторые
    # позиции реально указывают оба в названии, например жидкости — "1л
    # (1,030 кг)"), выбор одного значения происходит только здесь, в точке
    # записи в SupplierProduct. При конфликте побеждает вес (подтверждено
    # пользователем явно, см. docs/wiki/logs/2026-08-15-net-value-unit-merge.md).
    weight = product_data.get("weight")
    if weight is not None:
        return weight, weight_unit
    return product_data.get("volume"), volume_unit


def _supplier_product_matches_payload(*, supplier_product, product_data, unit, net_value, net_unit, pack=None):
    if not supplier_product:
        return False

    return all(
        [
            # pack — только если строка прайса несёт состав упаковки (TASK-111); без колонки
            # ручной состав товара не считается расхождением и не затирается.
            pack is None
            or (
                supplier_product.pack_items_count == pack["pack_items_count"]
                and _same_decimal(supplier_product.pack_item_value, pack["pack_item_value"])
                and _same_optional_id(supplier_product.pack_item_unit_id, pack["pack_item_unit"])
                and _same_optional_id(supplier_product.pack_type_id, pack["pack_type"])
            ),
            supplier_product.supplier_name == product_data["raw_name"],
            supplier_product.normalized_name == product_data["normalized_name"],
            supplier_product.package == _package_value(product_data),
            _same_optional_id(supplier_product.unit_id, unit),
            _same_decimal(supplier_product.net_value, net_value),
            _same_optional_id(supplier_product.net_unit_id, net_unit),
            _same_decimal(supplier_product.min_order_qty, product_data.get("min_order_qty")),
            _same_decimal(supplier_product.delivery_multiple, product_data.get("multiplicity")),
            supplier_product.status == _availability_status_value(product_data),
        ]
    )


def _upsert_upload_item(*, upload, row_number, values):
    upload_item = (
        SupplierPriceUploadItem.objects.select_for_update().filter(upload=upload, row_number=row_number).first()
    )
    if upload_item:
        for field, value in values.items():
            setattr(upload_item, field, value)
        upload_item.save(update_fields=[*values.keys(), "updated_at"])
        return upload_item, False

    return (
        SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=row_number,
            **values,
        ),
        True,
    )


_PRODUCT_BULK_UPDATE_FIELDS = [
    "unit",
    "brand",
    "category",
    "system_sku",
    "normalized_name",
    "image_url",
    "package",
    "net_value",
    "net_unit",
    "min_order_qty",
    "delivery_multiple",
    "pack_items_count",
    "pack_item_value",
    "pack_item_unit",
    "pack_type",
    "comment",
    "status",
    "last_upload",
    "updated_at",
]


_ITEM_BULK_UPDATE_FIELDS = [
    "raw_name",
    "raw_unit",
    "raw_price",
    "raw_payload",
    "normalized_name",
    "parsed_price",
    "parsed_unit",
    "status",
    "review_comment",
    "supplier_product",
    "updated_at",
]


class _ApplyBatchState:
    """Accumulator for apply_supplier_price_ai_result's classification pass
    — _classify_price_upload_row appends to these lists/dict instead of
    writing to the DB; the caller executes everything in bulk afterwards."""

    def __init__(self):
        self.new_products = []
        self.products_to_update = []
        self.items_to_create = []
        self.items_to_update = []
        # supplier_product.id -> {"product", "originals" (true pre-batch
        # active SupplierProductPrice rows), "final" (price, currency,
        # upload_item, comment) or None if no price-bearing row touched it.
        self.price_plan = {}
        self.created_items = 0
        self.created_products = 0
        self.matched_items = 0
        self.needs_review = 0


def _cached_resolve_brand(brand_name, cache):
    if not brand_name or not brand_name.strip():
        return None
    key = brand_name.strip().lower()
    if key not in cache:
        cache[key] = _resolve_brand(brand_name)
    return cache[key]


def _cached_resolve_category_path(category_path, cache):
    if not category_path:
        return None
    key = tuple((name or "").strip() for name in category_path)
    if key not in cache:
        cache[key] = _resolve_category_path(category_path)
    return cache[key]


def _stage_item_write(*, upload, row_number, values, existing_items_by_row, batch):
    existing = existing_items_by_row.get(row_number)
    if existing:
        for field, value in values.items():
            setattr(existing, field, value)
        existing.updated_at = timezone.now()
        batch.items_to_update.append(existing)
        return existing

    item = SupplierPriceUploadItem(upload=upload, row_number=row_number, **values)
    batch.items_to_create.append(item)
    batch.created_items += 1
    return item


def _classify_price_upload_row(
    *,
    upload,
    row_number,
    product_data,
    product_cache,
    active_prices_by_product,
    existing_items_by_row,
    pending_system_skus,
    brand_cache,
    category_cache,
    unit_cache,
    batch,
):
    """Per-row classification step for apply_supplier_price_ai_result's
    bulk-write path — see the module comment above this class for why.
    Mirrors the old per-row loop's control flow branch-for-branch, but
    builds unsaved instances / plan entries into `batch` instead of writing
    immediately. The only DB reads left here are against already-prefetched
    in-memory dicts / per-run caches — zero queries per row."""
    unit = _cached_resolve_ai_unit(product_data, unit_cache)
    volume_unit = _cached_resolve_quantity_unit(
        product_data, "volume_unit", expected_kind=UnitKind.VOLUME, cache=unit_cache
    )
    weight_unit = _cached_resolve_quantity_unit(
        product_data, "weight_unit", expected_kind=UnitKind.WEIGHT, cache=unit_cache
    )
    net_value, net_unit = _resolve_net_measure(product_data, weight_unit=weight_unit, volume_unit=volume_unit)
    pack = _cached_resolve_pack(product_data, unit_cache)
    item_status = UploadItemStatus.NEEDS_REVIEW if _ai_item_needs_review(product_data, unit) else UploadItemStatus.NEW

    item_values = {
        "raw_name": product_data["raw_name"],
        "raw_unit": product_data.get("unit_code") or str(product_data.get("unit_id") or ""),
        "raw_price": str(product_data["price"]) if product_data.get("price") is not None else "",
        "raw_payload": _json_safe(product_data),
        "normalized_name": product_data.get("normalized_name") or "",
        "parsed_price": product_data.get("price"),
        "parsed_unit": unit,
        "status": item_status,
        "review_comment": "Low confidence or incomplete AI data"
        if item_status == UploadItemStatus.NEEDS_REVIEW
        else "",
    }

    if item_status == UploadItemStatus.NEEDS_REVIEW:
        batch.needs_review += 1
        _stage_item_write(
            upload=upload,
            row_number=row_number,
            values=item_values,
            existing_items_by_row=existing_items_by_row,
            batch=batch,
        )
        return

    supplier_product = _resolve_supplier_product_from_cache(cache=product_cache, product_data=product_data)
    product_created = supplier_product is None
    supplier_product_status = _availability_status_value(product_data)
    package = _package_value(product_data)

    originals = [] if product_created else active_prices_by_product.get(supplier_product.id, [])
    # "Live" reference price for THIS row's matching/bookkeeping — if an
    # earlier row in this same batch already staged a price change for this
    # product (batch.price_plan), that target is what a sequential run
    # would actually see as "current" by now, not the true pre-batch
    # `originals`. Only matters for in-batch duplicate rows (see the module
    # comment above _ApplyBatchState); everywhere else pending_plan is None
    # and this is exactly _current_active_price's old .first()-of-originals.
    pending_plan = None if product_created else batch.price_plan.get(supplier_product.id)
    if pending_plan and pending_plan["final"] is not None:
        live_price, live_currency = pending_plan["final"][0], pending_plan["final"][1]
    elif originals:
        live_price, live_currency = originals[0].price, originals[0].currency
    else:
        live_price, live_currency = None, None
    price_is_match = (
        live_price is not None
        and product_data.get("price") is not None
        and live_price == product_data.get("price")
        and live_currency == _currency_value(product_data)
    )

    if (
        not product_created
        and _supplier_product_matches_payload(
            supplier_product=supplier_product,
            product_data=product_data,
            unit=unit,
            net_value=net_value,
            net_unit=net_unit,
            pack=pack,
        )
        and price_is_match
    ):
        brand = _cached_resolve_brand(product_data.get("brand"), brand_cache)
        category = _cached_resolve_category_path(product_data.get("category_path"), category_cache)
        changed = False
        if brand and supplier_product.brand_id != brand.id:
            supplier_product.brand = brand
            changed = True
        if category and supplier_product.category_id != category.id:
            supplier_product.category = category
            changed = True
        if not supplier_product.system_sku:
            supplier_product.system_sku = next(pending_system_skus)
            changed = True
        if changed:
            supplier_product.updated_at = timezone.now()
            batch.products_to_update.append(supplier_product)
        batch.matched_items += 1
        return

    if live_price is not None:
        item_values["raw_payload"] = {**item_values["raw_payload"], "old_price": str(live_price)}

    upload_item = _stage_item_write(
        upload=upload,
        row_number=row_number,
        values=item_values,
        existing_items_by_row=existing_items_by_row,
        batch=batch,
    )

    brand = _cached_resolve_brand(product_data.get("brand"), brand_cache)
    category = _cached_resolve_category_path(product_data.get("category_path"), category_cache)

    if product_created:
        supplier_product = SupplierProduct(
            supplier=upload.supplier,
            unit=unit,
            brand=brand,
            category=category,
            system_sku=next(pending_system_skus),
            supplier_sku=product_data.get("supplier_sku") or None,
            supplier_name=product_data["raw_name"],
            normalized_name=product_data["normalized_name"],
            image_url=product_data.get("image_url") or None,
            package=package,
            net_value=net_value,
            net_unit=net_unit,
            min_order_qty=product_data.get("min_order_qty"),
            delivery_multiple=product_data.get("multiplicity"),
            **(pack or {}),
            comment=product_data.get("comment") or None,
            status=supplier_product_status,
            last_upload=upload,
        )
        batch.new_products.append(supplier_product)
        batch.created_products += 1
        item_status = UploadItemStatus.NEW
        # A later row in this same batch may reference this product by
        # id/sku/raw_name/normalized_name+package — fold it into the cache
        # now so _resolve_supplier_product_from_cache finds it. Safe even
        # though `supplier_product` isn't saved yet: its .id is already the
        # real, final UUID (see module comment above).
        _remember_supplier_product_in_cache(cache=product_cache, supplier_product=supplier_product)
    else:
        supplier_product.unit = unit
        if brand:
            supplier_product.brand = brand
        if category:
            supplier_product.category = category
        if not supplier_product.system_sku:
            supplier_product.system_sku = next(pending_system_skus)
        supplier_product.normalized_name = product_data["normalized_name"]
        if product_data.get("image_url"):
            supplier_product.image_url = product_data["image_url"]
        supplier_product.package = package
        supplier_product.net_value = net_value
        supplier_product.net_unit = net_unit
        supplier_product.min_order_qty = product_data.get("min_order_qty")
        supplier_product.delivery_multiple = product_data.get("multiplicity")
        for field, value in (pack or {}).items():
            setattr(supplier_product, field, value)
        supplier_product.comment = product_data.get("comment") or supplier_product.comment
        supplier_product.status = supplier_product_status
        supplier_product.last_upload = upload
        supplier_product.updated_at = timezone.now()
        batch.products_to_update.append(supplier_product)
        item_status = UploadItemStatus.UPDATED

    upload_item.supplier_product = supplier_product
    upload_item.status = item_status

    if product_data.get("price") is not None:
        plan = batch.price_plan.setdefault(
            supplier_product.id,
            {"product": supplier_product, "originals": originals, "final": None},
        )
        plan["final"] = (product_data["price"], _currency_value(product_data), upload_item, None)


@transaction.atomic
def apply_supplier_price_ai_result(*, upload_id, data, progress_callback=None):
    # 2026-08-16: used to be select_for_update() on the SupplierPriceUpload
    # row itself, held for this whole function's per-product loop (hundreds
    # of rows, easily minutes now that normalize() defaults to async —
    # 2026-08-16-normalize-async-default-and-dedup-lock-hang.md). That row
    # lock blocked ANYTHING else touching the same row for the entire
    # duration: a normalize() retry re-entering _prepare_upload_for_normalization
    # (its own select_for_update on this row), and — the live incident that
    # found this — deleting the upload from /admin/, both hung until
    # gunicorn's --timeout 120 killed the worker. An advisory lock keyed on
    # upload_id (same pattern as _acquire_upload_dedup_lock below) still
    # serializes concurrent applies for the same upload (the guard this
    # existed for — SupplierPriceAIApplyView calls this directly, with no
    # upstream status gate) without holding a lock on the actual row, so
    # reads/writes/deletes elsewhere stay unblocked.
    _acquire_upload_apply_lock(upload_id=upload_id)
    upload = get_object_or_404(
        SupplierPriceUpload.objects.select_related("supplier"),
        pk=upload_id,
    )

    if data.get("upload_id") and data["upload_id"] != upload.id:
        raise ValidationError({"upload_id": "Payload upload_id does not match URL upload_id."})
    if data.get("supplier_id") and data["supplier_id"] != upload.supplier_id:
        raise ValidationError({"supplier_id": "Payload supplier_id does not match upload supplier_id."})
    if data.get("source_channel") and data["source_channel"] != upload.source_channel:
        raise ValidationError({"source_channel": "Payload source_channel does not match upload source_channel."})

    if getattr(settings, "CANONICAL_LEGACY_PARSER_ENABLED", False):
        from file_imports.legacy_adapter import parse_legacy_row

        by_row = {row.row_number: row for row in _rows_for_upload(upload)}
        guarded = []
        for index, product in enumerate(data["products"], 1):
            canonical = (product.get("raw_payload") or {}).get("canonical")
            if canonical is None:
                source = by_row.get(product.get("row_number") or index)
                if source:
                    reference = parse_legacy_row(source)
                    product = dict(product)
                    snapshot = reference["raw_payload"]["canonical"]
                    for key in (
                        "price",
                        "unit_code",
                        "min_order_qty",
                        "multiplicity",
                        "availability_status",
                        "supplier_sku",
                    ):
                        if str(product.get(key)) != str(reference.get(key)):
                            snapshot["parse_issues"].append(
                                {
                                    "code": "unverified_external_value",
                                    "field": key,
                                    "level": "error",
                                    "review_required": True,
                                    "message": f"Внешнее значение {key} требует подтверждения.",
                                }
                            )
                    product["raw_payload"] = reference["raw_payload"]
                else:
                    product = dict(product) | {"confidence": Decimal("0")}
            guarded.append(product)
        data = dict(data) | {"products": guarded}
    products_total = len(data["products"])
    supplier_product_cache = _prefetch_existing_supplier_products(upload=upload, products=data["products"])
    existing_product_ids = {
        sp.id
        for by_key in (
            supplier_product_cache["by_id"],
            supplier_product_cache["by_sku"],
            supplier_product_cache["by_raw_name"],
            supplier_product_cache["by_normalized_and_package"],
        )
        for sp in by_key.values()
    }
    # Safe over-fetch: this is the union of everything a row *could* resolve to, not what every
    # row actually will — cheap, and turns what used to be one
    # _current_active_price() query per existing-product row into one query
    # total for the whole batch.
    active_prices_by_product = _prefetch_active_prices_by_product(product_ids=existing_product_ids)
    row_numbers = [
        product_data.get("row_number") or index for index, product_data in enumerate(data["products"], start=1)
    ]
    existing_items_by_row = _prefetch_existing_upload_items_by_row(upload=upload, row_numbers=row_numbers)
    # Only some rows need a fresh system_sku (new product, or an existing one that never got one). Codes come
    # from a sequence, so they are fetched lazily in chunks: few queries, and no burned numbers for rows that
    # did not need one.
    pending_system_skus = _system_sku_stream()
    brand_cache = {}
    category_cache = {}
    unit_cache = {}
    batch = _ApplyBatchState()

    for index, product_data in enumerate(data["products"], start=1):
        if progress_callback and index % APPLY_PROGRESS_UPDATE_EVERY == 0:
            progress_callback(rows_done=index, rows_total=products_total)
        _classify_price_upload_row(
            upload=upload,
            row_number=product_data.get("row_number") or index,
            product_data=product_data,
            product_cache=supplier_product_cache,
            active_prices_by_product=active_prices_by_product,
            existing_items_by_row=existing_items_by_row,
            pending_system_skus=pending_system_skus,
            brand_cache=brand_cache,
            category_cache=category_cache,
            unit_cache=unit_cache,
            batch=batch,
        )

    # Bulk writes, in dependency order: products (so items/prices below can
    # reference real, INSERTed rows — their Python-side ids were already
    # valid at construction time, see the module comment above, but actual
    # FK constraints are still enforced at INSERT time), then items, then
    # prices.
    if batch.new_products:
        SupplierProduct.objects.bulk_create(batch.new_products, batch_size=500)
    if batch.products_to_update:
        SupplierProduct.objects.bulk_update(
            batch.products_to_update, fields=_PRODUCT_BULK_UPDATE_FIELDS, batch_size=500
        )

    if batch.items_to_create:
        SupplierPriceUploadItem.objects.bulk_create(batch.items_to_create, batch_size=500)
    if batch.items_to_update:
        SupplierPriceUploadItem.objects.bulk_update(
            batch.items_to_update, fields=_ITEM_BULK_UPDATE_FIELDS, batch_size=500
        )

    from file_imports.models import ImportRowReceipt

    for item in batch.items_to_create + batch.items_to_update:
        snapshot = ((item.raw_payload or {}).get("raw_payload") or {}).get("canonical")
        if snapshot and item.supplier_product_id:
            ImportRowReceipt.objects.get_or_create(
                source_item_id=item.id,
                defaults={
                    "source_session_id": upload.id,
                    "scope_type": "supplier",
                    "scope_id": upload.supplier_id,
                    "target_type": "supplier_product",
                    "target_id": item.supplier_product_id,
                    "parser_version": snapshot["parser_version"],
                    "snapshot": snapshot,
                },
            )
    now = timezone.now()
    supersede_product_ids = []
    new_price_rows = []
    sync_product_ids = []
    created_prices = 0
    for plan in batch.price_plan.values():
        if plan["final"] is None:
            continue
        price, currency, upload_item, comment = plan["final"]
        originals = plan["originals"]
        if len(originals) == 1 and originals[0].price == price and originals[0].currency == currency:
            continue
        if originals:
            supersede_product_ids.append(plan["product"].id)
        new_price_rows.append(
            SupplierProductPrice(
                supplier_product=plan["product"],
                upload_item=upload_item,
                price_type=PriceType.PRICE_LIST,
                price=price,
                currency=currency,
                status=PriceStatus.ACTIVE,
                valid_from=now,
                comment=comment,
            )
        )
        sync_product_ids.append(plan["product"].id)
        created_prices += 1

    if supersede_product_ids:
        SupplierProductPrice.objects.filter(
            supplier_product_id__in=supersede_product_ids,
            status=PriceStatus.ACTIVE,
            valid_to__isnull=True,
        ).update(status=PriceStatus.SUPERSEDED, valid_to=now)
    if new_price_rows:
        SupplierProductPrice.objects.bulk_create(new_price_rows, batch_size=500)
    # bulk_create doesn't fire SupplierProductPrice's post_save signal
    # (suppliers/signals.py) — set-based equivalent, one UPDATE for every
    # product touched instead of one per product.
    _bulk_sync_supplier_product_price_snapshots(sync_product_ids)

    if progress_callback:
        progress_callback(rows_done=products_total, rows_total=products_total)

    upload.ai_payload = _json_safe(data)
    upload.status = UploadStatus.NEEDS_REVIEW if batch.needs_review else UploadStatus.APPLIED
    upload.processed_at = timezone.now()
    upload.save(update_fields=["ai_payload", "status", "processed_at", "updated_at"])

    catalog_sync = _catalog_qdrant_sync_schedule_status()
    if catalog_sync["scheduled"]:
        supplier_id = upload.supplier_id
        scheduled_upload_id = upload.id
        transaction.on_commit(
            lambda: _enqueue_supplier_catalog_qdrant_sync(
                supplier_id=supplier_id,
                upload_id=scheduled_upload_id,
            )
        )

    items_total = len(data["products"])
    items_unchanged = items_total - batch.created_products - created_prices
    return {
        "upload_id": upload.id,
        "status": upload.status,
        "items_total": items_total,
        "items_created": batch.created_items,
        "products_created": batch.created_products,
        "products_updated": 0,
        "prices_created": created_prices,
        "items_matched": max(items_unchanged, 0),
        "needs_review": batch.needs_review,
        "catalog_sync": catalog_sync,
    }
