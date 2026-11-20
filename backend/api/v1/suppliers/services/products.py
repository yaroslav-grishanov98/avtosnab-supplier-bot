"""Supplier products: card, prices, archive, publish, images and bulk actions."""

from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.llm import LLMConnectorError
from api.v1.catalog.product_card_llm import suggest_product_card
from api.v1.catalog.selectors import find_candidate_catalog_products
from api.v1.suppliers.package_composition import validate_pack_fields
from api.v1.suppliers.storage import (
    persist_supplier_product_image,
)
from core.audit import record_audit_log
from core.models import (
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    Unit,
)
from suppliers.models import (
    PriceStatus,
    PriceType,
    ProductAvailability,
    Supplier,
    SupplierProduct,
    SupplierProductImage,
    SupplierProductPrice,
)
from suppliers.services import sync_supplier_product_price_snapshot

from ._shared import logger
from .product_refs import _next_system_sku_batch, _resolve_brand, _resolve_category_path


def _generate_system_sku():
    return _next_system_sku_batch(1)[0]


def _apply_price(*, supplier_product, price, currency="RUB", upload_item=None, comment=None, new_product=False):
    if price is None:
        return False

    # new_product: товар только что создан в этой же транзакции, активных цен у него быть не может -- блокирующий
    # SELECT не нужен (commit импорта вызывает это на каждой строке).
    active_prices = (
        []
        if new_product
        else list(
            SupplierProductPrice.objects.select_for_update()
            .filter(
                supplier_product=supplier_product,
                status=PriceStatus.ACTIVE,
                valid_to__isnull=True,
            )
            .order_by("-valid_from", "-created_at")
        )
    )
    if len(active_prices) == 1 and active_prices[0].price == price and active_prices[0].currency == currency:
        return False

    now = timezone.now()
    if active_prices:
        SupplierProductPrice.objects.filter(
            pk__in=[p.pk for p in active_prices],
        ).update(status=PriceStatus.SUPERSEDED, valid_to=now)
    # 2026-08-17: no explicit sync_supplier_product_price_snapshot() call
    # here — suppliers/signals.py's post_save receiver on SupplierProductPrice
    # already runs it for every .create()/.save(), this one included. It was
    # redundant (ran the same 3-query recompute twice per priced row) — real
    # cost on a large upload: apply_supplier_price_ai_result runs this once
    # per row, so 834 rows meant ~2500 wasted queries. The explicit calls
    # elsewhere in this file stay — they follow a bulk .update() (doesn't
    # fire the signal), a genuinely different case.
    new_price = SupplierProductPrice(
        supplier_product=supplier_product,
        upload_item=upload_item,
        price_type=PriceType.PRICE_LIST,
        price=price,
        currency=currency,
        status=PriceStatus.ACTIVE,
        valid_from=now,
        comment=comment or None,
    )
    if new_product:
        # A card created a moment ago has no other prices, and create_supplier_product already put this one into
        # current_price: bulk_create skips the post_save recompute (2 SELECTs + 1 UPDATE per card) that would only
        # arrive at the same snapshot.
        SupplierProductPrice.objects.bulk_create([new_price])
    else:
        new_price.save(force_insert=True)
    return True


