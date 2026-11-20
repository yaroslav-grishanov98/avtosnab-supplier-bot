from io import BytesIO

from django.test import SimpleTestCase, override_settings
from openpyxl import Workbook

from file_imports.extraction import extract_rows_from_xlsx
from file_imports.quality import (
    WARNING_LOW_PRICE_SHARE,
    is_numeric_name,
    price_share_warning,
    sheet_product_score,
)

_ALIASES = ({"название", "наименование", "товар"}, {"ед", "единица"}, {"цена"})


def _xlsx(sheets):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets:
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class NumericNameTests(SimpleTestCase):
    def test_number_like_names_are_flagged(self):
        for name in ("1250", "12,5", " 3 400.50 ", "2024-01-15", "100 / 5"):
            self.assertTrue(is_numeric_name(name), name)

    def test_real_names_are_not_flagged(self):
        for name in ("Мука 5 кг", "7UP", "Сок 1л", "", None, "-", "№"):
            self.assertFalse(is_numeric_name(name), name)


class PriceShareWarningTests(SimpleTestCase):
    def test_below_threshold_warns(self):
        warning = price_share_warning(10, 4)
        self.assertEqual(warning["code"], WARNING_LOW_PRICE_SHARE)
        self.assertEqual((warning["rows_total"], warning["rows_with_price"]), (10, 4))

    def test_at_threshold_is_fine(self):
        self.assertIsNone(price_share_warning(10, 5))

    def test_tiny_files_never_warn(self):
        self.assertIsNone(price_share_warning(4, 0))

    @override_settings(SUPPLIER_IMPORT_MIN_PRICE_SHARE=0.3)
    def test_threshold_is_configurable(self):
        self.assertIsNone(price_share_warning(10, 4))
        self.assertIsNotNone(price_share_warning(10, 2))


class SheetSelectionTests(SimpleTestCase):
    def test_score_counts_name_plus_number_rows(self):
        self.assertEqual(sheet_product_score([["Мука", "50"], ["Сахар", "60"], ["Прайс"], ["", ""]]), 2)

    def test_empty_first_sheet_is_skipped_for_headerless_workbook(self):
        content = _xlsx([("Пусто", []), ("Товары", [["Мука 5 кг", "шт", 500], ["Сахар 1 кг", "шт", 70]])])

        rows = extract_rows_from_xlsx(content, header_alias_groups=_ALIASES)

        self.assertEqual([row.cells["column_1"] for row in rows], ["Мука 5 кг", "Сахар 1 кг"])
        self.assertEqual(rows.metadata["sheet_name"], "Товары")
        self.assertEqual(rows[0].source["sheet_name"], "Товары")

    def test_headerless_workbook_picks_sheet_with_most_priced_rows(self):
        content = _xlsx(
            [
                ("Обложка", [["Прайс-лист ООО Ромашка"], ["Телефон", "8 800 555 35 35"]]),
                ("Товары", [[f"Товар {chr(1040 + i)}", "шт", 10 + i] for i in range(6)]),
            ]
        )

        rows = extract_rows_from_xlsx(content, header_alias_groups=_ALIASES)

        self.assertEqual(len(rows), 6)
        self.assertEqual(rows.metadata["sheet_name"], "Товары")
        self.assertTrue(any("Обложка" in warning for warning in rows.metadata["warnings"]))

    def test_sheets_with_recognised_headers_are_still_combined(self):
        header = ["Название", "Ед", "Цена"]
        content = _xlsx(
            [
                ("Пусто", []),
                ("Бакалея", [header, ["Мука", "кг", 50]]),
                ("Овощи", [header, ["Лук", "кг", 30]]),
            ]
        )

        rows = extract_rows_from_xlsx(content, header_alias_groups=_ALIASES)

        self.assertEqual([row.cells["название"] for row in rows], ["Мука", "Лук"])
