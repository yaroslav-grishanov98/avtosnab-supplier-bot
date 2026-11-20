"""
Tests for api/v1/suppliers/normalization.py.

Covers pure/algorithmic functions that need no database.
AI-dependent paths (normalize_products_with_ai, build_normalized_payload)
are excluded — they require live credentials or heavy mocking already done
in the main test file.
"""

from decimal import Decimal
from io import BytesIO

from django.test import SimpleTestCase, override_settings
from openpyxl import Workbook

from api.v1.suppliers.normalization import (
    ExtractedRow,
    _extract_multiplicity_ratio,
    _needs_ai_fallback,
    _normalize_money,
    _normalize_quantity,
    extract_price_rows_from_csv,
    extract_price_rows_from_markdown_text,
    extract_price_rows_from_txt,
    extract_price_rows_from_xlsx,
    extracted_rows_from_payload,
    extracted_rows_from_text,
    extracted_rows_to_payload,
    extractor_name_for_file,
    normalize_unit_code,
    parse_supplier_price_row,
    unit_code_aliases_from_units,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(cells, *, number=1):
    return ExtractedRow(row_number=number, cells=cells)


def _xlsx_bytes(rows):
    """Build minimal in-memory xlsx from a list of lists."""
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# extractor_name_for_file
# ---------------------------------------------------------------------------


class ExtractorNameForFileTests(SimpleTestCase):
    def test_csv(self):
        self.assertEqual(extractor_name_for_file("price.csv"), "csv")

    def test_xlsx(self):
        self.assertEqual(extractor_name_for_file("price.xlsx"), "xlsx")

    def test_pdf(self):
        self.assertEqual(extractor_name_for_file("price.pdf"), "docling_pdf")

    def test_docx(self):
        self.assertEqual(extractor_name_for_file("price.docx"), "docx")

    def test_doc(self):
        self.assertEqual(extractor_name_for_file("price.doc"), "docx")

    def test_txt(self):
        self.assertEqual(extractor_name_for_file("price.txt"), "txt")

    def test_jpg(self):
        self.assertEqual(extractor_name_for_file("price.jpg"), "image_ocr")

    def test_jpeg(self):
        self.assertEqual(extractor_name_for_file("price.jpeg"), "image_ocr")

    def test_png(self):
        self.assertEqual(extractor_name_for_file("price.png"), "image_ocr")

    def test_unsupported_extension(self):
        self.assertEqual(extractor_name_for_file("price.ods"), "unsupported")

    def test_none_returns_unsupported(self):
        self.assertEqual(extractor_name_for_file(None), "unsupported")

    def test_uppercase_extension(self):
        self.assertEqual(extractor_name_for_file("PRICE.XLSX"), "xlsx")


# ---------------------------------------------------------------------------
# normalize_unit_code
# ---------------------------------------------------------------------------


class NormalizeUnitCodeTests(SimpleTestCase):
    def test_шт(self):
        self.assertEqual(normalize_unit_code("шт"), "шт")

    def test_шт_dot(self):
        self.assertEqual(normalize_unit_code("шт."), "шт")

    def test_pcs(self):
        self.assertEqual(normalize_unit_code("pcs"), "шт")

    def test_pc(self):
        self.assertEqual(normalize_unit_code("pc"), "шт")

    def test_кг(self):
        self.assertEqual(normalize_unit_code("кг"), "кг")

    def test_kg(self):
        self.assertEqual(normalize_unit_code("kg"), "кг")

    def test_килограмм(self):
        self.assertEqual(normalize_unit_code("килограмм"), "кг")

    def test_г(self):
        self.assertEqual(normalize_unit_code("г"), "г")

    def test_гр(self):
        self.assertEqual(normalize_unit_code("гр"), "г")

    def test_л(self):
        self.assertEqual(normalize_unit_code("л"), "л")

    def test_мл(self):
        self.assertEqual(normalize_unit_code("мл"), "мл")

    def test_unknown_returns_empty(self):
        self.assertEqual(normalize_unit_code("unknown"), "")

    def test_empty_string(self):
        self.assertEqual(normalize_unit_code(""), "")

    def test_none(self):
        self.assertEqual(normalize_unit_code(None), "")

    def test_штука(self):
        self.assertEqual(normalize_unit_code("штука"), "шт")

    def test_recognizes_package_unit_from_db_aliases(self):
        aliases = unit_code_aliases_from_units([("уп", "Упаковка"), ("кор", "Коробка")])
        self.assertEqual(normalize_unit_code("уп", aliases=aliases), "уп")
        self.assertEqual(normalize_unit_code("Коробка", aliases=aliases), "кор")

    def test_without_aliases_still_only_knows_the_fixed_five(self):
        self.assertEqual(normalize_unit_code("уп"), "")


# ---------------------------------------------------------------------------
# _extract_multiplicity_ratio
# ---------------------------------------------------------------------------


class ExtractMultiplicityRatioTests(SimpleTestCase):
    def test_non_box_ratio_pattern_does_not_become_multiplicity(self):
        ratio_raw, multiplicity = _extract_multiplicity_ratio("Артикул 5/10 товар")
        self.assertEqual(ratio_raw, "5/10")
        self.assertIsNone(multiplicity)

    def test_genuine_one_over_n_ratio_still_sets_multiplicity(self):
        ratio_raw, multiplicity = _extract_multiplicity_ratio("Йогурт 1/12 в коробке")
        self.assertEqual(ratio_raw, "1/12")
        self.assertEqual(multiplicity, Decimal("12.0000"))


# ---------------------------------------------------------------------------
# _normalize_money
# ---------------------------------------------------------------------------


class NormalizeMoneyTests(SimpleTestCase):
    def test_integer_string(self):
        self.assertEqual(_normalize_money("150"), Decimal("150.00"))

    def test_comma_decimal_separator(self):
        self.assertEqual(_normalize_money("150,50"), Decimal("150.50"))

    def test_dot_decimal_separator(self):
        self.assertEqual(_normalize_money("150.50"), Decimal("150.50"))

    def test_space_as_thousands_separator(self):
        self.assertEqual(_normalize_money("1 500"), Decimal("1500.00"))

    def test_nbsp_as_thousands_separator(self):
        self.assertEqual(_normalize_money("1 500"), Decimal("1500.00"))

    def test_negative_returns_none(self):
        self.assertIsNone(_normalize_money("-100"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_normalize_money(""))

    def test_none_returns_none(self):
        self.assertIsNone(_normalize_money(None))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(_normalize_money("abc"))

    def test_rounds_to_2_decimals(self):
        self.assertEqual(_normalize_money("99.999"), Decimal("100.00"))

    def test_zero(self):
        self.assertEqual(_normalize_money("0"), Decimal("0.00"))

    def test_strips_currency_symbols(self):
        # PRICE_WITH_CURRENCY_PATTERN is used upstream; _normalize_money strips non-digits
        self.assertEqual(_normalize_money("100руб"), Decimal("100.00"))


# ---------------------------------------------------------------------------
# _normalize_quantity
# ---------------------------------------------------------------------------


class NormalizeQuantityTests(SimpleTestCase):
    def test_integer(self):
        self.assertEqual(_normalize_quantity("10"), Decimal("10.0000"))

    def test_decimal_comma(self):
        self.assertEqual(_normalize_quantity("2,5"), Decimal("2.5000"))

    def test_decimal_dot(self):
        self.assertEqual(_normalize_quantity("2.5"), Decimal("2.5000"))

    def test_none_returns_none(self):
        self.assertIsNone(_normalize_quantity(None))

    def test_empty_returns_none(self):
        self.assertIsNone(_normalize_quantity(""))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(_normalize_quantity("шт"))

    def test_negative_sign_stripped_not_rejected(self):
        # _normalize_quantity strips non-digit chars including "-", so "-5" → "5"
        self.assertEqual(_normalize_quantity("-5"), Decimal("5.0000"))


# ---------------------------------------------------------------------------
# _needs_ai_fallback
# ---------------------------------------------------------------------------


class NeedsAIFallbackTests(SimpleTestCase):
    def test_high_confidence_no_warnings_is_not_ambiguous(self):
        product = {"confidence": Decimal("0.9500"), "warnings": []}
        self.assertFalse(_needs_ai_fallback(product))

    def test_low_confidence_is_ambiguous(self):
        product = {"confidence": Decimal("0.6500"), "warnings": []}
        self.assertTrue(_needs_ai_fallback(product))

    def test_exactly_at_threshold_is_not_ambiguous(self):
        product = {"confidence": Decimal("0.8000"), "warnings": []}
        self.assertFalse(_needs_ai_fallback(product))

    def test_warnings_present_is_ambiguous(self):
        product = {"confidence": Decimal("0.9500"), "warnings": ["price_unknown"]}
        self.assertTrue(_needs_ai_fallback(product))

    def test_missing_confidence_is_ambiguous(self):
        product = {"warnings": []}
        self.assertTrue(_needs_ai_fallback(product))


# ---------------------------------------------------------------------------
# extracted_rows_to_payload / extracted_rows_from_payload
# ---------------------------------------------------------------------------


class RowSerializationTests(SimpleTestCase):
    def test_roundtrip_basic(self):
        rows = [
            ExtractedRow(row_number=1, cells={"наименование": "Болт М8", "цена": "150"}),
            ExtractedRow(row_number=2, cells={"наименование": "Гайка М10", "цена": "75"}),
        ]
        payload = extracted_rows_to_payload(rows)
        restored = extracted_rows_from_payload(payload)
        self.assertEqual(len(restored), 2)
        self.assertEqual(restored[0].row_number, 1)
        self.assertEqual(restored[0].cells["наименование"], "Болт М8")
        self.assertEqual(restored[1].row_number, 2)

    def test_empty_roundtrip(self):
        self.assertEqual(extracted_rows_to_payload([]), [])
        self.assertEqual(extracted_rows_from_payload([]), [])

    def test_to_payload_structure(self):
        rows = [ExtractedRow(row_number=5, cells={"text": "hello"})]
        payload = extracted_rows_to_payload(rows)
        self.assertEqual(payload[0]["row_number"], 5)
        self.assertEqual(payload[0]["cells"], {"text": "hello"})

    def test_from_payload_skips_invalid_rows(self):
        payload = [
            {"row_number": 1, "cells": {"name": "Valid"}},
            {"row_number": 2},  # no cells key
            "not a dict",
        ]
        result = extracted_rows_from_payload(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].cells["name"], "Valid")

    def test_from_payload_strips_empty_cell_values(self):
        payload = [{"row_number": 1, "cells": {"name": "Болт", "empty": "   "}}]
        result = extracted_rows_from_payload(payload)
        self.assertNotIn("empty", result[0].cells)


# ---------------------------------------------------------------------------
# extracted_rows_from_text
# ---------------------------------------------------------------------------


class ExtractedRowsFromTextTests(SimpleTestCase):
    def test_splits_by_line(self):
        rows = extracted_rows_from_text("line one\nline two\nline three")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].cells["text"], "line one")
        self.assertEqual(rows[2].cells["text"], "line three")

    def test_row_numbers_start_at_1(self):
        rows = extracted_rows_from_text("a\nb")
        self.assertEqual(rows[0].row_number, 1)
        self.assertEqual(rows[1].row_number, 2)

    def test_skips_empty_lines(self):
        rows = extracted_rows_from_text("a\n\n\nb")
        self.assertEqual(len(rows), 2)

    def test_empty_text_returns_empty(self):
        self.assertEqual(extracted_rows_from_text(""), [])

    def test_none_returns_empty(self):
        self.assertEqual(extracted_rows_from_text(None), [])

    def test_single_line_no_newline(self):
        rows = extracted_rows_from_text("только одна строка")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cells["text"], "только одна строка")


