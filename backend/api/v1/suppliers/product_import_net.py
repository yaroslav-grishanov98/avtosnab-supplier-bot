"""Нетто (вес/объём) из названия товара -- детерминированно, без LLM.

Модель раньше «подтверждала» нетто, но в 99% случаев (замер 2026-10-01, 3174 ответа) значение однозначно
берётся из текста: число с единицей. Эти функции -- то же правило, что раньше проверяло ответ модели
(product_import_llm._checked_net), вынесенное из вызова модели."""

import re
from decimal import Decimal, InvalidOperation

# «250г», «0,5 л», «1.5кг», «250 гр»; слева и справа не должно быть букв (не цеплять «г» из слов).
_NET_TOKEN_RE = re.compile(
    r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(кг|килограмм\w*|гр|грамм\w*|г|мл|миллилитр\w*|л|литр\w*)(?![\w])",
    re.IGNORECASE,
)
_NET_UNIT_ALIASES = {
    "кг": "кг",
    "килограмм": "кг",
    "гр": "г",
    "грамм": "г",
    "г": "г",
    "мл": "мл",
    "миллилитр": "мл",
    "л": "л",
    "литр": "л",
}
NET_BASE_FACTORS = {"кг": Decimal("1"), "г": Decimal("0.001"), "л": Decimal("1"), "мл": Decimal("0.001")}


def net_tokens_from_name(name):
    """Все «число + вес/объём» из названия: [(value, unit_code)]."""
    tokens = []
    for match in _NET_TOKEN_RE.finditer(name or ""):
        raw_unit = match.group(2).lower()
        unit_code = next((code for prefix, code in _NET_UNIT_ALIASES.items() if raw_unit.startswith(prefix)), None)
        if unit_code is None:
            continue
        try:
            value = Decimal(match.group(1).replace(",", "."))
        except InvalidOperation:
            continue
        if value > 0:
            tokens.append((value, unit_code))
    return tokens


def net_from_name(name):
    """(value, unit_code), если в названии ровно одно «число + вес/объём»; несколько -- неоднозначно -> None."""
    tokens = net_tokens_from_name(name)
    return tokens[0] if len(tokens) == 1 else None
