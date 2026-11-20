from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from api.v1.catalog.search import _load_fuzzy_catalog_product_candidates, fuzzy_catalog_product_candidates
from api.v1.catalog.tests import _make_supplier_product
from catalog.models import Brand, ProductCategory
from core.models import EntityStatus
from suppliers.models import Supplier, SupplierProduct, SupplierProductPrice


@override_settings(CATALOG_QDRANT_AUTO_SYNC_ENABLED=False)
class CatalogSearchTypoRegressionTests(APITestCase):
    url = "/api/v1/catalog/products"

    def search(self, query, **filters):
        response = self.client.get(self.url, {"q": query, **filters})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_milk_typos_rank_milk_above_carrot_spices_without_vector_wait(self, vector):
        _make_supplier_product(price="80", normalized_name="приправа для корейской моркови")
        milk = _make_supplier_product(price="100", normalized_name="молоко пастеризованное")
        for query in ("молко", "малоко", "моллоко", "молкоо"):
            with self.subTest(query=query):
                rows = self.search(query)
                self.assertTrue(rows)
                self.assertEqual(rows[0]["id"], str(milk.id))
        vector.assert_not_called()

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_typo_search_respects_explicit_supplier_filter(self, _vector):
        allowed = _make_supplier_product(price="100", normalized_name="молоко пастеризованное")
        _make_supplier_product(price="50", normalized_name="молоко пастеризованное")
        rows = self.search("молко", supplier_id=str(allowed.supplier_id))
        self.assertEqual([row["id"] for row in rows], [str(allowed.id)])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_typo_search_excludes_hidden_and_unpriced_products(self, _vector):
        visible = _make_supplier_product(price="100", normalized_name="молоко")
        _make_supplier_product(normalized_name="молоко")
        archived = Supplier.objects.create(legal_name="Archived", status=EntityStatus.ARCHIVED)
        unpublished = Supplier.objects.create(legal_name="Unpublished", is_published=False)
        for supplier in (archived, unpublished):
            _make_supplier_product(supplier=supplier, price="20", normalized_name="молоко")
        self.assertEqual([row["id"] for row in self.search("молко")], [str(visible.id)])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_typo_search_keeps_price_sort_and_query_price_cap(self, _vector):
        cheap = _make_supplier_product(price="90", normalized_name="молоко пастеризованное")
        middle = _make_supplier_product(price="110", normalized_name="молоко ультрапастеризованное")
        _make_supplier_product(price="200", normalized_name="молоко сухое")
        rows = self.search("молко до 120", sort="price_asc")
        self.assertEqual([row["id"] for row in rows], [str(cheap.id), str(middle.id)])

    @patch("api.v1.catalog.search.filter_catalog_product_candidates", side_effect=AssertionError("wide scan"))
    @patch("api.v1.catalog.search.scan_fuzzy_catalog_product_candidates", side_effect=AssertionError("Python scan"))
    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_name_typo_does_not_scan_broad_fields(self, _vector, _scan, _broad):
        milk = _make_supplier_product(price="100", normalized_name="молоко пастеризованное")
        with CaptureQueriesContext(connection) as queries:
            rows = self.search("малоко")
        self.assertEqual(rows[0]["id"], str(milk.id))
        fuzzy_queries = [query["sql"] for query in queries if "word_similarity(" in query["sql"].lower()]
        self.assertEqual(len(fuzzy_queries), 1)
        self.assertNotIn('"brands"."canonical_name"', fuzzy_queries[0].split(" WHERE ", 1)[1])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_category_typo_still_has_fallback(self, _vector):
        category = ProductCategory.objects.create(name="Замороженные", slug="frozen-typo")
        item = _make_supplier_product(price="100", normalized_name="продукция", category=category)
        self.assertEqual([row["id"] for row in self.search("замароженные")], [str(item.id)])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_brand_typo_still_has_fallback(self, _vector):
        brand = Brand.objects.create(canonical_name="Простоквашино", normalized_name="простоквашино")
        item = _make_supplier_product(price="100", normalized_name="продукция", brand=brand)
        self.assertEqual([row["id"] for row in self.search("простаквашино")], [str(item.id)])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_uppercase_query_and_typo_find_same_milk(self, _vector):
        item = _make_supplier_product(price="100", normalized_name="молоко пастеризованное")
        for query in ("МОЛКО", "МАЛОКО", "МОЛОКО"):
            with self.subTest(query=query):
                self.assertEqual(self.search(query)[0]["id"], str(item.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_short_unknown_word_is_not_corrected_to_dairy(self, _vector):
        _make_supplier_product(price="100", normalized_name="молоко")
        self.assertEqual(self.search("молк"), [])

    def threshold(self):
        with connection.cursor() as cursor:
            cursor.execute("SHOW pg_trgm.word_similarity_threshold")
            return cursor.fetchone()[0]

    def test_trigram_threshold_is_restored_after_success_in_outer_transaction(self):
        item = _make_supplier_product(price="100", normalized_name="молоко")
        previous = self.threshold()
        candidates = _load_fuzzy_catalog_product_candidates(SupplierProduct.objects.all(), ("малоко",), limit=5)
        self.assertEqual([candidate.id for candidate in candidates], [item.id])
        self.assertEqual(self.threshold(), previous)

    def test_trigram_threshold_is_restored_after_failure(self):
        previous = self.threshold()
        with patch("api.v1.catalog.search.fuzzy_catalog_product_candidates", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                _load_fuzzy_catalog_product_candidates(SupplierProduct.objects.all(), ("малоко",), limit=5)
        self.assertEqual(self.threshold(), previous)

    def test_typo_name_predicate_can_use_existing_gin_indexes(self):
        _make_supplier_product(price="100", normalized_name="молоко")
        with connection.cursor() as cursor:
            cursor.execute("SHOW enable_seqscan")
            previous = cursor.fetchone()[0]
            cursor.execute("SET LOCAL enable_seqscan = off")
        try:
            plan = fuzzy_catalog_product_candidates(
                SupplierProduct.objects.all(), ("малоко",), names_only=True
            ).explain()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('enable_seqscan', %s, true)", [previous])
        self.assertIn("sp_supplier_search_key_gin", plan)
        self.assertIn("sp_normalized_search_key_gin", plan)

    def test_typo_candidates_are_bounded(self):
        for index in range(5):
            _make_supplier_product(price="100", normalized_name=f"молоко {index}")
        candidates = _load_fuzzy_catalog_product_candidates(SupplierProduct.objects.all(), ("малоко",), limit=2)
        self.assertEqual(len(candidates), 2)

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_multiword_typo_keeps_the_product_qualifier(self, _vector):
        expected = _make_supplier_product(price="100", normalized_name="молоко безлактозное")
        _make_supplier_product(price="80", normalized_name="молоко пастеризованное")
        for query in ("малоко безлактозное", "безлактозное малоко"):
            with self.subTest(query=query):
                self.assertEqual([row["id"] for row in self.search(query)], [str(expected.id)])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_uppercase_product_name_is_found_with_ctype_c(self, _vector):
        item = _make_supplier_product(price="100", normalized_name="МОЛОКО ПАСТЕРИЗОВАННОЕ")
        for query in ("молоко", "малоко"):
            with self.subTest(query=query):
                self.assertEqual(self.search(query)[0]["id"], str(item.id))

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_yo_and_ye_are_equivalent_in_names(self, _vector):
        item = _make_supplier_product(price="100", normalized_name="мёд гречишный")
        self.assertEqual([row["id"] for row in self.search("мед")], [str(item.id)])

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_encoded_ascii_collision_is_not_a_product_match(self, _vector):
        from core.search_expressions import search_name_key

        _make_supplier_product(price="100", normalized_name=search_name_key("молоко"))
        self.assertEqual(self.search("молоко"), [])

    @patch("api.v1.catalog.search.scan_fuzzy_catalog_product_candidates", side_effect=AssertionError("Python scan"))
    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_typo_finds_product_in_catalog_larger_than_scan_budget(self, _vector, _scan):
        seed = _make_supplier_product(price="100", normalized_name="продукция тестовая")
        noise = SupplierProduct.objects.bulk_create(
            [
                SupplierProduct(
                    supplier=seed.supplier,
                    unit=seed.unit,
                    current_price="100",
                    supplier_name=f"продукция тестовая {index}",
                    normalized_name=f"продукция тестовая {index}",
                )
                for index in range(2100)
            ]
        )
        SupplierProductPrice.objects.bulk_create(
            [SupplierProductPrice(supplier_product=item, price="100") for item in noise]
        )
        expected = _make_supplier_product(price="100", normalized_name="клубника свежая")
        self.assertEqual([row["id"] for row in self.search("клубнка")], [str(expected.id)])

    def test_postgres_search_registration_preserves_import_lock_connections(self):
        from file_imports.task_locks import import_task_lock

        with import_task_lock("search-registration-regression", "isolated-test") as acquired:
            self.assertTrue(acquired)

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_exact_name_results_are_not_truncated_to_fuzzy_candidate_limit(self, _vector):
        seed = _make_supplier_product(price="100", normalized_name="гречка ядрица")
        products = SupplierProduct.objects.bulk_create(
            [
                SupplierProduct(
                    supplier=seed.supplier,
                    unit=seed.unit,
                    current_price="100",
                    supplier_name=f"гречка ядрица {index}",
                    normalized_name=f"гречка ядрица {index}",
                )
                for index in range(220)
            ]
        )
        SupplierProductPrice.objects.bulk_create(
            [SupplierProductPrice(supplier_product=item, price="100") for item in products]
        )
        response = self.client.get(self.url, {"q": "гречка", "limit": 12, "offset": 200})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 221)
        self.assertEqual(len(response.json()["results"]), 12)

    @patch("api.v1.catalog.search.vector_supplier_product_scores", return_value={})
    def test_food_families_work_without_a_category(self, _vector):
        for name in ("курица филе", "куриный фарш", "утка", "рыба свежая", "мясо свежее", "рисовый крахмал"):
            with self.subTest(name=name):
                item = _make_supplier_product(price="100", normalized_name=name)
                self.assertIn(str(item.id), [row["id"] for row in self.search(name)])