def build_supplier_product(*, supplier, data, system_sku=None, supplier_sku=None):
    """An unsaved SupplierProduct made from the product data of create_supplier_product. The import creates cards in
    bulk and builds them with this, so both ways agree on every field. Raises the same pack-composition errors."""
    # 2026-08-28: brand_id (existing brand, picked from GET /suppliers/brands
    # autocomplete) takes priority over brand (free text, get_or_create) if
    # both are somehow present.
    brand = data["brand_id"] if "brand_id" in data else _resolve_brand(data.get("brand"))
    category = _resolve_category_path(data.get("category_path"))
    raw_name = data.get("supplier_raw_name") or data.get("supplier_normalized_name")
    normalized_name = data.get("supplier_normalized_name") or raw_name
    validate_pack_fields(
        count=data.get("pack_items_count"),
        value=data.get("pack_item_value"),
        unit=data.get("pack_item_unit"),
        pack_type=data.get("pack_type"),
    )
    return SupplierProduct(
        supplier=supplier,
        unit=data.get("unit"),
        brand=brand,
        category=category,
        system_sku=data.get("system_sku") or system_sku or _generate_system_sku(),
        supplier_sku=supplier_sku,
        supplier_name=raw_name,
        normalized_name=normalized_name,
        country_of_origin=data.get("country_of_origin") or None,
        net_value=data.get("net_value"),
        net_unit=data.get("net_unit"),
        min_order_qty=data.get("min_order_qty"),
        delivery_multiple=data.get("delivery_multiple"),
        pack_items_count=data.get("pack_items_count"),
        pack_item_value=data.get("pack_item_value"),
        pack_item_unit=data.get("pack_item_unit"),
        pack_type=data.get("pack_type"),
        availability=data.get("availability") or ProductAvailability.IN_STOCK,
        comment=data.get("comment") or None,
        package=data.get("package") or None,
        image_url=data.get("image_url") or None,
        # The first price of a new card is its current price and nothing else: set it here so the price snapshot does
        # not have to be recomputed with three queries per card (see _apply_price).
        **({"current_price": data["price"]} if data.get("price") is not None else {}),
        # An import creates cards as drafts straight away instead of creating and then saving them again.
        **({"status": data["status"]} if data.get("status") else {}),
    )


@transaction.atomic
def create_supplier_product(*, supplier_id, data, supplier=None):
    """2026-08-08: ручное добавление одного товара — раньше SupplierProduct
    можно было создать только через парсинг загруженного прайса
    (normalization.py). brand/category_path резолвятся теми же хелперами
    (_resolve_brand/_resolve_category_path), что и там, и что и в
    update_supplier_product — get_or_create по названию, без дублей.

    `supplier` -- уже загруженный объект того же поставщика (commit импорта вызывает функцию на каждой строке и
    не должен каждый раз перечитывать поставщика); без него поставщик ищется по supplier_id."""
    if supplier is None or supplier.pk != supplier_id:
        supplier = get_object_or_404(Supplier, pk=supplier_id)

    supplier_product = build_supplier_product(supplier=supplier, data=data)
    try:
        with transaction.atomic():
            supplier_product.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError({"supplier_raw_name": "Товар с таким названием уже есть у этого поставщика."}) from exc

    if "price" in data:
        _apply_price(
            supplier_product=supplier_product,
            price=data["price"],
            comment=data.get("price_comment"),
            new_product=True,
        )

    return supplier_product


def suggest_supplier_product_card(*, name, unit=None):
    """TASK-87/TASK-69 задача 6 (реанимирована 2026-09-25): рекомендация для
    карточки товара поставщика на общем LLM-ядре (api.llm + api.v1.catalog.
    product_card_llm) — только предлагает normalized_name/unit/кандидатов,
    ничего не создаёт и не сохраняет; вызывающая сторона (view) решает, что
    делать с рекомендацией. При выключенном SUPPLIER_PRODUCT_CARD_LLM_ENABLED
    или недоступности LLM (провайдер не ответил/невалидный ответ) отдаёт
    пустую рекомендацию с available=False — это необязательная подсказка, не
    часть основного флоу создания/редактирования товара, поэтому не 5xx."""
    unavailable = {
        "available": False,
        "normalized_name": name,
        "suggested_unit": None,
        "candidates": [],
    }
    if not bool(getattr(settings, "SUPPLIER_PRODUCT_CARD_LLM_ENABLED", True)):
        return unavailable

    allowed_units = list(Unit.objects.filter(status=EntityStatus.ACTIVE).order_by("kind", "code"))
    candidates = find_candidate_catalog_products(name=name)
    try:
        suggestion = suggest_product_card(
            name=name,
            unit_id=unit.id if unit else None,
            allowed_units=[{"id": u.id, "code": u.code, "name": u.name} for u in allowed_units],
            candidates=[{"id": c.id, "name": c.canonical_name, "unit_id": c.base_unit_id} for c in candidates],
        )
    except (LLMConnectorError, ValueError) as error:
        logger.warning("supplier_product_autofill.unavailable name=%s error_type=%s", name, error.__class__.__name__)
        return unavailable

    units_by_id = {u.id: u for u in allowed_units}
    candidates_by_id = {c.id: c for c in candidates}
    return {
        "available": True,
        "normalized_name": suggestion.normalized_name,
        "suggested_unit": units_by_id.get(suggestion.suggested_unit_id),
        "candidates": [
            candidates_by_id[candidate_id]
            for candidate_id in suggestion.candidate_product_ids
            if candidate_id in candidates_by_id
        ],
    }


