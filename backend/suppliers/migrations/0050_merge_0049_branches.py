"""Merge: две миграции 0049 в suppliers создали независимо от одной и той же 0048 --
0049_assign_missing_categories_by_name (TASK-125) и 0049_botuserbinding_supplier_nullable
(привязка бота к веб-аккаунту). Без merge `manage.py test`/`migrate` падают с
«Conflicting migrations detected; multiple leaf nodes»."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0049_assign_missing_categories_by_name"),
        ("suppliers", "0049_botuserbinding_supplier_nullable"),
    ]

    operations = []
