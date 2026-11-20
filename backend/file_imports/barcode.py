"""Штрихкоды из ячеек прайса и подсказка страны по GS1 Prefix.

GS1 Prefix — это страна, где зарегистрирован код, а не где товар сделан, поэтому
`gs1_country` даёт только слабую подсказку (см. file_imports.gs1_prefixes).

Формы кода (`Barcode.kind`):
- `ean13`: 13 цифр;
- `upca`: 12 цифр, спереди добавляется `0` (value хранит 13 цифр). EAN-13 с ведущим нулём,
  у которого Excel «съел» ноль, неотличим от UPC-A и даёт тот же результат;
- `ean8`: 8 цифр (GS1 Prefix у него тоже первые три цифры);
- `gtin14`: 14 цифр; value — 13 цифр без первой цифры-индикатора упаковки, контрольная цифра
  проверяется по всем 14 цифрам, страна берётся по цифрам 2-4 (то есть по value[:3]).
  Внимание: value не является самостоятельным EAN-13 (его последняя цифра — контрольная
  цифра GTIN-14).

11-значное число считается невалидным кодом без попытки дополнить нулём: UPC-A, у которого
Excel потерял ведущий ноль, неотличим от любого другого короткого числа, угадывать нельзя.
Цифровые серии других длин (9, 10, 15 и больше) штрихкодом не считаются, возвращается [].
"""

import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from file_imports.gs1_prefixes import GS1_PREFIX_RANGES

# Префиксы, которые никогда не дают страну: внутренний оборот и служебные диапазоны.
IGNORED_PREFIX_RANGES = (
    (20, 29),  # restricted circulation в регионе
    (40, 49),  # restricted circulation в компании
    (50, 59),  # резерв GS1 US
    (200, 299),  # restricted circulation в регионе
    (960, 969),  # GTIN-8, Global Office
    (977, 979),  # ISSN, ISBN
    (980, 999),  # возвраты, купоны
)

_SINGLE_RUN_LENGTHS = frozenset({8, 12, 13, 14})
_JOINED_LENGTHS = frozenset({12, 13, 14})
_MAX_LENGTH = 14
_MAX_NUMBER = 10**_MAX_LENGTH
# A longer string is not a barcode cell; skipping it keeps parsing cheap and avoids int() limits.
_MAX_TEXT_LENGTH = 5000
_MAX_SCIENTIFIC_LENGTH = 64
_SCIENTIFIC = re.compile(r"^\s*(\d+)(?:[.,](\d*))?[eE]\+?(\d+)\s*$")
_DECIMAL_TAIL = re.compile(r"(?<=\d)\.0+(?!\d)")
_NON_CODE_CHARS = re.compile(r"[^\d\s\-]+")
_ONLY_NUMBER_CHARS = re.compile(r"^[\d\s\-]*$")
_DIGIT_RUN = re.compile(r"\d+")
_PREFIX_STARTS = [lo for lo, _hi, _country in GS1_PREFIX_RANGES]


@dataclass(frozen=True)
class Barcode:
    value: str
    kind: str  # ean13 | ean8 | upca | gtin14
    valid: bool


def _check_digit_ok(digits: str) -> bool:
    """GS1 mod-10: веса 3,1,3,... справа налево, считая без последней цифры."""
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _classify(digits: str, *, allow_short: bool) -> Barcode | None:
    size = len(digits)
    if size == 13:
        return Barcode(digits, "ean13", _check_digit_ok(digits))
    if size == 12:
        padded = "0" + digits
        return Barcode(padded, "upca", _check_digit_ok(padded))
    if size == 8:
        return Barcode(digits, "ean8", _check_digit_ok(digits))
    if size == 14:
        return Barcode(digits[1:], "gtin14", _check_digit_ok(digits))
    if size == 11 and allow_short:
        return Barcode(digits, "upca", False)
    return None