def list_supplier_product_price_history(*, supplier_product_id):
    """2026-08-09: история изменения цен для Вити — каждое изменение цены
    (вручную через PATCH/create, или из парсинга прайса) уже отдельная
    строка SupplierProductPrice (status=superseded, а не апдейт на месте) —
    новых данных не нужно, только читаем существующую историю.

    2026-08-10: доведено под Figma "История изменения цены" (модалка в
    "Истории заказов") — переход "было → стало" с процентом изменения и
    поставщиком. previous_price/change_percent/supplier_name вычисляются
    здесь, а не хранятся — список уже отсортирован новые-сначала, поэтому
    "предыдущая" цена для записи N — это supplier_product.price записи
    N+1, без дополнительных запросов к БД."""
    supplier_product = get_object_or_404(SupplierProduct.objects.select_related("supplier"), pk=supplier_product_id)
    prices = list(
        SupplierProductPrice.objects.filter(supplier_product_id=supplier_product_id).order_by(
            "-valid_from", "-created_at"
        )
    )
    for index, price_row in enumerate(prices):
        price_row.supplier_name = supplier_product.supplier.display_name
        older = prices[index + 1] if index + 1 < len(prices) else None
        price_row.previous_price = older.price if older else None
        if older and older.price:
            price_row.change_percent = ((price_row.price - older.price) / older.price * 100).quantize(Decimal("0.01"))
        else:
            price_row.change_percent = None
    return prices


_SUPPLIER_PRODUCT_UPDATE_FIELD_MAPPING = {
    "supplier_raw_name": "supplier_name",
    "supplier_normalized_name": "normalized_name",
    "unit": "unit",
    "country_of_origin": "country_of_origin",
    "net_value": "net_value",
    "net_unit": "net_unit",
    "package": "package",
    "min_order_qty": "min_order_qty",
    "delivery_multiple": "delivery_multiple",
    "pack_items_count": "pack_items_count",
    "pack_item_value": "pack_item_value",
    "pack_item_unit": "pack_item_unit",
    "pack_type": "pack_type",
    "status": "status",
    "comment": "comment",
    "availability": "availability",
}


@transaction.atomic
def update_supplier_product(*, supplier_product_id, data):
    supplier_product = get_object_or_404(
        SupplierProduct.objects.select_for_update(),
        pk=supplier_product_id,
    )

    product_fields = []
    for input_field, model_field in _SUPPLIER_PRODUCT_UPDATE_FIELD_MAPPING.items():
        if input_field not in data:
            continue

        setattr(supplier_product, model_field, data[input_field])
        product_fields.append(model_field)

    if any(field in data for field in ("pack_items_count", "pack_item_value", "pack_item_unit", "pack_type")):
        validate_pack_fields(
            count=supplier_product.pack_items_count,
            value=supplier_product.pack_item_value,
            unit=supplier_product.pack_item_unit,
            pack_type=supplier_product.pack_type,
        )

    if "image_url" in data:
        supplier_product.image_url = data["image_url"] or None
        product_fields.append("image_url")
        # Fix 3 (2026-09-21 final-review fix wave): keep the gallery's
        # position=0 row in sync — see _sync_gallery_main_photo's docstring.
        _sync_gallery_main_photo(supplier_product, supplier_product.image_url)

    if "short_description" in data:
        supplier_product.short_description = data["short_description"] or None
        if "short_description" not in product_fields:
            product_fields.append("short_description")

    if "brand_id" in data:
        supplier_product.brand = data["brand_id"]
        product_fields.append("brand")
    elif "brand" in data:
        supplier_product.brand = _resolve_brand(data["brand"])
        product_fields.append("brand")

    if "category_path" in data:
        category = _resolve_category_path(data["category_path"])
        if category:
            supplier_product.category = category
            product_fields.append("category")

    if "price" in data:
        _apply_price(
            supplier_product=supplier_product,
            price=data["price"],
            comment=data.get("price_comment"),
        )

        if "updated_at" not in product_fields:
            product_fields.append("updated_at")

    if product_fields:
        if "updated_at" not in product_fields:
            product_fields.append("updated_at")
        supplier_product.save(update_fields=product_fields)

    return supplier_product


