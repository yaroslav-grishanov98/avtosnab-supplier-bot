# Data-only migration, no schema change.
#
# 2026-08-15: продолжение чистки из catalog/0009 (см.
# docs/wiki/logs/2026-08-14-category-taxonomy-cleanup.md) — по прямой просьбе
# пользователя удалить оставшиеся группы верхнего уровня без единой подгруппы
# (тот же баг _category_path()/_resolve_category_path(), только полностью
# схлопнутый до 1 уровня вместо 2). Живая проверка GET /suppliers/categories
# нашла 5 таких групп: "Крупы", "чай" (0 товаров — точно безопасно) и
# "кондитерские изделия", "соусы и приправы", "Фрукты" (по 1 товару со
# status=active у одного и того же поставщика, не тестового). Пользователь
# явно подтвердил удаление всех пяти, зная про активные товары —
# SupplierProduct.category is on_delete=SET_NULL, эти 3 товара не удаляются,
# только теряют category_id.
from django.db import migrations

GROUP_IDS_WITHOUT_SUBGROUPS = [
    "6c3102b5-40f8-4ea8-993b-5a08e08487b7",  # Крупы (0 товаров)
    "47f41845-48bb-44ab-a737-b5b21c28b185",  # чай (0 товаров)
    "41c39c03-f48c-4a38-aef3-41441ce2df02",  # кондитерские изделия (1 активный товар)
    "59edb9d9-ccde-46fd-b0b3-00029c31702f",  # соусы и приправы (1 активный товар)
    "69751695-74bc-40a1-aca9-d1629455535c",  # Фрукты (1 активный товар)
]


def delete_groups(apps, schema_editor):
    ProductCategory = apps.get_model("catalog", "ProductCategory")
    ProductCategory.objects.filter(id__in=GROUP_IDS_WITHOUT_SUBGROUPS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0009_delete_junk_categories"),
    ]

    operations = [
        migrations.RunPython(delete_groups, migrations.RunPython.noop),
    ]
