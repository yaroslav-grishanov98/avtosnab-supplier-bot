"""Deterministic projection of canonical parser output for before/after comparison.

Used by the golden regression test and scripts/golden_parse.py. The LLM must be off
(CANONICAL_IMPORT_LLM_ENABLED=False) so results do not depend on a model.
"""

from django.core.files.uploadedfile import SimpleUploadedFile

from api.v1.catalog.importers import parse_catalog_file
from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file


def _issues(row):
    parsed = row.get("_parsed") or {}
    return sorted(f"{issue.get('code')}:{issue.get('field')}" for issue in parsed.get("parse_issues") or [])


def _public(row):
    projected = {key: value for key, value in row.items() if not key.startswith("_")}
    projected["_issues"] = _issues(row)
    return projected


def parse_projection(file_name, content):
    """Parse the same bytes through the supplier and the Master/Venue catalog paths."""
    supplier = parse_supplier_product_import_file(SimpleUploadedFile(file_name, content), canonical_enabled=True)
    catalog = parse_catalog_file(SimpleUploadedFile(file_name, content), canonical_enabled=True)
    return {
        "supplier": [_public(row) for row in supplier],
        "catalog": {
            "rows_without_name": catalog.rows_without_name,
            "rows": [_public(row) for row in catalog.rows],
        },
    }
