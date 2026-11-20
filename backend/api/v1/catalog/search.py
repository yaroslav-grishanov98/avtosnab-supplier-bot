from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.postgres.search import TrigramWordSimilarity
from django.db import connection, transaction
from django.db.models import Case, FloatField, Func, IntegerField, Q, QuerySet, TextField, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.db.models.lookups import Contains

from api.v1.catalog.embeddings import vector_supplier_product_scores
from core.search_expressions import SearchNameKey, search_name_key

_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_PRICE_MAX_RE = re.compile(
    r"(?:до|дешевле|меньше|не\s+дороже|цена\s+до)\s*(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)

_STOPWORDS = {
    "товар",
    "товары",
    "найди",
    "покажи",
    "есть",
    "что",
    "из",
    "каталога",
    "руб",
    "рублей",
    "р",
}

_ALIASES = {
    "вишенка": "вишня",
    "вишенки": "вишня",
    "вешенка": "вешенки",
    "вешенки": "вешенки",
    "цедра": "цитрусовые",
    "кулбника": "клубника",
    "помидоы": "помидоры",
    "курциа": "курица",
}

_EN_TO_RU_KEYBOARD = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.",
    "йцукенгшщзхъфывапролджэячсмитьбю",
)

_UPPER_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
_LOWER_LETTERS = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_CASEFOLD_TABLE = str.maketrans(_UPPER_LETTERS, _LOWER_LETTERS)


class _Casefold(Func):
    """Locale-independent case fold for PostgreSQL installations with LC_CTYPE=C."""

    function = "TRANSLATE"
    output_field = TextField()

    def __init__(self, expression):
        super().__init__(expression, Value(_UPPER_LETTERS), Value(_LOWER_LETTERS))


@dataclass(frozen=True)
class ParsedSearchQuery:
    raw: str
    normalized: str
    tokens: tuple[str, ...]
    price_max: Decimal | None = None


@dataclass(frozen=True)
class SemanticRule:
    id: str
    triggers: tuple[str, ...]
    positive_terms: tuple[str, ...]
    category_terms: tuple[str, ...] = ()
    negative_terms: tuple[str, ...] = ()
    strict: bool = False


_SEMANTIC_RULES = (
    SemanticRule(
        id="mushrooms",
        triggers=("гриб", "грибы", "вешенка", "вешенки", "шампиньон", "шампиньоны"),
        positive_terms=("гриб", "вешен", "шампиньон"),
        category_terms=("гриб",),
        strict=True,
    ),
    SemanticRule(
        id="berries-cherry",
        triggers=("вишня", "вишенка", "вишенки", "черешня"),
        positive_terms=("вишн", "черешн"),
        category_terms=("ягод", "фрукт"),
        strict=True,
    ),
    SemanticRule(
        id="citrus",
        triggers=("цедра", "цитрус", "цитрусовые", "лимон", "апельсин", "лайм", "грейпфрут"),
        positive_terms=("цедр", "цитрус", "лимон", "апельсин", "лайм", "грейпфрут"),
        category_terms=("фрукт", "цитрус"),
        strict=True,
    ),
    SemanticRule(
        id="rum",
        triggers=("ром",),
        positive_terms=("ром",),
        category_terms=("алкоголь", "напит"),
        negative_terms=("аром", "заморож"),
        strict=True,
    ),
    SemanticRule(
        id="rice",
        triggers=("рис", "рисовый", "басмати", "жасмин", "арборио"),
        positive_terms=("рис", "рисов", "басмати", "жасмин", "арборио"),
        category_terms=("круп", "бакалея"),
        negative_terms=("аромат", "заморож"),
        strict=True,
    ),
    SemanticRule(
        id="coffee",
        triggers=("кофе", "эспрессо", "арабика", "робуста", "капучино", "латте"),
        positive_terms=("кофе", "эспрессо", "арабика", "робуста", "капучино", "латте"),
        category_terms=("напит", "кофе"),
        strict=True,
    ),
    SemanticRule(
        id="tea",
        triggers=("чай", "матча", "улун", "пуэр", "сенча", "каркаде"),
        positive_terms=("чай", "матча", "улун", "пуэр", "сенча", "каркаде"),
        category_terms=("напит", "чай"),
        strict=True,
    ),
    SemanticRule(
        id="fish",
        triggers=("рыба", "лосось", "семга", "сёмга", "форель", "треска", "тунец", "судак"),
        positive_terms=("рыба", "рыбн", "рыб", "лосос", "семг", "сёмг", "форел", "треск", "тунц", "судак"),
        category_terms=("рыб", "морепродукт"),
        strict=True,
    ),
    SemanticRule(
        id="meat",
        triggers=("мясо", "говядина", "свинина", "баранина", "фарш", "бекон"),
        positive_terms=("мясо", "мясн", "мяс", "говядин", "свинин", "баранин", "фарш", "бекон"),
        category_terms=("мяс",),
        strict=True,
    ),
    SemanticRule(
        id="poultry",
        triggers=("курица", "куриный", "индейка", "утка", "цыпленок", "цыплёнок"),
        positive_terms=("курица", "курин", "курят", "кур", "индей", "утка", "утин", "утк", "цыплен", "цыплён"),
        category_terms=("птиц", "мяс"),
        strict=True,
    ),
    SemanticRule(
        id="dairy",
        triggers=("молоко", "сливки", "сметана", "творог", "сыр", "масло", "кефир", "йогурт"),
        positive_terms=("молок", "сливк", "сметан", "творог", "сыр", "масл", "кефир", "йогурт"),
        category_terms=("молоч", "сыр"),
        strict=False,
    ),
    SemanticRule(
        id="vegetables",
        triggers=("овощ", "томаты", "томат", "огурец", "огурцы", "лук", "морковь", "картофель"),
        positive_terms=("томат", "огур", "лук", "морков", "картоф", "овощ"),
        category_terms=("овощ",),
        strict=False,
    ),
    SemanticRule(
        id="household",
        triggers=("хоз", "хозтовары", "салфетки", "перчатки", "пакеты", "моющее", "чистящее"),
        positive_terms=("салфет", "перчат", "пакет", "моющ", "чистящ", "хоз"),
        category_terms=("хоз", "инвентарь", "упаков"),
        strict=False,
    ),
)


