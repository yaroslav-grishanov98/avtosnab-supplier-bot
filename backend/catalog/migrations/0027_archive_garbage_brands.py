import re

from django.db import migrations

# 2026-09-18, Pavel (скрин фильтра "Бренды" на маркетплейсе): часть значений
# в фильтре — не бренды, а целиком названия/упаковка товара, попавшие в
# колонку "бренд" при разборе прайса поставщика ("BURЁNKA СLUB 35% 125гр
# 1/9 БЗМЖ", "La Paulina 1/2 ~6кг") или тестовые записи ("test1", "тест").
# Подтверждено живым GET /api/v1/catalog/brands на dev — 19 из 55 значений
# явно не бренды. catalog_brands_queryset уже и так показывает только бренды
# с активным отценённым товаром (фильтр не виноват) — дело в мусорных
# Brand-записях, которые _resolve_brand создавал без всякой проверки
# (защищено на будущее тем же коммитом — см. api/v1/suppliers/services.py
# _looks_like_valid_brand_name). Здесь — разовая чистка уже накопленного:
# сама эвристика продублирована (не импортируется из app-кода — миграции
# должны быть самодостаточны и не ломаться от будущих правок services.py).
_REJECT_PATTERNS = (
    re.compile(r"%"),
    re.compile(r"\d+\s*/+\s*\d+"),
    re.compile(r"\d+\s*(кг|г|л|мл|шт|уп|бзмж)\b", re.IGNORECASE),
    re.compile(r"~\s*\d"),
)


def _looks_like_valid_brand_name(name):
    lowered = name.lower()
    if "тест" in lowered or "test" in lowered:
        return False
    return not any(pattern.search(name) for pattern in _REJECT_PATTERNS)


def archive_garbage_brands(apps, schema_editor):
    Brand = apps.get_model("catalog", "Brand")
    Product = apps.get_model("catalog", "Product")
    SupplierProduct = apps.get_model("suppliers", "SupplierProduct")

    garbage_ids = [
        brand.id
        for brand in Brand.objects.filter(status="active").only("id", "canonical_name")
        if not _looks_like_valid_brand_name(brand.canonical_name)
    ]
    if not garbage_ids:
        return

    Product.objects.filter(brand_id__in=garbage_ids).update(brand=None)
    SupplierProduct.objects.filter(brand_id__in=garbage_ids).update(brand=None)
    Brand.objects.filter(id__in=garbage_ids).update(status="archived")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0026_merge_20260917_1725"),
        ("suppliers", "0039_merge_weight_volume_into_net"),
    ]

    operations = [
        migrations.RunPython(archive_garbage_brands, noop),
    ]
