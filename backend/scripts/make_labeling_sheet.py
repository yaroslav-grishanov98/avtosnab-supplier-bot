"""Build the sheet for hand-labelling price-list rows (256 rows: 64 per file, 32 dev + 32 holdout).

Usage (LLM is forced off; use a database with the migrated unit dictionary):
    python scripts/make_labeling_sheet.py OUT.csv FILE [FILE ...]

Half of every file's rows are drawn from "interesting" ones (a pack expression, a measure, a country, or a
parser issue); the other half from all rows. A fixed seed makes the draw reproducible. The sheet has the parser's
current answer next to empty `true_*` columns: fill them from the source row, write `unknown` where the source
does not prove the value, and never copy the parser's answer without checking. Keep the sheet outside Git
(price-list contents are commercial data); var/ is git-ignored.
"""

import csv
import hashlib
import os
import random
import sys
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file  # noqa: E402

settings.CANONICAL_IMPORT_LLM_ENABLED = False

SEED = 20261015
PER_FILE = 64
PARSED_COLUMNS = (
    "name",
    "unit_label",
    "net_value",
    "net_unit_label",
    "package_composition",
    "country_of_origin",
    "price",
)
LABEL_COLUMNS = (
    "true_unit",
    "true_net_value",
    "true_net_unit",
    "true_pack_count",
    "true_pack_item_value",
    "true_pack_item_unit",
    "true_country",
    "evidence_cell",
    "evidence_fragment",
    "needs_manual_check",
    "labeller_note",
)


def _interesting(row):
    parsed = row.get("_parsed") or {}
    return bool(
        row.get("package_composition")
        or row.get("net_value")
        or row.get("country_of_origin")
        or parsed.get("parse_issues")
    )


def _bucket(file_name, row_number):
    return hashlib.sha256(f"{SEED}:{file_name}:{row_number}".encode()).hexdigest()


def _draw(rows, count, rng):
    return rng.sample(rows, min(count, len(rows)))


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    out_path, files = Path(argv[1]), argv[2:]
    rng = random.Random(SEED)
    sheet = []
    for file in files:
        path = Path(file)
        rows = parse_supplier_product_import_file(
            SimpleUploadedFile(path.name, path.read_bytes()), canonical_enabled=True
        )
        interesting = [row for row in rows if _interesting(row)]
        chosen = {row["row_number"]: row for row in _draw(interesting, PER_FILE // 2, rng)}
        rest = [row for row in rows if row["row_number"] not in chosen]
        for row in _draw(rest, PER_FILE - len(chosen), rng):
            chosen[row["row_number"]] = row
        # Exactly half of every file is dev, the rest holdout; the order comes from a seeded hash.
        by_hash = sorted(chosen, key=lambda number: _bucket(path.name, number))
        dev_numbers = set(by_hash[: len(by_hash) // 2])
        for number in sorted(chosen):
            row = chosen[number]
            sheet.append(
                {
                    "file": path.name,
                    "row_number": number,
                    "split": "dev" if number in dev_numbers else "holdout",
                    "raw_name": row.get("_raw_name", row["name"]),
                    **{f"parsed_{key}": row.get(key) for key in PARSED_COLUMNS},
                    **dict.fromkeys(LABEL_COLUMNS, ""),
                }
            )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sheet[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(sheet)
    dev = sum(row["split"] == "dev" for row in sheet)
    print(f"{len(sheet)} rows written to {out_path} (dev {dev}, holdout {len(sheet) - dev})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
