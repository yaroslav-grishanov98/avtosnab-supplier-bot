"""«Состав упаковки» товара поставщика (TASK-111): «8 шт. × 2 кг = 16 кг/кор.».

Хранится структурно на SupplierProduct (pack_items_count, pack_item_value,
pack_item_unit, pack_type), итог и строка для показа считаются здесь — одна
реализация для кабинета поставщика и каталога покупателя. Разбор строки из
колонки прайса — тоже здесь, чтобы формат показа и формат импорта жили рядом."""

import re
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

_COMPOSITION_RE = re.compile(
    r"^\s*(?P<count>\d+)\s*(?:шт\.?|pcs)?\s*(?:[×xх*]|по)\s*"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[^\s\d=/]+)"
    r"(?:\s*=\s*[^/]*)?"
    r"(?:\s*/\s*(?P<pack_type>[^\s\d/]+))?\s*$",
    re.IGNORECASE,
)


def format_quantity(value):
    """Decimal('0.7500') -> '0,75', Decimal('16.0000') -> '16' — как в макете."""
    text = format(Decimal(value).normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def package_composition(supplier_product):
    """Вложенный объект для ответа API или None, если состав не заполнен."""
    count = supplier_product.pack_items_count
    value = supplier_product.pack_item_value
    unit = supplier_product.pack_item_unit
    if count is None or value is None or unit is None:
        return None
    pack_type = supplier_product.pack_type
    total = Decimal(count) * Decimal(value)
    display = f"{count} шт. × {format_quantity(value)} {unit.code} = {format_quantity(total)} {unit.code}"
    if pack_type is not None:
        display += f"/{pack_type.code}."
    return {
        "items_count": count,
        "item_value": value,
        "item_unit_id": unit.id,
        "item_unit": unit.code,
        "total_value": total,
        "pack_type_id": pack_type.id if pack_type else None,
        "pack_type": pack_type.code if pack_type else None,
        "display": display,
    }


def validate_pack_fields(*, count, value, unit, pack_type):
    """Итоговое (после PATCH-слияния) состояние: либо заполнены count/value/unit
    вместе, либо все пусты; тара — только вместе с ними."""
    core = (count, value, unit)
    if any(v is not None for v in core) and any(v is None for v in core):
        raise ValidationError(
            {
                "package_composition": "Состав упаковки заполняется целиком: pack_items_count, pack_item_value "
                "и pack_item_unit_id вместе (или очистите все три)."
            }
        )
    if pack_type is not None and count is None:
        raise ValidationError(
            {"pack_type_id": "Тару указывают только вместе с pack_items_count, pack_item_value и pack_item_unit_id."}
        )


def parse_package_composition(text, *, unit_aliases, normalize_unit_code):
    """«8 шт. × 2 кг = 16 кг/кор.» -> {pack_items_count, pack_item_value,
    pack_item_unit_code, pack_type_code}. None, если строка не разобрана или
    единица не из справочника — тогда состав просто не заполняется (строку
    прайса из-за этого не бракуем)."""
    match = _COMPOSITION_RE.match(str(text or ""))
    if not match:
        return None
    count = int(match["count"])
    try:
        value = Decimal(match["value"].replace(",", "."))
    except InvalidOperation:
        return None
    unit_code = normalize_unit_code(match["unit"], aliases=unit_aliases)
    if count <= 0 or value <= 0 or not unit_code:
        return None
    pack_type_code = normalize_unit_code(match["pack_type"], aliases=unit_aliases) if match["pack_type"] else ""
    return {
        "pack_items_count": count,
        "pack_item_value": value,
        "pack_item_unit_code": unit_code,
        "pack_type_code": pack_type_code or None,
    }
