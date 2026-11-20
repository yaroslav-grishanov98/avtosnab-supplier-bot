"""Provider-agnostic company dedup — works with data from dadata, ФНС,
manual entry, or any future provider. Only cares about fields that are
common across all serious Russian company-lookup providers (ИНН, КПП,
ОГРН, название, адрес, статус), so switching providers later doesn't
require touching this module.

Adapted from a comparison of DaData alternatives commissioned by the user
(2026-07-23) — see docs/wiki/logs/2026-07-23-supplier-dedup-algorithm.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

SAME_BRANCH = "same_branch"
SAME_COMPANY = "same_company"
POSSIBLE_DUPLICATE = "possible_duplicate"
DIFFERENT = "different"

_POSSIBLE_DUPLICATE_THRESHOLD = 0.80

_LEGAL_FORM_PATTERNS = [
    # Longer/more specific phrases first — "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО"
    # must match before the plain "АКЦИОНЕРНОЕ ОБЩЕСТВО" pattern below, or
    # the shorter pattern eats the "АКЦИОНЕРНОЕ ОБЩЕСТВО" tail first and
    # leaves a dangling "ПУБЛИЧНОЕ" that never gets stripped.
    r"\bОБЩЕСТВО\s+С\s+ОГРАНИЧЕННОЙ\s+ОТВЕТСТВЕННОСТЬЮ\b",
    r"\bПУБЛИЧНОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b",
    r"\bНЕПУБЛИЧНОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b",
    r"\bАКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b",
    r"\bООО\b",
    r"\bАО\b",
    r"\bПАО\b",
    r"\bНАО\b",
]


@dataclass
class CompanyCanonical:
    inn: Optional[str] = None
    kpp: Optional[str] = None
    ogrn: Optional[str] = None
    full_name: Optional[str] = None
    address: Optional[str] = None
    status: Optional[str] = None


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""

    text = value.upper().strip()
    text = text.replace("Ё", "Е")
    text = re.sub(r"[\"'`«».,:;()\\/#+-]", " ", text)
    text = re.sub(r"\s+", " ", text)

    for pattern in _LEGAL_FORM_PATTERNS:
        text = re.sub(pattern, " ", text)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a: Optional[str], b: Optional[str]) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def dedup_decision(existing: CompanyCanonical, incoming: CompanyCanonical) -> tuple[str, float]:
    """Compares an incoming company against one existing candidate.

    Returns (label, confidence) where label is one of SAME_BRANCH,
    SAME_COMPANY, POSSIBLE_DUPLICATE, DIFFERENT.
    """
    if existing.inn and incoming.inn and existing.inn == incoming.inn:
        if existing.kpp and incoming.kpp and existing.kpp == incoming.kpp:
            return SAME_BRANCH, 0.99
        if not existing.kpp or not incoming.kpp:
            return SAME_COMPANY, 0.95
        return POSSIBLE_DUPLICATE, 0.90

    if existing.ogrn and incoming.ogrn and existing.ogrn == incoming.ogrn:
        return SAME_COMPANY, 0.97

    name_score = similarity(normalize_text(existing.full_name), normalize_text(incoming.full_name))
    addr_score = similarity(normalize_text(existing.address), normalize_text(incoming.address))
    score = (name_score * 0.65) + (addr_score * 0.35)

    if existing.status and incoming.status and existing.status != incoming.status:
        score *= 0.9

    if score >= _POSSIBLE_DUPLICATE_THRESHOLD:
        return POSSIBLE_DUPLICATE, round(score, 2)

    return DIFFERENT, round(score, 2)
