# ruff: noqa
"""Frozen-task LLM comparison (see docs/wiki/logs/2026-10-15-llm-model-comparison.md). Local paths are hard-coded: run on the author machine."""

import csv, sys, json, copy, collections
from pathlib import Path

sys.path.insert(0, "/home/itmedia_pc/projects/auto-snab-import-quality")
import django

django.setup()
from django.conf import settings

settings.CANONICAL_IMPORT_LLM_ENABLED = False
from django.core.files.uploadedfile import SimpleUploadedFile
from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file, _field_for_header_key
from file_imports.canonical import parse_product_row
from file_imports.types import RawImportRow
from file_imports import llm_candidates as L

G = Path("/mnt/c/Users/itmedia pc/git-backups")
D = Path("/mnt/c/Users/itmedia pc/Downloads")
OUT = Path("/home/itmedia_pc/projects/auto-snab-import-quality/var/llm-eval")
labels = {
    (r["file"], r["row_number"]): r
    for r in csv.DictReader(open(G / "labeling-sheet.consensus.csv", encoding="utf-8-sig"), delimiter=";")
}
decided = {k: v for k, v in labels.items() if v["label_status"] != "needs-human"}
tasks = []
for name in sorted({k[0] for k in decided}):
    rows = parse_supplier_product_import_file(SimpleUploadedFile(name, (D / name).read_bytes()), canonical_enabled=True)
    for row in rows:
        k = (name, str(row["row_number"]))
        if k not in decided:
            continue
        rawd = row["_parsed"]["raw"]
        parsed = parse_product_row(
            RawImportRow(rawd["row_number"], rawd["cells"], rawd["source"]), _field_for_header_key
        )
        req = L.requested_fields(parsed)
        tasks.append(
            {
                "file": name,
                "row_number": row["row_number"],
                "split": decided[k]["split"],
                "requested": req,
                "cells": L._row_cells(parsed),
                "label": {
                    f: decided[k][f]
                    for f in (
                        "true_unit",
                        "true_country",
                        "true_pack_count",
                        "true_pack_item_value",
                        "true_pack_item_unit",
                    )
                },
            }
        )
with_req = [t for t in tasks if t["requested"]]
print("decided rows", len(tasks), "| rows with requested fields", len(with_req))
print("requested fields:", collections.Counter(f for t in with_req for f in t["requested"]))
print("by split:", collections.Counter(t["split"] for t in with_req))
chunks = [with_req[i : i + 12] for i in range(0, len(with_req), 12)]
json.dump({"tasks": with_req}, open(OUT / "tasks.json", "w"), ensure_ascii=False, indent=1)
payload_chars = sum(
    len(
        json.dumps(
            {
                "rows": [
                    {"row_number": t["row_number"], "requested_fields": t["requested"], "cells": t["cells"]} for t in c
                ]
            },
            ensure_ascii=False,
        )
    )
    for c in chunks
)
print(
    "chunks",
    len(chunks),
    "| payload chars",
    payload_chars,
    "| ~input tokens/run",
    (payload_chars + len(L._PROMPT) * len(chunks)) // 3,
)
