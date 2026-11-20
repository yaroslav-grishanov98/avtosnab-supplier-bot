"""Картинки-заглушки по виду товара в каталоге (прототип, 2026-09-29)."""

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from api.v1.catalog import category_placeholders
from api.v1.catalog.tests import _make_supplier_product
from catalog.models import ProductCategory

_STATIC_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _chain(*names):
    parent = None
    for name in names:
        parent = ProductCategory.objects.create(parent=parent, name=name, slug=f"test-{uuid4().hex}")
    return parent


class ConceptKeyTests(SimpleTestCase):
    def _category(self, group, parent, name):
        g = ProductCategory(name=group)
        p = ProductCategory(name=parent, parent=g)
        return ProductCategory(name=name, parent=p)

    def test_normal_hierarchy(self):
        self.assertEqual(
            category_placeholders.concept_key(self._category("продукты питания", "Бакалея", "Макаронные изделия")),
            "макаронные изделия",
        )

    def test_reversed_hierarchy_on_stand(self):
        self.assertEqual(
            category_placeholders.concept_key(self._category("макаронные изделия", "продукты питания", "бакалея")),
            "макаронные изделия",
        )

    def test_ambiguous_leaf_gets_parent_context(self):
        self.assertEqual(
            category_placeholders.concept_key(self._category("продукты питания", "чай", "листовой")), "листовой (чай)"
        )
        self.assertEqual(
            category_placeholders.concept_key(self._category("листовой", "продукты питания", "чай")), "листовой (чай)"
        )

    def test_alias(self):
        self.assertEqual(
            category_placeholders.concept_key(self._category("Молочная продукция", "Сыры", "Камамбер")),
            "сыры с белой плесенью",
        )


class CatalogPlaceholderImageAPITests(APITestCase):
    def setUp(self):
        self.static_dir = tempfile.mkdtemp(prefix="placeholder-static-")
        self.addCleanup(shutil.rmtree, self.static_dir, ignore_errors=True)
        target = Path(self.static_dir) / "catalog" / "category-placeholders"
        target.mkdir(parents=True)
        (target / "pasta.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        overrides = override_settings(
            STATICFILES_DIRS=[self.static_dir],
            STATICFILES_FINDERS=["django.contrib.staticfiles.finders.FileSystemFinder"],
            STORAGES=_STATIC_STORAGES,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)
        category_placeholders._static_path_if_exists.cache_clear()
        self.addCleanup(category_placeholders._static_path_if_exists.cache_clear)
        self.pasta = _chain("продукты питания", "Бакалея", "Макаронные изделия")

    def _row(self, product):
        rows = self.client.get("/api/v1/catalog/products").json()["results"]
        return next(r for r in rows if r["id"] == str(product.id))

    def test_product_without_photo_gets_category_placeholder(self):
        product = _make_supplier_product(price="100.00", category=self.pasta)
        row = self._row(product)
        self.assertEqual(row["image_url"], "//testserver/static/catalog/category-placeholders/pasta.svg")
        self.assertTrue(row["image_is_placeholder"])

        card = self.client.get(f"/api/v1/catalog/products/{product.id}").json()
        self.assertEqual(card["image_url"], row["image_url"])
        self.assertTrue(card["image_is_placeholder"])

    def test_reversed_hierarchy_category_also_matched(self):
        reversed_pasta = _chain("макаронные изделия", "продукты питания", "бакалея")
        row = self._row(_make_supplier_product(price="100.00", category=reversed_pasta))
        self.assertTrue(row["image_is_placeholder"])
        self.assertTrue(row["image_url"].endswith("/pasta.svg"))

    def test_real_photo_wins(self):
        product = _make_supplier_product(price="100.00", category=self.pasta)
        product.image_url = "https://example.com/real.jpg"
        product.save(update_fields=["image_url"])
        row = self._row(product)
        self.assertEqual(row["image_url"], "https://example.com/real.jpg")
        self.assertFalse(row["image_is_placeholder"])

    def _add_generic_file(self):
        (Path(self.static_dir) / "catalog" / "category-placeholders" / "generic-grocery.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"/>'
        )
        category_placeholders._static_path_if_exists.cache_clear()

    def test_product_without_category_gets_generic_placeholder(self):
        self._add_generic_file()
        row = self._row(_make_supplier_product(price="100.00"))
        self.assertEqual(row["image_url"], "//testserver/static/catalog/category-placeholders/generic-grocery.svg")
        self.assertTrue(row["image_is_placeholder"])

    def test_concept_without_file_gets_generic_placeholder(self):
        self._add_generic_file()
        milk = _chain("продукты питания", "молочные продукты", "молоко")
        row = self._row(_make_supplier_product(price="100.00", category=milk))
        self.assertTrue(row["image_url"].endswith("/generic-grocery.svg"))
        self.assertTrue(row["image_is_placeholder"])

    def test_no_file_for_concept_means_no_placeholder(self):
        milk = _chain("продукты питания", "молочные продукты", "молоко")
        row = self._row(_make_supplier_product(price="100.00", category=milk))
        self.assertIsNone(row["image_url"])
        self.assertFalse(row["image_is_placeholder"])

    def test_product_without_category(self):
        row = self._row(_make_supplier_product(price="100.00"))
        self.assertIsNone(row["image_url"])
        self.assertFalse(row["image_is_placeholder"])

    def test_explicit_base_url_setting(self):
        product = _make_supplier_product(price="100.00", category=self.pasta)
        with override_settings(CATALOG_PLACEHOLDER_BASE_URL="https://cdn.example.com/"):
            row = self._row(product)
        self.assertEqual(row["image_url"], "https://cdn.example.com/static/catalog/category-placeholders/pasta.svg")


class PlaceholderFilesInRepoTests(SimpleTestCase):
    """Каждому виду товара из таблицы должен соответствовать реальный файл в статике
    приложения — иначе у этого вида молча не будет картинки."""

    def test_every_concept_has_a_static_file(self):
        category_placeholders._static_path_if_exists.cache_clear()
        self.addCleanup(category_placeholders._static_path_if_exists.cache_clear)
        missing = [
            f
            for f in set(category_placeholders.CONCEPT_FILES.values()) | {category_placeholders.GENERIC_FILE}
            if not category_placeholders._static_path_if_exists(f)
        ]
        self.assertEqual(missing, [])