def _digits_from_number(raw: object) -> str | None:
    """Целые цифры числа (int, float, Decimal) или None, если это не неотрицательное целое."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        # Bound before str(): a huge int would raise ValueError (4300-digit limit).
        return str(raw) if 0 <= raw < _MAX_NUMBER else None
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        raw = Decimal(repr(raw))
    if isinstance(raw, Decimal):
        if not raw.is_finite() or raw < 0 or raw.adjusted() >= _MAX_LENGTH:
            return None
        if raw != raw.to_integral_value():
            return None
        return str(int(raw))
    return None


def _digits_from_scientific(text: str) -> str | None:
    """`4.605496029632E12` -> `4605496029632`; None, если запись не научная или потеряла цифры."""
    if len(text) > _MAX_SCIENTIFIC_LENGTH:
        return None
    match = _SCIENTIFIC.match(text)
    if not match:
        return None
    integer, fraction, exponent = match.group(1), match.group(2) or "", int(match.group(3))
    if exponent > 30:
        return None
    mantissa_digits = len((integer + fraction).rstrip("0")) or 1
    try:
        value = Decimal(f"{integer}.{fraction or '0'}E{exponent}")
    except InvalidOperation:
        return None
    if value != value.to_integral_value():
        return None
    digits = str(int(value))
    # Excel в общем формате отбрасывает хвостовые нули мантиссы. Допускаем потерю одного нуля;
    # «4.6055E12» потеряла почти весь код, дописывать нули — выдумывать код.
    if len(digits) - mantissa_digits > 1:
        return None
    return digits


def _split_digit_groups(groups: list[str]) -> list[str]:
    """Разбить подряд идущие группы цифр (разделённые пробелами/дефисами) на коды.

    Длинные куски склеиваются в 12-14 цифр (`4 605496 029632`), одиночная группа может быть
    ещё и EAN-8. Выбирается разбиение, которое охватывает больше цифр, при равенстве — с
    меньшим числом кодов; группы, не вошедшие ни в один код, пропускаются.
    """
    count = len(groups)
    best: list[tuple[int, int, list[str]]] = [(0, 0, [])] * (count + 1)
    for start in range(count - 1, -1, -1):
        options = [best[start + 1]]
        current = ""
        for end in range(start, count):
            current += groups[end]
            if len(current) > _MAX_LENGTH:
                break
            allowed = _SINGLE_RUN_LENGTHS if end == start else _JOINED_LENGTHS
            if len(current) in allowed:
                rest = best[end + 1]
                options.append((rest[0] + len(current), rest[1] - 1, [current, *rest[2]]))
        best[start] = max(options, key=lambda option: (option[0], option[1]))
    return best[0][2]


def normalize_barcode(raw: object) -> list[Barcode]:
    """Все штрихкоды в ячейке (порядок сохраняется, повторы схлопываются). Не падает на мусоре."""
    number = _digits_from_number(raw)
    if number is not None:
        found = [_classify(number, allow_short=True)]
    elif isinstance(raw, str):
        if len(raw) > _MAX_TEXT_LENGTH:
            return []
        text = raw.strip()
        scientific = _digits_from_scientific(text)
        if scientific is not None:
            found = [_classify(scientific, allow_short=True)]
        else:
            only_number = bool(_ONLY_NUMBER_CHARS.match(_DECIMAL_TAIL.sub("", text)))
            found = []
            for segment in _NON_CODE_CHARS.split(_DECIMAL_TAIL.sub("", text)):
                groups = _DIGIT_RUN.findall(segment)
                if groups:
                    found.extend(_classify(code, allow_short=only_number) for code in _split_digit_groups(groups))
    else:
        return []

    result: list[Barcode] = []
    for barcode in found:
        if barcode is not None and barcode not in result:
            result.append(barcode)
    return result


def gs1_country(barcode: Barcode) -> str | None:
    """Страна регистрации GS1 Prefix или None (невалидный код, служебный/неизвестный префикс).

    Это страна регистрации кода, а не гарантированная страна производства.
    """
    if not barcode.valid:
        return None
    if barcode.kind == "ean8" and barcode.value[:1] in ("0", "2"):
        # EAN-8 starting with 0 or 2 is a restricted circulation number (RCN-8), not a GS1 Prefix.
        return None
    head = barcode.value[:3]
    if len(head) != 3 or not head.isdigit():
        return None
    prefix = int(head)
    if any(lo <= prefix <= hi for lo, hi in IGNORED_PREFIX_RANGES):
        return None
    index = bisect_right(_PREFIX_STARTS, prefix) - 1
    if index < 0:
        return None
    lo, hi, country = GS1_PREFIX_RANGES[index]
    return country if lo <= prefix <= hi else None