@transaction.atomic
def archive_supplier_product(*, supplier_product_id, actor=None):
    supplier_product = get_object_or_404(
        SupplierProduct.objects.select_for_update(),
        pk=supplier_product_id,
    )

    if supplier_product.status != EntityStatus.ARCHIVED:
        supplier_product.status = EntityStatus.ARCHIVED
        supplier_product.save(update_fields=["status", "updated_at"])

    now = timezone.now()
    SupplierProductPrice.objects.select_for_update().filter(
        supplier_product=supplier_product,
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    ).update(status=PriceStatus.CANCELLED, valid_to=now)
    sync_supplier_product_price_snapshot(supplier_product.id)

    record_audit_log(
        actor=actor,
        action=AuditLogAction.ARCHIVED,
        entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
        entity_id=supplier_product.id,
        summary=supplier_product.normalized_name,
        scope_id=supplier_product.supplier_id,
    )
    return supplier_product


@transaction.atomic
def bulk_archive_supplier_products(*, supplier_product_ids):
    """2026-08-12: Витя — archive-all/bulk-delete "вообще не пашет" на 200+
    товарах, ERR_CONNECTION_CLOSED. Причина: раньше это был Python-цикл по
    archive_supplier_product() — каждый вызов делает ~4 последовательных
    запроса (select_for_update + save + price-cancel + 3-query snapshot
    sync), на 200 id это ~800+ запросов внутри одной транзакции, за секунды
    упирается в таймаут гейтвея. Переписано на set-based SQL — 3 запроса
    суммарно, независимо от N: (1) погасить активные цены сразу для всех id,
    (2) обнулить current_price сразу для всех id (после (1) активной цены
    ни у кого из них больше нет — то же самое, что делал sync per-item, но
    без переоткрытия per-product previous_price/price_changed_at, которые
    архивация не трогает и без разницы не нужно пересчитывать), (3) перевести
    в archived только те, что были active (не трогать updated_at у уже
    архивированных). Существование всех id проверяется одним COUNT-запросом
    (не N) — та же причина, что у bulk_delete_supplier_products: для
    service-token вызовов нет ownership-скоупинга на уровне view, раньше
    404 на несуществующий id ловил per-item get_object_or_404."""
    if not supplier_product_ids:
        return
    existing_count = SupplierProduct.objects.filter(pk__in=supplier_product_ids).count()
    if existing_count != len(set(supplier_product_ids)):
        raise Http404
    now = timezone.now()
    SupplierProductPrice.objects.filter(
        supplier_product_id__in=supplier_product_ids,
        status=PriceStatus.ACTIVE,
        valid_to__isnull=True,
    ).update(status=PriceStatus.CANCELLED, valid_to=now)
    SupplierProduct.objects.filter(pk__in=supplier_product_ids).update(current_price=None)
    SupplierProduct.objects.filter(pk__in=supplier_product_ids, status=EntityStatus.ACTIVE).update(
        status=EntityStatus.ARCHIVED, updated_at=now
    )


@transaction.atomic
def delete_supplier_product(*, supplier_product_id, actor=None):
    """2026-08-11: Витя — DELETE не должен архивировать, а реально удалять.

    2026-09-23: то решение отменено (backlog#task-69, ТЗ «Карточка товара
    поставщика» §32 требует, чтобы удаление сохраняло историю цен/заявок).
    DELETE снова полностью совпадает с archive_supplier_product() — товар не
    удаляется физически, переводится в ARCHIVED, POST .../restore его
    возвращает. FK на SupplierProductPrice/SupplierProductVenuePrice/
    VenueSupplierProduct/WishlistItem обратно RESTRICT (suppliers/0044,
    catalog/0029 — были CASCADE), поэтому больше не нужно и компенсирующее
    проставление supplier_product_deleted на VenueProductMatrixItem/
    VenueCartItem/OrderItem/OrderTemplateItem/ReceivingDocument — их FK
    больше не обнуляется, строка товара никуда не девается."""
    archive_supplier_product(supplier_product_id=supplier_product_id, actor=actor)


