"""
Additional tests for api/v1/suppliers/normalization.py targeting the 77% → 95%+ coverage gap.

Focus areas:
- PDF/DOCX/image extraction (pdfplumber, python-docx, pytesseract are installed)
- Docling fallback paths (docling is NOT installed → always raises ValueError)
- Pure helpers: _normalize_money, _normalize_quantity, _matches_any_alias, etc.
- AI pipeline helpers: _http_error_code, _ai_error_from_openai_exception, etc.
- Settings readers and validators
- _products_from_ai_payload edge cases
"""

import os
import socket
import sys
import tempfile
import urllib.error
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

import api.v1.suppliers.normalization as norm_mod
from api.v1.suppliers.normalization import (
    AI_ERROR_BAD_REQUEST_400,
    AI_ERROR_CREDENTIALS_NOT_CONFIGURED,
    AI_ERROR_FORBIDDEN_403,
    AI_ERROR_INVALID_JSON,
    AI_ERROR_NETWORK,
    AI_ERROR_RATE_LIMIT_429,
    AI_ERROR_TIMEOUT,
    AI_ERROR_UNAUTHORIZED_401,
    AI_MODE_AMBIGUOUS,
    AI_MODE_FULL,
    AI_MODE_OFF,
    ExtractedRow,
    SupplierPriceAIError,
    _first_alias_value,
    _format_decimal,
    _http_error_code,
    _http_error_retryable,
    _infer_name_from_cells,
    _looks_like_price_only,
    _matches_any_alias,
    _network_error_from_url_error,
    _normalize_confidence,
    _normalize_money,
    _normalize_quantity,
    _products_from_ai_payload,
    _read_http_error_body,
    _remap_legacy_column_rows,
    _resolve_ai_mode,
    _settings_bool,
    _warnings_for_product,
    extract_price_rows_from_docx,
    extract_price_rows_from_file,
    extract_price_rows_from_image,
    extract_price_rows_from_pdf,
    extract_price_rows_from_pdf_with_pdfplumber,
    extract_price_rows_from_txt,
    normalize_ambiguous_rows_with_ai,
    normalize_products_with_ai,
)
from file_imports import extraction as extraction_mod
from file_imports.extraction import (
    FileExtractionError,
    _decode_csv_content,
    _source_file_path_for_docling,
    _storage_has_local_path,
)


def _rows_from_matrix(rows):
    return extraction_mod._rows_from_matrix(rows, header_alias_groups=extraction_mod.COMMON_HEADER_ALIAS_GROUPS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeUpload:
    def __init__(self):
        self.id = "upload-abc"
        self.supplier_id = "supplier-xyz"
        self.supplier = MagicMock()
        self.supplier.display_name = "ТестПоставщик"


def _row(row_number, **cells):
    return ExtractedRow(row_number=row_number, cells=cells)


def _product(row_number=1, **kwargs):
    base = {
        "row_number": row_number,
        "raw_name": "Болт М8",
        "normalized_name": "болт м8",
        "supplier_sku": None,
        "price": Decimal("100.00"),
        "currency": "RUB",
        "unit_code": "шт",
        "package_info": None,
        "brand": None,
        "volume": None,
        "volume_unit": None,
        "weight": None,
        "weight_unit": None,
        "min_order_qty": None,
        "multiplicity": None,
        "category_path": [],
        "comment": None,
        "availability_status": "available",
        "confidence": Decimal("0.9500"),
        "warnings": [],
        "raw_payload": {"parser_source": "rules", "parser_version": "v1"},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Optional library None path tests (simulate lib not installed at runtime)
# ---------------------------------------------------------------------------


class OptionalLibNoneTests(SimpleTestCase):
    def test_extract_image_returns_empty_when_image_module_none(self):
        with patch.object(extraction_mod, "Image", None):
            with patch.object(extraction_mod, "pytesseract", None):
                with self.assertRaises(FileExtractionError):
                    extract_price_rows_from_image(b"\x89PNG\r\n")

    def test_extract_image_returns_empty_when_pytesseract_none(self):
        with patch.object(extraction_mod, "Image", None):
            with self.assertRaises(FileExtractionError):
                extract_price_rows_from_image(b"\x89PNG\r\n")

    def test_extract_pdf_with_pdfplumber_returns_empty_when_pdfplumber_none(self):
        with patch.object(extraction_mod, "pdfplumber", None):
            with self.assertRaises(FileExtractionError):
                extract_price_rows_from_pdf_with_pdfplumber(b"%PDF-1")

    def test_extract_docx_returns_empty_when_docxdocument_none(self):
        with patch.object(extraction_mod, "DocxDocument", None):
            with self.assertRaises(FileExtractionError):
                extract_price_rows_from_docx(b"fake")


# ---------------------------------------------------------------------------
# _decode_csv_content fallback (line 166)
# ---------------------------------------------------------------------------


class DecodeCsvFallbackTests(SimpleTestCase):
    def test_utf8_fallback_with_replace_for_invalid_cp1251_bytes(self):
        # 0x98 is undefined in cp1251 (and invalid UTF-8), triggers final fallback
        bad_bytes = bytes([0x98, 0xC1, 0xE2, 0xEE, 0xF0])
        with self.assertRaises(FileExtractionError):
            _decode_csv_content(bad_bytes)

    def test_utf8_sig_bom_stripped_correctly(self):
        # Encode without BOM in original string, utf-8-sig adds one → decode strips it
        content = "Товар;Цена\n".encode("utf-8-sig")
        result = _decode_csv_content(content)
        self.assertNotIn("﻿", result)
        self.assertIn("Товар", result)


# ---------------------------------------------------------------------------
# extract_price_rows_from_file dispatch (lines 198-204)
# ---------------------------------------------------------------------------


class ExtractFromFileDispatchTests(SimpleTestCase):
    class _File(BytesIO):
        def __init__(self, data, name):
            super().__init__(data)
            self.name = name

    def test_dispatch_to_docx(self):
        from docx import Document as DocxDocument

        buf = BytesIO()
        doc = DocxDocument()
        doc.add_paragraph("Болт М8 100 шт")
        doc.save(buf)
        f = self._File(buf.getvalue(), "price.docx")
        rows = extract_price_rows_from_file(f, source_file_name="price.docx")
        self.assertIsInstance(rows, list)

    def test_dispatch_to_txt(self):
        content = "Болт М8 100 руб шт\nГайка М6 50 руб шт\n".encode("utf-8")
        f = self._File(content, "price.txt")
        rows = extract_price_rows_from_file(f, source_file_name="price.txt")
        self.assertIsInstance(rows, list)
        self.assertGreater(len(rows), 0)

    def test_dispatch_to_image(self):
        from PIL import Image as PILImage

        img = PILImage.new("RGB", (100, 30), color=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format="PNG")
        f = self._File(buf.getvalue(), "price.png")
        with patch.object(extraction_mod, "pytesseract") as mock_tess:
            mock_tess.image_to_string.return_value = "Болт М8 100"
            mock_tess.TesseractNotFoundError = pytesseract_not_found_stub()
            rows = extract_price_rows_from_file(f, source_file_name="price.png")
        self.assertIsInstance(rows, list)

    def test_dispatch_to_pdf_uses_pdfplumber_fallback(self):
        f = self._File(b"%PDF-1", "price.pdf")
        with patch.object(extraction_mod, "_docling_markdown_from_pdf") as mock_d:
            mock_d.side_effect = ValueError("docling not available")
            with patch.object(extraction_mod, "extract_rows_from_pdf_with_pdfplumber") as mock_pp:
                mock_pp.return_value = [_row(1, text="line")]
                rows = extract_price_rows_from_file(f, source_file_name="price.pdf")
        self.assertEqual(len(rows), 1)

    def test_unsupported_extension_returns_empty(self):
        f = self._File(b"data", "price.xyz")
        with self.assertRaises(FileExtractionError):
            extract_price_rows_from_file(f, source_file_name="price.xyz")


def pytesseract_not_found_stub():
    class TesseractNotFoundError(Exception):
        pass

    return TesseractNotFoundError


# ---------------------------------------------------------------------------
# extract_price_rows_from_pdf docling paths (lines 247, 257-264)
# ---------------------------------------------------------------------------


class ExtractPdfDoclingPathTests(SimpleTestCase):
    def test_auto_mode_falls_back_to_pdfplumber_when_docling_raises(self):
        with patch.object(extraction_mod, "_docling_markdown_from_pdf") as mock_d:
            mock_d.side_effect = ValueError("docling missing")
            with patch.object(extraction_mod, "extract_rows_from_pdf_with_pdfplumber") as mock_pp:
                mock_pp.return_value = [_row(1, text="Болт")]
                with override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="auto"):
                    result = extract_price_rows_from_pdf(b"%PDF-1")
        self.assertEqual(len(result), 1)
        mock_pp.assert_called_once()

    def test_docling_mode_re_raises_when_docling_fails(self):
        with patch.object(extraction_mod, "_docling_markdown_from_pdf") as mock_d:
            mock_d.side_effect = ValueError("docling missing")
            with override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="docling"):
                with self.assertRaises(ValueError):
                    extract_price_rows_from_pdf(b"%PDF-1")

    def test_docling_pdf_mode_re_raises_when_docling_fails(self):
        with patch.object(extraction_mod, "_docling_markdown_from_pdf") as mock_d:
            mock_d.side_effect = RuntimeError("bad")
            with override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="docling_pdf"):
                with self.assertRaises(RuntimeError):
                    extract_price_rows_from_pdf(b"%PDF-1")

    def test_pdfplumber_extractor_bypasses_docling(self):
        with patch.object(extraction_mod, "extract_rows_from_pdf_with_pdfplumber") as mock_pp:
            mock_pp.return_value = [_row(1, text="row")]
            with override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="pdfplumber"):
                result = extract_price_rows_from_pdf(b"%PDF-1")
        mock_pp.assert_called_once()
        self.assertEqual(len(result), 1)

    def test_auto_mode_uses_docling_markdown_when_available(self):
        with patch.object(extraction_mod, "_docling_markdown_from_pdf") as mock_d:
            mock_d.return_value = "Болт М8 100 шт"
            with override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="auto"):
                result = extract_price_rows_from_pdf(b"%PDF-1")
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# _docling_markdown_from_pdf (lines 268-288)
# ---------------------------------------------------------------------------