def _normalize_text(value: object) -> str:
    return " ".join(_WORD_RE.findall(str(value or "").lower().replace("ё", "е")))


def _words(value: object) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(str(value or "").lower().replace("ё", "е")))


def _term_matches(text: str, term: str) -> bool:
    words = _words(text)
    if len(term) <= 3:
        return any(word == term for word in words)
    return any(word == term or word.startswith(term) for word in words)


def _allowed_typo_distance(term: str) -> int:
    """Allow generic typo correction only for words long enough to stay precise."""
    if len(term) < 5:
        return 0
    if len(term) < 9:
        return 1
    return 2


def _edit_distance_at_most(left: str, right: str, limit: int) -> bool:
    """Bounded Damerau-Levenshtein distance with adjacent transpositions."""
    if left == right:
        return True
    if limit <= 0 or abs(len(left) - len(right)) > limit:
        return False

    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[right_index - 1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
            if (
                previous_previous is not None
                and left_index > 1
                and right_index > 1
                and left_char == right[right_index - 2]
                and left[left_index - 2] == right_char
            ):
                current[right_index] = min(current[right_index], previous_previous[right_index - 2] + 1)
        previous_previous, previous = previous, current
    return previous[-1] <= limit


def _fuzzy_term_matches(text: str, term: str) -> bool:
    limit = _allowed_typo_distance(term)
    if not limit:
        return False
    for word in _words(text):
        prefix = word[: len(term)] if len(word) > len(term) else word
        if _edit_distance_at_most(word, term, limit) or _edit_distance_at_most(prefix, term, limit):
            return True
    return False


def keyboard_layout_variant(value: str) -> str:
    """Return a Russian-layout variant for text typed on an English keyboard."""
    if not value or re.search(r"[а-яё]", value, re.IGNORECASE):
        return value
    translated = value.lower().translate(_EN_TO_RU_KEYBOARD)
    return translated if translated != value.lower() else value


def _query_terms(query: ParsedSearchQuery) -> set[str]:
    terms = {query.normalized, *query.tokens}
    terms.update(keyboard_layout_variant(term) for term in tuple(terms))
    return {term for term in terms if len(term) >= 2}


def parse_search_query(raw_query: str | None) -> ParsedSearchQuery | None:
    raw = (raw_query or "").strip()
    if not raw:
        return None

    price_max = None
    price_match = _PRICE_MAX_RE.search(raw)
    if price_match:
        try:
            price_max = Decimal(price_match.group(1).replace(",", "."))
        except InvalidOperation:
            price_max = None
        raw_without_price = _PRICE_MAX_RE.sub(" ", raw)
    else:
        raw_without_price = raw

    normalized_tokens = []
    for token in _words(raw_without_price):
        if token in _STOPWORDS:
            continue
        normalized_tokens.append(_ALIASES.get(token, token))

    normalized = " ".join(normalized_tokens) or _normalize_text(raw_without_price)
    return ParsedSearchQuery(raw=raw, normalized=normalized, tokens=tuple(normalized_tokens), price_max=price_max)


def _matching_rules(query: ParsedSearchQuery) -> tuple[SemanticRule, ...]:
    query_text = f"{query.normalized} {' '.join(query.tokens)}"
    matched = []
    for rule in _SEMANTIC_RULES:
        exact_match = any(_normalize_text(trigger) in query_text for trigger in rule.triggers)
        known_typo_match = any(
            len(token) >= 4
            and abs(len(token) - len(normalized_trigger)) <= 1
            and min(len(token), len(normalized_trigger)) >= 4
            and _edit_distance_at_most(token, normalized_trigger, 1)
            for token in query.tokens
            for normalized_trigger in (_normalize_text(trigger) for trigger in rule.triggers)
        )
        if exact_match or known_typo_match:
            matched.append(rule)
    return tuple(matched)


def build_candidate_filter(query: ParsedSearchQuery) -> Q:
    candidate_q = Q()
    for term in _query_terms(query):
        candidate_q |= Q(supplier_name__icontains=term)
        candidate_q |= Q(normalized_name__icontains=term)
        candidate_q |= Q(supplier_sku__icontains=term)
        candidate_q |= Q(system_sku__icontains=term)
        candidate_q |= Q(supplier__display_name__icontains=term)
        candidate_q |= Q(brand__canonical_name__icontains=term)
        candidate_q |= Q(category__name__icontains=term)
        candidate_q |= Q(category__parent__name__icontains=term)
        candidate_q |= Q(category__parent__parent__name__icontains=term)

    for rule in (rule for rule in _matching_rules(query) if rule.strict):
        for term in (*rule.positive_terms, *rule.category_terms):
            candidate_q |= Q(supplier_name__icontains=term)
            candidate_q |= Q(normalized_name__icontains=term)
            candidate_q |= Q(category__name__icontains=term)
            candidate_q |= Q(category__parent__name__icontains=term)
            candidate_q |= Q(category__parent__parent__name__icontains=term)

    return candidate_q


def build_primary_name_candidate_filter(query: ParsedSearchQuery) -> Q:
    """Indexed fast path for the common case: every query token is in a product name.

    Keep SKU fields out of this OR: unlike the two name fields they do not have
    trigram indexes, so mixing them into the predicate can make PostgreSQL choose
    a sequential scan for ordinary name searches. SKU/category/supplier searches
    are still handled by the broad fallback when the name query has no rows.
    """
    candidate_q = Q()
    tokens = query.tokens or ((query.normalized,) if query.normalized else ())
    for token in tokens:
        token_q = Q()
        for term in {token, keyboard_layout_variant(token)}:
            if not term:
                continue
            token_q |= Q(Contains(SearchNameKey("supplier_name"), Value(search_name_key(term))))
            token_q |= Q(Contains(SearchNameKey("normalized_name"), Value(search_name_key(term))))
        candidate_q &= token_q
    return candidate_q


def _exact_name_results(queryset: QuerySet, query: ParsedSearchQuery) -> QuerySet | None:
    """Paginate all strong name matches in SQL, without the fuzzy top-K cap."""
    if not query.tokens or any(rule.negative_terms for rule in _matching_rules(query)):
        return None
    queryset = queryset.alias(
        supplier_folded_name=_Casefold("supplier_name"),
        normalized_folded_name=_Casefold("normalized_name"),
    ).filter(build_primary_name_candidate_filter(query))
    boundary = r"[^0-9a-zа-яё]"
    validated = Q()
    for token in query.tokens:
        token_filter = Q()
        for term in {token, keyboard_layout_variant(token)}:
            pattern = re.escape(term).replace("е", "[её]")
            pattern = rf"(^|{boundary}){pattern}" + (rf"($|{boundary})" if len(term) <= 3 else "")
            token_filter |= Q(supplier_folded_name__regex=pattern) | Q(normalized_folded_name__regex=pattern)
        validated &= token_filter
    queryset = queryset.filter(validated)
    if not queryset.exists():
        return None
    phrase = query.normalized
    return queryset.annotate(
        search_name_rank=Case(
            When(Q(supplier_folded_name=phrase) | Q(normalized_folded_name=phrase), then=Value(0)),
            When(Q(supplier_folded_name__contains=phrase) | Q(normalized_folded_name__contains=phrase), then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by("search_name_rank", "normalized_name", "id")


def _product_text(product) -> tuple[str, str]:
    def value(text: object) -> str:
        return str(text or "")

    name_text = _normalize_text(
        " ".join(
            [
                value(product.supplier_name),
                value(product.normalized_name),
                value(product.supplier_sku),
                value(product.system_sku),
                value(getattr(product.brand, "canonical_name", "")),
            ]
        )
    )
    category_text = _normalize_text(
        " ".join(
            [
                value(getattr(product.category, "name", "")),
                value(getattr(getattr(product.category, "parent", None), "name", "")),
                value(getattr(getattr(getattr(product.category, "parent", None), "parent", None), "name", "")),
            ]
        )
    )
    return name_text, category_text


def score_supplier_product(product, query: ParsedSearchQuery) -> int:
    name_text, category_text = _product_text(product)
    combined = f"{name_text} {category_text}"
    rules = _matching_rules(query)

    score = 0
    compact_query = re.sub(r"[^0-9a-zа-яё]", "", query.raw.lower())
    sku_values = {
        re.sub(r"[^0-9a-zа-яё]", "", str(value or "").lower())
        for value in (product.supplier_sku, product.system_sku)
        if value
    }
    if compact_query and compact_query in sku_values:
        score += 500
    if query.normalized and (" " in query.normalized and query.normalized in name_text):
        score += 100

    for token in _query_terms(query):
        if len(token) < 2:
            continue
        if _term_matches(name_text, token):
            score += 35
        elif _term_matches(category_text, token):
            score += 12
        elif _fuzzy_term_matches(name_text, token):
            score += 28
        elif _fuzzy_term_matches(category_text, token):
            score += 8

    strict_rules = [rule for rule in rules if rule.strict]
    for rule in rules:
        if any(term in combined for term in rule.negative_terms):
            score -= 80
        if any(_term_matches(name_text, term) for term in rule.positive_terms):
            score += 60
        if any(_term_matches(category_text, term) for term in rule.category_terms):
            score += 20

    if strict_rules and not any(
        any(_term_matches(combined, term) for term in (*rule.positive_terms, *rule.category_terms))
        for rule in strict_rules
    ):
        return 0

    return max(score, 0)


def explain_supplier_product_match(product, raw_query: str | None) -> dict:
    query = parse_search_query(raw_query)
    if query is None:
        return {"reason": "catalog_order", "matched_fields": [], "normalized_query": ""}

    terms = _query_terms(query)
    matched_fields = []
    fields = {
        "supplier_sku": product.supplier_sku,
        "system_sku": product.system_sku,
        "name": f"{product.supplier_name} {product.normalized_name}",
        "brand": getattr(product.brand, "canonical_name", ""),
        "category": " ".join(
            filter(
                None,
                (
                    getattr(product.category, "name", ""),
                    getattr(getattr(product.category, "parent", None), "name", ""),
                    getattr(getattr(getattr(product.category, "parent", None), "parent", None), "name", ""),
                ),
            )
        ),
    }
    compact_query = re.sub(r"[^0-9a-zа-яё]", "", query.raw.lower())
    for field, value in fields.items():
        normalized_value = _normalize_text(value)
        compact_value = re.sub(r"[^0-9a-zа-яё]", "", str(value or "").lower())
        if field.endswith("sku") and compact_query and compact_query == compact_value:
            matched_fields.append(field)
        elif any(_term_matches(normalized_value, term) for term in terms):
            matched_fields.append(field)

    corrected = keyboard_layout_variant(query.normalized)
    return {
        "reason": "exact_sku"
        if any(field.endswith("sku") for field in matched_fields)
        else ("lexical" if matched_fields else "semantic_vector"),
        "matched_fields": matched_fields,
        "normalized_query": query.normalized,
        "keyboard_corrected_query": corrected if corrected != query.normalized else None,
        "semantic_rules": list(semantic_rule_ids(raw_query)),
    }


def filter_catalog_product_candidates(queryset: QuerySet, raw_query: str | None) -> QuerySet:
    query = parse_search_query(raw_query)
    if query is None:
        return queryset
    candidate_filter = build_candidate_filter(query)
    if not candidate_filter:
        return queryset.none()
    return queryset.filter(candidate_filter)


def _missing_fuzzy_terms(candidates: list, query: ParsedSearchQuery) -> tuple[str, ...]:
    searchable_texts = [" ".join(_product_text(product)) for product in candidates]
    return tuple(
        dict.fromkeys(
            term
            for term in query.tokens
            if _allowed_typo_distance(term)
            and not any(
                _term_matches(text, variant)
                for variant in {term, keyboard_layout_variant(term)}
                if variant
                for text in searchable_texts
            )
        )
    )


def fuzzy_catalog_product_candidates(
    queryset: QuerySet, terms: tuple[str, ...], *, names_only: bool = False
) -> QuerySet:
    """Use pg_trgm to shortlist typo candidates; Python scoring validates edit distance."""
    if not terms:
        return queryset.none()

    fields = (
        "supplier_name",
        "normalized_name",
        "brand__canonical_name",
        "category__name",
        "category__parent__name",
        "category__parent__parent__name",
    )
    if names_only:
        queryset = queryset.alias(
            supplier_search_key=SearchNameKey("supplier_name"),
            normalized_search_key=SearchNameKey("normalized_name"),
        )
        fields = ("supplier_search_key", "normalized_search_key")
        # A scalar word_similarity(...) >= threshold cannot use GIN indexes.
        # The operator can use the search-key indexes before similarity sorting.
        predicate = Q()
        for term in terms:
            for field in fields:
                predicate |= Q(**{f"{field}__trigram_word_similar": search_name_key(term)})
        queryset = queryset.filter(predicate)
    similarities = [
        Coalesce(TrigramWordSimilarity(search_name_key(term) if names_only else term, field), Value(0.0))
        for term in terms
        for field in fields
    ]
    threshold = float(getattr(settings, "CATALOG_FUZZY_MIN_SIMILARITY", 0.2) or 0.2)
    return (
        queryset.annotate(fuzzy_similarity=Greatest(*similarities, output_field=FloatField()))
        .filter(fuzzy_similarity__gte=threshold)
        .order_by("-fuzzy_similarity")
    )


def _load_fuzzy_catalog_product_candidates(queryset: QuerySet, terms: tuple[str, ...], *, limit: int) -> list:
    if not terms:
        # Preserve the empty-query contract without issuing SQL or touching GUCs.
        return list(fuzzy_catalog_product_candidates(queryset, terms)[:limit])

    threshold = float(getattr(settings, "CATALOG_FUZZY_MIN_SIMILARITY", 0.2) or 0.2)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SHOW pg_trgm.word_similarity_threshold")
            previous_threshold = cursor.fetchone()[0]
            cursor.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", [str(threshold)])
        candidate_ids = list(
            fuzzy_catalog_product_candidates(queryset, terms, names_only=True).values_list("pk", flat=True)[:limit]
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", [previous_threshold])
    products = {product.pk: product for product in queryset.filter(pk__in=candidate_ids)}
    candidates = [products[product_id] for product_id in candidate_ids if product_id in products]
    return [product for product in candidates if _candidate_matches_fuzzy_terms(product, terms)]


def _candidate_matches_fuzzy_terms(product, terms: tuple[str, ...]) -> bool:
    combined = " ".join(_product_text(product))
    return all(_fuzzy_term_matches(combined, term) for term in terms)


def scan_fuzzy_catalog_product_candidates(
    queryset: QuerySet,
    terms: tuple[str, ...],
    *,
    result_limit: int,
) -> list:
    """Bounded fallback when pg_trgm does not return a valid typo candidate."""
    if not terms or result_limit <= 0:
        return []

    configured_scan_limit = int(getattr(settings, "CATALOG_FUZZY_SCAN_LIMIT", 20_000) or 20_000)
    # Never let an HTTP request fall back to a synchronous full-catalog Python scan.
    scan_limit = min(configured_scan_limit, 2_000)
    fields = (
        "pk",
        "supplier_name",
        "normalized_name",
        "brand__canonical_name",
        "category__name",
        "category__parent__name",
        "category__parent__parent__name",
    )
    matched_ids = []
    rows = queryset.order_by().values_list(*fields)[:scan_limit].iterator(chunk_size=2_000)
    for row in rows:
        product_id, *text_fields = row
        combined = _normalize_text(" ".join(str(value or "") for value in text_fields))
        if all(_fuzzy_term_matches(combined, term) for term in terms):
            matched_ids.append(product_id)
            if len(matched_ids) >= result_limit:
                break

    if not matched_ids:
        return []
    matched_products = {str(product.id): product for product in queryset.filter(pk__in=matched_ids)}
    return [matched_products[str(product_id)] for product_id in matched_ids if str(product_id) in matched_products]


def apply_directory_product_search(
    queryset: QuerySet,
    raw_query: str | None,
    *,
    fields: tuple[str, ...],
    name_field: str,
) -> QuerySet:
    """Fast typo-tolerant search for owner/venue-scoped product lists."""
    query = parse_search_query(raw_query)
    if query is None:
        return queryset

    tokens = query.tokens or ((query.normalized,) if query.normalized else ())
    if connection.vendor == "postgresql":
        folded_fields = {f"directory_search_field_{index}": _Casefold(field) for index, field in enumerate(fields)}
        queryset = queryset.annotate(**folded_fields)
        search_fields = tuple(folded_fields)
        name_search_field = next(
            alias for alias, field in zip(search_fields, fields, strict=True) if field == name_field
        )
    else:
        search_fields = fields
        name_search_field = name_field

    exact_filter = Q()
    token_filters = []
    for token in tokens:
        token_filter = Q()
        for variant in {token, keyboard_layout_variant(token)}:
            if variant:
                folded_variant = variant.translate(_CASEFOLD_TABLE)
                for field in search_fields:
                    lookup = "contains" if connection.vendor == "postgresql" else "icontains"
                    token_filter |= Q(**{f"{field}__{lookup}": folded_variant})
        token_filters.append((token, token_filter))
        exact_filter &= token_filter

    if connection.vendor != "postgresql":
        return queryset.filter(exact_filter).order_by(name_field, "id")

    threshold = float(getattr(settings, "DIRECTORY_SEARCH_TRIGRAM_THRESHOLD", 0.3) or 0.3)
    for index, (token, token_filter) in enumerate(token_filters):
        token_terms = {token.translate(_CASEFOLD_TABLE), keyboard_layout_variant(token).translate(_CASEFOLD_TABLE)}
        token_similarities = [
            Coalesce(TrigramWordSimilarity(term, field), Value(0.0))
            for term in token_terms
            if term
            for field in search_fields
        ]
        similarity_alias = f"directory_search_token_{index}"
        queryset = queryset.annotate(
            **{similarity_alias: Greatest(Value(0.0), *token_similarities, output_field=FloatField())}
        ).filter(token_filter | Q(**{f"{similarity_alias}__gte": threshold}))

    similarity_terms = {
        value.translate(_CASEFOLD_TABLE)
        for value in (query.normalized, keyboard_layout_variant(query.normalized), *tokens)
        if value
    }
    similarities = [
        Coalesce(TrigramWordSimilarity(term, field), Value(0.0))
        for term in similarity_terms
        if term
        for field in search_fields
    ]
    similarity = Greatest(Value(0.0), *similarities, output_field=FloatField())
    normalized_name = query.normalized.translate(_CASEFOLD_TABLE)
    name_exact = Q(**{name_search_field: normalized_name})
    name_prefix = Q(**{f"{name_search_field}__startswith": normalized_name})
    return (
        queryset.annotate(search_similarity=similarity)
        .annotate(
            search_match_rank=Case(
                When(name_exact, then=Value(0)),
                When(name_prefix, then=Value(1)),
                When(exact_filter, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by("search_match_rank", "-search_similarity", name_field, "id")
    )


def apply_hybrid_product_search(
    queryset: QuerySet,
    raw_query: str | None,
    *,
    candidate_limit: int | None = None,
) -> QuerySet:
    query = parse_search_query(raw_query)
    if query is None:
        return queryset

    candidate_limit = candidate_limit or int(getattr(settings, "CATALOG_SEARCH_CANDIDATE_LIMIT", 200) or 200)

    exact_results = _exact_name_results(queryset, query)
    if exact_results is not None:
        return exact_results
    primary_filter = build_primary_name_candidate_filter(query)
    candidates = list(queryset.filter(primary_filter)[:candidate_limit]) if primary_filter else []
    fuzzy_limit = int(getattr(settings, "CATALOG_FUZZY_CANDIDATE_LIMIT", 200) or 200)
    missing_terms = _missing_fuzzy_terms(candidates, query)
    fuzzy_candidates = _load_fuzzy_catalog_product_candidates(queryset, missing_terms, limit=fuzzy_limit)
    if not candidates and not fuzzy_candidates:
        candidate_queryset = filter_catalog_product_candidates(queryset, query.raw)
        candidates = list(candidate_queryset[:candidate_limit])
        missing_terms = _missing_fuzzy_terms(candidates, query)
    if missing_terms and not fuzzy_candidates:
        # Exact brand/category matches above need no broad similarity scan.
        # Keep that fallback for genuine typos in non-name fields.
        trigram_candidates = list(fuzzy_catalog_product_candidates(queryset, missing_terms)[:fuzzy_limit])
        fuzzy_candidates = [
            product for product in trigram_candidates if _candidate_matches_fuzzy_terms(product, missing_terms)
        ]
    if missing_terms and not fuzzy_candidates:
        fuzzy_candidates = scan_fuzzy_catalog_product_candidates(
            queryset,
            missing_terms,
            result_limit=fuzzy_limit,
        )
    candidates_by_id = {str(product.id): product for product in (*candidates, *fuzzy_candidates)}
    lexical_scores = {
        product_id: float(score)
        for product_id, product in candidates_by_id.items()
        if (score := score_supplier_product(product, query)) > 0
    }
    lexical_ranked = sorted(lexical_scores, key=lexical_scores.get, reverse=True)

    vector_skip_score = float(getattr(settings, "CATALOG_VECTOR_SKIP_MIN_LEXICAL_SCORE", 35) or 35)
    strong_lexical_match = bool(lexical_scores) and max(lexical_scores.values()) >= vector_skip_score
    lexical_fill_target = int(getattr(settings, "CATALOG_VECTOR_FALLBACK_MIN_LEXICAL", 20) or 20)
    if strong_lexical_match or len(lexical_ranked) >= lexical_fill_target:
        vector_scores = {}
    else:
        vector_limit = int(getattr(settings, "CATALOG_VECTOR_SEARCH_TOP_K", 100) or 100)
        vector_scores = vector_supplier_product_scores(query.normalized or query.raw, limit=vector_limit)
    allowed_vector_ids = {str(value) for value in queryset.filter(pk__in=vector_scores).values_list("pk", flat=True)}

    rrf_k = float(getattr(settings, "CATALOG_RRF_K", 60) or 60)
    lexical_weight = float(getattr(settings, "CATALOG_RRF_LEXICAL_WEIGHT", 1.35) or 1.35)
    vector_weight = float(getattr(settings, "CATALOG_RRF_VECTOR_WEIGHT", 1.0) or 1.0)
    fused_scores: dict[str, float] = {}

    for rank, product_id in enumerate(lexical_ranked, start=1):
        fused_scores[product_id] = fused_scores.get(product_id, 0.0) + lexical_weight / (rrf_k + rank)
    for rank, product_id in enumerate(vector_scores, start=1):
        if product_id in allowed_vector_ids:
            fused_scores[product_id] = fused_scores.get(product_id, 0.0) + vector_weight / (rrf_k + rank)

    ranked = sorted(
        fused_scores,
        key=lambda product_id: (
            fused_scores[product_id],
            lexical_scores.get(product_id, 0.0),
            vector_scores.get(product_id, 0.0),
        ),
        reverse=True,
    )
    if not ranked:
        return queryset.none()

    ordering = Case(
        *[When(pk=product_id, then=position) for position, product_id in enumerate(ranked)],
        output_field=IntegerField(),
    )
    return queryset.filter(pk__in=ranked).order_by(ordering)


def semantic_rule_ids(raw_query: str | None) -> Iterable[str]:
    query = parse_search_query(raw_query)
    if query is None:
        return ()
    return tuple(rule.id for rule in _matching_rules(query))
