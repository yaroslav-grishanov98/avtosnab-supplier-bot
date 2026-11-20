import json
import logging

from utils import normalize_confidence, normalize_decimal, normalize_unit, pick_ai_text, strip_code_fence

MAX_ROWS_PER_CHUNK = 30
logger = logging.getLogger(__name__)


def validate_chunk(chunk: dict):
    if len(chunk.get("rows", [])) > MAX_ROWS_PER_CHUNK:
        raise ValueError(
            f"Chunk overflow: rows.length={len(chunk['rows'])}, chunk_index={chunk.get('chunk_index', 'unknown')}"
        )


def resolve_row_number(value, index: int, context_rows: list) -> int:
    try:
        number = int(value)
        if number > 0:
            for row in context_rows:
                if int(row.get("row_number", 0)) == number:
                    return int(row["row_number"])
            if number - 1 < len(context_rows):
                return int(context_rows[number - 1].get("row_number", index + 1))
            return number
    except (ValueError, TypeError):
        logger.debug("Не удалось использовать номер строки из ответа AI: %r", value)
    return int(context_rows[index].get("row_number", index + 1)) if index < len(context_rows) else index + 1


def build_ai_payload(ai_response: dict, context: dict) -> dict:
    ai_text = strip_code_fence(pick_ai_text(ai_response))

    if not ai_text:
        raise ValueError(f"AI response does not contain message content. Keys: {list(ai_response.keys())}")

    try:
        parsed = json.loads(ai_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI вернул невалидный JSON: {e}. Raw: {ai_text[:500]}")

    products = []
    for i, product in enumerate(parsed.get("products", [])):
        warnings = [str(w) for w in product.get("warnings", [])]
        products.append(
            {
                "row_number": resolve_row_number(product.get("row_number"), i, context.get("rows", [])),
                "raw_name": str(product.get("raw_name", product.get("name", ""))).strip(),
                "normalized_name": str(product.get("normalized_name", "")).strip(),
                "supplier_sku": str(product["supplier_sku"]).strip() if product.get("supplier_sku") else None,
                "price": normalize_decimal(product.get("price")),
                "currency": str(product.get("currency", "RUB")).strip(),
                "unit_code": normalize_unit(product.get("unit_code", product.get("unit", ""))),
                "package_info": str(product.get("package", product.get("package_info", ""))).strip() or None,
                "brand": str(product["brand"]).strip() if product.get("brand") else None,
                "volume": str(product["volume"]).strip() if product.get("volume") else None,
                "volume_unit": str(product["volume_unit"]).strip() if product.get("volume_unit") else None,
                "weight": str(product["weight"]).strip() if product.get("weight") else None,
                "weight_unit": str(product["weight_unit"]).strip() if product.get("weight_unit") else None,
                "min_order_qty": normalize_decimal(product.get("min_order_qty"), None),
                "multiplicity": normalize_decimal(product.get("multiplicity"), None),
                "category_path": [str(c) for c in product.get("category_path", [])],
                "comment": str(product["comment"]).strip() if product.get("comment") else None,
                "availability_status": str(product.get("availability_status", "available")).strip(),
                "confidence": normalize_confidence(product.get("confidence")),
                "warnings": warnings,
            }
        )

    return {
        "schema_version": "supplier_price_ai.v1",
        "supplier_id": context["supplier_id"],
        "upload_id": context["upload_id"],
        "source_channel": context.get("source_channel", "telegram"),
        "processing_version": str(parsed.get("processing_version", f"bot-chunk-{context.get('chunk_index', 1)}")),
        "supplier_name": str(parsed.get("supplier_name", context.get("supplier_name", ""))),
        "products": products,
    }