# ---------------------------------------------------------------------------
# extract_price_rows_from_csv
# ---------------------------------------------------------------------------


class ExtractPriceRowsFromCSVTests(SimpleTestCase):
    def _csv(self, text):
        return extract_price_rows_from_csv(text.encode("utf-8"))

    def test_semicolon_delimited_with_header(self):
        rows = self._csv("наименование;цена;ед\nБолт М8;150;шт\nГайка М10;75;шт")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].cells["наименование"], "Болт М8")
        self.assertEqual(rows[0].cells["цена"], "150")
        self.assertEqual(rows[0].cells["ед"], "шт")

    def test_comma_delimited_with_header(self):
        rows = self._csv("name,price,unit\nBolt,150,pcs\nNut,75,pcs")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].cells["name"], "Bolt")

    def test_skips_empty_rows(self):
        rows = self._csv("наименование;цена\nБолт;150\n\nГайка;75")
        self.assertEqual(len(rows), 2)

    def test_no_header_uses_column_names(self):
        # No known aliases in first row → first row is data, all rows returned as column_N
        rows = self._csv("Болт М8;150;шт\nГайка М10;75;шт")
        self.assertEqual(len(rows), 2)
        first_key = list(rows[0].cells.keys())[0]
        self.assertIn("column_", first_key)

    def test_returns_row_numbers(self):
        rows = self._csv("наименование;цена\nБолт;150\nГайка;75")
        self.assertEqual(rows[0].row_number, 2)
        self.assertEqual(rows[1].row_number, 3)

    def test_cp1251_encoding(self):
        text = "наименование;цена\nБолт М8;150"
        rows = extract_price_rows_from_csv(text.encode("cp1251"))
        self.assertEqual(rows[0].cells["наименование"], "Болт М8")