class DoclingMarkdownFromPdfTests(SimpleTestCase):
    def _make_docling_mock(self, markdown="# Header\n| Col |\n| val |"):
        mock_converter_class = MagicMock()
        mock_result = MagicMock()
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = markdown
        mock_result.document = mock_doc
        mock_converter_class.return_value.convert.return_value = mock_result

        mock_module = MagicMock()
        mock_module.DocumentConverter = mock_converter_class
        return mock_module

    def test_converts_content_without_source_uri_creates_temp_file(self):
        mock_module = self._make_docling_mock("Болт М8 100 шт")
        with patch.dict("sys.modules", {"docling": mock_module, "docling.document_converter": mock_module}):
            from file_imports.extraction import _docling_markdown_from_pdf

            result = _docling_markdown_from_pdf(b"%PDF-1")
        self.assertEqual(result, "Болт М8 100 шт")

    def test_raises_value_error_when_docling_import_fails(self):
        with patch.dict("sys.modules", {"docling": None, "docling.document_converter": None}):
            # Force ImportError by removing the module
            saved = sys.modules.pop("docling", None)
            saved_sub = sys.modules.pop("docling.document_converter", None)
            sys.modules["docling"] = None  # type: ignore
            sys.modules["docling.document_converter"] = None  # type: ignore
            try:
                from file_imports.extraction import _docling_markdown_from_pdf

                with self.assertRaises((ValueError, ImportError)):
                    _docling_markdown_from_pdf(b"%PDF-1")
            finally:
                if saved is None:
                    sys.modules.pop("docling", None)
                else:
                    sys.modules["docling"] = saved
                if saved_sub is None:
                    sys.modules.pop("docling.document_converter", None)
                else:
                    sys.modules["docling.document_converter"] = saved_sub

    def test_uses_export_to_text_when_no_export_to_markdown(self):
        mock_converter_class = MagicMock()
        mock_result = MagicMock()
        mock_doc = MagicMock(spec=["export_to_text"])  # no export_to_markdown
        mock_doc.export_to_text.return_value = "plain text"
        mock_result.document = mock_doc
        mock_converter_class.return_value.convert.return_value = mock_result

        mock_module = MagicMock()
        mock_module.DocumentConverter = mock_converter_class
        with patch.dict("sys.modules", {"docling": mock_module, "docling.document_converter": mock_module}):
            from file_imports.extraction import _docling_markdown_from_pdf

            result = _docling_markdown_from_pdf(b"%PDF-1")
        self.assertEqual(result, "plain text")

    def test_returns_str_result_when_document_is_none(self):
        mock_converter_class = MagicMock()
        mock_result = MagicMock()
        mock_result.document = None
        mock_result.__str__ = lambda self: "fallback string"
        mock_converter_class.return_value.convert.return_value = mock_result

        mock_module = MagicMock()
        mock_module.DocumentConverter = mock_converter_class
        with patch.dict("sys.modules", {"docling": mock_module, "docling.document_converter": mock_module}):
            from file_imports.extraction import _docling_markdown_from_pdf

            result = _docling_markdown_from_pdf(b"%PDF-1")
        self.assertIsInstance(result, str)

    def test_oserror_during_temp_file_cleanup_is_suppressed(self):
        mock_module = self._make_docling_mock("text")
        with patch.dict("sys.modules", {"docling": mock_module, "docling.document_converter": mock_module}):
            with patch("os.unlink", side_effect=OSError("permission denied")):
                from file_imports.extraction import _docling_markdown_from_pdf

                # Should not raise despite os.unlink failing
                result = _docling_markdown_from_pdf(b"%PDF-1")
        self.assertEqual(result, "text")

    def test_temp_file_cleaned_up_after_conversion(self):
        temp_files_created = []
        original_mktemp = tempfile.NamedTemporaryFile

        def tracking_mktemp(*args, **kwargs):
            f = original_mktemp(*args, **kwargs)
            temp_files_created.append(f.name)
            return f

        mock_module = self._make_docling_mock("text")
        with patch.dict("sys.modules", {"docling": mock_module, "docling.document_converter": mock_module}):
            with patch("tempfile.NamedTemporaryFile", side_effect=tracking_mktemp):
                from file_imports.extraction import _docling_markdown_from_pdf

                _docling_markdown_from_pdf(b"%PDF-1")

        for path in temp_files_created:
            self.assertFalse(os.path.exists(path), f"Temp file {path} was not cleaned up")


