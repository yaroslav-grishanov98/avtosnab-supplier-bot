"""Only new import diagnostics are optional; existing schema requirements stay intact."""


def optional_import_diagnostics(result, generator, request, public):
    optional = {"parser_version", "field_meta", "parse_issues", "business_issues"}
    for name, schema in result.get("components", {}).get("schemas", {}).items():
        if name in ("SupplierProductImportItem", "CatalogImportSessionItem"):
            schema["required"] = [key for key in schema.get("required", []) if key not in optional]
    return result
