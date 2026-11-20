"""ТЗ §14/§15/§16-18/§23 REVIEW+COMMIT stages: per-item field edits, per-item
action changes, bulk availability edit, final commit. Deliberately separate
from product_import_selectors.py (read-side) -- this module only ever
mutates."""

import json
import logging
import time
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from api.v1.suppliers.bot_events import notify_linked_supplier_chats
from api.v1.suppliers.package_composition import validate_pack_fields
from api.v1.suppliers.services import (
    _next_system_sku_batch,
    build_supplier_product,
    create_supplier_product,
    update_supplier_product,
)
from core.audit import record_audit_log
from core.models import AuditLogAction, AuditLogEntityType, Unit, User
from file_imports.errors import import_error_messages
from file_imports.quality import NUMERIC_NAME_MESSAGE, is_numeric_name
from suppliers.models import (
    EntityStatus,
    PriceStatus,
    PriceType,
    Supplier,
    SupplierProduct,
    SupplierProductImportAction,
    SupplierProductImportCommitStatus,
    SupplierProductImportItem,
    SupplierProductImportSession,
    SupplierProductImportSessionStatus,
    SupplierProductPrice,
)

from .product_import_inherit import drop_inherited_fields, inherit_target_fields, inherited_fields_for_write
from .product_import_matching import find_supplier_product_import_candidates, hidden_sku_holder_message

logger = logging.getLogger("autosnab.supplier_import")

_EDITABLE_FIELDS = {
    "name",
    "country_of_origin",
    "unit_id",
    "price",
    "delivery_multiple",
    "net_value",
    "net_unit_id",
    "min_order_qty",
    "availability",
    "supplier_sku",
    "pack_items_count",
    "pack_item_value",
    "pack_item_unit",
    "pack_type",
}
_PACK_FIELDS = ("pack_items_count", "pack_item_value", "pack_item_unit", "pack_type")
# ТЗ §14: only name/unit changes invalidate an existing match.
_REMATCH_TRIGGER_FIELDS = {"name", "unit_id"}
# Patched model field -> key in item.field_sources. Enrichment writes the unit keys as "unit"/"net_unit"
# (documented in the session serializer), so a manual edit must overwrite that same key, otherwise the
# "Предложено" marker (ТЗ §12) never disappears after the user changes the LLM-suggested unit.
_SOURCE_KEYS = {
    "name": "name",
    "country_of_origin": "country_of_origin",
    "unit_id": "unit",
    "net_value": "net_value",
    "net_unit_id": "net_unit",
}


def _lock_item(item):
    session = SupplierProductImportSession.objects.select_for_update().filter(pk=item.session_id).first()
    if session is None:
        raise NotFound("Сессия импорта уже удалена.")
    locked = (
        session.items.select_for_update(of=("self",))
        .select_related("unit", "net_unit", "pack_item_unit", "pack_type", "target_supplier_product")
        .filter(pk=item.pk)
        .first()
    )
    if locked is None:
        raise NotFound("Строка уже обработана или удалена.")
    return locked


# Sale units for which the net quantity of one unit is one of that unit (mirrors the parser's "price-per-unit" rule).
_ONE_PER_UNIT_CODES = ("кг", "л", "шт")


def _net_is_derived_from_sale_unit(item):
    """True when the net quantity was only inferred from the sale unit (parser rule "price-per-unit")."""
    if {item.field_sources.get("net_value"), item.field_sources.get("net_unit")} & {"user", "llm"}:
        return False
    if item.field_sources.get("net_value") == item.field_sources.get("net_unit") == "rule" and item.net_value == 1:
        return True  # set by the bulk unit choice (see _derive_one_unit_net)
    fields = (item.parsed_snapshot or {}).get("fields", {})
    for key in ("net_value", "net_unit_label"):
        candidates = fields.get(key, {}).get("candidates") or []
        if not candidates or any(candidate.get("method") != "price-per-unit" for candidate in candidates):
            return False
    return True


def _recompute_derived_net(item, data):
    """After the sale unit changed, redo a net quantity that was derived from the old sale unit.

    Returns the snapshot overrides for the recomputed net fields, or None when nothing was recomputed. A net the user
    set, one the file or the LLM provided, or one given in the same request is never touched."""
    if "unit_id" not in data or "net_value" in data or "net_unit_id" in data:
        return None
    if not _net_is_derived_from_sale_unit(item):
        return None
    unit = Unit.objects.filter(pk=item.unit_id).first() if item.unit_id else None
    if unit is not None and unit.code in _ONE_PER_UNIT_CODES:
        item.net_value = Decimal("1")
        item.net_unit_id = unit.pk
        return {"net_value": "1", "net_unit_label": unit.code}
    item.net_value = None
    item.net_unit_id = None
    return {"net_value": None, "net_unit_label": None}


