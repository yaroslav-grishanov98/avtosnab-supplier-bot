"""ТЗ §5/§6 MATCH stage: up to MATCH_TOP_K similar ACTIVE/DRAFT SupplierProduct rows
of THIS supplier per raw name, one query for the whole batch (same
unnest+LATERAL pg_trgm pattern as api.v1.catalog.selectors.master_catalog._venue_import_candidates,
TASK-71/78) -- see that function's docstring for why exact match must win over
similarity()."""

import re

from django.conf import settings
from django.db import connection

from core.models import EntityStatus
from suppliers.models import SupplierProduct

MATCH_TOP_K = 5
# The trigram query is wide on purpose (a brand or a pack size alone gives a high score); the word check below
# narrows it, so fetch more than we keep.
_FETCH_FACTOR = 3
_STEM = 5
# Cyrillic words that say how the item is sold, not what it is.
_NOT_DISTINGUISHING = {
    "бутылка",
    "банка",
    "ведро",
    "ведре",
    "коробка",
    "коробке",
    "пакет",
    "мешок",
    "упаковка",
    "штука",
    "весовой",
    "весовая",
    "весовое",
    "весовые",
    "россия",
    "марка",
    "торговая",
}


def distinguishing_words(name):
    """Stems of the words that tell one product from another of the same kind ("барбекю" in «Соус Барбекю»).

    Dropped: the head word (it is the product kind, "соус", "крупа"), Latin words (brands), short words and
    abbreviations ("ТМ", "СП", "в/с"), numbers, and words about packaging. Inflection is ignored by comparing stems.
    """
    text = str(name).casefold().replace("ё", "е")
    # The word after "ТМ" / "торговая марка" is a brand, whatever its alphabet.
    text = re.sub(r"(?<![a-zа-я])(?:т\.?\s?м\.?|торговая марка)\s*[\"«']?[a-zа-я0-9-]+", " ", text)
    words = re.findall(r"[a-zа-я]+", text)
    result = set()
    for word in words[1:]:
        if not re.fullmatch(r"[а-я]+", word) or len(word) < 4 or word in _NOT_DISTINGUISHING:
            continue
        if word.startswith(("упак", "короб")):
            continue
        result.add(word[:_STEM])
    return result


def shares_distinguishing_word(raw_name, candidate_name):
    """False when both names say what the product is and the candidate says something else (buckwheat vs barley).

    A name with no distinguishing word at all gives nothing to compare, so the trigram similarity decides then.
    """
    wanted = distinguishing_words(raw_name)
    if not wanted:
        return True
    return bool(wanted & distinguishing_words(candidate_name))


def _product_identity_pattern(name):
    """Require the leading product word; packaging alone is not identity.

    Conservatively allow Russian inflection endings, without conflating unrelated
    foods that happen to have the same weight, packaging or brand.
    """
    words = re.findall(r"[a-zа-я]+", str(name).casefold().replace("ё", "е"))
    if not words:
        return r"^$"
    word = words[0]
    if len(word) > 4 and word[-1] in "аяыи":
        return "^" + re.escape(word[:-1]) + "[аяыи]?$"
    return "^" + re.escape(word) + "$"


def same_product_kind(name, other_name):
    """Both names start with the same product word ("Сыр Хохланд" and "Сыр Hochland"), inflection aside."""
    words = re.findall(r"[a-zа-я]+", str(other_name).casefold().replace("ё", "е"))
    return bool(words) and re.match(_product_identity_pattern(name), words[0]) is not None


