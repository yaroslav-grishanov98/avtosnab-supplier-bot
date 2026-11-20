import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from api.v1.catalog import embeddings
from api.v1.catalog.search import parse_search_query
from api.v1.suppliers import services as supplier_services
from api.v1.suppliers import tasks as supplier_tasks


class FakeVector(list):
    def tolist(self):
        return list(self)


@override_settings(
    QDRANT_URL="https://qdrant.example",
    QDRANT_API_KEY="secret",
    QDRANT_COLLECTION="autosnab_test",
    CATALOG_EMBEDDING_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    CATALOG_VECTOR_SIZE=3,
    CATALOG_VECTOR_MIN_SCORE=0.35,
)
class QdrantEmbeddingTests(SimpleTestCase):
    def tearDown(self):
        embeddings._model_failure = None
        embeddings.embedding_model.cache_clear()
        embeddings.qdrant_client.cache_clear()

    def test_create_embeddings_normalizes_and_validates_dimensions(self):
        model = Mock()
        model.encode.return_value = [FakeVector([1.0, 0.0, 0.0])]
        with patch.object(embeddings, "embedding_model", return_value=model):
            result = embeddings.create_embeddings(["клубника"])
        self.assertEqual(result, [[1.0, 0.0, 0.0]])
        model.encode.assert_called_once_with(["клубника"], normalize_embeddings=True, show_progress_bar=False)

    def _fake_sentence_transformers(self, factory):
        return patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=factory)})

    def test_embedding_model_prefers_local_cache(self):
        factory = Mock(return_value="model")
        with self._fake_sentence_transformers(factory):
            self.assertEqual(embeddings.embedding_model(), "model")
        factory.assert_called_once_with(embeddings.catalog_embedding_model(), local_files_only=True)

    def test_embedding_model_downloads_when_not_cached(self):
        factory = Mock(side_effect=[OSError("not cached"), "model"])
        with self._fake_sentence_transformers(factory):
            self.assertEqual(embeddings.embedding_model(), "model")
        self.assertEqual(factory.call_count, 2)
        self.assertNotIn("local_files_only", factory.call_args_list[1].kwargs)

    @override_settings(CATALOG_EMBEDDING_FAILURE_TTL_SECONDS=60)
    def test_embedding_model_failure_is_cached_and_does_not_retry_network(self):
        factory = Mock(side_effect=OSError("dns"))
        with self._fake_sentence_transformers(factory):
            with self.assertRaises(embeddings.EmbeddingModelUnavailable):
                embeddings.embedding_model()
            calls_after_first = factory.call_count
            with self.assertRaises(embeddings.EmbeddingModelUnavailable):
                embeddings.embedding_model()
        self.assertEqual(calls_after_first, 2)
        self.assertEqual(factory.call_count, calls_after_first)

    @override_settings(CATALOG_EMBEDDING_FAILURE_TTL_SECONDS=0)
    def test_embedding_model_retries_after_failure_ttl_expires(self):
        factory = Mock(side_effect=[OSError("dns"), OSError("dns"), "model"])
        with self._fake_sentence_transformers(factory):
            with self.assertRaises(embeddings.EmbeddingModelUnavailable):
                embeddings.embedding_model()
            self.assertEqual(embeddings.embedding_model(), "model")

    @override_settings(CATALOG_VECTOR_SEARCH_ENABLED=True)
    def test_vector_search_uses_qdrant_threshold_and_supplier_product_payload(self):
        product_id = str(uuid4())
        point = SimpleNamespace(score=0.81, payload={"supplierProductId": product_id})
        client = Mock()
        client.collection_exists.return_value = True
        client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=3)))
        )
        client.query_points.return_value = SimpleNamespace(points=[point])
        with (
            patch.object(embeddings, "qdrant_client", return_value=client),
            patch.object(embeddings, "create_embedding", return_value=[1.0, 0.0, 0.0]),
        ):
            result = embeddings.vector_supplier_product_scores("клубника", limit=5)
        self.assertEqual(result, {product_id: 0.81})
        self.assertEqual(client.query_points.call_args.kwargs["score_threshold"], 0.35)
        client.collection_exists.assert_not_called()
        client.get_collection.assert_not_called()
        client.create_collection.assert_not_called()

    def test_search_model_never_downloads_on_cache_miss(self):
        factory = Mock(side_effect=OSError("not cached"))
        with self._fake_sentence_transformers(factory):
            with self.assertRaises(embeddings.EmbeddingModelUnavailable):
                embeddings.embedding_model(allow_download=False)
        factory.assert_called_once_with(embeddings.catalog_embedding_model(), local_files_only=True)

    @override_settings(QDRANT_TIMEOUT_SECONDS=5, CATALOG_VECTOR_SEARCH_TIMEOUT_SECONDS=1)
    def test_search_transport_timeout_is_separate_from_indexing(self):
        with patch("qdrant_client.QdrantClient") as factory:
            embeddings.qdrant_client(search=True)
            self.assertEqual(factory.call_args.kwargs["timeout"], 1)
            embeddings.qdrant_client()
            self.assertEqual(factory.call_args.kwargs["timeout"], 5)

    @override_settings(CATALOG_VECTOR_SEARCH_ENABLED=True)
    def test_search_returns_empty_when_qdrant_is_unavailable(self):
        client = Mock()
        client.query_points.side_effect = TimeoutError("timeout")
        with (
            patch.object(embeddings, "qdrant_client", return_value=client),
            patch.object(embeddings, "create_embedding", return_value=[1.0, 0.0, 0.0]),
        ):
            self.assertEqual(embeddings.vector_supplier_product_scores("сложный запрос"), {})
        client.create_collection.assert_not_called()

    def test_sync_reuses_compatible_point_without_reembedding(self):
        product_id = uuid4()
        item = SimpleNamespace(
            id=product_id,
            product_id=uuid4(),
            normalized_name="Клубника",
            supplier_name="Поставщик",
            brand=None,
            category=None,
            package="1 кг",
            unit=None,
            country_of_origin="Россия",
        )
        source_hash = embeddings.embedding_source_hash(embeddings.build_supplier_product_embedding_text(item))
        existing = SimpleNamespace(id=product_id, payload={"sourceHash": source_hash})
        client = Mock()
        client.collection_exists.return_value = True
        client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=3)))
        )
        client.retrieve.return_value = [existing]
        with (
            patch.object(embeddings, "qdrant_client", return_value=client),
            patch.object(embeddings, "create_embeddings") as create,
        ):
            processed, skipped = embeddings.sync_supplier_products_to_qdrant([item])
        self.assertEqual((processed, skipped), (0, 1))
        create.assert_not_called()
        client.upsert.assert_not_called()


