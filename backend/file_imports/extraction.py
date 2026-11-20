"""Shared, deterministic file extraction for supplier, master and venue imports.

Domain adapters interpret fields; this module owns formats, encoding, table
boundaries, headers and extraction limits, including the legacy upload path.
"""

import csv
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.core.files.storage import default_storage
from openpyxl import load_workbook

from file_imports.quality import pick_best_sheet
from file_imports.types import ExtractedRows, RawImportRow

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

try:
    import pytesseract
    from PIL import Image
except ImportError:
    Image = None
    pytesseract = None

logger = logging.getLogger("autosnab.file_imports.extraction")


class UnreadableSpreadsheetError(ValueError):
    """Файл с расширением Excel, но не читается как xlsx (например, бинарный .xls)."""


class FileExtractionError(ValueError):
    """Actionable file error shared by HTTP and background import adapters."""


SUPPORTED_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
}
MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024
MAX_PHYSICAL_ROWS = 200000
COMMON_HEADER_ALIAS_GROUPS = (
    {
        "название",
        "наименование",
        "номенклатура",
        "товар",
        "name",
        "product",
        "product name",
        "item",
        "item name",
        "master product",
        "мастер товар",
    },
    {"ед", "ед изм", "единица", "единица измерения", "единица изм", "unit", "uom"},
    {"цена", "price"},
    {"нетто", "вес", "объем", "объём", "net", "net weight", "net value"},
    {"страна", "country"},
    {"кратность", "минимальное количество", "артикул", "sku", "состав упаковки", "примечание", "note"},
)


def max_extracted_rows():
    """Потолок слоя чтения, выше любого продуктового лимита: решение «слишком много строк» принимает продукт,
    слой не должен молча обрезать данные раньше."""
    return int(getattr(settings, "FILE_IMPORT_MAX_EXTRACTED_ROWS", 60000) or 60000)


HEADER_DETECTION_SCAN_ROWS = 20


def _clean_text(value, *, max_length=None):
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_length] if max_length else text


def _normalize_key(value):
    return re.sub(r"[^\w]+", " ", _clean_text(value).casefold().replace("ё", "е").replace("_", " ")).strip()


def _decode_csv_content(content):
    if content.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return content.decode("utf-32")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = content.decode(encoding)
            if any(ord(char) < 32 and char not in "\t\r\n" or 127 <= ord(char) <= 159 for char in text):
                raise FileExtractionError("Не удалось определить кодировку. Сохраните файл в UTF-8 с BOM.")
            return text
        except UnicodeDecodeError:
            continue
    raise FileExtractionError("Не удалось определить кодировку файла.")


def _matches_any_alias(value, aliases):
    normalized = _normalize_key(value)
    if not normalized:
        return False
    normalized_compact = normalized.replace("_", " ")
    for alias in aliases:
        alias_normalized = _normalize_key(alias).replace("_", " ")
        if not alias_normalized:
            continue
        # Заголовок бывает «Наименование товара в УС», «Номенклатура, Упаковка» --
        # алиас как первое слово(а).
        if alias_normalized == normalized_compact or re.match(re.escape(alias_normalized) + r"\W", normalized_compact):
            return True
    return False


def extractor_name_for_file(file_name):
    file_name = (file_name or "").lower()
    if file_name.endswith((".csv", ".tsv")):
        return "csv"
    # Keep the existing job classifier; the reader distinguishes OOXML and BIFF by signature.
    if file_name.endswith((".xlsx", ".xlsm", ".xls")):
        return "xlsx"
    if file_name.endswith(".pdf"):
        return "docling_pdf"
    if file_name.endswith((".docx", ".doc")):
        return "docx"
    if file_name.endswith(".txt"):
        return "txt"
    if file_name.endswith((".jpg", ".jpeg", ".png")):
        return "image_ocr"
    return "unsupported"


def extract_rows_from_file(source_file, *, header_alias_groups, source_file_uri=None, source_file_name=None):
    try:
        return _extract_rows_from_file(
            source_file,
            header_alias_groups=header_alias_groups,
            source_file_uri=source_file_uri,
            source_file_name=source_file_name,
        )
    except (FileExtractionError, UnreadableSpreadsheetError):
        raise
    except Exception as error:
        raise FileExtractionError("Не удалось прочитать файл. Проверьте формат и целостность файла.") from error


