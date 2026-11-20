"""Страна происхождения в тексте строки прайса и в заголовке раздела.

Название страны в самой строке — свидетельство (D5): ищем с учётом регистра и только
как отдельную клаузу («…, Китай», «(Австрия)», «500гр Китай»), чтобы слова «Чили»
(перец, соус), «российский», «Французская» и «Китайская капуста» не принимались за страну.
Исключения, где контекст не нужен: заглавные сокращения (РФ, РБ, КНР, США, ОАЭ) и явный
маркер («пр-во», «производство», «made in», «страна происхождения»).
"""

import re
from dataclasses import dataclass

from core.countries import COUNTRY_ALIASES, normalize_country

ROW_COUNTRY_PATTERN = (
    "Россия|Беларусь|Белоруссия|Казахстан|Украина|Молдова|Грузия|Армения|Азербайджан|Узбекистан|Киргизия|Китай|"
    "Вьетнам|Таиланд|Тайланд|Индия|Индонезия|Малайзия|Филиппины|Шри-Ланка|Пакистан|Япония|Монголия|Турция|"
    "Египет|Марокко|Тунис|Израиль|Иран|Испания|Италия|Франция|Германия|Польша|Нидерланды|Бельгия|Греция|"
    "Португалия|Австрия|Швейцария|Норвегия|Финляндия|Дания|Швеция|Исландия|Литва|Латвия|Эстония|Чехия|Венгрия|"
    "Болгария|Румыния|Сербия|Македония|Эквадор|Перу|Аргентина|Бразилия|Колумбия|Коста-Рика|Мексика|ЮАР|"
    "Новая Зеландия|Австралия|США|Эфиопия|Кения|Куба"
)
COUNTRY_IN_ROW = re.compile(rf"(?<![\w-])(?P<country>{ROW_COUNTRY_PATTERN})(?![\w-])")
COUNTRY_CONTEXT = re.compile(r"(?:[,()]|\d|кг|гр?|мл|л|шт)\s*$")
# Заглавные сокращения однозначны сами по себе, клауза не нужна.
COUNTRY_ABBREVIATION_IN_ROW = re.compile(r"(?<![\w-])(?P<country>РФ|РБ|КНР|США|ОАЭ)(?![\w-])")

_MARKER = r"(?:пр-во|производство|made\s+in|product\s+of|страна\s+происхождения|страна[\s-]+производитель|страна\s*:)"
# После маркера страна тоже должна быть написана как название (D5, с учётом регистра): «производство того же завода»
# и «пр-во мали» — не страны. Допустимы «Италия»/«Italy»/«Hong Kong» и заглавные сокращения; без учёта регистра — только маркер.
_UPPERCASE_ALIASES = {"рф", "рб", "кнр", "юар", "usa", "uae", "uk"}


def _written_forms(alias):
    forms = {alias.capitalize(), alias.title()}
    if alias in _UPPERCASE_ALIASES or alias.upper() == COUNTRY_ALIASES[alias]:
        forms.add(alias.upper())
    return forms


_COUNTRY_FORMS = sorted({form for alias in COUNTRY_ALIASES for form in _written_forms(alias)}, key=len, reverse=True)
# Possessive `\s*+`: the separator never gives spaces back, so a long run of spaces after a marker is linear, not
# quadratic (the same texts match as with «\s*[:-]?\s*»).
COUNTRY_AFTER_MARKER = re.compile(
    rf"(?<![\w-])(?i:{_MARKER})\.?\s*+(?:[:\-–—]\s*+)?(?P<country>{'|'.join(map(re.escape, _COUNTRY_FORMS))})(?![\w-])"
)
# Маркер в заголовке раздела: «Продукция Беларуси», «Производство Россия», «Страна-производитель: Китай».
_HEADING_MARKER = re.compile(
    rf"^(?:{_MARKER}|страна|продукция)\.?(?:\s*[:\-–—]\s*|\s+)",
    re.I,
)


@dataclass(frozen=True)
class CountryHit:
    value: str
    evidence: str
    span: tuple[int, int]


def _hit(match):
    """Попадание только если токен — страна из COUNTRY_NAMES (иначе None, например «Корея» неоднозначна)."""
    token = match["country"]
    value = normalize_country(token)
    return CountryHit(value=value, evidence=token, span=match.span("country")) if value else None


def find_country_in_text(text):
    """Страна, названная в тексте строки, или None. Прилагательные («российский») не считаются."""
    if not text:
        return None
    hits = [_hit(match) for match in COUNTRY_AFTER_MARKER.finditer(text)]
    hits += [_hit(match) for match in COUNTRY_ABBREVIATION_IN_ROW.finditer(text)]
    hits += [
        _hit(match) for match in COUNTRY_IN_ROW.finditer(text) if COUNTRY_CONTEXT.search(text[: match.start()].rstrip())
    ]
    hits = [hit for hit in hits if hit]
    return min(hits, key=lambda hit: hit.span) if hits else None


def country_from_heading(text):
    """Страна, которую целиком называет заголовок раздела (с маркером или без), или None.

    «Чили» без маркера — не страна (соус, перец), «Продукция Чили» — страна.
    """
    if not text:
        return None
    value = str(text).strip().rstrip(".:").strip()
    marker = _HEADING_MARKER.match(value)
    if marker:
        value = value[marker.end() :]
    country = normalize_country(value)
    if country == "Чили" and not marker:
        return None
    return country