def supplier_product_import_match_min_similarity():
    return float(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_MATCH_MIN_SIMILARITY", 0.3) or 0.3)


def find_supplier_product_import_candidates(*, supplier_id, names):
    if not names:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT r.idx, m.id, m.supplier_name, m.unit_id, u.name AS unit_name, m.score
            FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS r(raw_name, identity_pattern, idx)
            CROSS JOIN LATERAL (
                SELECT
                    sp.id,
                    sp.supplier_name,
                    sp.unit_id,
                    lower(sp.supplier_name) = lower(r.raw_name) AS is_exact,
                    CASE
                        WHEN lower(sp.supplier_name) = lower(r.raw_name) THEN 1.0::float8
                        ELSE similarity(sp.supplier_name, r.raw_name)::float8
                    END AS score
                FROM {SupplierProduct._meta.db_table} AS sp
                WHERE sp.supplier_id = %s
                  AND sp.status = ANY(%s)
                  AND (
                      lower(sp.supplier_name) = lower(r.raw_name)
                      OR (
                          sp.supplier_name %% r.raw_name
                          AND similarity(sp.supplier_name, r.raw_name) >= %s
                          AND substring(replace(lower(sp.supplier_name), 'ё', 'е') FROM '[[:alpha:]]+')
                              ~ r.identity_pattern
                      )
                  )
                ORDER BY is_exact DESC, score DESC, sp.id ASC
                LIMIT %s
            ) AS m
            LEFT JOIN units u ON u.id = m.unit_id
            ORDER BY r.idx, m.is_exact DESC, m.score DESC, m.id ASC
            """,
            [
                list(names),
                [_product_identity_pattern(name) for name in names],
                str(supplier_id),
                [EntityStatus.ACTIVE, EntityStatus.DRAFT],
                supplier_product_import_match_min_similarity(),
                MATCH_TOP_K * _FETCH_FACTOR,
            ],
        )
        fetched = cursor.fetchall()

    result = {}
    for idx, product_id, name, unit_id, unit_name, score in fetched:
        position = idx - 1
        candidates = result.setdefault(position, [])
        # An exact name always stays; a similar one must be the same kind of product, not just share a brand or a pack.
        if len(candidates) >= MATCH_TOP_K or not (score >= 1.0 or shares_distinguishing_word(names[position], name)):
            continue
        candidates.append(
            {"id": product_id, "name": name, "unit_id": unit_id, "unit_name": unit_name, "score": float(score)}
        )
    return {position: candidates for position, candidates in result.items() if candidates}


def find_supplier_products_by_sku(*, supplier_id, skus):
    """TASK-133: точное совпадение по артикулу поставщика ({sku: SupplierProduct}), ACTIVE/DRAFT -- как и у
    поиска по названию (replace сохраняет статус target). Артикул уникален в рамках поставщика
    (supplier_products_supplier_sku_uniq), поэтому на один sku не больше одного товара."""
    wanted = {sku for sku in skus if sku}
    if not wanted:
        return {}
    products = SupplierProduct.objects.filter(
        supplier_id=supplier_id, status__in=[EntityStatus.ACTIVE, EntityStatus.DRAFT], supplier_sku__in=wanted
    ).select_related("unit")
    return {product.supplier_sku: product for product in products}


def find_hidden_sku_holders(*, supplier_id, skus):
    """Аудит идентификаторов, F6: {sku: SupplierProduct} для архивных и неактивных товаров поставщика.

    Матчинг их не видит, а UNIQUE(supplier, supplier_sku) действует на все статусы, поэтому новая карточка с таким
    артикулом падает при сохранении. Артикул архивного товара остаётся за ним, пока не согласована процедура
    повторного использования (03-proposed-architecture.md)."""
    wanted = {sku for sku in skus if sku}
    if not wanted:
        return {}
    products = SupplierProduct.objects.filter(
        supplier_id=supplier_id,
        status__in=[EntityStatus.ARCHIVED, EntityStatus.INACTIVE],
        supplier_sku__in=wanted,
    )
    return {product.supplier_sku: product for product in products}


def hidden_sku_holder_message(sku, product):
    if product.status == EntityStatus.ARCHIVED:
        return (
            f"Артикул {sku} занят товаром «{product.supplier_name}» в архиве. "
            "Восстановите товар из архива или исправьте артикул."
        )
    return (
        f"Артикул {sku} занят неактивным товаром «{product.supplier_name}». "
        "Сделайте товар снова активным или исправьте артикул."
    )