def _extract_rows_from_file(source_file, *, header_alias_groups, source_file_uri=None, source_file_name=None):
    """Read raw rows with common format/header rules.

    header_alias_groups is retained for caller compatibility; product header
    selection uses the common vocabulary so domains see the same raw rows.
    """
    file_name = (source_file_name or getattr(source_file, "name", "") or "").lower()
    source_file.seek(0)
    if getattr(source_file, "size", 0) > MAX_IMPORT_FILE_BYTES:
        raise FileExtractionError("Файл слишком большой. Максимальный размер — 10 МБ.")
    content = source_file.read(MAX_IMPORT_FILE_BYTES + 1)
    source_file.seek(0)

    if len(content) > MAX_IMPORT_FILE_BYTES:
        raise FileExtractionError("Файл слишком большой. Максимальный размер — 10 МБ.")
    if not content:
        raise FileExtractionError("Файл пустой.")
    suffix = Path(file_name).suffix
    content_type = str(getattr(source_file, "content_type", "") or "").casefold()
    if content.startswith(b"PK"):
        try:
            with ZipFile(BytesIO(content)) as archive:
                members = set(archive.namelist())
            if "xl/workbook.xml" in members:
                suffix = ".xlsx"
            elif "word/document.xml" in members:
                suffix = ".docx"
        except BadZipFile:
            pass
    elif content.startswith(b"%PDF-"):
        suffix = ".pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif content.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif suffix not in SUPPORTED_EXTENSIONS:
        suffix = {
            "text/csv": ".csv",
            "text/tab-separated-values": ".tsv",
            "text/plain": ".txt",
            "application/vnd.ms-excel": ".xls",
        }.get(content_type, suffix)
    file_name = str(Path(file_name).with_suffix(suffix)) if file_name else "upload" + suffix
    if suffix not in SUPPORTED_EXTENSIONS:
        raise FileExtractionError("Неподдерживаемый формат файла.")

    if file_name.endswith((".csv", ".tsv")):
        return extract_rows_from_csv(content, header_alias_groups=header_alias_groups)
    if file_name.endswith((".xlsx", ".xlsm", ".xls")):
        if content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            return extract_rows_from_xls(content, header_alias_groups=header_alias_groups)
        return extract_rows_from_xlsx(content, header_alias_groups=header_alias_groups)
    if file_name.endswith(".pdf"):
        return extract_rows_from_pdf(content, header_alias_groups=header_alias_groups, source_file_uri=source_file_uri)
    if file_name.endswith(".doc") and not content.startswith(b"PK"):
        return extract_rows_from_doc(content, header_alias_groups=header_alias_groups)
    if file_name.endswith((".docx", ".doc")):
        return extract_rows_from_docx(content, header_alias_groups=header_alias_groups)
    if file_name.endswith(".txt"):
        return extract_rows_from_txt(content, header_alias_groups=header_alias_groups)
    if file_name.endswith((".jpg", ".jpeg", ".png")):
        return extract_rows_from_image(content)
    return []


def extract_rows_from_stored_file(*, source_file_uri, header_alias_groups, source_file_name=None):
    with default_storage.open(source_file_uri, "rb") as source_file:
        return extract_rows_from_file(
            source_file,
            header_alias_groups=header_alias_groups,
            source_file_uri=source_file_uri,
            source_file_name=source_file_name or getattr(source_file, "name", ""),
        )


def extract_rows_from_csv(content, *, header_alias_groups):
    text = _decode_csv_content(content)
    delimiter = _detect_csv_delimiter(text)
    reader = csv.reader(StringIO(text), delimiter=delimiter)
    raw_rows = []
    for row in reader:
        if len(row) > 512 or any(len(cell) > 32767 for cell in row):
            raise FileExtractionError("Слишком много колонок или слишком длинная ячейка таблицы.")
        raw_rows.append(list(row))
        if len(raw_rows) > MAX_PHYSICAL_ROWS:
            raise FileExtractionError("Файл слишком большой: слишком много строк, включая пустые.")
    return _rows_from_matrix(raw_rows, header_alias_groups=header_alias_groups)


def _detect_csv_delimiter(text):
    sample = "\n".join(text.splitlines()[:100])
    scores = []
    for delimiter in (";", "\t", ",", "|"):
        try:
            rows = list(csv.reader(StringIO(sample), delimiter=delimiter))
        except csv.Error:
            continue
        multi = [row for row in rows if len(row) > 1]
        header_score = max(
            (
                sum(any(_matches_any_alias(cell, group) for cell in row) for group in COMMON_HEADER_ALIAS_GROUPS)
                for row in multi
            ),
            default=0,
        )
        scores.append((header_score, len(multi), sum(len(row) for row in multi), delimiter))
    return max(scores)[-1] if scores else ","


