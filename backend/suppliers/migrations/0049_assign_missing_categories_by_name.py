"""Разово проставить вид товара позициям поставщиков без категории — по словам в названии
(2026-09-30, TASK-125: без категории не было картинки-заглушки вида товара). Правила и
подробности — suppliers/uncategorized_category_rules.py. Категории, которые уже стоят, не
трогаются; откат — noop (какие категории были проставлены автоматически, не запоминается)."""

from django.db import migrations

from suppliers.uncategorized_category_rules import assign_missing_categories


def assign(apps, schema_editor):
    assign_missing_categories(
        apps.get_model("suppliers", "SupplierProduct"),
        apps.get_model("catalog", "ProductCategory"),
        apply=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0038_catalog_import_sessions"),
        ("suppliers", "0048_supplierproductimportitem_commit_guards"),
    ]

    operations = [
        migrations.RunPython(assign, migrations.RunPython.noop),
    ]
