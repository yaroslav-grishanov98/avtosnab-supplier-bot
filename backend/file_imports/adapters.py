"""Compatibility adapters: the canonical DTO is never a public request schema."""

from file_imports.canonical import FieldStatus

SUPPLIER_FIELDS = (
    "name",
    "country_of_origin",
    "unit_label",
    "price",
    "delivery_multiple",
    "net_value",
    "net_unit_label",
    "min_order_qty",
    "availability_label",
    "supplier_sku",
    "package_composition",
)


def supplier_row(parsed):
    result = {"row_number": parsed.row_number}
    for key in SUPPLIER_FIELDS:
        value = parsed.fields.get(key)
        result[key] = None if value is None or value.status == FieldStatus.INVALID else value.value
        if result[key] is not None:
            result[key] = str(result[key])
    result["_parsed"] = parsed.snapshot()
    result["_raw_name"] = parsed.raw["cells"].get(parsed.fields["name"].source_column, result["name"])
    return result


def catalog_row(parsed):
    result = {"row_number": parsed.row_number}
    for public, canonical in {"name": "name", "unit": "unit_label", "net": "net_value", "note": "note"}.items():
        field = parsed.fields.get(canonical)
        if field and field.value is not None and field.status != FieldStatus.INVALID:
            result[public] = str(field.value)
    result["_parsed"] = parsed.snapshot()
    return result


def field_metadata(snapshot):
    result = {
        key: {k: v for k, v in value.items() if k not in ("candidates", "conflicts")}
        for key, value in (snapshot or {}).get("fields", {}).items()
    }
    for override in (snapshot or {}).get("overrides", []):
        result[override["field"]] = {k: v for k, v in override.items() if k != "field"}
    return result