def _combine_tables(tables, *, header_alias_groups, section_rows=None, cell_metadata=None):
    # Select product-bearing sheets when a workbook also contains a cover sheet.
    recognized = [
        table
        for table in tables
        if _detect_header(
            [(i + 1, row) for i, row in enumerate(table[1]) if any(_clean_text(v) for v in row)],
            header_alias_groups=COMMON_HEADER_ALIAS_GROUPS,
        )[0]
        >= 0
    ]
    # No sheet has a recognisable header: skip empty sheets and keep the one that looks most like a price list.
    selected = recognized or pick_best_sheet(tables)
    combined = ExtractedRows(metadata={"sources": [], "warnings": []})
    offset = 0
    for title, matrix in tables:
        if (title, matrix) not in selected:
            combined.metadata["warnings"].append(f"Лист {title} пропущен: не найдена таблица товаров.")
            offset += len(matrix)
            continue
        rows = _rows_from_matrix(
            matrix, header_alias_groups=header_alias_groups, section_rows=(section_rows or {}).get(title, set())
        )
        combined.metadata["sources"].append({"name": title, **rows.metadata})
        if len(combined.metadata["sources"]) == 1:
            combined.metadata.update({k: v for k, v in rows.metadata.items() if k != "warnings"})
            combined.metadata["sheet_name"] = title
        else:
            combined.metadata["blank_rows"] = combined.metadata.get("blank_rows", 0) + rows.metadata.get(
                "blank_rows", 0
            )
        for row in rows:
            combined.append(
                replace(
                    row,
                    row_number=offset + row.row_number,
                    source={
                        **row.source,
                        "sheet_name": title,
                        "source_row_number": row.row_number,
                        "raw_cells": (cell_metadata or {})
                        .get(title, {})
                        .get(row.row_number, row.source.get("raw_cells", [])),
                    },
                )
            )
        offset += len(matrix)
        _check_row_limit(combined)
    if len(combined.metadata["sources"]) > 1:
        combined.metadata["sheet_name"] = None
    return combined


def _check_row_limit(rows):
    if len(rows) > max_extracted_rows():
        raise FileExtractionError(f"Слишком много строк. Максимум слоя чтения — {max_extracted_rows()}.")


def extract_rows_from_xlsx(content, *, header_alias_groups):
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 100 * 1024 * 1024:
                raise FileExtractionError("Слишком большой распакованный Excel-файл (максимум 100 МБ).")
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            formulas = load_workbook(BytesIO(content), read_only=True, data_only=False)
        except Exception:
            workbook.close()
            raise
    except FileExtractionError:
        raise
    except Exception as error:
        raise UnreadableSpreadsheetError(
            "Не удалось прочитать Excel-файл. Проверьте формат и целостность файла."
        ) from error
    tables = []
    section_rows = {}
    cell_metadata = {}
    physical = 0
    try:
        if len(workbook.worksheets) > 100:
            raise FileExtractionError("Слишком много листов Excel (максимум 100).")
        for sheet, formula_sheet in zip(workbook.worksheets, formulas.worksheets, strict=True):
            if sheet.sheet_state != "visible":
                continue
            matrix = []
            formats = []
            cell_metadata[sheet.title] = {}
            if sheet.max_column and sheet.max_column > 512:
                raise FileExtractionError("Слишком много колонок Excel (максимум 512).")
            for values, expressions in zip(sheet.iter_rows(), formula_sheet.iter_rows(values_only=True), strict=True):
                physical += 1
                if physical > MAX_PHYSICAL_ROWS:
                    raise FileExtractionError("Файл слишком большой: слишком много строк, включая пустые.")
                if any(
                    isinstance(expr, str) and expr.startswith("=") and value.value is None
                    for value, expr in zip(values, expressions, strict=True)
                ):
                    raise FileExtractionError(
                        "В Excel есть формулы без сохранённого результата. Пересчитайте и сохраните файл или замените формулы значениями."
                    )
                if any(len(str(cell.value or "")) > 32767 for cell in values):
                    raise FileExtractionError("Слишком длинная ячейка Excel.")
                matrix.append([_display_cell_value(cell.value, cell.number_format) for cell in values])
                cell_metadata[sheet.title][len(matrix)] = [
                    {
                        "column": index + 1,
                        "value": _raw_cell_value(cell.value),
                        "data_type": cell.data_type,
                        "number_format": cell.number_format,
                        "formula": expr if isinstance(expr, str) and expr.startswith("=") else None,
                    }
                    for index, (cell, expr) in enumerate(zip(values, expressions, strict=True))
                ]

                formats.append(
                    [
                        (
                            getattr(cell.alignment, "indent", 0) or 0,
                            bool(getattr(cell.fill, "patternType", None)),
                            getattr(cell, "_style_id", 0),
                        )
                        for cell in values
                    ]
                )
            tables.append((sheet.title, matrix))
            section_rows[sheet.title] = _hierarchical_section_rows(matrix, formats)
        return _combine_tables(
            tables, header_alias_groups=header_alias_groups, section_rows=section_rows, cell_metadata=cell_metadata
        )
    finally:
        workbook.close()
        formulas.close()


