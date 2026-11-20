"""Table structure and row classification, separated from physical file reading.

Legacy extraction helpers delegate here to retain existing callers.
"""

import re

from file_imports.extraction import (
    COMMON_HEADER_ALIAS_GROUPS,
    HEADER_DETECTION_SCAN_ROWS,
    FileExtractionError,
    _check_row_limit,
    _clean_text,
    _matches_any_alias,
    _normalize_key,
    _raw_cell_value,
)
from file_imports.types import ExtractedRows, RawImportRow


class SectionRows(set):
    """Physical numbers of section rows; ``levels`` maps each row number to its indent.

    ``row_levels`` maps the other (product) rows with a filled name cell to their indent: a product at the indent of
    a heading or shallower is outside that heading."""

    def __init__(self, rows=(), levels=None, row_levels=None):
        super().__init__(rows)
        self.levels = dict(levels or {})
        self.row_levels = dict(row_levels or {})


def _hierarchical_section_rows(matrix, formats):
    """Recognize formatted 1C group rows, retaining ordinary unpriced products.

    A group has only a name, a shaded name cell (at any indent level, including 0)
    and deeper product rows below it. Missing price alone never makes a row a group.
    """
    nonempty = [(i + 1, row) for i, row in enumerate(matrix) if any(row)]
    header_index, headers = _detect_header(nonempty, header_alias_groups=COMMON_HEADER_ALIAS_GROUPS)
    if header_index < 0:
        return SectionRows()
    name_column = next(i for i, key in enumerate(headers) if _is_name_header(key))
    result = set()
    descendants = []
    for position in range(len(nonempty) - 1, header_index, -1):
        number, row = nonempty[position]
        if not row[name_column]:
            continue
        indent, shaded, _ = formats[number - 1][name_column]
        has_product = False
        while descendants and descendants[-1][0] > indent:
            has_product |= descendants.pop()[1]
        if descendants and descendants[-1][0] == indent:
            descendants.pop()
        name_only = sum(bool(value) for value in row) == 1
        if name_only and shaded and has_product:
            result.add(number)
        descendants.append((indent, has_product or not name_only))
    group_formats = {formats[number - 1][name_column] for number in result}
    # 1C sometimes emits consecutive group labels at the same or lower indent.
    # Reuse only group formatting established by actual deeper product rows.
    result.update(
        number
        for number, row in nonempty[header_index + 1 :]
        if row[name_column]
        and sum(bool(value) for value in row) == 1
        and formats[number - 1][name_column] in group_formats
    )
    return SectionRows(
        result,
        {number: formats[number - 1][name_column][0] for number in result},
        row_levels={
            number: formats[number - 1][name_column][0]
            for number, row in nonempty[header_index + 1 :]
            if row[name_column] and number not in result
        },
    )


def _rows_from_matrix(raw_rows, *, header_alias_groups, section_rows=None):
    non_empty_rows = [(i + 1, list(row)) for i, row in enumerate(raw_rows) if any(_clean_text(cell) for cell in row)]
    if not non_empty_rows:
        return ExtractedRows()
    header_index, headers = _detect_header(non_empty_rows, header_alias_groups=COMMON_HEADER_ALIAS_GROUPS)
    original_index = header_index
    if original_index < 0:
        first_row = non_empty_rows[0][1]
        known_fields = sum(
            any(_matches_any_alias(cell, group) for cell in first_row) for group in COMMON_HEADER_ALIAS_GROUPS[1:]
        )
        if known_fields >= 1:
            raise FileExtractionError("Не найдена колонка с названием товара.")
    display = (
        dict(zip(headers, (_clean_text(v) for v in non_empty_rows[header_index][1]), strict=True))
        if header_index >= 0
        else {}
    )
    header_index, headers = _merge_header_continuation(non_empty_rows, header_index, headers)
    if header_index != original_index:
        display = {key: label for key, label in zip(headers, display.values())}
    first = non_empty_rows[header_index][0] + 1 if header_index >= 0 else non_empty_rows[0][0]
    last = non_empty_rows[-1][0]
    fallback_column = next((i for i, cell in enumerate(non_empty_rows[0][1]) if _clean_text(cell)), 0)
    extracted = ExtractedRows(
        metadata={
            "header_row_number": non_empty_rows[original_index][0] if original_index >= 0 else None,
            "header_columns": display,
            "blank_rows": 0,
            "section_rows_skipped": len(section_rows or ()),
            "excluded_rows": [
                {"row_number": number, "row_type": "section", "raw_cells": raw_rows[number - 1]}
                for number in sorted(section_rows or ())
            ],
            "fallback_name_column": f"column_{fallback_column + 1}",
        }
    )
    name_column = next((i for i, key in enumerate(headers) if _is_name_header(key)), fallback_column)
    levels = getattr(section_rows, "levels", None) or {}
    row_levels = getattr(section_rows, "row_levels", None) or {}
    # Stack of (level, heading) for the sections enclosing the current row. Never shared
    # between calls, so a path cannot leak from one sheet into the next.
    section_stack = []
    section_run_open = False
    for physical_row_number in range(first, last + 1):
        if physical_row_number in (section_rows or ()):
            section_row = raw_rows[physical_row_number - 1]
            heading = _clean_text(section_row[name_column] if name_column < len(section_row) else "") or next(
                (text for text in map(_clean_text, section_row) if text), ""
            )
            if heading:
                # Mixed/partial levels: a row without an indent falls back to run-based nesting.
                if physical_row_number in levels:
                    level = levels[physical_row_number]
                    while section_stack and section_stack[-1][0] >= level:
                        section_stack.pop()
                else:
                    # Without indents, the last run of consecutive headings is the whole path.
                    if not section_run_open:
                        section_stack.clear()
                    level = section_stack[-1][0] + 1 if section_stack else 0
                section_stack.append((level, heading))
                section_run_open = True
            continue
        row = raw_rows[physical_row_number - 1]
        if (
            original_index >= 0
            and any(_is_name_header(row[i]) for i, key in enumerate(headers) if _is_name_header(key) and i < len(row))
            and sum(
                any(_matches_any_alias(value, aliases) for value in row if value)
                for aliases in COMMON_HEADER_ALIAS_GROUPS
            )
            >= 2
        ):
            extracted.metadata["excluded_rows"].append(
                {"row_number": physical_row_number, "row_type": "header", "raw_cells": row}
            )
            continue
        if original_index >= 0 and [_normalize_key(v) for v in row] == [
            _normalize_key(v) for v in non_empty_rows[original_index][1]
        ]:
            continue
        cells = {
            header: value for i, header in enumerate(headers) if (value := _clean_text(row[i] if i < len(row) else ""))
        }
        if cells:
            if physical_row_number in row_levels:
                # A product at a heading's indent or shallower is outside that heading. Without indents the
                # stack is left as it is.
                while section_stack and section_stack[-1][0] >= row_levels[physical_row_number]:
                    section_stack.pop()
            extracted.append(
                RawImportRow(
                    row_number=physical_row_number,
                    cells=cells,
                    source={
                        "headerless": original_index < 0,
                        "fallback_name_column": f"column_{fallback_column + 1}",
                        "raw_cells": [{"column": i + 1, "value": _raw_cell_value(v)} for i, v in enumerate(row)],
                        "header_columns": display,
                        "section_path": [heading for _, heading in section_stack],
                    },
                )
            )
            section_run_open = False
            _check_row_limit(extracted)
        else:
            extracted.metadata["blank_rows"] += 1
    return extracted