@transaction.atomic
def bulk_delete_supplier_products(*, supplier_product_ids):
    """2026-08-12: тот же перф-фикс, что и bulk_archive_supplier_products —
    раньше Python-цикл по delete_supplier_product() (get_object_or_404 +
    .delete() каждый — N запросов минимум), теперь один bulk-вызов.

    2026-09-23: делегирует в bulk_archive_supplier_products (backlog#task-69,
    та же отмена решения от 2026-08-11, что и у delete_supplier_product) —
    та же set-based реализация, включая её собственную проверку
    существования всех id одним COUNT-запросом."""
    bulk_archive_supplier_products(supplier_product_ids=supplier_product_ids)


@transaction.atomic
def restore_supplier_product(*, supplier_product_id):
    """Undo archive_supplier_product() (2026-08-04). By explicit product
    decision, restoring un-cancels the price archive cancelled too — the
    product wouldn't be sellable again without one, and re-uploading it
    from scratch would be a worse UX than just reverting the archive's own
    side effect."""
    supplier_product = get_object_or_404(
        SupplierProduct.objects.select_for_update(),
        pk=supplier_product_id,
    )

    if supplier_product.status == EntityStatus.ARCHIVED:
        supplier_product.status = EntityStatus.ACTIVE
        supplier_product.save(update_fields=["status", "updated_at"])

    already_active = SupplierProductPrice.objects.filter(
        supplier_product=supplier_product,
        price_type=PriceType.PRICE_LIST,
        status=PriceStatus.ACTIVE,
    ).exists()
    if not already_active:
        latest_cancelled_price = (
            SupplierProductPrice.objects.select_for_update()
            .filter(
                supplier_product=supplier_product,
                price_type=PriceType.PRICE_LIST,
                status=PriceStatus.CANCELLED,
            )
            .order_by("-valid_to", "-created_at")
            .first()
        )
        if latest_cancelled_price is not None:
            latest_cancelled_price.status = PriceStatus.ACTIVE
            latest_cancelled_price.valid_to = None
            latest_cancelled_price.save(update_fields=["status", "valid_to", "updated_at"])
    sync_supplier_product_price_snapshot(supplier_product.id)

    return supplier_product


def publish_supplier_product(*, supplier_product_id, actor=None):
    """Переводит draft -> active после ручной проверки, отдельно от commit
    (2026-09-29, unified-import-architecture spec §9.8, подтверждено Павлом --
    commit больше не публикует товар сам). Идемпотентно: повторный вызов на
    уже active товаре ничего не меняет и не пишет второй audit log (как и
    archive/restore)."""
    supplier_product = get_object_or_404(
        SupplierProduct.objects.select_for_update(),
        pk=supplier_product_id,
    )

    if supplier_product.status == EntityStatus.DRAFT:
        supplier_product.status = EntityStatus.ACTIVE
        supplier_product.save(update_fields=["status", "updated_at"])
        record_audit_log(
            actor=actor,
            action=AuditLogAction.PUBLISHED,
            entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
            entity_id=supplier_product.id,
            summary=supplier_product.normalized_name,
            scope_id=supplier_product.supplier_id,
        )

    return supplier_product


def bulk_publish_supplier_products(*, supplier_product_ids):
    """Bulk-версия publish_supplier_product, тот же перф-паттерн, что
    bulk_archive_supplier_products (2026-08-12): один set-based UPDATE вместо
    Python-цикла. Без per-id audit log -- тот же осознанный компромисс, что и
    у bulk-archive/bulk-restore."""
    if not supplier_product_ids:
        return
    existing_count = SupplierProduct.objects.filter(pk__in=supplier_product_ids).count()
    if existing_count != len(set(supplier_product_ids)):
        raise Http404
    SupplierProduct.objects.filter(pk__in=supplier_product_ids, status=EntityStatus.DRAFT).update(
        status=EntityStatus.ACTIVE, updated_at=timezone.now()
    )


