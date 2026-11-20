from utils import clean_value

CHUNK_SIZE_SPREADSHEET = 10
CHUNK_SIZE_TEXT = 30
MAX_CELL_LENGTH = 240
MAX_LINE_LENGTH = 600
MAX_COLUMNS = 14

SYSTEM_KEYS = {
    "id",
    "status",
    "supplier_id",
    "source_channel",
    "source_file_name",
    "raw_payload",
    "ai_payload",
    "processed_at",
    "created_at",
    "updated_at",
}


def looks_like_upload_response(data: dict) -> bool:
    if not data or not isinstance(data, dict):
        return False
    keys = set(data.keys())
    system_key_count = len(keys & SYSTEM_KEYS)
    return bool(data.get("id") and data.get("status") and data.get("source_channel") and system_key_count >= 3)


def prepare_chunks_from_spreadsheet(rows: list, context: dict) -> list[dict]:
    cleaned_rows = []
    for item in rows:
        source = item if isinstance(item, dict) else {}
        if looks_like_upload_response(source):
            continue

        clean = {}
        for key, value in source.items():
            if key in ("__rowNum__", "row_number", "rowNumber"):
                continue
            normalized_key = clean_value(key, 80)
            text = clean_value(value, MAX_CELL_LENGTH)
            if not normalized_key or not text:
                continue
            if len(clean) >= MAX_COLUMNS:
                break
            clean[normalized_key] = text

        if not clean:
            continue

        source_row_number = int(source.get("__rowNum__", source.get("row_number", source.get("rowNumber", 0))))
        row_number = source_row_number if source_row_number > 0 else len(cleaned_rows) + 1
        cleaned_rows.append({"row_number": row_number, "cells": clean})

    if not cleaned_rows:
        return [
            {
                **context,
                "chunk_index": 1,
                "chunk_total": 1,
                "chunk_start_row": None,
                "chunk_end_row": None,
                "rows": [],
                "ai_input_text": "Прайс не содержит распознанных строк.",
            }
        ]

    chunks = []
    for start in range(0, len(cleaned_rows), CHUNK_SIZE_SPREADSHEET):
        chunks.append(cleaned_rows[start : start + CHUNK_SIZE_SPREADSHEET])

    return [
        {
            **context,
            "chunk_index": i + 1,
            "chunk_total": len(chunks),
            "chunk_start_row": chunk[0]["row_number"],
            "chunk_end_row": chunk[-1]["row_number"],
            "rows": chunk,
            "ai_input_text": "\n".join(
                f"row_number={row['row_number']}; " + "; ".join(f"{k}: {v}" for k, v in row["cells"].items())
                for row in chunk
            ),
        }
        for i, chunk in enumerate(chunks)
    ]


def prepare_chunks_from_text(text: str, context: dict) -> list[dict]:
    lines = [clean_value(line, MAX_LINE_LENGTH) for line in text.split("\n")]
    lines = [line for line in lines if line]

    rows = (
        [{"row_number": i + 1, "cells": {"text": line}} for i, line in enumerate(lines)]
        if lines
        else [{"row_number": 1, "cells": {"text": clean_value(text, MAX_LINE_LENGTH)}}]
    )

    chunks = []
    for start in range(0, len(rows), CHUNK_SIZE_TEXT):
        chunks.append(rows[start : start + CHUNK_SIZE_TEXT])

    return [
        {
            **context,
            "chunk_index": i + 1,
            "chunk_total": len(chunks),
            "chunk_start_row": chunk[0]["row_number"],
            "chunk_end_row": chunk[-1]["row_number"],
            "rows": chunk,
            "ai_input_text": "\n".join(
                f"row_number={row['row_number']}; text: {row['cells']['text']}" for row in chunk
            ),
        }
        for i, chunk in enumerate(chunks)
    ]