def extract_rows_from_xls(content, *, header_alias_groups):
    try:
        import xlrd
    except ImportError as error:
        raise FileExtractionError("Не установлен модуль чтения XLS. Сохраните файл как XLSX.") from error
    try:
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    except Exception as error:
        raise UnreadableSpreadsheetError(
            "Не удалось прочитать Excel-файл XLS. Проверьте файл или сохраните файл как .xlsx."
        ) from error
    try:
        tables = []
        physical = 0
        cell_metadata = {}
        if workbook.nsheets > 100:
            raise FileExtractionError("Слишком много листов Excel (максимум 100).")
        for sheet in workbook.sheets():
            if sheet.visibility:
                continue
            physical += sheet.nrows
            if physical > MAX_PHYSICAL_ROWS:
                raise FileExtractionError("Файл слишком большой: слишком много строк, включая пустые.")
            if sheet.ncols > 512:
                raise FileExtractionError("Слишком много колонок Excel (максимум 512).")
            matrix = [[_clean_text(cell) for cell in sheet.row_values(i)] for i in range(sheet.nrows)]
            cell_metadata[sheet.name] = {
                i + 1: [
                    {
                        "column": j + 1,
                        "value": _raw_cell_value(sheet.cell_value(i, j)),
                        "data_type": sheet.cell_type(i, j),
                    }
                    for j in range(sheet.ncols)
                ]
                for i in range(sheet.nrows)
            }
            tables.append((sheet.name, matrix))
        return _combine_tables(tables, header_alias_groups=header_alias_groups, cell_metadata=cell_metadata)
    finally:
        workbook.release_resources()


def extract_rows_from_doc(content, *, header_alias_groups):
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".doc") as file:
            file.write(content)
            path = file.name
        result = subprocess.run(["antiword", "-m", "UTF-8.txt", path], capture_output=True, timeout=60, check=False)
        if result.returncode:
            raise FileExtractionError("Не удалось прочитать документ DOC. Сохраните его как DOCX.")
        return extract_rows_from_txt(result.stdout, header_alias_groups=header_alias_groups)
    except FileNotFoundError as error:
        raise FileExtractionError("Не установлен конвертер DOC. Сохраните документ как DOCX.") from error
    except subprocess.TimeoutExpired as error:
        raise FileExtractionError("Превышено время чтения DOC. Сохраните документ как DOCX.") from error
    finally:
        if path:
            os.unlink(path)


def extract_rows_from_pdf(content, *, header_alias_groups, source_file_uri=None):
    extractor = str(
        getattr(settings, "FILE_IMPORT_PDF_EXTRACTOR", None)
        or getattr(settings, "SUPPLIER_PRICE_PDF_EXTRACTOR", "auto")
        or "auto"
    ).lower()
    if extractor in {"auto", "docling", "docling_pdf"}:
        try:
            markdown_text = _docling_markdown_from_pdf(content, source_file_uri=source_file_uri)
        except Exception as error:
            if extractor in {"docling", "docling_pdf"}:
                raise
            logger.warning("Docling PDF extraction failed, falling back to pdfplumber: %s", error)
        else:
            return extract_rows_from_markdown_text(markdown_text, header_alias_groups=header_alias_groups)

    return extract_rows_from_pdf_with_pdfplumber(content, header_alias_groups=header_alias_groups)