@transaction.atomic
def bulk_restore_supplier_products(*, supplier_product_ids):
    """2026-08-15: Витя — restore-all/bulk-restore на 380 товарах не
    укладывается в таймаут гейтвея (ERR_CONNECTION_TIMED_OUT после ~2 мин),
    хотя bulk-archive на том же объёме — 1.67с. Причина буквально та же, что
    уже нашли и починили для bulk_archive_supplier_products/
    bulk_delete_supplier_products 2026-08-12 (см. их докстринги) — тот фикс
    просто не докатили до restore: это был Python-цикл по
    restore_supplier_product(), а она сама по себе делает ~5-6
    последовательных запросов (select_for_update + save + exists-проверка +
    возможный select_for_update/save отменённой цены + 3-запросный
    sync_supplier_product_price_snapshot) — на 380 id это 1900+ запросов
    внутри одной транзакции.

    Переписано на set-based SQL — фиксированное число запросов независимо от
    N: (1) COUNT для проверки существования всех id (как у archive/delete),
    (2) один UPDATE, реактивирующий именно последнюю (по valid_to/created_at)
    отменённую price-list цену каждого товара через коррелированный
    подзапрос — тот же порядок сортировки, что был в
    restore_supplier_product, пропускает товары, у которых уже есть активная
    цена, (3) один UPDATE current_price/previous_price/price_changed_at через
    коррелированные подзапросы — тот же расчёт, что делает
    sync_supplier_product_price_snapshot() по одному товару за раз, но одним
    SQL-запросом на все id сразу, (4) перевести в active только те, что были
    archived (не трогать updated_at у уже активных)."""
    if not supplier_product_ids:
        return
    existing_count = SupplierProduct.objects.filter(pk__in=supplier_product_ids).count()
    if existing_count != len(set(supplier_product_ids)):
        raise Http404

    already_active_product_ids = SupplierProductPrice.objects.filter(
        supplier_product_id__in=supplier_product_ids,
        price_type=PriceType.PRICE_LIST,
        status=PriceStatus.ACTIVE,
    ).values("supplier_product_id")
    latest_cancelled_price_ids = list(
        SupplierProductPrice.objects.filter(
            supplier_product_id__in=supplier_product_ids,
            price_type=PriceType.PRICE_LIST,
            status=PriceStatus.CANCELLED,
        )
        .exclude(supplier_product_id__in=already_active_product_ids)
        .filter(
            id=Subquery(
                SupplierProductPrice.objects.filter(
                    supplier_product_id=OuterRef("supplier_product_id"),
                    price_type=PriceType.PRICE_LIST,
                    status=PriceStatus.CANCELLED,
                )
                .order_by("-valid_to", "-created_at")
                .values("id")[:1]
            )
        )
        .values_list("id", flat=True)
    )
    SupplierProductPrice.objects.filter(id__in=latest_cancelled_price_ids).update(
        status=PriceStatus.ACTIVE, valid_to=None
    )

    previous_price_subquery = SupplierProductPrice.objects.filter(
        supplier_product_id=OuterRef("pk"),
        price_type=PriceType.PRICE_LIST,
        status=PriceStatus.SUPERSEDED,
    ).order_by("-valid_to", "-created_at")
    SupplierProduct.objects.filter(pk__in=supplier_product_ids).update(
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

    SupplierProduct.objects.filter(pk__in=supplier_product_ids, status=EntityStatus.ARCHIVED).update(
        status=EntityStatus.ACTIVE, updated_at=timezone.now()
    )


@transaction.atomic
def bulk_update_supplier_products(*, supplier_product_ids, data):
    """2026-08-27 — Витя спросил, не нагрузит ли систему массовое изменение
    бренда/страны (добавлены 2026-08-27 для мультивыбора). Оказалось да: это
    был Python-цикл по update_supplier_product() — тот же класс проблемы,
    что уже чинили для archive/delete/restore (2026-08-12/15, см. их
    докстрайны выше). Измерено: 20 товаров с одним и тем же новым брендом —
    105 запросов (SELECT FOR UPDATE + get_or_create бренда + UPDATE на
    каждый товар, хотя бренд у всех один и тот же). Переписано на
    set-based SQL: brand/category резолвятся ОДИН раз (не на каждый id),
    остальные плоские поля — один общий UPDATE. price остаётся построчным
    через update_supplier_product — ему нужна отдельная запись истории на
    каждый товар (_apply_price), одним UPDATE это не сделать."""
    if not supplier_product_ids:
        return []
    if "price" in data:
        return [update_supplier_product(supplier_product_id=pid, data=data) for pid in supplier_product_ids]

    existing_count = SupplierProduct.objects.filter(pk__in=supplier_product_ids).count()
    if existing_count != len(set(supplier_product_ids)):
        raise Http404

    update_kwargs = {}
    for input_field, model_field in _SUPPLIER_PRODUCT_UPDATE_FIELD_MAPPING.items():
        if input_field in data:
            update_kwargs[model_field] = data[input_field]
    if "image_url" in data:
        update_kwargs["image_url"] = data["image_url"] or None
    if "brand_id" in data:
        update_kwargs["brand"] = data["brand_id"]
    elif "brand" in data:
        update_kwargs["brand"] = _resolve_brand(data["brand"])
    if "category_path" in data:
        category = _resolve_category_path(data["category_path"])
        if category:
            update_kwargs["category"] = category

    if update_kwargs:
        update_kwargs["updated_at"] = timezone.now()
        SupplierProduct.objects.filter(pk__in=supplier_product_ids).update(**update_kwargs)

    return list(SupplierProduct.objects.filter(pk__in=supplier_product_ids))


def update_all_supplier_products(*, supplier_id, data):
    """2026-08-27, Vitya — bulk-actions таблицы товаров умеют выбрать "все
    товары" галочкой, без явного списка id. Scope — только active, как у
    archive_all_supplier_products() (архивные обычно не видны в основной
    таблице, откуда вызывается bulk-actions)."""
    get_object_or_404(Supplier, pk=supplier_id)
    ids = list(
        SupplierProduct.objects.filter(supplier_id=supplier_id, status=EntityStatus.ACTIVE).values_list("pk", flat=True)
    )
    bulk_update_supplier_products(supplier_product_ids=ids, data=data)
    return len(ids)


@transaction.atomic
def archive_all_supplier_products(*, supplier_id):
    """2026-08-04: "Мои компании" — Витя нужна ручка archiveAll для товаров
    компании прямо со списка компаний, без захода в таблицу товаров и ручного
    мультивыбора. Архивирует все ещё активные товары поставщика; уже
    архивированные не трогает (bulk_archive_supplier_products по каждому id
    всё равно идемпотентен, но так меньше лишних UPDATE)."""
    get_object_or_404(Supplier, pk=supplier_id)
    ids = list(
        SupplierProduct.objects.filter(supplier_id=supplier_id, status=EntityStatus.ACTIVE).values_list("pk", flat=True)
    )
    bulk_archive_supplier_products(supplier_product_ids=ids)
    return len(ids)


@transaction.atomic
def delete_all_supplier_products(*, supplier_id):
    """DELETE-all equivalent of archive_all_supplier_products().

    2026-09-23: DELETE больше не hard delete (backlog#task-69) — делегирует
    в archive_all_supplier_products() и наследует его семантику: трогает
    только ещё активные товары, уже архивированные не переоткрывает
    (в отличие от прежней версии, которая до 2026-09-23 намеренно проходила
    по всем — active и archived — раз реально удаляла строку; теперь это
    больше не нужно, разницы между «удалить» и «архивировать» для уже
    архивированного товара нет)."""
    return archive_all_supplier_products(supplier_id=supplier_id)


@transaction.atomic
def restore_all_supplier_products(*, supplier_id):
    """Undo archive_all_supplier_products() (2026-08-04) — restores every
    still-archived product of a supplier back to active, price included
    (see restore_supplier_product())."""
    get_object_or_404(Supplier, pk=supplier_id)
    ids = list(
        SupplierProduct.objects.filter(supplier_id=supplier_id, status=EntityStatus.ARCHIVED).values_list(
            "pk", flat=True
        )
    )
    bulk_restore_supplier_products(supplier_product_ids=ids)
    return len(ids)


def _sync_gallery_main_photo(supplier_product, image_url):
    """2026-09-21 final-review fix wave (Fix 3): Task 5's gallery invariant
    is "a non-empty SupplierProduct.image_url has a matching
    SupplierProductImage row at position=0" (Task 5.1's backfill migration
    established it once, Task 5.2's gallery CRUD — add/reorder/delete —
    keeps it going forward). upload_supplier_product_image and
    update_supplier_product's image_url branch predate the gallery and
    write image_url directly, with zero gallery rows — the next gallery
    upload then sees images.count() == 0 and claims position=0 for itself,
    silently discarding the legacy photo. Upserting the position=0 row here
    keeps both legacy paths honoring the same invariant, without touching
    positions 1+."""
    if not image_url:
        return
    main_image = supplier_product.images.filter(position=0).first()
    if main_image is None:
        SupplierProductImage.objects.create(supplier_product=supplier_product, image_url=image_url, position=0)
    elif main_image.image_url != image_url:
        main_image.image_url = image_url
        main_image.save(update_fields=["image_url", "updated_at"])


@transaction.atomic
def upload_supplier_product_image(*, supplier_product_id, image_file):
    """Same reasoning as upload_supplier_logo (2026-07-28) — file uploads and
    saves in one call, no separate PATCH .../products/{id} with image_url
    needed after (2026-08-03: Витя — таблица товаров нуждалась в реальной
    загрузке картинки файлом, не только вставке готовой ссылки).

    @transaction.atomic is required, not decorative: select_for_update()
    raises TransactionManagementError outside a transaction on Postgres —
    caught only by a live runserver run against real Postgres, not by the
    unit tests, since Django's TestCase already wraps every test method in
    an outer atomic block, incidentally masking the missing wrapper here.

    2026-09-13: галерея вместо одной картинки (та же причина, что у
    upload_supplier_logo) — каждый вызов добавляет фото, не заменяет.

    2026-09-21 (§4 ТЗ): explicit `position` instead of relying on
    `created_at` ordering — needed so the gallery can be reordered later
    (see reorder_supplier_product_image). select_for_update() on the parent
    also serializes concurrent uploads so `next_position` can't race (two
    uploads landing on the same position)."""
    supplier_product = get_object_or_404(SupplierProduct.objects.select_for_update(), pk=supplier_product_id)
    image_url = persist_supplier_product_image(image_file=image_file, supplier_product_id=supplier_product.pk)
    next_position = supplier_product.images.count()
    image = SupplierProductImage.objects.create(
        supplier_product=supplier_product, image_url=image_url, position=next_position
    )
    if next_position == 0:
        supplier_product.image_url = image_url
        supplier_product.save(update_fields=["image_url", "updated_at"])
    return image


@transaction.atomic
def reorder_supplier_product_image(*, supplier_product_id, ordered_image_ids):
    """2026-09-21 (§4 ТЗ): пользователь может изменить порядок фотографий,
    после чего первое становится главным — эндпоинта на это не было (галерея
    из 2026-09-13 упорядочивала только по created_at). ordered_image_ids
    must be exactly the product's current image ids (no subset, no foreign
    ids) — first element becomes position=0, i.e. the main photo, and
    SupplierProduct.image_url is kept in sync with it."""
    supplier_product = get_object_or_404(SupplierProduct.objects.select_for_update(), pk=supplier_product_id)
    images_by_id = {img.id: img for img in supplier_product.images.select_for_update()}
    if set(ordered_image_ids) != set(images_by_id.keys()):
        raise ValidationError({"ordered_image_ids": "Список должен содержать ровно все фото товара, без пропусков."})

    for position, image_id in enumerate(ordered_image_ids):
        image = images_by_id[image_id]
        if image.position != position:
            image.position = position
            image.save(update_fields=["position", "updated_at"])

    main_image = images_by_id[ordered_image_ids[0]]
    if supplier_product.image_url != main_image.image_url:
        supplier_product.image_url = main_image.image_url
        supplier_product.save(update_fields=["image_url", "updated_at"])


@transaction.atomic
def delete_supplier_product_image(*, supplier_product_id, image_id):
    """2026-09-21 (§4 ТЗ): deleting now also compacts the remaining images'
    positions (no gaps), not just resyncs the cover URL — needed so
    reordering stays well-defined after a deletion."""
    supplier_product = get_object_or_404(SupplierProduct.objects.select_for_update(), pk=supplier_product_id)
    image = get_object_or_404(SupplierProductImage, pk=image_id, supplier_product_id=supplier_product_id)
    image.delete()

    remaining = list(supplier_product.images.select_for_update().order_by("position"))
    for position, remaining_image in enumerate(remaining):
        if remaining_image.position != position:
            remaining_image.position = position
            remaining_image.save(update_fields=["position", "updated_at"])

    new_cover_url = remaining[0].image_url if remaining else None
    if supplier_product.image_url != new_cover_url:
        supplier_product.image_url = new_cover_url
        supplier_product.save(update_fields=["image_url", "updated_at"])
