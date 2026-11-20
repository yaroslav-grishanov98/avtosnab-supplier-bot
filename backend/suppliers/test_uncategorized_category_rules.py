"""Категории по словам в названии для товаров поставщиков без категории (TASK-125, 2026-09-30)."""

from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from api.v1.catalog.tests import _make_supplier_product_for
from catalog.models import ProductCategory
from suppliers.models import Supplier, SupplierProduct
from suppliers.uncategorized_category_rules import assign_missing_categories, match_rule


def _chain(*names):
    parent = None
    for name in names:
        parent = ProductCategory.objects.create(parent=parent, name=name, slug=f"test-{uuid4().hex}")
    return parent


class MatchRuleTests(SimpleTestCase):
    def test_names_from_stand(self):
        cases = {
            "Паста Пенне 450 гр": ("Бакалея", "Макаронные изделия"),
            "АКЦИЯ от 6шт!!! Соус Унаги Tamaki (1,8л)": ("Соусы и приправы", "Соусы"),
            "Булочка для бургера с кунжутом 100гр": ("Хлеб и хлебобулочные изделия", "Хлеб,батон,лаваш"),
            "Мука овсяная 1кг": ("Бакалея", "мука и все для выпечки"),
            "Маслины фаршированные анчоусом 300мл": ("Овощи и грибы", "Консервированные"),
            "Масло оливковое Extra Virgin 0,5л": ("Бакалея", "Растительное масло"),
            "Майонез Оливковый 1кг": ("Соусы и приправы", "Майонез"),
            "Мисо паста белая 1кг": ("Бакалея", "азиатская кухня"),
            "Суп куриный с лапшой 1кг": ("Бакалея", "Продукты быстрого приготовления"),
            "Фруктоза 1кг": ("Бакалея", "сахар,соль"),
            "Лапша Яичная сухая 300гр": ("Бакалея", "Макаронные изделия"),
            "Картофель Фри 9*9 мм в панировке Фрай Ми/Fry Me 2,5 кг": ("Овощи и грибы", "Замороженные"),
            "Наггетсы куриные 1,5кг": ("Мясо и птица", "Полуфабрикаты"),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(match_rule(name), expected)

    def test_left_uncategorized(self):
        for name in ["Креветки тигровые 1кг", "Яйцо куриное С0 30шт", "Мишка плюшевый белый 50см", "тест1", "—"]:
            with self.subTest(name=name):
                self.assertIsNone(match_rule(name))


class AssignMissingCategoriesTests(TestCase):
    def setUp(self):
        # Дерево категорий из seed-миграций мешает проверять выбор среди дублей — тест строит своё.
        ProductCategory.objects.all().delete()
        self.supplier = Supplier.objects.create(legal_name="Test LLC", display_name="Test Supplier")
        self.pasta = _chain("продукты питания", "Бакалея", "Макаронные изделия")

    def test_assigns_only_missing_and_matched(self):
        penne = _make_supplier_product_for(self.supplier, "Паста Пенне 450 гр", "100.00")
        shrimp = _make_supplier_product_for(self.supplier, "Креветки тигровые 1кг", "100.00")
        already = _make_supplier_product_for(self.supplier, "Спагетти 450гр", "100.00")
        other = _chain("продукты питания", "Бакалея", "крупы и бобовые")
        SupplierProduct.objects.filter(pk=already.pk).update(category=other)

        assigned, unmatched = assign_missing_categories(SupplierProduct, ProductCategory, apply=True)

        self.assertEqual(assigned[("Бакалея", "Макаронные изделия")], 1)
        self.assertEqual(unmatched, ["Креветки тигровые 1кг"])
        penne.refresh_from_db(), shrimp.refresh_from_db(), already.refresh_from_db()
        self.assertEqual(penne.category, self.pasta)
        self.assertIsNone(shrimp.category)
        self.assertEqual(already.category, other)

    def test_prefers_food_branch_over_duplicate(self):
        _chain("бакалея", "Бакалея", "Макаронные изделия")
        item = _make_supplier_product_for(self.supplier, "Паста Пенне 450 гр", "100.00")
        assign_missing_categories(SupplierProduct, ProductCategory, apply=True)
        item.refresh_from_db()
        self.assertEqual(item.category, self.pasta)

    def test_rule_without_category_in_tree_is_unmatched(self):
        item = _make_supplier_product_for(self.supplier, "Майонез 67% 1кг", "100.00")
        assigned, unmatched = assign_missing_categories(SupplierProduct, ProductCategory, apply=True)
        self.assertFalse(assigned)
        self.assertEqual(unmatched, ["Майонез 67% 1кг"])
        item.refresh_from_db()
        self.assertIsNone(item.category)

    def test_command_dry_run_does_not_write(self):
        item = _make_supplier_product_for(self.supplier, "Паста Пенне 450 гр", "100.00")
        out = StringIO()
        call_command("assign_missing_categories", stdout=out)
        item.refresh_from_db()
        self.assertIsNone(item.category)
        self.assertIn("would assign 1", out.getvalue())

        call_command("assign_missing_categories", "--apply", stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(item.category, self.pasta)