def _override_value(key, value):
    """Snapshot override for a patched field. Unit references are stored as the dictionary code, not as an id."""
    if value is None:
        return None
    if key in ("unit_id", "net_unit_id"):
        code = Unit.objects.filter(pk=value).values_list("code", flat=True).first()
        return code if code is not None else str(value)
    return str(value)


@transaction.atomic
def patch_supplier_product_import_item(*, item, data, actor_id=None):
    item = _lock_item(item)
    if item.imported_at is not None:
        raise ValidationError({"detail": "Уже импортированную строку нельзя изменить."})
    changed_fields = []
    field_sources = dict(item.field_sources)
    triggers_rematch = False

    for field, value in data.items():
        if field not in _EDITABLE_FIELDS:
            continue
        setattr(item, field, value)
        changed_fields.append(field)
        field_sources[_SOURCE_KEYS.get(field, field)] = "user"
        if field in _REMATCH_TRIGGER_FIELDS:
            triggers_rematch = True

    if any(field in data for field in _PACK_FIELDS):
        validate_pack_fields(
            count=item.pack_items_count, value=item.pack_item_value, unit=item.pack_item_unit, pack_type=item.pack_type
        )

    if triggers_rematch:
        item.target_supplier_product = None
        item.target_expected_updated_at = None
        item.action = SupplierProductImportAction.REVIEW
        # The row is no longer a replace of that card: its copied fields go, unless this very edit set them.
        for inherited in inherited_fields_for_write(item):
            if inherited not in data:
                setattr(item, inherited, None)
                field_sources.pop(inherited, None)
                changed_fields.append(inherited)
        candidates = find_supplier_product_import_candidates(
            supplier_id=item.session.supplier_id, names=[item.name]
        ).get(0, [])
        item.match_candidates = [
            {
                "id": str(c["id"]),
                "name": c["name"],
                "unit_id": str(c["unit_id"]) if c["unit_id"] else None,
                "unit_name": c["unit_name"],
                "score": c["score"],
            }
            for c in candidates
        ]
        if not candidates:
            item.action = SupplierProductImportAction.NEW
        changed_fields += ["target_supplier_product", "target_expected_updated_at", "action", "match_candidates"]

    derived_net = _recompute_derived_net(item, data)
    if derived_net is not None:
        changed_fields += ["net_value", "net_unit"]

    if item.parsed_snapshot:
        from file_imports.canonical import override_snapshot

        canonical_keys = {
            "unit_id": "unit_label",
            "net_unit_id": "net_unit_label",
            "pack_item_unit": "pack_item_unit_label",
            "pack_type": "pack_type_label",
            "availability": "availability_label",
        }
        updates = {
            canonical_keys.get(key, key): _override_value(key, value)
            for key, value in data.items()
            if key in _EDITABLE_FIELDS
        }
        updates.update(derived_net or {})
        item.parsed_snapshot = override_snapshot(
            item.parsed_snapshot, updates, at=timezone.now().isoformat(), actor_id=actor_id
        )
        changed_fields.append("parsed_snapshot")
    item.validation_errors = []
    changed_fields.append("validation_errors")
    item.field_sources = field_sources
    changed_fields.append("field_sources")
    item.save(update_fields=list(dict.fromkeys(changed_fields)) + ["updated_at"])
    return item


@transaction.atomic
def set_supplier_product_import_item_action(*, item, action, target_supplier_product_id=None, actor_id=None):
    item = _lock_item(item)
    if item.imported_at is not None:
        raise ValidationError({"detail": "Уже импортированную строку нельзя изменить."})
    if action == SupplierProductImportAction.REPLACE:
        target = SupplierProduct.objects.filter(
            pk=target_supplier_product_id,
            supplier_id=item.session.supplier_id,
            status__in=[EntityStatus.ACTIVE, EntityStatus.DRAFT],
        ).first()
        if target is None:
            raise DjangoValidationError("action=replace требует активный товар или черновик текущего поставщика.")
        item.target_supplier_product = target
        item.target_expected_updated_at = target.updated_at
        drop_inherited_fields(item)
        inherit_target_fields(item, target)
    else:
        item.target_supplier_product = None
        item.target_expected_updated_at = None
        drop_inherited_fields(item)

    item.action = action
    item.validation_errors = []
    if action in (SupplierProductImportAction.NEW, SupplierProductImportAction.REPLACE) and item.parsed_snapshot:
        from file_imports.canonical import accept_issues

        item.parsed_snapshot = accept_issues(item.parsed_snapshot, at=timezone.now().isoformat(), actor_id=actor_id)
    item.save(
        update_fields=[
            "action",
            "target_supplier_product",
            "target_expected_updated_at",
            "validation_errors",
            "parsed_snapshot",
            "country_of_origin",
            "min_order_qty",
            "delivery_multiple",
            "availability",
            "field_sources",
            "updated_at",
        ]
    )
    return item