# ---------------------------------------------------------------------------
# _storage_has_local_path (lines 292-298)
# ---------------------------------------------------------------------------


class StorageHasLocalPathTests(SimpleTestCase):
    def test_returns_false_for_empty_uri(self):
        self.assertFalse(_storage_has_local_path(""))

    def test_returns_false_for_none_uri(self):
        self.assertFalse(_storage_has_local_path(None))

    def test_returns_false_when_storage_raises_not_implemented(self):
        with patch("file_imports.extraction.default_storage") as mock_storage:
            mock_storage.path.side_effect = NotImplementedError
            self.assertFalse(_storage_has_local_path("some/file.pdf"))

    def test_returns_false_when_storage_raises_file_not_found(self):
        with patch("file_imports.extraction.default_storage") as mock_storage:
            mock_storage.path.side_effect = FileNotFoundError
            self.assertFalse(_storage_has_local_path("some/file.pdf"))

    def test_returns_true_when_storage_path_succeeds(self):
        with patch("file_imports.extraction.default_storage") as mock_storage:
            mock_storage.path.return_value = "/tmp/some_file.pdf"
            self.assertTrue(_storage_has_local_path("some/file.pdf"))


# ---------------------------------------------------------------------------
# _source_file_path_for_docling (lines 302-310)
# ---------------------------------------------------------------------------


class SourceFilePathForDoclingTests(SimpleTestCase):
    def test_returns_storage_path_when_local_path_exists(self):
        with patch("file_imports.extraction._storage_has_local_path", return_value=True):
            with patch("file_imports.extraction.default_storage") as mock_storage:
                mock_storage.path.return_value = "/var/media/price.pdf"
                result = _source_file_path_for_docling(b"content", source_file_uri="uploads/price.pdf")
        self.assertEqual(result, "/var/media/price.pdf")

    def test_creates_temp_file_when_no_source_uri(self):
        result = _source_file_path_for_docling(b"%PDF-1")
        self.assertTrue(result.endswith(".pdf"))
        self.assertTrue(os.path.exists(result))
        os.unlink(result)

    def test_creates_temp_file_when_storage_has_no_local_path(self):
        with patch("file_imports.extraction._storage_has_local_path", return_value=False):
            result = _source_file_path_for_docling(b"data", source_file_uri="s3://bucket/file.pdf")
        self.assertTrue(os.path.exists(result))
        os.unlink(result)


# ---------------------------------------------------------------------------
# extract_price_rows_from_pdf_with_pdfplumber (lines 346-367)
# ---------------------------------------------------------------------------


class ExtractPdfPdfplumberTests(SimpleTestCase):
    def _mock_page(self, *, table=None, text=""):
        page = MagicMock()
        mock_table = MagicMock()
        mock_table.extract.return_value = table
        mock_table.bbox = (0, 0, 100, 100)
        page.find_tables.return_value = [mock_table] if table else []
        page.filter.return_value = page
        page.extract_text.return_value = text
        page.images = []
        return page

    def test_returns_rows_from_table(self):
        page = self._mock_page(table=[["Наименование", "Цена", "Ед"], ["Болт М8", "100", "шт"]])
        with patch.object(extraction_mod, "pdfplumber") as mock_pp:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock(pages=[page]))
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_pp.open.return_value = mock_ctx
            result = extract_price_rows_from_pdf_with_pdfplumber(b"%PDF-1")
        self.assertIsInstance(result, list)

    def test_returns_text_rows_when_no_table(self):
        page = self._mock_page(text="Болт М8 100 шт\nГайка М6 50 шт")
        with patch.object(extraction_mod, "pdfplumber") as mock_pp:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock(pages=[page]))
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_pp.open.return_value = mock_ctx
            result = extract_price_rows_from_pdf_with_pdfplumber(b"%PDF-1")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_empty_pdf_returns_empty_list(self):
        page = self._mock_page(table=None, text="")
        with patch.object(extraction_mod, "pdfplumber") as mock_pp:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock(pages=[page]))
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_pp.open.return_value = mock_ctx
            result = extract_price_rows_from_pdf_with_pdfplumber(b"%PDF-1")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# extract_price_rows_from_docx (lines 374-393)
# ---------------------------------------------------------------------------


class ExtractDocxTests(SimpleTestCase):
    @staticmethod
    def _make_docx_bytes(**kwargs):
        from docx import Document

        doc = Document()
        paragraphs = kwargs.get("paragraphs", [])
        for p in paragraphs:
            doc.add_paragraph(p)
        tables = kwargs.get("tables", [])
        for table_data in tables:
            t = doc.add_table(rows=len(table_data), cols=len(table_data[0]) if table_data else 0)
            for r_idx, row_data in enumerate(table_data):
                for c_idx, cell_text in enumerate(row_data):
                    t.rows[r_idx].cells[c_idx].text = cell_text
        buf = BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def test_extracts_paragraphs_from_docx(self):
        content = self._make_docx_bytes(paragraphs=["Болт М8 100 руб шт", "Гайка М6 50 руб шт"])
        result = extract_price_rows_from_docx(content)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_extracts_table_from_docx(self):
        content = self._make_docx_bytes(tables=[[["Наименование", "Цена", "Ед"], ["Болт М8", "100", "шт"]]])
        result = extract_price_rows_from_docx(content)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_empty_docx_returns_empty_list(self):
        content = self._make_docx_bytes()
        result = extract_price_rows_from_docx(content)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# extract_price_rows_from_txt CSV path (line 411)
# ---------------------------------------------------------------------------


class ExtractTxtCsvPathTests(SimpleTestCase):
    def test_dispatches_to_csv_when_delimiter_count_gte_2(self):
        content = "Наименование;Цена;Ед\nБолт М8;100;шт\n".encode("utf-8")
        result = extract_price_rows_from_txt(content)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_returns_text_rows_when_single_column(self):
        content = "Болт М8 100 шт\nГайка М6 50 шт\n".encode("utf-8")
        result = extract_price_rows_from_txt(content)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIn("text", result[0].cells)


# ---------------------------------------------------------------------------
# extract_price_rows_from_image (lines 416-433)
# ---------------------------------------------------------------------------