# ---------------------------------------------------------------------------
# extract_price_rows_from_xlsx
# ---------------------------------------------------------------------------


class ExtractPriceRowsFromXLSXTests(SimpleTestCase):
    def test_reads_named_columns(self):
        data = _xlsx_bytes(
            [
                ["наименование", "цена", "ед"],
                ["Болт М8", 150, "шт"],
                ["Гайка М10", 75, "шт"],
            ]
        )
        rows = extract_price_rows_from_xlsx(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].cells["наименование"], "Болт М8")
        self.assertEqual(rows[0].cells["цена"], "150")

    def test_skips_empty_rows(self):
        data = _xlsx_bytes(
            [
                ["наименование", "цена"],
                ["Болт М8", 150],
                [None, None],
                ["Гайка М10", 75],
            ]
        )
        rows = extract_price_rows_from_xlsx(data)
        self.assertEqual(len(rows), 2)

    def test_single_data_row(self):
        data = _xlsx_bytes(
            [
                ["наименование", "цена"],
                ["Болт М8", 99],
            ]
        )
        rows = extract_price_rows_from_xlsx(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cells["наименование"], "Болт М8")

    def test_row_numbers_reflect_physical_row(self):
        data = _xlsx_bytes(
            [
                ["наименование", "цена"],
                ["Болт М8", 150],
            ]
        )
        rows = extract_price_rows_from_xlsx(data)
        self.assertEqual(rows[0].row_number, 2)


