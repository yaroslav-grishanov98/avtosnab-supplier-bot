"""Cheap table-quality checks for supplier imports.

They only warn or flag rows; they never block an import or change parsed values.
"""

import re
from decimal import Decimal, InvalidOperation

from django.conf import settings

# Share of data rows that must carry a price; below it the session gets a warning. Tune on real price lists
# (settings.SUPPLIER_IMPORT_MIN_PRICE_SHARE overrides it).
MIN_PRICE_SHARE = Decimal("0.5")
# Tiny files are too noisy for a share to mean anything.
MIN_ROWS_FOR_PRICE_SHARE = 5

WARNING_LOW_PRICE_SHARE = "low_price_share"
WARNING_SHEET_SKIPPED = "sheet_skipped"
ISSUE_NUMERIC_NAME = "numeric_name"
NUMERIC_NAME_MESSAGE = "Название состоит только из числа: возможно, колонки сдвинуты. Проверьте строку."

_NUMERIC_NAME = re.compile(r"[\s\d.,:/\\\-–—+%№#*()]*")
_NUMBER_CELL = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?")
_WORDS = re.compile(r"[^\W\d_]{3,}")


def min_price_share():
    value = getattr(settings, "SUPPLIER_IMPORT_MIN_PRICE_SHARE", None)
    try:
        return Decimal(str(value)) if value is not None else MIN_PRICE_SHARE
    except InvalidOperation:
        return MIN_PRICE_SHARE


def is_numeric_name(name):
    """A name without a single letter (e.g. «1250», «12,5», «2024-01») is a column-shift symptom, not a product."""
    text = str(name or "").strip()
    return bool(text) and any(ch.isdigit() for ch in text) and _NUMERIC_NAME.fullmatch(text) is not None


def price_share_warning(total, with_price):
    """Session-level warning dict when too few data rows have a price, else None."""
    if total < MIN_ROWS_FOR_PRICE_SHARE:
        return None
    share = Decimal(with_price) / Decimal(total)
    threshold = min_price_share()
    if share >= threshold:
        return None
    return {
        "code": WARNING_LOW_PRICE_SHARE,
        "message": (
            f"Цена найдена только в {with_price} из {total} строк ({int(share * 100)}%). "
            "Проверьте, что выбран нужный лист и колонки не сдвинуты."
        ),
        "rows_total": total,
        "rows_with_price": with_price,
    }


def sheet_product_score(matrix):
    """Rows that look like a product with a price: a word-like cell plus a separate numeric cell."""
    score = 0
    for row in matrix:
        cells = [str(cell).strip() for cell in row if str(cell).strip()]
        if len(cells) < 2:
            continue
        if any(_WORDS.search(cell) for cell in cells) and any(_NUMBER_CELL.fullmatch(cell) for cell in cells):
            score += 1
    return score


def pick_best_sheet(tables):
    """Among sheets without a recognised header: drop empty ones, keep the one with most product-like rows.

    Returns the chosen (title, matrix) tables; all non-empty sheets when none looks like a product list.
    """
    non_empty = [t for t in tables if any(str(c).strip() for row in t[1] for c in row)]
    if len(non_empty) <= 1:
        return non_empty or list(tables)
    scored = [(sheet_product_score(t[1]), t) for t in non_empty]
    best = max(score for score, _ in scored)
    if best == 0:
        return non_empty
    return [next(t for score, t in scored if score == best)]