def _docling_markdown_from_pdf(content, *, source_file_uri=None):
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as error:
        raise ValueError("Docling is required for PDF extraction.") from error

    path = _source_file_path_for_docling(content, source_file_uri=source_file_uri)
    remove_after = source_file_uri is None or not _storage_has_local_path(source_file_uri)
    try:
        result = DocumentConverter().convert(path)
        document = getattr(result, "document", None)
        if document and hasattr(document, "export_to_markdown"):
            return document.export_to_markdown()
        if document and hasattr(document, "export_to_text"):
            return document.export_to_text()
        return str(result or "")
    finally:
        if remove_after:
            try:
                os.unlink(path)
            except OSError:
                pass


def _storage_has_local_path(source_file_uri):
    if not source_file_uri:
        return False
    try:
        default_storage.path(source_file_uri)
    except (NotImplementedError, FileNotFoundError, ValueError):
        return False
    return True


def _source_file_path_for_docling(content, *, source_file_uri=None):
    if source_file_uri and _storage_has_local_path(source_file_uri):
        return default_storage.path(source_file_uri)

    temporary_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        temporary_file.write(content)
        return temporary_file.name
    finally:
        temporary_file.close()


def extract_rows_from_markdown_text(markdown_text, *, header_alias_groups):
    result = ExtractedRows()
    pending = []
    offset = 0

    def flush():
        nonlocal offset
        if pending:
            block = _rows_from_matrix(pending, header_alias_groups=header_alias_groups)
            result.extend(replace(row, row_number=offset + row.row_number) for row in block)
            offset += len(pending)
            pending.clear()

    for line in str(markdown_text or "").splitlines():
        cells = _split_markdown_table_line(line.strip()) if "|" in line else []
        if len(cells) >= 2:
            if not _is_markdown_separator_row(cells):
                pending.append(cells)
        else:
            flush()
            for row in _text_lines_to_rows([line]):
                offset += 1
                result.append(replace(row, row_number=offset))
        _check_row_limit(result)
    flush()
    _check_row_limit(result)
    return result


def _markdown_table_rows(markdown_text):
    rows = []
    for line in str(markdown_text or "").splitlines():
        stripped = line.strip()
        if "|" not in stripped:
            continue
        cells = _split_markdown_table_line(stripped)
        if len(cells) < 2 or _is_markdown_separator_row(cells):
            continue
        rows.append(cells)
    return rows


def _split_markdown_table_line(line):
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_clean_text(cell) for cell in line.split("|")]


def _is_markdown_separator_row(cells):
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells if cell)


def _text_lines_to_rows(lines):
    rows = []
    for index, line in enumerate(lines, start=1):
        clean = _clean_text(line)
        if clean:
            rows.append(RawImportRow(row_number=index, cells={"text": clean}))
        _check_row_limit(rows)
    return ExtractedRows(rows)


def extract_rows_from_pdf_with_pdfplumber(content, *, header_alias_groups):
    if pdfplumber is None:
        raise FileExtractionError("Не установлен модуль чтения PDF.")
    rows = ExtractedRows()
    offset = 0
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            for table_number, table in enumerate(tables, start=1):
                matrix = table.extract()
                block = _rows_from_matrix(matrix, header_alias_groups=header_alias_groups)
                rows.extend(
                    replace(
                        row,
                        row_number=offset + row.row_number,
                        source={**row.source, "page": page_number, "table": table_number},
                    )
                    for row in block
                )
                offset += len(matrix)
            # Exclude characters inside table boxes; retain text outside tables.
            text_page = (
                page.filter(
                    lambda obj: (
                        not any(
                            obj.get("x0", -1) >= t.bbox[0]
                            and obj.get("x1", -1) <= t.bbox[2]
                            and obj.get("top", -1) >= t.bbox[1]
                            and obj.get("bottom", -1) <= t.bbox[3]
                            for t in tables
                        )
                    )
                )
                if tables
                else page
            )
            text = text_page.extract_text() or ""
            if not text.strip() and not tables and page.images:
                image = page.to_image(resolution=200).original
                try:
                    text = _ocr_text(image)
                finally:
                    image.close()
                block = extract_rows_from_txt(text.encode(), header_alias_groups=COMMON_HEADER_ALIAS_GROUPS)
            else:
                block = _text_lines_to_rows(text.splitlines())
            rows.extend(replace(row, row_number=offset + row.row_number, source={"page": page_number}) for row in block)
            offset += len(text.splitlines())
            _check_row_limit(rows)
    return rows