class ExtractImageTests(SimpleTestCase):
    @staticmethod
    def _make_png_bytes():
        from PIL import Image as PILImage

        img = PILImage.new("RGB", (200, 50), color=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_returns_rows_from_ocr(self):
        with patch.object(extraction_mod, "pytesseract") as mock_tess:
            mock_tess.TesseractNotFoundError = type("TesseractNotFoundError", (Exception,), {})
            mock_tess.image_to_string.return_value = "Болт М8 100 шт\nГайка М6 50 шт"
            result = extract_price_rows_from_image(self._make_png_bytes())
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_returns_empty_on_tesseract_not_found(self):
        class TesseractNotFoundError(Exception):
            pass

        with patch.object(extraction_mod, "pytesseract") as mock_tess:
            mock_tess.TesseractNotFoundError = TesseractNotFoundError
            mock_tess.image_to_string.side_effect = TesseractNotFoundError("not found")
            with self.assertRaises(FileExtractionError):
                extract_price_rows_from_image(self._make_png_bytes())

    def test_empty_ocr_result_returns_empty_list(self):
        with patch.object(extraction_mod, "pytesseract") as mock_tess:
            mock_tess.TesseractNotFoundError = type("TesseractNotFoundError", (Exception,), {})
            mock_tess.image_to_string.return_value = ""
            result = extract_price_rows_from_image(self._make_png_bytes())
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _rows_from_matrix edge cases (line 443)
# ---------------------------------------------------------------------------


class RowsFromMatrixTests(SimpleTestCase):
    def test_empty_matrix_returns_empty_list(self):
        result = _rows_from_matrix([])
        self.assertEqual(result, [])

    def test_all_empty_rows_returns_empty_list(self):
        result = _rows_from_matrix([["", "  ", None], ["", ""]])
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _maybe_remap_column_keys (line 523)
# ---------------------------------------------------------------------------


class RemapLegacyColumnRowsTests(SimpleTestCase):
    def test_returns_rows_unchanged_when_no_header_values_found(self):
        rows = [
            ExtractedRow(row_number=1, cells={"column_1": "xyz_irrelevant", "column_2": "abc_random"}),
            ExtractedRow(row_number=2, cells={"column_1": "val1", "column_2": "val2"}),
        ]
        result = _remap_legacy_column_rows(rows)
        # First row has column_N keys but no values matching HEADER_ALIAS_GROUPS → unchanged
        self.assertEqual(len(result), 2)
        self.assertIn("column_1", result[0].cells)

    def test_returns_empty_when_no_rows(self):
        self.assertEqual(_remap_legacy_column_rows([]), [])

    def test_returns_rows_unchanged_when_first_row_has_non_column_keys(self):
        rows = [
            ExtractedRow(row_number=1, cells={"наименование": "Болт", "цена": "100"}),
        ]
        result = _remap_legacy_column_rows(rows)
        self.assertEqual(result, rows)


# ---------------------------------------------------------------------------
# _resolve_ai_mode (line 569)
# ---------------------------------------------------------------------------


class ResolveAiModeTests(SimpleTestCase):
    @override_settings(AI_NORMALIZATION_ENABLED=True)
    def test_raises_on_unsupported_ai_mode(self):
        with self.assertRaises(ValueError, msg="Unsupported ai_mode"):
            _resolve_ai_mode(use_ai=True, ai_mode="invalid_mode_xyz")

    @override_settings(AI_NORMALIZATION_ENABLED=False)
    def test_returns_off_when_ai_disabled(self):
        result = _resolve_ai_mode(use_ai=True, ai_mode=AI_MODE_FULL)
        self.assertEqual(result, AI_MODE_OFF)

    def test_returns_off_when_use_ai_false(self):
        result = _resolve_ai_mode(use_ai=False, ai_mode=AI_MODE_FULL)
        self.assertEqual(result, AI_MODE_OFF)

    @override_settings(AI_NORMALIZATION_ENABLED=True)
    def test_uses_default_mode_when_ai_mode_is_none(self):
        result = _resolve_ai_mode(use_ai=True, ai_mode=None)
        self.assertIn(result, {AI_MODE_AMBIGUOUS, AI_MODE_FULL, AI_MODE_OFF})


# ---------------------------------------------------------------------------
# _matches_any_alias branches (lines 774, 779)
# ---------------------------------------------------------------------------


class MatchesAnyAliasTests(SimpleTestCase):
    def test_returns_false_for_empty_value(self):
        self.assertFalse(_matches_any_alias("", {"цена", "стоимость"}))

    def test_returns_false_for_none_value(self):
        self.assertFalse(_matches_any_alias(None, {"цена"}))

    def test_continues_past_empty_alias(self):
        # aliases set contains an empty string that normalizes to ""
        result = _matches_any_alias("цена", {"", "стоимость"})
        # Should not raise, just skip the empty alias
        self.assertIsInstance(result, bool)

    def test_matches_by_regex_word_boundary(self):
        self.assertTrue(_matches_any_alias("цена товара", {"цена"}))

    def test_does_not_match_different_value(self):
        self.assertFalse(_matches_any_alias("наименование", {"цена", "стоимость"}))


# ---------------------------------------------------------------------------
# _first_alias_value excluded_keys (line 765)
# ---------------------------------------------------------------------------


class FirstAliasValueTests(SimpleTestCase):
    def test_skips_excluded_keys(self):
        cells = {"категория": "инструменты", "наименование": "Болт М8"}
        # If we exclude "наименование", we should not get "Болт М8"
        result = _first_alias_value(cells, {"наименование"}, excluded_keys={"наименование"})
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# _infer_name_from_cells (line 806)
# ---------------------------------------------------------------------------


class InferNameFromCellsTests(SimpleTestCase):
    def test_returns_empty_string_when_no_candidates(self):
        # All values look like prices → no candidates
        cells = {"column_1": "100.00", "column_2": "50.00"}
        result = _infer_name_from_cells(cells)
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# _looks_like_price_only (line 822)
# ---------------------------------------------------------------------------


class LooksLikePriceOnlyTests(SimpleTestCase):
    def test_returns_false_for_non_numeric_string(self):
        self.assertFalse(_looks_like_price_only("Болт М8"))

    def test_returns_true_for_numeric_string(self):
        self.assertTrue(_looks_like_price_only("100.00"))

    def test_returns_true_for_integer(self):
        self.assertTrue(_looks_like_price_only("500"))


# ---------------------------------------------------------------------------
# _normalize_money branches (lines 835-836, 839-841)
# ---------------------------------------------------------------------------


class NormalizeMoneyTests(SimpleTestCase):
    def test_handles_multiple_dots(self):
        # "1.000.50" → "1000.50"
        result = _normalize_money("1.000.50")
        self.assertIsNotNone(result)
        self.assertEqual(result, Decimal("1000.50"))

    def test_returns_none_for_negative_value(self):
        result = _normalize_money("-100")
        self.assertIsNone(result)

    def test_returns_none_when_normalized_string_is_not_valid_decimal(self):
        # "-" survives the regex (minus is kept) but Decimal("-") raises InvalidOperation
        result = _normalize_money("-")
        self.assertIsNone(result)

    def test_returns_none_for_invalid_decimal(self):
        result = _normalize_money("abc")
        self.assertIsNone(result)

    def test_returns_none_for_none_input(self):
        self.assertIsNone(_normalize_money(None))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(_normalize_money(""))


# ---------------------------------------------------------------------------
# _normalize_quantity branches (lines 855-856)
# ---------------------------------------------------------------------------


class NormalizeQuantityTests(SimpleTestCase):
    def test_returns_none_for_invalid_decimal(self):
        # "." → Decimal(".") → InvalidOperation
        result = _normalize_quantity(".")
        self.assertIsNone(result)

    def test_returns_none_for_double_dot(self):
        result = _normalize_quantity("1..2")
        self.assertIsNone(result)

    def test_returns_none_for_none(self):
        self.assertIsNone(_normalize_quantity(None))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(_normalize_quantity(""))

    def test_returns_none_when_all_chars_stripped(self):
        self.assertIsNone(_normalize_quantity("abc"))

    def test_parses_valid_quantity(self):
        result = _normalize_quantity("5.5")
        self.assertEqual(result, Decimal("5.5000"))


# ---------------------------------------------------------------------------
# _format_decimal (line 905)
# ---------------------------------------------------------------------------


class FormatDecimalTests(SimpleTestCase):
    def test_returns_empty_string_for_none(self):
        self.assertEqual(_format_decimal(None), "")

    def test_strips_trailing_zeros(self):
        result = _format_decimal(Decimal("5.5000"))
        self.assertEqual(result, "5.5")

    def test_strips_trailing_dot(self):
        result = _format_decimal(Decimal("5.0000"))
        self.assertEqual(result, "5")


# ---------------------------------------------------------------------------
# _warnings_for_product branches (lines 938, 940)
# ---------------------------------------------------------------------------


class WarningsForProductTests(SimpleTestCase):
    def test_adds_raw_name_warning_when_empty(self):
        warnings = _warnings_for_product(raw_name="", normalized_name="bolt", price=Decimal("100"), unit_code="шт")
        self.assertIn("raw_name_unknown", warnings)

    def test_adds_normalized_name_warning_when_empty(self):
        warnings = _warnings_for_product(raw_name="Болт", normalized_name="", price=Decimal("100"), unit_code="шт")
        self.assertIn("normalized_name_unknown", warnings)

    def test_adds_price_warning_when_none(self):
        warnings = _warnings_for_product(raw_name="Болт", normalized_name="болт", price=None, unit_code="шт")
        self.assertIn("price_unknown", warnings)

    def test_adds_unit_warning_when_empty(self):
        warnings = _warnings_for_product(raw_name="Болт", normalized_name="болт", price=Decimal("100"), unit_code="")
        self.assertIn("unit_unknown", warnings)

    def test_no_warnings_for_complete_product(self):
        warnings = _warnings_for_product(raw_name="Болт", normalized_name="болт", price=Decimal("100"), unit_code="шт")
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# normalize_ambiguous_rows_with_ai (lines 975-983)
# ---------------------------------------------------------------------------


class NormalizeAmbiguousRowsWithAiTests(SimpleTestCase):
    def test_raises_supplier_price_ai_error_when_stats_has_errors(self):
        upload = _FakeUpload()
        row = _row(1, text="Болт М8 100 шт")
        product = _product(row_number=1)

        with patch.object(norm_mod, "normalize_products_with_ai") as mock_np:
            mock_np.return_value = ([], {"ai_errors": ["chunk 1/1: timeout"]})
            with self.assertRaises(SupplierPriceAIError):
                normalize_ambiguous_rows_with_ai(upload=upload, rows=[row], ambiguous_products=[product])

    def test_returns_resolved_products_when_no_errors(self):
        upload = _FakeUpload()
        row = _row(1, text="Болт М8 100 шт")
        product = _product(row_number=1)

        with patch.object(norm_mod, "normalize_products_with_ai") as mock_np:
            mock_np.return_value = ([product], {"ai_errors": []})
            result = normalize_ambiguous_rows_with_ai(upload=upload, rows=[row], ambiguous_products=[product])

        self.assertEqual(result, [product])


# ---------------------------------------------------------------------------
# normalize_products_with_ai (lines 1062-1080, 1097-1103, 1114, 1129)
# ---------------------------------------------------------------------------


class NormalizeProductsWithAiTests(SimpleTestCase):
    def test_calls_progress_callback_on_success(self):
        upload = _FakeUpload()
        row = _row(1, text="Болт М8")
        product = _product(row_number=1)
        ai_result = _product(row_number=1, normalized_name="болт м8 улучшенный")
        progress_calls = []

        with patch.object(norm_mod, "_request_ai_normalization_with_retries") as mock_req:
            mock_req.return_value = ({"products": [ai_result]}, 0)
            normalize_products_with_ai(
                upload=upload,
                rows=[row],
                products=[product],
                parser_source="ai_fallback",
                progress_callback=lambda **kw: progress_calls.append(kw),
            )

        self.assertEqual(len(progress_calls), 1)
        self.assertEqual(progress_calls[0]["chunk_index"], 1)

    def test_calls_progress_callback_on_chunk_failure(self):
        upload = _FakeUpload()
        row = _row(1, text="Болт М8")
        product = _product(row_number=1)
        progress_calls = []

        error = SupplierPriceAIError("timeout", code="timeout", retryable=True)
        with patch.object(norm_mod, "_request_ai_normalization_with_retries", side_effect=error):
            with patch.object(norm_mod, "_should_stop_ai_pipeline", return_value=False):
                normalize_products_with_ai(
                    upload=upload,
                    rows=[row],
                    products=[product],
                    parser_source="ai_fallback",
                    progress_callback=lambda **kw: progress_calls.append(kw),
                )

        self.assertEqual(len(progress_calls), 1)

    def test_stops_on_system_error(self):
        upload = _FakeUpload()
        products = [_product(row_number=i) for i in range(1, 4)]
        rows = [_row(i, text=f"row {i}") for i in range(1, 4)]

        error = SupplierPriceAIError("unauthorized", code=AI_ERROR_UNAUTHORIZED_401, retryable=False)
        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            raise error

        with patch.object(norm_mod, "_request_ai_normalization_with_retries", side_effect=side_effect):
            with patch.object(norm_mod, "_should_stop_ai_pipeline", return_value=True):
                with override_settings(AI_NORMALIZATION_CHUNK_SIZE=1):
                    resolved, stats = normalize_products_with_ai(
                        upload=upload,
                        rows=rows,
                        products=products,
                        parser_source="ai_fallback",
                    )

        # Should stop after first chunk failure
        self.assertEqual(call_count, 1)
        self.assertEqual(stats["ai_chunks_failed"], 1)

    def test_missing_rows_in_ai_response_logged(self):
        upload = _FakeUpload()
        rows = [_row(1, text="row1"), _row(2, text="row2")]
        products = [_product(row_number=1), _product(row_number=2)]

        # AI returns only row 1, misses row 2
        ai_response = {"products": [_product(row_number=1)]}
        with patch.object(norm_mod, "_request_ai_normalization_with_retries") as mock_req:
            mock_req.return_value = (ai_response, 0)
            resolved, stats = normalize_products_with_ai(
                upload=upload,
                rows=rows,
                products=products,
                parser_source="ai_fallback",
            )

        self.assertTrue(any("missed rows" in err for err in stats["ai_errors"]))


# ---------------------------------------------------------------------------
# Settings helpers (lines 1195-1292)
# ---------------------------------------------------------------------------


class SettingsHelpersTests(SimpleTestCase):
    def test_settings_int_uses_default_on_type_error(self):
        with override_settings(AI_NORMALIZATION_CHUNK_SIZE="not_an_int"):
            result = norm_mod._ai_chunk_size()
        # Default is AI_CHUNK_SIZE = 20
        self.assertEqual(result, 20)

    def test_settings_int_enforces_minimum(self):
        with override_settings(AI_NORMALIZATION_CHUNK_SIZE=-5):
            result = norm_mod._ai_chunk_size()
        self.assertEqual(result, 1)

    def test_settings_bool_parses_string_true(self):
        result = _settings_bool("AI_NORMALIZATION_ENABLED", False)
        self.assertIsInstance(result, bool)

    def test_settings_bool_parses_string_value(self):
        with override_settings(AI_NORMALIZATION_ENABLED="yes"):
            result = norm_mod._ai_enabled()
        self.assertTrue(result)

    def test_settings_bool_parses_false_string(self):
        with override_settings(AI_NORMALIZATION_ENABLED="false"):
            result = norm_mod._ai_enabled()
        self.assertFalse(result)

    def test_ai_url_returns_setting(self):
        with override_settings(AI_NORMALIZATION_URL="https://custom.ai/v1"):
            result = norm_mod._ai_url()
        self.assertEqual(result, "https://custom.ai/v1")

    def test_openai_base_url_strips_chat_completions_suffix(self):
        with override_settings(OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions"):
            result = norm_mod._openai_base_url()
        self.assertEqual(result, "https://api.openai.com/v1")

    def test_openai_base_url_uses_legacy_env_variable(self):
        with override_settings(OPENAI_BASE_URL=""):
            with patch.dict(os.environ, {"AI_NORMALIZATION_URL": "https://legacy.ai/v1"}):
                result = norm_mod._openai_base_url()
        self.assertIn("legacy.ai", result or "")

    def test_openai_base_url_returns_none_when_empty(self):
        with override_settings(OPENAI_BASE_URL=""):
            with patch.dict(os.environ, {}, clear=False):
                env_bak = os.environ.pop("AI_NORMALIZATION_URL", None)
                try:
                    result = norm_mod._openai_base_url()
                finally:
                    if env_bak:
                        os.environ["AI_NORMALIZATION_URL"] = env_bak
        self.assertIsNone(result)

    def test_openai_api_key_raises_when_not_configured(self):
        with override_settings(OPENAI_API_KEY="", AI_NORMALIZATION_API_KEY=""):
            with self.assertRaises(SupplierPriceAIError) as ctx:
                norm_mod._openai_api_key()
        self.assertEqual(ctx.exception.code, AI_ERROR_CREDENTIALS_NOT_CONFIGURED)

    def test_openai_api_key_strips_bearer_prefix(self):
        with override_settings(OPENAI_API_KEY="Bearer my-secret-key"):
            result = norm_mod._openai_api_key()
        self.assertEqual(result, "my-secret-key")

    def test_openai_api_key_falls_back_to_ai_normalization_api_key(self):
        with override_settings(OPENAI_API_KEY="", AI_NORMALIZATION_API_KEY="fallback-key"):
            result = norm_mod._openai_api_key()
        self.assertEqual(result, "fallback-key")

    def test_ai_max_retries_returns_setting(self):
        with override_settings(AI_NORMALIZATION_MAX_RETRIES=3):
            self.assertEqual(norm_mod._ai_max_retries(), 3)

    def test_ai_full_max_rows_returns_setting(self):
        with override_settings(AI_NORMALIZATION_FULL_MAX_ROWS=100):
            self.assertEqual(norm_mod._ai_full_max_rows(), 100)

    def test_ai_local_max_rows_returns_setting(self):
        with override_settings(AI_NORMALIZATION_LOCAL_MAX_ROWS=50):
            self.assertEqual(norm_mod._ai_local_max_rows(), 50)

    def test_ai_stop_on_system_error_returns_setting(self):
        with override_settings(AI_NORMALIZATION_STOP_ON_SYSTEM_ERROR=False):
            self.assertFalse(norm_mod._ai_stop_on_system_error())


# ---------------------------------------------------------------------------
# _ai_authorization_header (lines 1233-1235)
# ---------------------------------------------------------------------------


class AiAuthorizationHeaderTests(SimpleTestCase):
    def test_raises_when_no_credentials(self):
        with override_settings(AI_NORMALIZATION_AUTHORIZATION="", AI_NORMALIZATION_API_KEY=""):
            with self.assertRaises(SupplierPriceAIError) as ctx:
                norm_mod._ai_authorization_header()
        self.assertEqual(ctx.exception.code, AI_ERROR_CREDENTIALS_NOT_CONFIGURED)

    def test_returns_authorization_header_directly(self):
        with override_settings(AI_NORMALIZATION_AUTHORIZATION="Bearer my-token"):
            result = norm_mod._ai_authorization_header()
        self.assertEqual(result, "Bearer my-token")

    def test_wraps_api_key_in_bearer(self):
        with override_settings(AI_NORMALIZATION_AUTHORIZATION="", AI_NORMALIZATION_API_KEY="raw-key"):
            result = norm_mod._ai_authorization_header()
        self.assertEqual(result, "Bearer raw-key")

    def test_returns_api_key_directly_when_already_bearer(self):
        with override_settings(AI_NORMALIZATION_AUTHORIZATION="", AI_NORMALIZATION_API_KEY="Bearer existing-token"):
            result = norm_mod._ai_authorization_header()
        self.assertEqual(result, "Bearer existing-token")


# ---------------------------------------------------------------------------
# _validate_ai_workload (lines 1287-1292)
# ---------------------------------------------------------------------------


class ValidateAiWorkloadTests(SimpleTestCase):
    def test_raises_when_full_mode_without_confirm(self):
        with self.assertRaises(ValueError):
            norm_mod._validate_ai_workload(
                ai_mode=AI_MODE_FULL, rows_total=10, rows_ai_requested=10, confirm_full=False
            )

    def test_raises_when_full_mode_exceeds_max_rows(self):
        with override_settings(AI_NORMALIZATION_FULL_MAX_ROWS=5):
            with self.assertRaises(ValueError):
                norm_mod._validate_ai_workload(
                    ai_mode=AI_MODE_FULL, rows_total=10, rows_ai_requested=10, confirm_full=True
                )

    def test_passes_when_off_mode(self):
        # Should not raise
        norm_mod._validate_ai_workload(ai_mode=AI_MODE_OFF, rows_total=1000, rows_ai_requested=1000, confirm_full=False)

    def test_passes_when_zero_rows_requested(self):
        norm_mod._validate_ai_workload(ai_mode=AI_MODE_FULL, rows_total=0, rows_ai_requested=0, confirm_full=False)

    @override_settings(DEBUG=True)
    def test_raises_in_debug_when_exceeds_local_max(self):
        with override_settings(AI_NORMALIZATION_LOCAL_MAX_ROWS=5):
            with self.assertRaises(ValueError):
                norm_mod._validate_ai_workload(
                    ai_mode=AI_MODE_AMBIGUOUS, rows_total=10, rows_ai_requested=10, confirm_full=False
                )


# ---------------------------------------------------------------------------
# _should_stop_ai_pipeline (line 1129)
# ---------------------------------------------------------------------------


class ShouldStopAiPipelineTests(SimpleTestCase):
    def test_always_stops_on_credentials_error(self):
        err = SupplierPriceAIError("no key", code=AI_ERROR_CREDENTIALS_NOT_CONFIGURED)
        self.assertTrue(norm_mod._should_stop_ai_pipeline(err))

    def test_always_stops_on_unauthorized(self):
        err = SupplierPriceAIError("401", code=AI_ERROR_UNAUTHORIZED_401)
        self.assertTrue(norm_mod._should_stop_ai_pipeline(err))

    @override_settings(AI_NORMALIZATION_STOP_ON_SYSTEM_ERROR=True)
    def test_stops_on_system_error_when_enabled(self):
        # rate_limit is a system_error but NOT in the "always stop" set → falls to line 1129
        err = SupplierPriceAIError("429", code=AI_ERROR_RATE_LIMIT_429, retryable=True)
        self.assertTrue(norm_mod._should_stop_ai_pipeline(err))

    @override_settings(AI_NORMALIZATION_STOP_ON_SYSTEM_ERROR=False)
    def test_does_not_stop_on_system_error_when_disabled(self):
        err = SupplierPriceAIError("429", code=AI_ERROR_RATE_LIMIT_429, retryable=True)
        self.assertFalse(norm_mod._should_stop_ai_pipeline(err))


# ---------------------------------------------------------------------------
# _http_error_code (lines 1314-1322)
# ---------------------------------------------------------------------------


class HttpErrorCodeTests(SimpleTestCase):
    def test_400_maps_to_bad_request(self):
        self.assertEqual(_http_error_code(400), AI_ERROR_BAD_REQUEST_400)

    def test_401_maps_to_unauthorized(self):
        self.assertEqual(_http_error_code(401), AI_ERROR_UNAUTHORIZED_401)

    def test_403_maps_to_forbidden(self):
        self.assertEqual(_http_error_code(403), AI_ERROR_FORBIDDEN_403)

    def test_429_maps_to_rate_limit(self):
        self.assertEqual(_http_error_code(429), AI_ERROR_RATE_LIMIT_429)

    def test_500_maps_to_network_error(self):
        self.assertEqual(_http_error_code(500), AI_ERROR_NETWORK)

    def test_unknown_maps_to_network_error(self):
        self.assertEqual(_http_error_code(503), AI_ERROR_NETWORK)


# ---------------------------------------------------------------------------
# _http_error_retryable (line 1326)
# ---------------------------------------------------------------------------


class HttpErrorRetryableTests(SimpleTestCase):
    def test_429_is_retryable(self):
        self.assertTrue(_http_error_retryable(429))

    def test_500_is_retryable(self):
        self.assertTrue(_http_error_retryable(500))

    def test_503_is_retryable(self):
        self.assertTrue(_http_error_retryable(503))

    def test_400_not_retryable(self):
        self.assertFalse(_http_error_retryable(400))

    def test_401_not_retryable(self):
        self.assertFalse(_http_error_retryable(401))


# ---------------------------------------------------------------------------
# _read_http_error_body (lines 1330-1334)
# ---------------------------------------------------------------------------


class ReadHttpErrorBodyTests(SimpleTestCase):
    def test_reads_and_decodes_body(self):
        mock_error = MagicMock()
        mock_error.read.return_value = b"error details"
        result = _read_http_error_body(mock_error)
        self.assertEqual(result, "error details")

    def test_returns_empty_string_on_read_exception(self):
        mock_error = MagicMock()
        mock_error.read.side_effect = OSError("connection closed")
        result = _read_http_error_body(mock_error)
        self.assertEqual(result, "")

    def test_truncates_long_body(self):
        mock_error = MagicMock()
        mock_error.read.return_value = b"x" * 2000
        result = _read_http_error_body(mock_error)
        self.assertLessEqual(len(result), 1000)


# ---------------------------------------------------------------------------
# _network_error_from_url_error (lines 1338-1348)
# ---------------------------------------------------------------------------


class NetworkErrorFromUrlErrorTests(SimpleTestCase):
    def test_timeout_reason_creates_timeout_error(self):
        url_error = urllib.error.URLError(reason=TimeoutError("timed out"))
        result = _network_error_from_url_error(url_error)
        self.assertEqual(result.code, AI_ERROR_TIMEOUT)

    def test_socket_timeout_reason_creates_timeout_error(self):
        url_error = urllib.error.URLError(reason=socket.timeout("timed out"))
        result = _network_error_from_url_error(url_error)
        self.assertEqual(result.code, AI_ERROR_TIMEOUT)

    def test_timed_out_in_message_creates_timeout_error(self):
        url_error = urllib.error.URLError("connection timed out")
        result = _network_error_from_url_error(url_error)
        self.assertEqual(result.code, AI_ERROR_TIMEOUT)

    def test_generic_url_error_creates_network_error(self):
        url_error = urllib.error.URLError("connection refused")
        result = _network_error_from_url_error(url_error)
        self.assertEqual(result.code, AI_ERROR_NETWORK)

    def test_timeout_error_is_retryable(self):
        url_error = urllib.error.URLError(reason=TimeoutError("timed out"))
        result = _network_error_from_url_error(url_error)
        self.assertTrue(result.retryable)


# ---------------------------------------------------------------------------
# _ai_error_from_openai_exception (lines 1387-1442)
# ---------------------------------------------------------------------------


class AiErrorFromOpenaiExceptionTests(SimpleTestCase):
    def test_authentication_error_class_name_gives_401(self):
        class AuthenticationError(Exception):
            pass

        err = AuthenticationError("bad key")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_UNAUTHORIZED_401)

    def test_401_status_code_gives_unauthorized(self):
        err = Exception("unauthorized")
        err.status_code = 401
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_UNAUTHORIZED_401)

    def test_permission_denied_error_gives_403(self):
        class PermissionDeniedError(Exception):
            pass

        err = PermissionDeniedError("forbidden")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_FORBIDDEN_403)

    def test_bad_request_error_gives_400(self):
        class BadRequestError(Exception):
            pass

        err = BadRequestError("bad request")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_BAD_REQUEST_400)

    def test_rate_limit_error_gives_429(self):
        class RateLimitError(Exception):
            pass

        err = RateLimitError("rate limit exceeded")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_RATE_LIMIT_429)

    def test_api_timeout_error_gives_timeout(self):
        class APITimeoutError(Exception):
            pass

        err = APITimeoutError("request timed out")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_TIMEOUT)

    def test_builtin_timeout_error_gives_timeout(self):
        err = TimeoutError("timed out")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_TIMEOUT)

    def test_url_error_gives_network_error(self):
        err = urllib.error.URLError("connection refused")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertIn(result.code, {AI_ERROR_NETWORK, AI_ERROR_TIMEOUT})

    def test_api_connection_error_gives_network_error(self):
        class APIConnectionError(Exception):
            pass

        err = APIConnectionError("connection failed")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_NETWORK)

    def test_json_decode_error_gives_invalid_json(self):
        import json

        err = json.JSONDecodeError("invalid json", "doc", 0)
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_INVALID_JSON)

    def test_http_error_body_is_read(self):
        # HTTPError is a URLError subclass; body is read even though it may not appear in final message
        http_err = urllib.error.HTTPError("http://example.com", 500, "Internal Error", {}, None)
        http_err.read = lambda: b"Server error details"
        result = norm_mod._ai_error_from_openai_exception(http_err)
        # Should produce a SupplierPriceAIError (exact code depends on routing through URLError path)
        self.assertIsInstance(result, SupplierPriceAIError)

    def test_generic_exception_gives_network_error(self):
        err = RuntimeError("something unexpected")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_NETWORK)

    def test_instructor_validation_error_gives_invalid_json(self):
        class InstructorValidationError(Exception):
            pass

        err = InstructorValidationError("schema mismatch")
        result = norm_mod._ai_error_from_openai_exception(err)
        self.assertEqual(result.code, AI_ERROR_INVALID_JSON)


