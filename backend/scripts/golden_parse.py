"""Snapshot or compare canonical parsing of real price files (kept outside Git).

Usage (LLM is forced off; use a database with the migrated unit dictionary):
    python scripts/golden_parse.py snapshot LABEL FILE [FILE ...]   # writes var/golden/LABEL/*.json
    python scripts/golden_parse.py compare LABEL FILE [FILE ...]    # exits 1 on any difference

Snapshots are written under var/golden/, which is git-ignored: price files and their
parsed output are commercial data and must not be committed.
"""

import json
import os
import sys
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402

from file_imports.golden import parse_projection  # noqa: E402

settings.CANONICAL_IMPORT_LLM_ENABLED = False


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def main(argv):
    if len(argv) < 4 or argv[1] not in {"snapshot", "compare"}:
        print(__doc__)
        return 2
    mode, label, files = argv[1], argv[2], argv[3:]
    out_dir = ROOT / "var" / "golden" / label
    differences = 0
    for file in files:
        path = Path(file)
        projected = _dumps(parse_projection(path.name, path.read_bytes()))
        target = out_dir / f"{path.name}.json"
        if mode == "snapshot":
            out_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(projected, "utf-8")
            print(f"snapshot {target}")
        elif not target.exists() or target.read_text("utf-8") != projected:
            differences += 1
            print(f"DIFFERS {path.name}")
        else:
            print(f"same    {path.name}")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