@transaction.atomic
def delete_supplier_product_import_session(*, session):
    """Убрать сессию импорта вместе со строками (кнопка «Удалить» в списке незавершённых). Нельзя, пока идёт разбор
    или применение: воркер держит сессию. Применённые ранее товары остаются в каталоге."""
    from suppliers.models import SupplierProductImportCommitStatus, SupplierProductImportProcessingStatus

    locked = SupplierProductImportSession.objects.select_for_update().filter(pk=session.pk).first()
    if locked is None:
        raise NotFound("Сессия импорта уже удалена.")
    if locked.processing_status in (
        SupplierProductImportProcessingStatus.PENDING,
        SupplierProductImportProcessingStatus.PROCESSING,
    ):
        raise ValidationError({"detail": "Импорт ещё обрабатывается."})
    if locked.commit_status in (SupplierProductImportCommitStatus.PENDING, SupplierProductImportCommitStatus.RUNNING):
        raise ValidationError({"detail": "Идёт применение импорта."})
    locked.delete()


@transaction.atomic
def bulk_set_supplier_product_import_items_availability(*, session, item_ids, availability):
    session = SupplierProductImportSession.objects.select_for_update().filter(pk=session.pk).first()
    if session is None:
        raise NotFound("Сессия импорта уже удалена.")
    items = list(
        session.items.filter(pk__in=item_ids, imported_at__isnull=True)
        .exclude(action=SupplierProductImportAction.SKIP)
        .only("id", "validation_errors", "parsed_snapshot", "field_sources")
    )
    for item in items:
        item.availability = availability
        item.field_sources = dict(item.field_sources) | {"availability": "user"}
        if item.parsed_snapshot:
            from file_imports.canonical import override_snapshot

            item.parsed_snapshot = override_snapshot(
                item.parsed_snapshot, {"availability_label": availability}, at=timezone.now().isoformat()
            )
        item.validation_errors = [
            message for message in item.validation_errors if message != "Не заполнено поле availability."
        ]
    session.items.model.objects.bulk_update(
        items, ["availability", "validation_errors", "field_sources", "parsed_snapshot"], batch_size=500
    )
    return len(items)


# Model field -> key in the canonical snapshot overrides (availability is stored as a label there).
_DEFAULTS_CANONICAL_KEYS = {
    "country_of_origin": "country_of_origin",
    "min_order_qty": "min_order_qty",
    "delivery_multiple": "delivery_multiple",
    "availability": "availability_label",
}


@transaction.atomic
def apply_supplier_product_import_defaults(*, session, values, actor_id=None) -> int:
    """Fill the given mandatory fields for every open row where they are still empty (None or "").

    Skips rows with action=skip and rows already imported; never overwrites a value. Returns the number of rows
    where at least one field actually changed."""
    from file_imports.canonical import override_snapshot

    fields = [field for field in _DEFAULTS_CANONICAL_KEYS if values.get(field) not in (None, "")]
    unit = values.get("unit")
    session = SupplierProductImportSession.objects.select_for_update().filter(pk=session.pk).first()
    if session is None:
        raise NotFound("Сессия импорта уже удалена.")
    if not fields and unit is None:
        return 0
    items = (
        session.items.filter(imported_at__isnull=True)
        .exclude(action=SupplierProductImportAction.SKIP)
        .only("id", "validation_errors", "parsed_snapshot", "field_sources", "unit", "net_value", "net_unit", *fields)
    )
    now = timezone.now().isoformat()
    changed = []
    for item in items:
        filled = [field for field in fields if getattr(item, field) in (None, "")]
        unit_filled = unit is not None and item.unit_id is None
        if not filled and not unit_filled:
            continue
        for field in filled:
            setattr(item, field, values[field])
        sources = dict(item.field_sources) | {field: "user" for field in filled}
        overrides = {_DEFAULTS_CANONICAL_KEYS[field]: values[field] for field in filled}
        removed = {f"Не заполнено поле {field}." for field in filled}
        if unit_filled:
            item.unit = unit
            sources["unit"] = "user"
            overrides["unit_label"] = unit.code
            removed.add("Не заполнено поле unit_id.")
            if _derive_one_unit_net(item, unit):
                sources |= {"net_value": "rule", "net_unit": "rule"}
                overrides |= {"net_value": "1", "net_unit_label": unit.code}
                removed |= {"Не заполнено поле net_value.", "Не заполнено поле net_unit_id."}
        item.field_sources = sources
        if item.parsed_snapshot:
            item.parsed_snapshot = override_snapshot(item.parsed_snapshot, overrides, at=now, actor_id=actor_id)
        item.validation_errors = [message for message in item.validation_errors if message not in removed]
        changed.append(item)
    session.items.model.objects.bulk_update(
        changed,
        [*fields, "unit", "net_value", "net_unit", "validation_errors", "field_sources", "parsed_snapshot"],
        batch_size=500,
    )
    return len(changed)


