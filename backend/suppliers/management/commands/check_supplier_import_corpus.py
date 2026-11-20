"""Регрессионный прогон парсера импорта товаров поставщика по корпусу реальных прайсов.

Сами файлы лежат вне git (правило проекта: raw-файлы не коммитим); в репозитории только
ожидаемые метрики -- `file_imports/corpus_expected.json`. Парсинг детерминированный:
без БД, без LLM, секунды на весь корпус.

    python manage.py check_supplier_import_corpus --dir "/путь/к/прайсам"
    python manage.py check_supplier_import_corpus --dir "/путь/к/прайсам" --update

Падает (exit 1), если у файла появилась ошибка или упало число строк / строк с ценой /
единицей / артикулом относительно baseline, либо выросло число дублей имён
(`duplicate_names`) или строк без цены (`unpriced_rows`). Рост "хороших" метрик -- не ошибка,
но подсказывает пересохранить baseline через --update; `--update` ДОБАВЛЯЕТ/обновляет записи
измеренных файлов, остальные ключи baseline сохраняются.

Режим парсера задаётся настройкой `CANONICAL_SUPPLIER_PARSER_ENABLED` (по умолчанию выключена =
legacy-парсер). В проде работает канонический режим, поэтому baseline нужно снимать с
`CANONICAL_SUPPLIER_PARSER_ENABLED=true` (LLM выключен -- это значение по умолчанию):

    CANONICAL_SUPPLIER_PARSER_ENABLED=true python manage.py check_supplier_import_corpus --dir ... --update
"""

import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from api.v1.suppliers.product_import_parsing import SupplierProductImportParseError, parse_supplier_product_import_file
from file_imports.extraction import extractor_name_for_file

DEFAULT_EXPECTED_PATH = Path(__file__).resolve().parents[3] / "file_imports" / "corpus_expected.json"
_HIGHER_IS_BETTER = ("rows", "with_price", "with_numeric_price", "with_unit", "with_sku")
_LOWER_IS_BETTER = ("duplicate_names", "unpriced_rows")
_METRICS = _HIGHER_IS_BETTER + _LOWER_IS_BETTER


def _is_number(value):
    try:
        Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return False
    return True


def measure_file(path):
    """Метрики разбора одного файла: {"error": "..."} либо счётчики из _METRICS."""
    upload = SimpleUploadedFile(path.name, path.read_bytes())
    try:
        rows = parse_supplier_product_import_file(upload)
    except SupplierProductImportParseError as error:
        return {"error": str(error)}
    name_counts = Counter(str(row["name"]).casefold() for row in rows)
    return {
        "rows": len(rows),
        "with_price": sum(1 for row in rows if row["price"]),
        "with_numeric_price": sum(1 for row in rows if row["price"] and _is_number(row["price"])),
        "with_unit": sum(1 for row in rows if row["unit_label"]),
        "with_sku": sum(1 for row in rows if row["supplier_sku"]),
        "duplicate_names": sum(1 for row in rows if name_counts[str(row["name"]).casefold()] > 1),
        "unpriced_rows": sum(1 for row in rows if not row["price"]),
    }


def compare_with_baseline(measured, expected):
    """Возвращает (regressions, improvements): списки строк-описаний."""
    regressions, improvements = [], []
    if expected is None:
        return regressions, improvements
    if "error" in measured:
        if "error" not in expected:
            regressions.append(f"ошибка разбора: {measured['error']}")
        return regressions, improvements
    if "error" in expected:
        improvements.append("файл теперь читается (в baseline была ошибка)")
        return regressions, improvements
    for metric in _METRICS:
        if metric not in expected or metric not in measured:
            continue
        worse = (
            measured[metric] > expected[metric] if metric in _LOWER_IS_BETTER else measured[metric] < expected[metric]
        )
        if worse:
            regressions.append(f"{metric}: {expected[metric]} -> {measured[metric]}")
        elif measured[metric] != expected[metric]:
            improvements.append(f"{metric}: {expected[metric]} -> {measured[metric]}")
    return regressions, improvements


class Command(BaseCommand):
    help = "Прогоняет корпус реальных прайсов через парсер импорта товаров и сверяет с baseline."

    def add_arguments(self, parser):
        parser.add_argument("--dir", required=True, help="Папка с файлами прайсов (вне git).")
        parser.add_argument("--expected", default=str(DEFAULT_EXPECTED_PATH), help="JSON с baseline-метриками.")
        parser.add_argument(
            "--update",
            action="store_true",
            help="Обновить baseline метриками измеренных файлов (остальные записи сохраняются).",
        )

    def handle(self, *args, **options):
        mode = "canonical" if getattr(settings, "CANONICAL_SUPPLIER_PARSER_ENABLED", False) else "legacy"
        self.stdout.write(f"Режим парсера: {mode} (CANONICAL_SUPPLIER_PARSER_ENABLED)")
        directory = Path(options["dir"])
        if not directory.is_dir():
            raise CommandError(f"Папка не найдена: {directory}")
        expected_path = Path(options["expected"])
        expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else {}

        files = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and extractor_name_for_file(path.name) != "unsupported"
        )
        if not files:
            raise CommandError(f"В {directory} нет файлов поддерживаемых форматов.")

        measured_all, failed = {}, False
        for path in files:
            measured = measure_file(path)
            measured_all[path.name] = measured
            regressions, improvements = compare_with_baseline(measured, expected.get(path.name))
            if "error" in measured:
                summary = f"ОШИБКА: {measured['error']}"
            else:
                summary = ", ".join(f"{metric}={measured[metric]}" for metric in _METRICS)
            status = "REGRESSION" if regressions else ("new" if path.name not in expected else "ok")
            self.stdout.write(f"[{status}] {path.name}: {summary}")
            for line in regressions:
                self.stdout.write(self.style.ERROR(f"    - {line}"))
            for line in improvements:
                self.stdout.write(self.style.SUCCESS(f"    + {line}"))
            failed = failed or bool(regressions)

        if options["update"]:
            merged = {**expected, **measured_all}
            expected_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            self.stdout.write(self.style.SUCCESS(f"Baseline записан: {expected_path}"))
            return
        if failed:
            raise CommandError("Есть регрессии относительно baseline.")
        self.stdout.write(self.style.SUCCESS("Регрессий нет."))
