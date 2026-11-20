from django.core.management.base import BaseCommand

from catalog.models import ProductCategory
from suppliers.models import SupplierProduct
from suppliers.uncategorized_category_rules import assign_missing_categories


class Command(BaseCommand):
    help = (
        "Проставить вид товара позициям поставщиков без категории по словам в названии "
        "(см. suppliers/uncategorized_category_rules.py). По умолчанию только показывает, что будет сделано."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Записать категории в базу.")
        parser.add_argument("--show-unmatched", action="store_true", help="Вывести названия без подходящего правила.")

    def handle(self, *args, **options):
        assigned, unmatched = assign_missing_categories(SupplierProduct, ProductCategory, apply=options["apply"])
        for (subgroup, leaf), count in assigned.most_common():
            self.stdout.write(f"{count:5}  {subgroup} / {leaf}")
        if options["show_unmatched"]:
            for name in sorted(set(unmatched)):
                self.stdout.write(f"  - {name}")
        verb = "assigned" if options["apply"] else "dry-run: would assign"
        self.stdout.write(self.style.SUCCESS(f"{verb} {sum(assigned.values())}, unmatched {len(unmatched)}"))
