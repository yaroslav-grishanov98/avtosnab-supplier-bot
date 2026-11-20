"""Golden regression for the shared canonical parser.

The expected file records the CURRENT behaviour, including known defects, so a parser
change shows up as an explicit diff for the supplier and the Master/Venue paths alike.
Regenerate intentionally with: UPDATE_GOLDEN=1 manage.py test file_imports.test_golden_parsing
"""

import json
import os
from pathlib import Path

from django.test import TestCase, override_settings

from file_imports.golden import parse_projection

GOLDEN_PATH = Path(__file__).parent / "golden" / "synthetic_parse.json"

CASES = [
    "Томаты №2 800г",
    "Томаты 2 800 г",
    "Огурцы сорт 2 500 г",
    "Перец ЧИЛИ 170 1/12, шт",
    "Сок 270 1/8, шт",
    "Молоко 1000 мл1/12",
    "Сыр 0,5 кг",
    "Макароны плоские 400г*20",
    "Мука 1 кг",
    "Яйцо С1 10 шт",
]


def _csv_bytes():
    lines = ["Наименование;Цена;Ед."]
    for index, name in enumerate(CASES, start=1):
        lines.append(f"{name};{100 + index},50;шт")
    return ("\n".join(lines) + "\n").encode("utf-8")


@override_settings(CANONICAL_IMPORT_LLM_ENABLED=False)
class GoldenParsingTests(TestCase):
    def test_synthetic_file_matches_golden(self):
        actual = parse_projection("golden.csv", _csv_bytes())
        if os.environ.get("UPDATE_GOLDEN") == "1":
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            dumped = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            GOLDEN_PATH.write_text(dumped + "\n", "utf-8")
        expected = json.loads(GOLDEN_PATH.read_text("utf-8"))
        # Round-trip through JSON so Decimal and similar values compare as stored.
        self.assertEqual(json.loads(json.dumps(actual, ensure_ascii=False, default=str)), expected)
