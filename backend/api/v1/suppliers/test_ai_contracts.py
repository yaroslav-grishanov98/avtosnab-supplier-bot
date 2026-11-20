from decimal import Decimal

import pytest
from pydantic import ValidationError

from api.v1.suppliers.ai_contracts import ParsedSupplierCatalog, ParsedSupplierProductRow

VALID_ROW = {
    "row_number": 1,
    "raw_name": "Молоко 3.2%",
    "normalized_name": "Молоко 3.2%",
}


def make_row(**overrides):
    payload = {**VALID_ROW, **overrides}
    return ParsedSupplierProductRow.model_validate(payload)


def test_product_row_minimal_valid_payload_uses_stable_defaults():
    row = make_row()

    assert row.currency == "RUB"
    assert row.unit_code == ""
    assert row.category_path == []
    assert row.availability_status == "available"
    assert row.confidence == Decimal("0.9500")
    assert row.warnings == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (" usd ", "USD"),
        ("eur-extra", "EUR"),
        ("", "RUB"),
    ],
)
def test_currency_is_normalized_to_three_uppercase_characters(source, expected):
    assert make_row(currency=source).currency == expected


def test_currency_none_is_rejected_by_string_contract():
    with pytest.raises(ValidationError):
        make_row(currency=None)


@pytest.mark.parametrize("field", ["unit_code", "volume_unit", "weight_unit"])
def test_unit_codes_are_stripped(field):
    row = make_row(**{field: "  кг  "})
    assert getattr(row, field) == "кг"


@pytest.mark.parametrize("field", ["unit_code", "volume_unit", "weight_unit"])
def test_none_unit_code_becomes_empty_string(field):
    row = make_row(**{field: None})
    assert getattr(row, field) == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_number", 0),
        ("price", Decimal("-0.01")),
        ("volume", Decimal("-0.01")),
        ("weight", Decimal("-0.01")),
        ("min_order_qty", Decimal("-0.01")),
        ("multiplicity", Decimal("-0.01")),
        ("confidence", Decimal("-0.01")),
        ("confidence", Decimal("1.01")),
    ],
)
def test_numeric_contract_rejects_out_of_range_values(field, value):
    with pytest.raises(ValidationError):
        make_row(**{field: value})


@pytest.mark.parametrize("field", ["raw_name", "normalized_name"])
def test_required_product_names_must_not_be_empty(field):
    with pytest.raises(ValidationError):
        make_row(**{field: ""})


def test_extra_fields_are_ignored_on_product_row():
    row = make_row(unexpected_field="ignored")
    assert "unexpected_field" not in row.model_dump()


def test_catalog_defaults_products_to_empty_list():
    catalog = ParsedSupplierCatalog.model_validate({})
    assert catalog.products == []


def test_catalog_parses_rows_and_ignores_extra_fields():
    catalog = ParsedSupplierCatalog.model_validate(
        {
            "products": [{**VALID_ROW, "currency": " usd "}],
            "metadata": {"source": "should be ignored"},
        }
    )

    assert len(catalog.products) == 1
    assert catalog.products[0].currency == "USD"
    assert "metadata" not in catalog.model_dump()