# ---------------------------------------------------------------------------
# extract_price_rows_from_txt
# ---------------------------------------------------------------------------


class ExtractPriceRowsFromTXTTests(SimpleTestCase):
    def _txt(self, text):
        return extract_price_rows_from_txt(text.encode("utf-8"))

    def test_plain_text_produces_text_cells(self):
        rows = self._txt("Болт М8 150 руб шт\nГайка М10 75 руб шт")
        self.assertEqual(len(rows), 2)
        self.assertIn("text", rows[0].cells)

    def test_csv_like_txt_parsed_as_csv(self):
        rows = self._txt("наименование;цена;ед\nБолт М8;150;шт\nГайка М10;75;шт")
        self.assertEqual(len(rows), 2)
        self.assertIn("наименование", rows[0].cells)

    def test_empty_lines_skipped(self):
        rows = self._txt("line1\n\nline2")
        self.assertEqual(len(rows), 2)


# ---------------------------------------------------------------------------
# extract_price_rows_from_markdown_text
# ---------------------------------------------------------------------------


class ExtractPriceRowsFromMarkdownTests(SimpleTestCase):
    def test_parses_markdown_table(self):
        md = "| наименование | цена | ед |\n| --- | --- | --- |\n| Болт М8 | 150 | шт |\n| Гайка М10 | 75 | шт |\n"
        rows = extract_price_rows_from_markdown_text(md)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].cells["наименование"], "Болт М8")
        self.assertEqual(rows[0].cells["цена"], "150")

    def test_separator_row_excluded(self):
        # Use known aliases so first row is detected as header; separator skipped; 1 data row
        md = "| наименование | цена |\n|---|---|\n| Болт М8 | 150 |"
        rows = extract_price_rows_from_markdown_text(md)
        self.assertEqual(len(rows), 1)

    def test_no_table_falls_back_to_text_rows(self):
        text = "Болт М8 150 руб\nГайка М10 75 руб"
        rows = extract_price_rows_from_markdown_text(text)
        self.assertEqual(len(rows), 2)
        self.assertIn("text", rows[0].cells)

    def test_empty_markdown_returns_empty(self):
        self.assertEqual(extract_price_rows_from_markdown_text(""), [])