def _derive_one_unit_net(item, unit):
    """A unit chosen for a row that had none: kg, l or pieces with no net in the row mean a net of one such unit
    (the same rule the parser applies to a unit it found itself). Returns True when the net was set."""
    if unit.code not in _ONE_PER_UNIT_CODES or item.net_value is not None or item.net_unit_id is not None:
        return False
    net = ((item.parsed_snapshot or {}).get("fields", {}).get("net_value") or {}).get("status")
    if net not in (None, "missing"):
        return False
    item.net_value = Decimal("1")
    item.net_unit = unit
    return True


# ТЗ §2: these are the fields marked "Обязательное" -- everything else (SKU) is optional.
_REQUIRED_FIELDS_FOR_READY = (
    "country_of_origin",
    "unit_id",
    "price",
    "delivery_multiple",
    "net_value",
    "net_unit_id",
    "min_order_qty",
    "availability",
)


def _item_errors(item, *, validate_units=True):
    from file_imports.canonical import effective_issues

    errors = [issue["message"] for issue in effective_issues(getattr(item, "parsed_snapshot", {}))]
    snapshot = getattr(item, "parsed_snapshot", {}) or {}
    currency = snapshot.get("fields", {}).get("currency", {})
    if currency.get("value") not in (None, "RUB"):
        errors.append("Импорт карточки поставщика поддерживает цену в RUB; конвертация валют не выполняется.")
    basis = snapshot.get("fields", {}).get("price_basis", {}).get("value")
    unit_label = snapshot.get("fields", {}).get("unit_label", {}).get("value")
    if basis and unit_label and basis != unit_label:
        errors.append("База цены отличается от единицы продажи. Уточните цену в источнике.")
    if not (item.name or "").strip():
        errors.append("Не заполнено название.")
    elif is_numeric_name(item.name) and NUMERIC_NAME_MESSAGE not in errors:
        errors.append(NUMERIC_NAME_MESSAGE)
    for field in _REQUIRED_FIELDS_FOR_READY:
        value = getattr(item, field)
        if value is None or value == "":
            errors.append(f"Не заполнено поле {field}.")
        elif field == "price" and (not value.is_finite() or value < 0):
            errors.append("Цена должна быть неотрицательной.")
        elif field in ("delivery_multiple", "net_value", "min_order_qty") and (not value.is_finite() or value <= 0):
            errors.append(f"Поле {field} должно быть положительным.")
    for field in ("unit_id", "net_unit_id"):
        identifier = getattr(item, field)
        if identifier:
            active = (
                Unit.objects.filter(pk=identifier, status=EntityStatus.ACTIVE).exists()
                if validate_units
                else getattr(item, field.removesuffix("_id")).status == EntityStatus.ACTIVE
            )
            if not active:
                errors.append(f"Единица {field} не найдена или архивна.")
    if item.action == SupplierProductImportAction.REPLACE and item.target_supplier_product_id is None:
        errors.append("Не выбрана карточка для замены.")
    return errors


def _is_item_ready(item, *, validate_units=True):
    return item.action in (SupplierProductImportAction.NEW, SupplierProductImportAction.REPLACE) and not _item_errors(
        item, validate_units=validate_units
    )


def _item_write_data(item):
    data = {
        "supplier_raw_name": item.name,
        "supplier_normalized_name": item.name,
        "unit": item.unit,
        "country_of_origin": item.country_of_origin,
        "net_value": item.net_value,
        "net_unit": item.net_unit,
        "min_order_qty": item.min_order_qty,
        "delivery_multiple": item.delivery_multiple,
        "availability": item.availability,
        "price": item.price,
    }
    # Состав упаковки пишем только если он есть у строки: при replace пустой состав в файле не должен затирать
    # состав, уже заполненный у товара.
    if item.pack_items_count is not None:
        data.update(
            pack_items_count=item.pack_items_count,
            pack_item_value=item.pack_item_value,
            pack_item_unit=item.pack_item_unit,
            pack_type=item.pack_type,
        )
    package = (item.parsed_snapshot or {}).get("fields", {}).get("pack_type_label", {})
    if package.get("value") and package.get("status") not in ("invalid", "ambiguous"):
        data["package"] = package["value"]
    if item.action == SupplierProductImportAction.REPLACE:
        for field in inherited_fields_for_write(item):
            data.pop(field, None)
    return data


