import json
import tempfile
from io import BytesIO, StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from openpyxl import Workbook

from suppliers.management.commands.check_supplier_import_corpus import compare_with_baseline


def _write_xlsx(path, rows):
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    path.write_bytes(buffer.getvalue())


class CheckSupplierImportCorpusCommandTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "corpus"
        self.directory.mkdir()
        self.expected = Path(self._tmp.name) / "expected.json"
        _write_xlsx(
            self.directory / "a.xlsx",
            [["Название", "Ед. изм.", "Цена"], ["Молоко", "шт", "85"], ["Сыр", "кг", "под заказ"]],
        )

    def _run(self, *extra):
        out = StringIO()
        call_command(
            "check_supplier_import_corpus",
            "--dir",
            str(self.directory),
            "--expected",
            str(self.expected),
            *extra,
            stdout=out,
        )
        return out.getvalue()

    def test_update_writes_baseline_and_next_run_is_clean(self):
        self._run("--update")
        baseline = json.loads(self.expected.read_text(encoding="utf-8"))
        self.assertEqual(
            baseline["a.xlsx"],
            {
                "rows": 2,
                "with_price": 2,
                "with_numeric_price": 1,
                "with_unit": 2,
                "with_sku": 0,
                "duplicate_names": 0,
                "unpriced_rows": 0,
            },
        )
        self.assertIn("Регрессий нет", self._run())

    def test_drop_in_metrics_fails(self):
        self._run("--update")
        _write_xlsx(self.directory / "a.xlsx", [["Название", "Ед. изм."], ["Молоко", "шт"], ["Сыр", "кг"]])
        with self.assertRaisesMessage(CommandError, "регрессии"):
            self._run()

    def test_unreadable_file_is_a_regression_against_a_good_baseline(self):
        self._run("--update")
        (self.directory / "a.xlsx").write_bytes(b"not a spreadsheet")
        with self.assertRaises(CommandError):
            self._run()

    def test_file_without_baseline_is_reported_as_new_not_failed(self):
        self._run("--update")
        _write_xlsx(self.directory / "b.xlsx", [["Название"], ["Мука"]])
        output = self._run()
        self.assertIn("[new] b.xlsx", output)
        self.assertIn("Регрессий нет", output)

    def test_missing_directory_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("check_supplier_import_corpus", "--dir", "/nonexistent/corpus")


class CompareWithBaselineTests(SimpleTestCase):
    def test_growth_is_improvement_not_regression(self):
        measured = {"rows": 5, "with_price": 5, "with_numeric_price": 5, "with_unit": 5, "with_sku": 0}
        expected = {**measured, "with_price": 3}
        self.assertEqual(compare_with_baseline(measured, expected), ([], ["with_price: 3 -> 5"]))
