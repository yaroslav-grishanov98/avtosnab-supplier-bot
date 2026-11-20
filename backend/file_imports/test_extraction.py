from django.test import SimpleTestCase

from file_imports.extraction import (
    extract_rows_from_csv,
    extract_rows_from_docx,
    extract_rows_from_txt,
    extract_rows_from_xlsx,
    extractor_name_for_file,
)
from file_imports.types import RawImportRow

_NAME_UNIT_PRICE_ALIASES = ({"название", "наименование", "товар"}, {"ед", "единица"}, {"цена"})


class ExtractorNameForFileTests(SimpleTestCase):
    def test_recognizes_all_supported_extensions(self):
        self.assertEqual(extractor_name_for_file("a.csv"), "csv")
        self.assertEqual(extractor_name_for_file("a.xlsx"), "xlsx")
        self.assertEqual(extractor_name_for_file("a.xlsm"), "xlsx")
        self.assertEqual(extractor_name_for_file("a.xls"), "xlsx")
        self.assertEqual(extractor_name_for_file("a.pdf"), "docling_pdf")
        self.assertEqual(extractor_name_for_file("a.docx"), "docx")
        self.assertEqual(extractor_name_for_file("a.doc"), "docx")
        self.assertEqual(extractor_name_for_file("a.txt"), "txt")
        self.assertEqual(extractor_name_for_file("a.png"), "image_ocr")
        self.assertEqual(extractor_name_for_file("a.zip"), "unsupported")


class ExtractRowsFromCsvTests(SimpleTestCase):
    def test_parses_semicolon_delimited_rows_with_header(self):
        content = 'Название;Ед;Цена\n"Кунжут 1кг";шт;850\n"Сахар";кг;60\n'.encode("utf-8-sig")

        rows = extract_rows_from_csv(content, header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[0], RawImportRow)
        self.assertEqual(rows[0].cells["название"], "Кунжут 1кг")
        self.assertEqual(rows[0].cells["цена"], "850")
        self.assertEqual(rows[1].cells["название"], "Сахар")

    def test_no_recognizable_header_falls_back_to_column_numbers(self):
        content = "Кунжут 1кг;шт;850\n".encode("utf-8-sig")

        rows = extract_rows_from_csv(content, header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cells["column_1"], "Кунжут 1кг")


class ExtractRowsFromTxtTests(SimpleTestCase):
    def test_delimited_text_is_parsed_as_csv(self):
        content = "Название;Ед;Цена\nКунжут;шт;850\n".encode("utf-8-sig")

        rows = extract_rows_from_txt(content, header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(rows[0].cells["название"], "Кунжут")

    def test_plain_text_becomes_one_row_per_line(self):
        content = "Кунжут 1кг 850 руб\nСахар 60 руб\n".encode("utf-8-sig")

        rows = extract_rows_from_txt(content, header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].cells["text"], "Кунжут 1кг 850 руб")


class ExtractRowsFromXlsxTests(SimpleTestCase):
    def test_parses_workbook_rows(self):
        from io import BytesIO

        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Название", "Ед", "Цена"])
        sheet.append(["Кунжут 1кг", "шт", 850])
        buffer = BytesIO()
        workbook.save(buffer)

        rows = extract_rows_from_xlsx(buffer.getvalue(), header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cells["название"], "Кунжут 1кг")
        self.assertEqual(rows[0].cells["цена"], "850")