class TypoNormalizationTests(SimpleTestCase):
    def test_known_transposition_typos_are_normalized(self):
        self.assertEqual(parse_search_query("кулбника").normalized, "клубника")
        self.assertEqual(parse_search_query("помидоы").normalized, "помидоры")
        self.assertEqual(parse_search_query("курциа").normalized, "курица")


@override_settings(
    CATALOG_QDRANT_AUTO_SYNC_ENABLED=True,
    CATALOG_QDRANT_SYNC_BATCH_SIZE=64,
    QDRANT_URL="https://qdrant.example",
    QDRANT_API_KEY="secret",
)
class QdrantAutoSyncTests(SimpleTestCase):
    def test_successful_import_enqueues_supplier_scoped_sync(self):
        upload = SimpleNamespace(id=uuid4(), supplier_id=uuid4())
        async_result = SimpleNamespace(id="qdrant-task-id")
        with patch.object(
            supplier_tasks.sync_supplier_catalog_to_qdrant_task, "delay", return_value=async_result
        ) as delay:
            result = supplier_services._enqueue_supplier_catalog_qdrant_sync(
                supplier_id=upload.supplier_id,
                upload_id=upload.id,
            )

        self.assertEqual(result, {"accepted": True, "task_id": "qdrant-task-id"})
        delay.assert_called_once_with(supplier_id=str(upload.supplier_id), upload_id=str(upload.id))