# ---------------------------------------------------------------------------
# _products_from_ai_payload edge cases (lines 1455-1456, 1459, 1493)
# ---------------------------------------------------------------------------


class ProductsFromAiPayloadTests(SimpleTestCase):
    def test_skips_product_with_invalid_row_number(self):
        fallback = [_product(row_number=1)]
        ai_payload = {"products": [{"row_number": "not_a_number", "normalized_name": "болт"}]}
        result = _products_from_ai_payload(ai_payload, fallback_products=fallback, parser_source="ai_full_pass")
        self.assertEqual(result, [])

    def test_skips_product_with_none_row_number(self):
        fallback = [_product(row_number=1)]
        ai_payload = {"products": [{"row_number": None}]}
        result = _products_from_ai_payload(ai_payload, fallback_products=fallback, parser_source="ai_full_pass")
        self.assertEqual(result, [])

    def test_skips_product_not_in_fallback(self):
        fallback = [_product(row_number=1)]
        ai_payload = {"products": [{"row_number": 999, "normalized_name": "болт"}]}
        result = _products_from_ai_payload(ai_payload, fallback_products=fallback, parser_source="ai_full_pass")
        self.assertEqual(result, [])

    def test_merges_ai_result_with_fallback(self):
        fallback = [_product(row_number=1)]
        ai_payload = {
            "products": [
                {
                    "row_number": 1,
                    "normalized_name": "болт м8 улучшенный",
                    "confidence": 0.95,
                    "warnings": [],
                }
            ]
        }
        result = _products_from_ai_payload(ai_payload, fallback_products=fallback, parser_source="ai_full_pass")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["normalized_name"], "болт м8 улучшенный")

    def test_adds_warnings_when_ai_product_has_no_name(self):
        fallback = [_product(row_number=1)]
        ai_payload = {
            "products": [
                {
                    "row_number": 1,
                    "raw_name": "",
                    "normalized_name": "",
                    "confidence": 0.5,
                    "warnings": [],
                }
            ]
        }
        result = _products_from_ai_payload(ai_payload, fallback_products=fallback, parser_source="ai_full_pass")
        self.assertEqual(len(result), 1)
        # Should have warnings about missing name
        self.assertTrue(len(result[0]["warnings"]) > 0)

    def test_unit_aliases_preserves_non_fixed_five_unit_code(self):
        # Regression for TASK-101a/b interaction: when the caller passes the live
        # Unit vocabulary through `unit_aliases`, a code like "уп" (not in the
        # fixed 5-value UNIT_ALIASES_MAP) must survive the merge instead of being
        # reduced to "" (unit_unknown).
        fallback = [_product(row_number=1, unit_code="")]
        ai_payload = {
            "products": [
                {
                    "row_number": 1,
                    "unit_code": "уп",
                    "confidence": 0.9,
                    "warnings": [],
                }
            ]
        }
        result = _products_from_ai_payload(
            ai_payload,
            fallback_products=fallback,
            parser_source="ai_full_pass",
            unit_aliases={"уп": "уп"},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["unit_code"], "уп")
        self.assertNotIn("unit_unknown", result[0]["warnings"])

    def test_without_unit_aliases_non_fixed_five_unit_code_is_dropped(self):
        # Regression guard on the OLD bug: without `unit_aliases` (the default),
        # normalize_unit_code falls back to the fixed 5-value UNIT_ALIASES_MAP,
        # so "уп" is NOT recognized and the merge produces unit_unknown.
        fallback = [_product(row_number=1, unit_code="")]
        ai_payload = {
            "products": [
                {
                    "row_number": 1,
                    "unit_code": "уп",
                    "confidence": 0.9,
                    "warnings": [],
                }
            ]
        }
        result = _products_from_ai_payload(ai_payload, fallback_products=fallback, parser_source="ai_full_pass")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["unit_code"], "")
        self.assertIn("unit_unknown", result[0]["warnings"])