class SectionRowTests(SimpleTestCase):
    @staticmethod
    def _extract(*, shade_group=True):
        from io import BytesIO

        from openpyxl import Workbook
        from openpyxl.styles import Alignment, PatternFill

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Номенклатура", "Цена"])
        sheet.append(["Крупы", None])
        sheet.append(["Греча 800г", 100])
        sheet.append(["Хлопья овсяные №2 800г", None])
        fill = PatternFill(patternType="solid", fgColor="FFD9D9D9")
        if shade_group:
            sheet.cell(row=2, column=1).fill = fill
        sheet.cell(row=2, column=1).alignment = Alignment(indent=0)
        for row_number in (3, 4):
            sheet.cell(row=row_number, column=1).alignment = Alignment(indent=2)
        buffer = BytesIO()
        workbook.save(buffer)
        return extract_rows_from_xlsx(buffer.getvalue(), header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

    def test_zero_indent_shaded_group_is_section(self):
        rows = self._extract()

        self.assertEqual([row.cells["номенклатура"] for row in rows], ["Греча 800г", "Хлопья овсяные №2 800г"])
        self.assertEqual(rows.metadata["section_rows_skipped"], 1)

    def test_unshaded_unpriced_product_stays_product(self):
        rows = self._extract()

        excluded = {item["row_number"] for item in rows.metadata["excluded_rows"]}
        self.assertNotIn(4, excluded)
        self.assertIn("Хлопья овсяные №2 800г", [row.cells["номенклатура"] for row in rows])


class SectionPathTests(SimpleTestCase):
    MATRIX = (
        ["Номенклатура", "Цена"],
        ["Соль 1кг", 20],
        ["Бульоны, супы", None],
        ["Бульоны", None],
        ["Mareven Professional", None],
        ["Бульон куриный", 50],
        ["Бульон говяжий", 55],
        ["Роллтон", None],
        ["Лапша быстрая", 15],
    )

    @staticmethod
    def _paths(rows):
        return {row.cells["номенклатура"]: row.source["section_path"] for row in rows}

    def _extract(self, section_rows):
        from file_imports.extraction import _rows_from_matrix

        return _rows_from_matrix(
            [list(row) for row in self.MATRIX], header_alias_groups=_NAME_UNIT_PRICE_ALIASES, section_rows=section_rows
        )

    def test_section_path_follows_indent_levels(self):
        from file_imports.structure import SectionRows

        rows = self._extract(SectionRows({3, 4, 5, 8}, levels={3: 1, 4: 2, 5: 3, 8: 3}))

        self.assertEqual(
            self._paths(rows),
            {
                "Соль 1кг": [],
                "Бульон куриный": ["Бульоны, супы", "Бульоны", "Mareven Professional"],
                "Бульон говяжий": ["Бульоны, супы", "Бульоны", "Mareven Professional"],
                "Лапша быстрая": ["Бульоны, супы", "Бульоны", "Роллтон"],
            },
        )

    def test_section_rows_stays_usable_as_plain_set(self):
        from file_imports.structure import SectionRows

        section_rows = SectionRows({3, 4})

        self.assertEqual(section_rows.levels, {})
        self.assertIn(3, section_rows)
        self.assertEqual(sorted(section_rows), [3, 4])
        self.assertEqual(len(section_rows), 2)

    def test_levels_missing(self):
        rows = self._extract({3, 4, 5, 8})

        self.assertEqual(
            self._paths(rows),
            {
                "Соль 1кг": [],
                "Бульон куриный": ["Бульоны, супы", "Бульоны", "Mareven Professional"],
                "Бульон говяжий": ["Бульоны, супы", "Бульоны", "Mareven Professional"],
                "Лапша быстрая": ["Роллтон"],
            },
        )

    def test_path_does_not_cross_sheets(self):
        from file_imports.extraction import _combine_tables
        from file_imports.structure import SectionRows

        first = [["Номенклатура", "Цена"], ["Италия", None], ["Паста", 10]]
        second = [["Номенклатура", "Цена"], ["Соль", 5]]

        combined = _combine_tables(
            [("Первый", first), ("Второй", second)],
            header_alias_groups=_NAME_UNIT_PRICE_ALIASES,
            section_rows={"Первый": SectionRows({2}, levels={2: 0})},
        )

        self.assertEqual(self._paths(combined), {"Паста": ["Италия"], "Соль": []})

    def test_hierarchical_section_rows_levels_come_from_indents(self):
        from file_imports.structure import SectionRows, _hierarchical_section_rows

        blank = (0, False, 0)
        group = (1, True, 2)
        product = (2, False, 3)
        matrix = [
            ["Номенклатура", "Цена"],
            ["Бульоны, супы", None],
            ["Бульоны", None],
            ["Бульон куриный", 50],
            ["Мясо", None],
            ["Тушёнка", 90],
            ["Пустая группа", None],  # found only by the second pass (established group format)
        ]
        formats = [
            [blank, blank],
            [(0, True, 1), blank],
            [group, blank],
            [product, blank],
            [group, blank],
            [product, blank],
            [group, blank],
        ]

        section_rows = _hierarchical_section_rows(matrix, formats)

        self.assertIsInstance(section_rows, SectionRows)
        self.assertEqual(section_rows, {2, 3, 5, 7})
        self.assertEqual(section_rows.levels, {2: 0, 3: 1, 5: 1, 7: 1})

    def test_xlsx_sibling_group_replaces_previous_one_and_keeps_outer_heading(self):
        from io import BytesIO

        from openpyxl import Workbook
        from openpyxl.styles import Alignment, PatternFill

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Номенклатура", "Цена"])
        sheet.append(["Бульоны, супы", None])
        sheet.append(["Бульоны", None])
        sheet.append(["Бульон куриный", 50])
        sheet.append(["Мясо", None])
        sheet.append(["Тушёнка", 90])
        sheet.append(["Пустая группа", None])
        fill = PatternFill(patternType="solid", fgColor="FFD9D9D9")
        for row_number, indent in ((2, 0), (3, 1), (4, 2), (5, 1), (6, 2), (7, 1)):
            sheet.cell(row=row_number, column=1).alignment = Alignment(indent=indent)
        for row_number in (2, 3, 5, 7):
            sheet.cell(row=row_number, column=1).fill = fill
        buffer = BytesIO()
        workbook.save(buffer)

        rows = extract_rows_from_xlsx(buffer.getvalue(), header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        # The "last run of headings" fallback would give ["Мясо"] for the second product.
        self.assertEqual(
            self._paths(rows),
            {
                "Бульон куриный": ["Бульоны, супы", "Бульоны"],
                "Тушёнка": ["Бульоны, супы", "Мясо"],
            },
        )

    def test_xlsx_heading_ends_at_a_product_at_its_own_or_a_shallower_indent(self):
        from io import BytesIO

        from openpyxl import Workbook
        from openpyxl.styles import Alignment, PatternFill

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Номенклатура", "Цена"])
        sheet.append(["Макароны", None])
        sheet.append(["Италия", None])
        sheet.append(["Спагетти", 100])
        sheet.append(["Макароны Шебекинские", 60])
        sheet.append(["Соль 1кг", 20])
        fill = PatternFill(patternType="solid", fgColor="FFD9D9D9")
        for row_number, indent in ((2, 0), (3, 1), (4, 2), (5, 1), (6, 0)):
            sheet.cell(row=row_number, column=1).alignment = Alignment(indent=indent)
        for row_number in (2, 3):
            sheet.cell(row=row_number, column=1).fill = fill
        buffer = BytesIO()
        workbook.save(buffer)

        rows = extract_rows_from_xlsx(buffer.getvalue(), header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(
            self._paths(rows),
            {
                "Спагетти": ["Макароны", "Италия"],
                "Макароны Шебекинские": ["Макароны"],
                "Соль 1кг": [],
            },
        )

    def test_product_indents_in_section_rows_close_deeper_headings(self):
        from file_imports.structure import SectionRows

        rows = self._extract(
            SectionRows({3, 4, 5, 8}, levels={3: 1, 4: 2, 5: 3, 8: 3}, row_levels={2: 0, 6: 4, 7: 2, 9: 4})
        )

        self.assertEqual(
            self._paths(rows),
            {
                "Соль 1кг": [],
                "Бульон куриный": ["Бульоны, супы", "Бульоны", "Mareven Professional"],
                "Бульон говяжий": ["Бульоны, супы"],
                "Лапша быстрая": ["Бульоны, супы", "Роллтон"],
            },
        )


class ExtractRowsFromDocxTests(SimpleTestCase):
    def test_parses_table_rows(self):
        from io import BytesIO

        from docx import Document

        document = Document()
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Название"
        table.rows[0].cells[1].text = "Цена"
        table.rows[1].cells[0].text = "Кунжут 1кг"
        table.rows[1].cells[1].text = "850"
        buffer = BytesIO()
        document.save(buffer)

        rows = extract_rows_from_docx(buffer.getvalue(), header_alias_groups=_NAME_UNIT_PRICE_ALIASES)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cells["название"], "Кунжут 1кг")