# Keys of the write data that are named differently on the card.
_CARD_ATTRIBUTE = {
    "supplier_raw_name": "supplier_name",
    "supplier_normalized_name": "normalized_name",
    "unit": "unit_id",
    "net_unit": "net_unit_id",
    "pack_item_unit": "pack_item_unit_id",
    "pack_type": "pack_type_id",
    "price": "current_price",
}
_NOT_ON_CARD = object()


def _replace_changes_nothing(target, data):
    """True when writing `data` over `target` would leave the card as it is. Anything that cannot be compared with
    certainty counts as a change, so the card is written as before."""
    for key, value in data.items():
        current = getattr(target, _CARD_ATTRIBUTE.get(key, key), _NOT_ON_CARD)
        if current is _NOT_ON_CARD:
            return False
        new = getattr(value, "pk", value)
        if isinstance(new, Decimal) or isinstance(current, Decimal):
            try:
                if (None if new is None else Decimal(str(new))) != (None if current is None else Decimal(str(current))):
                    return False
            except ArithmeticError:
                return False
        elif new != current:
            return False
    return True


def _taken_sku_message(sku, holder):
    """`holder` is None when an earlier row of the same batch took the article number."""
    if holder is not None and holder.status in (EntityStatus.ARCHIVED, EntityStatus.INACTIVE):
        return hidden_sku_holder_message(sku, holder)
    return f"Артикул {sku} уже занят другим товаром этого поставщика."


def _create_new_products(*, supplier, pending, next_skus, now):
    """Create the new cards of `pending` ([(item, write data)]) in a few queries instead of a dozen per card.

    Names and article numbers that are taken (in the catalog or earlier in the batch) are found with one query each and
    those rows fail the way a unique-constraint hit did before. The cards and their first prices are then inserted
    together; if a concurrent writer takes a name between the check and the insert, the rows are created one at a time
    so each failure is pinned to its own row. Returns ([(item, product)], [(item, messages)])."""
    created, failed, ready = [], [], []
    names = [data["supplier_raw_name"] for _, data in pending]
    skus = [item.supplier_sku for item, _ in pending if item.supplier_sku]
    taken_names = set(
        SupplierProduct.objects.filter(supplier=supplier, supplier_name__in=names).values_list(
            "supplier_name", flat=True
        )
    )
    sku_holders = (
        {
            product.supplier_sku: product
            for product in SupplierProduct.objects.filter(supplier=supplier, supplier_sku__in=skus).only(
                "supplier_sku", "supplier_name", "status"
            )
        }
        if skus
        else {}
    )
    taken_skus = set(sku_holders)
    name_taken = import_error_messages(
        ValidationError({"supplier_raw_name": "Товар с таким названием уже есть у этого поставщика."})
    )
    for item, data in pending:
        try:
            validate_pack_fields(
                count=data.get("pack_items_count"),
                value=data.get("pack_item_value"),
                unit=data.get("pack_item_unit"),
                pack_type=data.get("pack_type"),
            )
        except (DjangoValidationError, ValidationError) as error:
            failed.append((item, import_error_messages(error)))
            continue
        if data["supplier_raw_name"] in taken_names:
            failed.append((item, name_taken))
            continue
        if item.supplier_sku and item.supplier_sku in taken_skus:
            failed.append((item, [_taken_sku_message(item.supplier_sku, sku_holders.get(item.supplier_sku))]))
            continue
        taken_names.add(data["supplier_raw_name"])
        if item.supplier_sku:
            taken_skus.add(item.supplier_sku)
        ready.append((item, data))

    if not ready:
        return created, failed
    # One query for the whole batch, and only for rows that passed validation: sequence numbers are not returned on
    # rollback, so taking them while the batch is still being collected would burn codes.
    for (_, data), system_sku in zip(ready, next_skus(len(ready)), strict=True):
        data["system_sku"] = system_sku
    products = [
        build_supplier_product(supplier=supplier, data=data, supplier_sku=item.supplier_sku or None)
        for item, data in ready
    ]
    prices = [
        SupplierProductPrice(
            supplier_product=product,
            price_type=PriceType.PRICE_LIST,
            price=data["price"],
            currency="RUB",
            status=PriceStatus.ACTIVE,
            valid_from=now,
        )
        for product, (_, data) in zip(products, ready, strict=True)
        if data.get("price") is not None
    ]
    try:
        with transaction.atomic():
            SupplierProduct.objects.bulk_create(products, batch_size=200)
            SupplierProductPrice.objects.bulk_create(prices, batch_size=200)
    except IntegrityError:
        for item, data in ready:
            try:
                with transaction.atomic():
                    product = create_supplier_product(supplier_id=supplier.pk, data=data, supplier=supplier)
                    if item.supplier_sku and product.supplier_sku != item.supplier_sku:
                        product.supplier_sku = item.supplier_sku
                        product.save(update_fields=["supplier_sku", "updated_at"])
            except (DjangoValidationError, ValidationError, IntegrityError) as error:
                messages = (
                    ["Конфликт уникальных полей товара."]
                    if isinstance(error, IntegrityError)
                    else import_error_messages(error)
                )
                failed.append((item, messages))
            else:
                created.append((item, product))
        return created, failed
    created.extend(zip((item for item, _ in ready), products, strict=True))
    return created, failed