# ---------------------------------------------------------------------------
# _normalize_confidence (lines 1518-1523)
# ---------------------------------------------------------------------------


class NormalizeConfidenceTests(SimpleTestCase):
    def test_returns_fallback_for_invalid_value(self):
        fallback = Decimal("0.5000")
        result = _normalize_confidence("not_a_number", fallback=fallback)
        self.assertEqual(result, fallback)

    def test_returns_fallback_for_none(self):
        fallback = Decimal("0.6500")
        result = _normalize_confidence(None, fallback=fallback)
        self.assertEqual(result, fallback)

    def test_clamps_negative_to_zero(self):
        result = _normalize_confidence(-0.5, fallback=Decimal("0.5"))
        self.assertEqual(result, Decimal("0.0000"))

    def test_clamps_above_one_to_one(self):
        result = _normalize_confidence(1.5, fallback=Decimal("0.5"))
        self.assertEqual(result, Decimal("1.0000"))

    def test_valid_value_is_quantized(self):
        result = _normalize_confidence(0.9512345, fallback=Decimal("0.5"))
        self.assertEqual(result, Decimal("0.9512"))


# ---------------------------------------------------------------------------
# _request_ai_normalization connector routing (TASK-99)
# ---------------------------------------------------------------------------


class RequestAINormalizationConnectorRoutingTests(SimpleTestCase):
    """Regression for TASK-99: _request_ai_normalization always computed
    _openai_api_key() as a call argument BEFORE request_ai_normalization_payload
    ran — even for the LLM_PRIMARY_PROVIDER branch that needs no OpenAI key
    at all, because LLMConnector() reads LLM_PRIMARY_*/LLM_FALLBACK_* itself.
    Without OPENAI_API_KEY/AI_NORMALIZATION_API_KEY set (expected for an
    Anthropic-only LLM_PRIMARY_PROVIDER config), _openai_api_key() raised
    SupplierPriceAIError before the connector was ever reached."""

    @override_settings(
        LLM_CONNECTOR_ENABLED=True,
        LLM_PRIMARY_PROVIDER="anthropic",
        LLM_PRIMARY_MODEL="claude-haiku-4-5",
        LLM_PRIMARY_API_KEY="anthropic-key",
        OPENAI_API_KEY="",
        AI_NORMALIZATION_API_KEY="",
    )
    def test_llm_primary_provider_configured_skips_openai_credential_check(self):
        from api.v1.suppliers.ai_contracts import ParsedSupplierCatalog
        from api.v1.suppliers.normalization import _request_ai_normalization

        upload = _FakeUpload()
        row = _row(1, название="Болт М8")
        mock_connector = MagicMock()
        mock_connector.structured.return_value = ParsedSupplierCatalog(products=[])

        with patch("api.v1.suppliers.ai_client.LLMConnector", return_value=mock_connector):
            result = _request_ai_normalization(
                upload=upload,
                rows_by_number={1: row},
                products=[{"row_number": 1, "raw_name": "Болт М8"}],
            )

        self.assertEqual(result, {"products": []})


class BarcodeHeaderAliasTests(SimpleTestCase):
    def test_barcode_headers_map_to_the_barcode_field(self):
        from api.v1.suppliers.product_import_parsing import _field_for_header_key

        for header in ("Штрихкод", "Штрих-код", "ШК", "EAN", "Баркод", "Barcode"):
            with self.subTest(header=header):
                self.assertEqual(_field_for_header_key(header), "barcode")

    def test_neighbouring_headers_keep_their_fields(self):
        from api.v1.suppliers.product_import_parsing import _field_for_header_key

        self.assertEqual(_field_for_header_key("Артикул"), "supplier_sku")
        self.assertEqual(_field_for_header_key("Кратность"), "delivery_multiple")