# ---------------------------------------------------------------------------
# parse_supplier_price_row
# ---------------------------------------------------------------------------


class ParseSupplierPriceRowTests(SimpleTestCase):
    def test_full_structured_row_high_confidence(self):
        row = _row({"наименование": "Болт М8", "цена": "150", "ед": "шт"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["raw_name"], "Болт М8")
        self.assertEqual(product["price"], Decimal("150.00"))
        self.assertEqual(product["unit_code"], "шт")
        self.assertEqual(product["confidence"], Decimal("0.9500"))
        self.assertEqual(product["warnings"], [])

    def test_missing_price_sets_warning(self):
        row = _row({"наименование": "Болт М8", "ед": "шт"})
        product = parse_supplier_price_row(row)
        self.assertIn("price_unknown", product["warnings"])
        # only price_unknown → 0.7600; multiple warnings → 0.6500
        self.assertEqual(product["confidence"], Decimal("0.7600"))

    def test_missing_unit_sets_warning(self):
        row = _row({"наименование": "Болт М8", "цена": "100"})
        product = parse_supplier_price_row(row)
        self.assertIn("unit_unknown", product["warnings"])

    def test_only_price_unknown_gives_medium_confidence(self):
        row = _row({"наименование": "Болт М8", "ед": "шт"})
        product = parse_supplier_price_row(row)
        # price_unknown only → 0.7600
        if product["warnings"] == ["price_unknown"]:
            self.assertEqual(product["confidence"], Decimal("0.7600"))

    def test_price_from_text_cell(self):
        # Use "Болт 150 руб" — avoid "М8 150" which would be parsed as "8150"
        row = _row({"text": "Болт 150 руб шт"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["price"], Decimal("150.00"))

    def test_price_recognizes_full_word_ruble_forms(self):
        """2026-08-03: Витя — "Чай улун 100г 300 рублей" уходил в needs_review с
        price=None. PRICE_WITH_CURRENCY_PATTERN распознавал только руб/руб./₽/rub,
        не полные словоформы (рубль/рубля/рублей/рублях и т.п.)."""
        for text, expected in [
            ("Чай улун 300 рублей шт", Decimal("300.00")),
            ("Чай улун 300 рубля шт", Decimal("300.00")),
            ("Чай улун 300 рубль шт", Decimal("300.00")),
            ("Чай улун 300 рублях шт", Decimal("300.00")),
        ]:
            row = _row({"text": text})
            product = parse_supplier_price_row(row)
            self.assertEqual(product["price"], expected, text)

    def test_price_recognizes_ruble_sign_without_space(self):
        """Тот же баг попутно вскрыл второй: ₽ как символ (не буквенное
        rub/руб) никогда не матчился — \\b не считается границей слова между
        небуквенным ₽ и небуквенным пробелом/концом строки."""
        row = _row({"text": "Чай улун 300₽ шт"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["price"], Decimal("300.00"))

    def test_unit_extracted_from_name(self):
        # Unit must be standalone (space-separated) for _extract_sale_unit to pick it up
        row = _row({"наименование": "Масло моторное 5 л", "цена": "500"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["unit_code"], "л")

    def test_brand_extracted_from_name(self):
        row = _row({"наименование": 'Масло ТМ "Лукойл" 5л', "цена": "500", "ед": "л"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["brand"], "Лукойл")

    def test_category_path_extracted(self):
        row = _row(
            {
                "наименование": "Болт М8",
                "цена": "100",
                "ед": "шт",
                "группа товара": "Крепёж",
                "подгруппа товара": "Болты",
            }
        )
        product = parse_supplier_price_row(row)
        self.assertIn("Крепёж", product["category_path"])
        self.assertIn("Болты", product["category_path"])

    def test_min_order_qty_parsed(self):
        row = _row({"наименование": "Болт М8", "цена": "100", "ед": "шт", "мин заказ": "5"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["min_order_qty"], Decimal("5.0000"))

    def test_multiplicity_parsed(self):
        row = _row({"наименование": "Болт М8", "цена": "100", "ед": "шт", "кратность": "10"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["multiplicity"], Decimal("10.0000"))

    def test_supplier_sku_parsed(self):
        row = _row({"наименование": "Болт М8", "цена": "100", "ед": "шт", "артикул": "AB-123"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["supplier_sku"], "AB-123")

    def test_row_number_preserved(self):
        row = _row({"наименование": "Болт М8", "цена": "100", "ед": "шт"}, number=42)
        product = parse_supplier_price_row(row)
        self.assertEqual(product["row_number"], 42)

    def test_currency_always_rub(self):
        row = _row({"наименование": "Болт М8", "цена": "100", "ед": "шт"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["currency"], "RUB")

    def test_normalized_name_is_lowercase(self):
        row = _row({"наименование": "Болт М8", "цена": "100", "ед": "шт"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["normalized_name"], product["normalized_name"].lower())

    def test_package_weight_extracted(self):
        row = _row({"наименование": "Масло 5кг", "цена": "1000", "ед": "кг"})
        product = parse_supplier_price_row(row)
        self.assertIsNotNone(product["weight"])
        self.assertEqual(product["weight"], Decimal("5.0000"))
        self.assertEqual(product["weight_unit"], "кг")

    def test_package_volume_extracted(self):
        row = _row({"наименование": "Антифриз 1л", "цена": "200", "ед": "л"})
        product = parse_supplier_price_row(row)
        self.assertIsNotNone(product["volume"])
        self.assertEqual(product["volume"], Decimal("1.0000"))
        self.assertEqual(product["volume_unit"], "л")

    def test_zero_price_stays_zero_even_with_ruble_amount_in_another_cell(self):
        row = _row({"наименование": "Промо-товар", "цена": "0", "примечание": "доставка от 500 руб"})
        product = parse_supplier_price_row(row)
        self.assertEqual(product["price"], Decimal("0.00"))

    def test_resolves_package_unit_when_unit_aliases_given(self):
        row = _row({"наименование": "Молоко", "цена": "50", "ед": "уп"})
        unit_aliases = unit_code_aliases_from_units([("уп", "Упаковка")])
        product = parse_supplier_price_row(row, unit_aliases=unit_aliases)
        self.assertEqual(product["unit_code"], "уп")


# ---------------------------------------------------------------------------
# SupplierPriceAIError.system_error property
# ---------------------------------------------------------------------------


class SupplierPriceAIErrorSystemErrorTests(SimpleTestCase):
    def _err(self, code):
        from api.v1.suppliers.normalization import SupplierPriceAIError

        return SupplierPriceAIError("msg", code=code)

    def test_credentials_not_configured_is_system_error(self):
        from api.v1.suppliers.normalization import AI_ERROR_CREDENTIALS_NOT_CONFIGURED

        self.assertTrue(self._err(AI_ERROR_CREDENTIALS_NOT_CONFIGURED).system_error)

    def test_unauthorized_is_system_error(self):
        from api.v1.suppliers.normalization import AI_ERROR_UNAUTHORIZED_401

        self.assertTrue(self._err(AI_ERROR_UNAUTHORIZED_401).system_error)

    def test_forbidden_is_system_error(self):
        from api.v1.suppliers.normalization import AI_ERROR_FORBIDDEN_403

        self.assertTrue(self._err(AI_ERROR_FORBIDDEN_403).system_error)

    def test_timeout_is_system_error(self):
        from api.v1.suppliers.normalization import AI_ERROR_TIMEOUT

        self.assertTrue(self._err(AI_ERROR_TIMEOUT).system_error)

    def test_invalid_json_is_system_error(self):
        from api.v1.suppliers.normalization import AI_ERROR_INVALID_JSON

        self.assertTrue(self._err(AI_ERROR_INVALID_JSON).system_error)

    def test_network_error_is_system_error(self):
        from api.v1.suppliers.normalization import AI_ERROR_NETWORK

        self.assertTrue(self._err(AI_ERROR_NETWORK).system_error)

    def test_custom_code_is_not_system_error(self):
        from api.v1.suppliers.normalization import SupplierPriceAIError

        err = SupplierPriceAIError("msg", code="unknown_custom")
        self.assertFalse(err.system_error)

    def test_retryable_flag_stored(self):
        from api.v1.suppliers.normalization import AI_ERROR_TIMEOUT, SupplierPriceAIError

        err = SupplierPriceAIError("msg", code=AI_ERROR_TIMEOUT, retryable=True)
        self.assertTrue(err.retryable)

    def test_not_retryable_by_default(self):
        from api.v1.suppliers.normalization import SupplierPriceAIError

        err = SupplierPriceAIError("msg", code="x")
        self.assertFalse(err.retryable)


# ---------------------------------------------------------------------------
# _ai_authorization_header
# ---------------------------------------------------------------------------


class AIAuthorizationHeaderTests(SimpleTestCase):
    def test_returns_bearer_from_api_key(self):
        from api.v1.suppliers.normalization import _ai_authorization_header

        with override_settings(AI_NORMALIZATION_API_KEY="sk-test", AI_NORMALIZATION_AUTHORIZATION=""):
            header = _ai_authorization_header()
        self.assertEqual(header, "Bearer sk-test")

    def test_returns_explicit_authorization_header(self):
        from api.v1.suppliers.normalization import _ai_authorization_header

        with override_settings(AI_NORMALIZATION_AUTHORIZATION="Bearer my-token", AI_NORMALIZATION_API_KEY=""):
            header = _ai_authorization_header()
        self.assertEqual(header, "Bearer my-token")

    def test_api_key_already_bearer_prefixed(self):
        from api.v1.suppliers.normalization import _ai_authorization_header

        with override_settings(AI_NORMALIZATION_API_KEY="Bearer sk-test", AI_NORMALIZATION_AUTHORIZATION=""):
            header = _ai_authorization_header()
        self.assertEqual(header, "Bearer sk-test")

    def test_raises_when_no_credentials(self):
        from api.v1.suppliers.normalization import (
            AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
            SupplierPriceAIError,
            _ai_authorization_header,
        )

        with override_settings(AI_NORMALIZATION_API_KEY="", AI_NORMALIZATION_AUTHORIZATION=""):
            with self.assertRaises(SupplierPriceAIError) as ctx:
                _ai_authorization_header()
        self.assertEqual(ctx.exception.code, AI_ERROR_CREDENTIALS_NOT_CONFIGURED)


# ---------------------------------------------------------------------------
# build_normalized_payload with use_ai=False (no DB, mock upload)
# ---------------------------------------------------------------------------


class BuildNormalizedPayloadNoAITests(SimpleTestCase):
    # TASK-101a: build_normalized_payload now always queries the live Unit
    # table once (active_unit_codes_and_names()) to build unit_aliases, so
    # this class — otherwise DB-free, using a MagicMock upload — needs read
    # access to the default DB. No writes happen here, so no rollback/cleanup
    # concern.
    databases = {"default"}

    def _make_upload(self):
        from unittest.mock import MagicMock

        upload = MagicMock()
        upload.id = "00000000-0000-0000-0000-000000000001"
        upload.supplier_id = "00000000-0000-0000-0000-000000000002"
        upload.source_channel = "telegram"
        upload.supplier.display_name = "Test Supplier"
        return upload

    def test_returns_payload_and_diagnostics(self):
        from api.v1.suppliers.normalization import ExtractedRow, build_normalized_payload

        upload = self._make_upload()
        rows = [
            ExtractedRow(row_number=1, cells={"наименование": "Болт М8", "цена": "150", "ед": "шт"}),
            ExtractedRow(row_number=2, cells={"наименование": "Гайка М10", "цена": "75", "ед": "шт"}),
        ]
        payload, diagnostics = build_normalized_payload(upload=upload, rows=rows, use_ai=False)
        self.assertEqual(len(payload["products"]), 2)
        self.assertEqual(diagnostics["ai_mode"], "off")
        self.assertEqual(diagnostics["rows_total"], 2)
        self.assertEqual(diagnostics["rows_ai_requested"], 0)

    def test_payload_has_required_keys(self):
        from api.v1.suppliers.normalization import ExtractedRow, build_normalized_payload

        upload = self._make_upload()
        rows = [ExtractedRow(row_number=1, cells={"наименование": "Болт М8", "цена": "150", "ед": "шт"})]
        payload, _ = build_normalized_payload(upload=upload, rows=rows, use_ai=False)
        for key in ("schema_version", "processing_version", "supplier_id", "upload_id", "products"):
            self.assertIn(key, payload)

    def test_each_product_has_row_number(self):
        from api.v1.suppliers.normalization import ExtractedRow, build_normalized_payload

        upload = self._make_upload()
        rows = [
            ExtractedRow(row_number=5, cells={"наименование": "Болт М8", "цена": "150", "ед": "шт"}),
        ]
        payload, _ = build_normalized_payload(upload=upload, rows=rows, use_ai=False)
        self.assertEqual(payload["products"][0]["row_number"], 5)

    def test_full_mode_requires_confirm_full(self):
        from api.v1.suppliers.normalization import ExtractedRow, build_normalized_payload

        upload = self._make_upload()
        rows = [ExtractedRow(row_number=1, cells={"наименование": "Болт М8", "цена": "150", "ед": "шт"})]
        with self.assertRaises(ValueError):
            build_normalized_payload(upload=upload, rows=rows, use_ai=True, ai_mode="full", confirm_full=False)

    def test_empty_rows_returns_empty_products(self):
        from api.v1.suppliers.normalization import build_normalized_payload

        upload = self._make_upload()
        payload, diagnostics = build_normalized_payload(upload=upload, rows=[], use_ai=False)
        self.assertEqual(payload["products"], [])
        self.assertEqual(diagnostics["rows_total"], 0)