def _is_name_header(value):
    key = _normalize_key(value)
    key = re.sub(r" в ус$", "", key)
    aliases = COMMON_HEADER_ALIAS_GROUPS[0] | {"название товара", "наименование товара", "номенклатура упаковка"}
    return key in aliases


def _detect_header(non_empty_rows, *, header_alias_groups):
    scan_limit = min(HEADER_DETECTION_SCAN_ROWS, len(non_empty_rows))
    best_index, best_score = -1, 0
    for row_index in range(scan_limit):
        row_values = non_empty_rows[row_index][1]
        # Строка с большим числом разных групп колонок -- настоящий заголовок
        # (над ним бывают шапки вроде «Номенклатура» в одну ячейку).
        score = sum(
            any(_matches_any_alias(cell, aliases) for cell in row_values if cell) for aliases in header_alias_groups
        )
        if score > best_score and any(_is_name_header(cell) for cell in row_values):
            best_index, best_score = row_index, score
    if best_index >= 0:
        row_values = non_empty_rows[best_index][1]
        headers = [_normalize_key(cell) or f"column_{index + 1}" for index, cell in enumerate(row_values)]
        return best_index, _dedupe_headers(headers)

    width = max((len(row) for _, row in non_empty_rows), default=0)
    return -1, [f"column_{index + 1}" for index in range(width)]


_HEADER_CONTINUATION_MAX_ROWS = 3
_HEADER_CONTINUATION_MAX_CELL_LENGTH = 30
_NUMERIC_CELL = re.compile(r"[\d\s.,%-]+")


def _merge_header_continuation(non_empty_rows, header_index, headers):
    """Шапка бывает в несколько строк («Цена» / «руб.» / «Включает НДС» под названием
    колонки, «Товар» и «Цена / Ед. / Валюта» строкой ниже). Строки сразу под заголовком,
    где только короткий нечисловой текст правее самой левой колонки заголовка, дописываются
    к ключам колонок. Строка данных (число, длинный текст, ячейка в первой колонке) обрывает
    склейку, поэтому строки-разделы и товары в неё не попадают."""
    if header_index < 0:
        return header_index, headers
    header_cells = non_empty_rows[header_index][1]
    filled_columns = [index for index, cell in enumerate(header_cells) if _clean_text(cell)]
    if not filled_columns:
        return header_index, headers
    leftmost_column = filled_columns[0]
    parts = [[_clean_text(cell)] if _clean_text(cell) else [] for cell in header_cells]
    consumed = 0
    for _, row in non_empty_rows[header_index + 1 : header_index + 1 + _HEADER_CONTINUATION_MAX_ROWS]:
        cells = [(index, _clean_text(cell)) for index, cell in enumerate(row) if _clean_text(cell)]
        is_continuation = bool(cells) and all(
            index > leftmost_column
            and len(text) <= _HEADER_CONTINUATION_MAX_CELL_LENGTH
            and not _NUMERIC_CELL.fullmatch(text)
            for index, text in cells
        )
        if not is_continuation:
            break
        for index, text in cells:
            while len(parts) <= index:
                parts.append([])
            old = " ".join(parts[index])
            if any(_matches_any_alias(text, group) for group in COMMON_HEADER_ALIAS_GROUPS) and not any(
                _matches_any_alias(old, group) for group in COMMON_HEADER_ALIAS_GROUPS
            ):
                parts[index] = [text]
            else:
                parts[index].append(text)
        consumed += 1
    if not consumed:
        return header_index, headers
    merged = [_normalize_key(" ".join(column)) or f"column_{index + 1}" for index, column in enumerate(parts)]
    return header_index + consumed, _dedupe_headers(merged)


def _dedupe_headers(headers):
    seen = {}
    result = []
    for header in headers:
        count = seen.get(header, 0)
        seen[header] = count + 1
        result.append(header if count == 0 else f"{header}_{count + 1}")
    return result