@transaction.atomic
def commit_supplier_product_import_session(*, session, actor=None, keep_session=False):
    """keep_session=True: когда строк не осталось, сессия не удаляется, а получает status=COMMITTED -- итог commit
    хранится в ней (commit_result) и читается опросом; удаляет её отложенная очистка. keep_session=False -- прежнее
    поведение (синхронный commit): сессия удаляется сразу."""
    started = time.monotonic()
    # Сериализуем параллельные commit/PATCH одной сессии (две вкладки): строки и сама сессия могут исчезнуть.
    locked_session = type(session).objects.select_for_update().filter(pk=session.pk).first()
    if locked_session is None:
        raise NotFound("Сессия импорта уже удалена.")
    session = locked_session
    session_id = session.pk  # после session.delete() pk обнуляется
    # Serialize product creation for a supplier, then lock replacement cards in PK order.
    supplier = type(session.supplier).objects.select_for_update().get(pk=session.supplier_id)
    list(
        SupplierProduct.objects.select_for_update()
        .filter(
            pk__in=session.items.exclude(target_supplier_product_id=None).values_list(
                "target_supplier_product_id", flat=True
            )
        )
        .order_by("pk")
    )
    # Единицы уже загружены select_related: статус берём с них, а не отдельным запросом на каждую строку.
    items = list(
        session.items.select_related("unit", "net_unit", "pack_item_unit", "pack_type", "target_supplier_product").all()
    )

    errors = []
    imported_count = 0
    skipped_review_count = 0
    skipped_incomplete_count = 0
    conflicted_count = 0
    not_ready_row_numbers = []
    draft_supplier_product_ids = []
    replace_targets_seen = set()
    imported_items = []
    receipts = []
    now = timezone.now()

    from file_imports.models import ImportRowReceipt

    pending_new = []

    def record_failure(item, messages):
        nonlocal skipped_incomplete_count
        skipped_incomplete_count += 1
        not_ready_row_numbers.append(item.row_number)
        item.refresh_from_db()
        item.validation_errors = messages
        item.save(update_fields=["validation_errors", "updated_at"])
        errors.append({"item_id": str(item.id), "row_number": item.row_number, "messages": messages})

    def record_success(item, product):
        nonlocal imported_count
        imported_count += 1
        item.imported_at = now
        imported_items.append(item)
        if item.parsed_snapshot:
            # Written together after the loop (source_item_id is unique, a repeated one is skipped).
            receipts.append(
                ImportRowReceipt(
                    source_item_id=item.id,
                    source_session_id=session.id,
                    scope_type="supplier",
                    scope_id=session.supplier_id,
                    target_type="supplier_product",
                    target_id=product.id,
                    parser_version=item.parsed_snapshot["parser_version"],
                    snapshot=item.parsed_snapshot,
                )
            )
        if item.action == SupplierProductImportAction.NEW:
            draft_supplier_product_ids.append(product.id)
        else:
            replace_targets_seen.add(item.target_supplier_product_id)

    def flush_new():
        if not pending_new:
            return
        created, failed = _create_new_products(
            supplier=supplier, pending=pending_new, next_skus=_next_system_sku_batch, now=now
        )
        pending_new.clear()
        for item, product in created:
            record_success(item, product)
        for item, messages in failed:
            record_failure(item, messages)

    for item in items:
        if item.imported_at is not None:
            continue
        if item.action == SupplierProductImportAction.REVIEW:
            skipped_review_count += 1
            continue
        if item.action == SupplierProductImportAction.SKIP:
            item.imported_at = now
            imported_items.append(item)
            continue
        if (
            item.action == SupplierProductImportAction.REPLACE
            and item.target_supplier_product_id in replace_targets_seen
        ):
            # ТЗ §23: "отсутствие двух replace на один target" -- second offender just stays
            # not-ready, doesn't fail the whole commit.
            skipped_incomplete_count += 1
            not_ready_row_numbers.append(item.row_number)
            continue
        if item.action == SupplierProductImportAction.REPLACE:
            target = (
                SupplierProduct.objects.select_for_update()
                .filter(
                    pk=item.target_supplier_product_id,
                    supplier_id=session.supplier_id,
                    status__in=[EntityStatus.ACTIVE, EntityStatus.DRAFT],
                )
                .first()
            )
            if target is None or target.updated_at != item.target_expected_updated_at:
                skipped_incomplete_count += 1
                conflicted_count += 1
                not_ready_row_numbers.append(item.row_number)
                item.action = SupplierProductImportAction.REVIEW
                item.target_supplier_product = None
                item.target_expected_updated_at = None
                drop_inherited_fields(item)
                item.save(
                    update_fields=[
                        "action",
                        "target_supplier_product",
                        "target_expected_updated_at",
                        "country_of_origin",
                        "min_order_qty",
                        "delivery_multiple",
                        "availability",
                        "field_sources",
                        "updated_at",
                    ]
                )
                continue
        if not _is_item_ready(item, validate_units=False):
            skipped_incomplete_count += 1
            not_ready_row_numbers.append(item.row_number)
            item.validation_errors = _item_errors(item, validate_units=False)
            item.save(update_fields=["validation_errors", "updated_at"])
            errors.append({"item_id": str(item.id), "row_number": item.row_number, "messages": item.validation_errors})
            continue

        if item.action == SupplierProductImportAction.NEW:
            # 2026-09-29 (unified-import-architecture spec §9.8, подтверждено Павлом):
            # commit больше не публикует товар сам -- новый товар создаётся как draft,
            # не виден в buyer-facing поиске, пока не будет опубликован отдельным
            # действием (POST products/<id>/publish или bulk-publish). replace не
            # переводит существующий target в draft -- он уже был active и на виду у
            # покупателя, правка полей через импорт не должна его скрыть.
            data = _item_write_data(item)
            data["status"] = EntityStatus.DRAFT
            pending_new.append((item, data))
            if len(pending_new) >= 200:
                flush_new()
            continue

        try:
            with transaction.atomic():
                data = _item_write_data(item)
                before = {
                    "name": target.supplier_name,
                    "unit_id": str(target.unit_id) if target.unit_id else None,
                }
                if _replace_changes_nothing(target, data):
                    # Same card, same values: nothing to write, "updated" must not move and no audit entry is made.
                    product = target
                    unchanged = True
                else:
                    product = update_supplier_product(supplier_product_id=item.target_supplier_product_id, data=data)
                    unchanged = False
                if not unchanged:
                    record_audit_log(
                        actor=actor,
                        action=AuditLogAction.REPLACED,
                        entity_type=AuditLogEntityType.SUPPLIER_PRODUCT,
                        entity_id=product.id,
                        scope_id=session.supplier_id,
                        summary=json.dumps(
                            {
                                "event": "supplier_product_import_replace",
                                "import_session_id": str(session.id),
                                "before": before,
                                "after": {
                                    "name": product.supplier_name,
                                    "unit_id": str(product.unit_id) if product.unit_id else None,
                                },
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )

                if item.supplier_sku and product.supplier_sku != item.supplier_sku:
                    product.supplier_sku = item.supplier_sku
                    product.save(update_fields=["supplier_sku", "updated_at"])
        except (DjangoValidationError, ValidationError, IntegrityError) as error:
            messages = (
                ["Конфликт уникальных полей товара."]
                if isinstance(error, IntegrityError)
                else import_error_messages(error)
            )
            record_failure(item, messages)
            continue
        record_success(item, product)

    flush_new()
    not_ready_row_numbers.sort()
    errors.sort(key=lambda entry: entry["row_number"])

    # Заимпортированные и пропущенные (skip) строки из разбора убираем, чтобы в сессии оставалось только
    # то, что ещё надо разобрать; когда строк не осталось, удаляется и сама сессия.
    if receipts:
        from file_imports.models import ImportRowReceipt

        ImportRowReceipt.objects.bulk_create(receipts, batch_size=500, ignore_conflicts=True)
    if imported_items:
        SupplierProductImportItem.objects.bulk_update(imported_items, ["imported_at"], batch_size=500)
    session.items.filter(imported_at__isnull=False).delete()
    no_rows_left = not session.items.exists()
    session_deleted = no_rows_left and not keep_session
    if no_rows_left:
        session.status = SupplierProductImportSessionStatus.COMMITTED
        if keep_session:
            session.save(update_fields=["status", "updated_at"])
        else:
            session.delete()
    else:
        session.status = SupplierProductImportSessionStatus.OPEN
        session.save(update_fields=["status", "updated_at"])

    logger.info(
        "import.commit.done session=%s rows=%s imported=%s skipped_incomplete=%s total_s=%.2f",
        session_id,
        len(items),
        imported_count,
        skipped_incomplete_count,
        time.monotonic() - started,
    )
    return {
        "errors": errors,
        "imported_count": imported_count,
        "skipped_review_count": skipped_review_count,
        "skipped_incomplete_count": skipped_incomplete_count,
        "conflicted_count": conflicted_count,
        "not_ready_row_numbers": not_ready_row_numbers,
        "session_status": session.status,
        "session_deleted": session_deleted,
        "draft_supplier_product_ids": draft_supplier_product_ids,
    }


def import_committed_notification_text(*, supplier, imported_count):
    return (
        "✅ Импорт товаров с сайта завершён\n\n"
        f"Компания: «{supplier.display_name}»\n"
        f"Добавлено или обновлено: {imported_count}."
    )


def notify_supplier_product_import_committed(*, supplier, imported_count):
    """Сообщение в привязанные Telegram-чаты поставщика после фонового commit (уведомление добавлено в 58723e1,
    2026-09-30). Синхронный commit во view вызывает notify_linked_supplier_chats сам, с тем же текстом. Сбой
    уведомления не должен ломать уже сохранённый импорт."""
    if not imported_count:
        return
    try:
        notify_linked_supplier_chats(
            supplier=supplier,
            text=import_committed_notification_text(supplier=supplier, imported_count=imported_count),
        )
    except Exception:
        logger.exception("import.commit.notify_failed supplier=%s", supplier.pk)


def commit_actor_ref(actor):
    """Сериализуемая ссылка на того, кто нажал «Применить» (для audit-лога replace в фоновой задаче)."""
    if isinstance(actor, Supplier):
        return {"type": "supplier", "id": str(actor.pk)}
    if isinstance(actor, User):
        return {"type": "core_user", "id": str(actor.pk)}
    return None  # сервисный токен и прочее -- audit-лог пишет SERVICE


def resolve_commit_actor(actor_ref):
    if not actor_ref:
        return None
    if actor_ref.get("type") == "supplier":
        return Supplier.objects.filter(pk=actor_ref.get("id")).first()
    if actor_ref.get("type") == "core_user":
        return User.objects.filter(pk=actor_ref.get("id")).first()
    return None


def _commit_failure_message(error):
    # В commit_error попадает в UI поставщика: только понятный текст, без деталей внутренностей (они в логе).
    from celery.exceptions import SoftTimeLimitExceeded

    if isinstance(error, NotFound):
        return "Сессия импорта уже удалена."
    if isinstance(error, SoftTimeLimitExceeded):
        return "Применение заняло слишком много времени. Повторите попытку."
    return "Не удалось применить импорт. Повторите попытку позже или обратитесь в поддержку."


def run_supplier_product_import_commit(*, session_id, actor=None):
    """Фоновое применение импорта: pending|running -> running -> none (итог в commit_result) | failed.
    running принимается и на входе: воркер мог умереть посреди прошлой попытки (транзакция commit откатилась целиком,
    строки не тронуты), повторная доставка acks_late подхватывает работу. Параллельный запуск исключён блокировкой
    задачи. Возвращает итог commit либо None, если применять нечего (не ждёт commit / сессии нет)."""
    claimed = SupplierProductImportSession.objects.filter(
        pk=session_id,
        commit_status__in=(SupplierProductImportCommitStatus.PENDING, SupplierProductImportCommitStatus.RUNNING),
    ).update(commit_status=SupplierProductImportCommitStatus.RUNNING, commit_error=None, updated_at=timezone.now())
    if not claimed:
        return None
    session = SupplierProductImportSession.objects.select_related("supplier").filter(pk=session_id).first()
    if session is None:
        return None
    try:
        result = commit_supplier_product_import_session(session=session, actor=actor, keep_session=True)
    except Exception as error:
        logger.exception("import.commit.failed session=%s error=%s", session_id, error)
        SupplierProductImportSession.objects.filter(pk=session_id).update(
            commit_status=SupplierProductImportCommitStatus.FAILED,
            commit_error=_commit_failure_message(error),
            updated_at=timezone.now(),
        )
        raise
    SupplierProductImportSession.objects.filter(pk=session_id).update(
        commit_status=SupplierProductImportCommitStatus.NONE,
        commit_result=json.loads(json.dumps(result, default=str)),
        commit_error=None,
        updated_at=timezone.now(),
    )
    notify_supplier_product_import_committed(supplier=session.supplier, imported_count=result["imported_count"])
    return result
