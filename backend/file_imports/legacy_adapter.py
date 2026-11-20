"""Legacy product JSON adapter; defaults remain explicit and reviewable."""

from decimal import Decimal

from file_imports.canonical import FieldCandidate, FieldStatus, parse_product_row, resolve_field


def parse_legacy_row(row, *, unit_aliases=None, llm_budget=None):
    from api.v1.suppliers.normalization import _category_path, _extract_brand, normalize_unit_code
    from api.v1.suppliers.product_import_parsing import _field_for_header_key

    parsed = parse_product_row(row, _field_for_header_key)
    # Legacy caller handles a file-level budget; keep individual row adapter deterministic.

    if llm_budget is not None:
        from file_imports.llm_candidates import enrich_candidates

        parsed = enrich_candidates(parsed, request_budget=llm_budget)
    fields = parsed.fields

    def value(key):
        f = fields.get(key)
        return f.value if f and f.status not in (FieldStatus.INVALID, FieldStatus.AMBIGUOUS) else None

    # Defaults belong to the legacy workflow, not the common parser.
    if fields["currency"].status == FieldStatus.MISSING:
        fields["currency"] = resolve_field(
            "currency",
            [FieldCandidate("RUB", source="default", method="legacy-currency-policy", status=FieldStatus.DEFAULTED)],
        )
    availability = value("availability_label")
    if availability is None:
        fields["availability_label"] = resolve_field(
            "availability_label",
            [
                FieldCandidate(
                    "available", source="default", method="legacy-availability-policy", status=FieldStatus.DEFAULTED
                )
            ],
        )
    statuses = {
        "в наличии": "available",
        "нет в наличии": "out_of_stock",
        "нет": "out_of_stock",
        "под заказ": "on_order",
    }
    raw_name = row.cells.get(fields["name"].source_column, "")
    net_unit = value("net_unit_label")
    weight = value("net_value") if net_unit in ("кг", "г") else None
    volume = value("net_value") if net_unit in ("л", "мл") else None
    warnings = [issue["code"] for issue in parsed.parse_issues]
    if parsed.row_type not in ("product", "possible_product"):
        parsed.parse_issues.append(
            {
                "code": "non_product_row",
                "field": "name",
                "level": "critical",
                "review_required": True,
                "message": "Строка не является товаром.",
            }
        )
    return {
        "row_number": row.row_number,
        "raw_name": raw_name,
        "normalized_name": value("name") or raw_name,
        "supplier_sku": value("supplier_sku"),
        "price": value("price"),
        "currency": value("currency"),
        "unit_code": normalize_unit_code(value("unit_label"), aliases=unit_aliases) or "",
        "package_info": value("package_composition"),
        "brand": _extract_brand(raw_name) or None,
        "weight": weight,
        "weight_unit": net_unit if weight is not None else None,
        "volume": volume,
        "volume_unit": net_unit if volume is not None else None,
        "min_order_qty": value("min_order_qty"),
        "multiplicity": value("delivery_multiple"),
        "pack_items_count": value("pack_items_count"),
        "pack_item_value": value("pack_item_value"),
        "pack_item_unit_code": value("pack_item_unit_label"),
        "pack_type_code": value("pack_type_label"),
        "category_path": _category_path(row.cells),
        "comment": None,
        "availability_status": statuses.get(str(availability).casefold(), availability)
        if availability
        else "available",
        "confidence": Decimal(".95") if not warnings else Decimal(".65"),
        "warnings": warnings,
        "raw_payload": {
            "row_number": row.row_number,
            "cells": dict(row.cells),
            "source": dict(row.source),
            "parser_source": "canonical",
            "parser_version": parsed.parser_version,
            "canonical": row.source.get("canonical_initial") or parsed.snapshot(),
        },
    }
