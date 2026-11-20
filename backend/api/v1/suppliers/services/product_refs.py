"""Brand, category and system SKU resolution shared by product editing and price uploads."""

import re

from django.db import connection

from catalog.models import Brand

# 2026-09-18, Pavel (скрин фильтра "Бренды" на маркетплейсе): часть значений
# в фильтре — не бренды, а целиком названия/упаковка товара, попавшие в
# колонку "бренд" при разборе прайса поставщика ("BURЁNKA СLUB 35% 125гр
# 1/9 БЗМЖ", "La Paulina 1/2 ~6кг") или тестовые записи ("test1", "тест").
# catalog_brands_queryset уже и так показывает только бренды с активным
# отценённым товаром — сам фильтр не виноват, дело в мусорных Brand-записях,
# которые _resolve_brand создавал без всякой проверки на любую непустую
# строку. Эвристика ниже не претендует на 100% точность (это не полноценный
# NLP-классификатор), только отсекает явные признаки "это описание товара/
# упаковки, а не бренд": проценты, дробь вида "1/9", количество+единица
# измерения (кг/г/л/мл/шт/уп), тильда-примерность ("~6кг") и слово
# "тест"/"test" где угодно в строке.
_BRAND_NAME_REJECT_PATTERNS = (
    re.compile(r"%"),
    re.compile(r"\d+\s*/+\s*\d+"),
    re.compile(r"\d+\s*(кг|г|л|мл|шт|уп|бзмж)\b", re.IGNORECASE),
    re.compile(r"~\s*\d"),
)


def _looks_like_valid_brand_name(name):
    lowered = name.lower()
    if "тест" in lowered or "test" in lowered:
        return False
    return not any(pattern.search(name) for pattern in _BRAND_NAME_REJECT_PATTERNS)


def _resolve_brand(brand_name):
    if not brand_name or not brand_name.strip():
        return None
    name = brand_name.strip()
    if not _looks_like_valid_brand_name(name):
        return None
    normalized = name.lower()
    brand, _ = Brand.objects.get_or_create(
        normalized_name=normalized,
        defaults={"canonical_name": name},
    )
    return brand


def _resolve_category_path(category_path):
    from catalog.models import ProductCategory

    if not category_path:
        return None
    parent = None
    for name in category_path:
        name = name.strip()
        if not name:
            continue
        slug = name.lower().replace(" ", "-").replace(",", "")
        cat = ProductCategory.objects.filter(slug=slug, parent=parent).first()
        if not cat:
            # slug must be globally unique — append parent slug if conflict
            unique_slug = slug
            if parent:
                unique_slug = f"{parent.slug}__{slug}"
            if ProductCategory.objects.filter(slug=unique_slug).exists():
                cat = ProductCategory.objects.filter(slug=unique_slug).first()
            else:
                cat = ProductCategory.objects.create(slug=unique_slug, parent=parent, name=name)
        parent = cat
    return parent


def _next_system_sku_batch(count):
    """`count` new system SKUs (AS-000001, ...) in one query.

    Numbers come from the PostgreSQL sequence `suppliers_system_sku_seq` (migration suppliers.0064): unlike
    "max existing code + 1" it is safe under concurrent imports, never hands out a deleted code again and orders
    numerically past AS-999999. Taken numbers are not returned on rollback, so callers should not over-allocate:
    use `_system_sku_stream` when the number needed is not known in advance."""
    if count <= 0:
        return []
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('suppliers_system_sku_seq') FROM generate_series(1, %s)", [count])
        numbers = sorted(row[0] for row in cursor.fetchall())
    return [f"AS-{number:06d}" for number in numbers]


def _system_sku_stream(chunk_size=50):
    """Lazy SKU iterator for loops that do not know up front how many rows need a code. Wastes at most
    `chunk_size - 1` numbers per loop instead of one per row that turned out not to need one."""
    while True:
        yield from _next_system_sku_batch(chunk_size)