def extract_rows_from_docx(content, *, header_alias_groups):
    if DocxDocument is None:
        raise FileExtractionError("Не установлен модуль чтения DOCX.")
    doc = DocxDocument(BytesIO(content))
    from docx.table import Table

    rows = ExtractedRows()
    offset = 0
    for block_number, block in enumerate(doc.iter_inner_content(), start=1):
        if isinstance(block, Table):
            matrix = [[_clean_text(cell.text) for cell in row.cells] for row in block.rows]
            extracted = _rows_from_matrix(matrix, header_alias_groups=header_alias_groups)
            size = len(matrix)
        else:
            extracted = _text_lines_to_rows(block.text.splitlines())
            size = len(block.text.splitlines())
        rows.extend(
            replace(row, row_number=offset + row.row_number, source={**row.source, "block": block_number})
            for row in extracted
        )
        offset += size
        _check_row_limit(rows)
    return rows


def extract_rows_from_txt(content, *, header_alias_groups):
    text = _decode_csv_content(content)
    delimiter = _detect_csv_delimiter(text)
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if first_line.count(delimiter) >= 1:
        rows = extract_rows_from_csv(content, header_alias_groups=header_alias_groups)
        # A comma inside an ordinary product name is not a table header.
        if rows and getattr(rows, "metadata", {}).get("header_row_number"):
            return rows
    return _text_lines_to_rows(text.splitlines())


def _ocr_text(image):
    if pytesseract is None:
        raise FileExtractionError("Не установлен модуль распознавания изображений.")
    try:
        return pytesseract.image_to_string(image, lang="rus+eng")
    except pytesseract.TesseractNotFoundError as error:
        raise FileExtractionError("Не установлен Tesseract OCR для распознавания изображения.") from error
    except pytesseract.TesseractError as error:
        raise FileExtractionError(
            "Не удалось распознать изображение. Проверьте установку OCR и языков rus/eng."
        ) from error


def extract_rows_from_image(content):
    if Image is None:
        raise FileExtractionError("Не установлен модуль распознавания изображений.")
    with Image.open(BytesIO(content)) as image:
        text = _ocr_text(image)
    return extract_rows_from_txt(text.encode(), header_alias_groups=COMMON_HEADER_ALIAS_GROUPS)


def _hierarchical_section_rows(matrix, formats):
    from file_imports.structure import _hierarchical_section_rows as implementation

    return implementation(matrix, formats)


def _rows_from_matrix(raw_rows, *, header_alias_groups, section_rows=None):
    from file_imports.structure import _rows_from_matrix as implementation

    return implementation(raw_rows, header_alias_groups=header_alias_groups, section_rows=section_rows)


def _is_name_header(value):
    from file_imports.structure import _is_name_header as implementation

    return implementation(value)


def _detect_header(non_empty_rows, *, header_alias_groups):
    from file_imports.structure import _detect_header as implementation

    return implementation(non_empty_rows, header_alias_groups=header_alias_groups)


def _merge_header_continuation(non_empty_rows, header_index, headers):
    from file_imports.structure import _merge_header_continuation as implementation

    return implementation(non_empty_rows, header_index, headers)


def _dedupe_headers(headers):
    from file_imports.structure import _dedupe_headers as implementation

    return implementation(headers)


def raw_rows_to_payload(rows):
    return [{"row_number": row.row_number, "cells": row.cells, "source": row.source} for row in rows]


def raw_rows_from_payload(payload_rows):
    rows = []
    for index, row in enumerate(payload_rows or [], start=1):
        cells = row.get("cells") if isinstance(row, dict) else None
        if not isinstance(cells, dict):
            continue
        row_number = row.get("row_number") if isinstance(row, dict) else None
        rows.append(
            RawImportRow(
                row_number=int(row_number) if str(row_number or "").isdigit() else index,
                source=row.get("source") if isinstance(row.get("source"), dict) else {},
                cells={_clean_text(key): _clean_text(value) for key, value in cells.items() if _clean_text(value)},
            )
        )
    return rows


def with_row_number(row, row_number):
    return replace(row, row_number=row_number)


def _raw_cell_value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _display_cell_value(value, number_format):
    # Recover deliberately formatted numeric SKU zeros without inventing zeros from precision formats.
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and float(value).is_integer()
        and re.fullmatch(r"0+", number_format or "")
    ):
        return str(int(value)).zfill(len(number_format))
    return _clean_text(value)
