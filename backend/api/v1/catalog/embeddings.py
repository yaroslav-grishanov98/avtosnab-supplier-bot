from __future__ import annotations

import hashlib
import logging
import time
from functools import lru_cache

from django.conf import settings
from django.db import transaction

from catalog.models import Product
from core.models import EntityStatus

logger = logging.getLogger("autosnab.catalog.embeddings")

_UNAVAILABLE_MESSAGE = "Сервис сопоставления товаров временно недоступен. Загрузите файл ещё раз чуть позже."


def catalog_embedding_model() -> str:
    return str(
        getattr(settings, "CATALOG_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )


def catalog_vector_search_enabled() -> bool:
    return bool(getattr(settings, "CATALOG_VECTOR_SEARCH_ENABLED", True))


def catalog_vector_size() -> int:
    return int(getattr(settings, "CATALOG_VECTOR_SIZE", 384) or 384)


def catalog_qdrant_collection() -> str:
    base = str(getattr(settings, "QDRANT_COLLECTION", "autosnab_backend_products") or "").strip()
    if not base:
        raise ValueError("QDRANT_COLLECTION is required")
    marker = hashlib.sha256(catalog_embedding_model().encode()).hexdigest()[:8]
    return f"{base}_{marker}"


def build_supplier_product_embedding_text(item) -> str:
    parts = [
        item.normalized_name,
        item.supplier_name,
        getattr(item.brand, "canonical_name", ""),
        getattr(item.category, "name", ""),
        getattr(getattr(item.category, "parent", None), "name", ""),
        getattr(getattr(getattr(item.category, "parent", None), "parent", None), "name", ""),
        item.package,
        getattr(item.unit, "code", ""),
        item.country_of_origin,
    ]
    return " ".join(str(part).strip() for part in parts if part).strip()


def embedding_source_hash(text: str) -> str:
    return hashlib.sha256(f"{catalog_embedding_model()}\n{text}".encode()).hexdigest()


class EmbeddingModelUnavailable(RuntimeError):
    """Модель сопоставления не загрузилась (нет в локальном кэше и недоступен Hugging Face)."""


# (monotonic-дедлайн, имя модели) последней неудачной загрузки: пока он не истёк, повторные вызовы падают сразу,
# а не ждут DNS/HTTP-таймауты Hugging Face заново.
_model_failure = None


def catalog_embedding_failure_ttl() -> float:
    return float(getattr(settings, "CATALOG_EMBEDDING_FAILURE_TTL_SECONDS", 60) or 0)


@lru_cache(maxsize=2)
def embedding_model(*, allow_download: bool = True):
    """Модель берётся из локального кэша (в образе она запекается при сборке, см. Dockerfile) и только при его
    отсутствии скачивается. Успешная загрузка кэшируется lru_cache, неудачная -- на CATALOG_EMBEDDING_FAILURE_TTL_SECONDS."""
    global _model_failure
    from sentence_transformers import SentenceTransformer

    name = catalog_embedding_model()
    failure = _model_failure
    if failure and failure[1] == name and time.monotonic() < failure[0]:
        raise EmbeddingModelUnavailable(_UNAVAILABLE_MESSAGE)
    try:
        try:
            model = SentenceTransformer(name, local_files_only=True)
        except Exception:
            if not allow_download:
                raise
            logger.warning("Embedding model %s not in local cache, downloading", name)
            model = SentenceTransformer(name)
    except Exception as error:
        _model_failure = (time.monotonic() + catalog_embedding_failure_ttl(), name)
        logger.error("Embedding model %s failed to load: %s", name, error)
        raise EmbeddingModelUnavailable(_UNAVAILABLE_MESSAGE) from error
    _model_failure = None
    return model


def create_embeddings(texts: list[str], *, allow_download: bool = True) -> list[list[float]]:
    if not texts:
        return []
    model = embedding_model() if allow_download else embedding_model(allow_download=False)
    encoded = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    vectors = [vector.tolist() for vector in encoded]
    expected = catalog_vector_size()
    if any(len(vector) != expected for vector in vectors):
        raise ValueError(f"Expected {expected}-dim embeddings")
    return vectors


def create_embedding(text: str, *, allow_download: bool = True) -> list[float]:
    return create_embeddings([text], allow_download=allow_download)[0]


@lru_cache(maxsize=2)
def qdrant_client(*, search: bool = False):
    from qdrant_client import QdrantClient

    url = str(getattr(settings, "QDRANT_URL", "") or "").strip()
    key = str(getattr(settings, "QDRANT_API_KEY", "") or "").strip()
    if not url or not key:
        raise ValueError("QDRANT_URL and QDRANT_API_KEY are required")
    timeout = float(getattr(settings, "QDRANT_TIMEOUT_SECONDS", 5) or 5)
    if search:
        timeout = min(timeout, max(0.1, float(getattr(settings, "CATALOG_VECTOR_SEARCH_TIMEOUT_SECONDS", 1) or 1)))
    return QdrantClient(url=url, api_key=key, timeout=timeout, prefer_grpc=False)


def ensure_qdrant_collection() -> str:
    from qdrant_client.models import Distance, VectorParams

    client = qdrant_client()
    name = catalog_qdrant_collection()
    size = catalog_vector_size()
    if not client.collection_exists(name):
        client.create_collection(collection_name=name, vectors_config=VectorParams(size=size, distance=Distance.COSINE))
        return name
    info = client.get_collection(name)
    configured = getattr(info.config.params.vectors, "size", None)
    if configured != size:
        raise ValueError(f"Qdrant collection {name} has vector size {configured}, expected {size}")
    return name


@transaction.atomic
def ensure_product_for_supplier_product(item) -> Product:
    if item.product_id:
        return item.product
    text = build_supplier_product_embedding_text(item)
    product, _ = Product.objects.get_or_create(
        normalized_name=item.normalized_name,
        brand=item.brand,
        category=item.category,
        package=item.package,
        defaults={
            "canonical_name": item.normalized_name,
            "enriched_text": text,
            "base_unit": item.unit,
            "status": EntityStatus.ACTIVE,
        },
    )
    update_fields = []
    if product.canonical_name != item.normalized_name:
        product.canonical_name = item.normalized_name
        update_fields.append("canonical_name")
    if product.enriched_text != text:
        product.enriched_text = text
        update_fields.append("enriched_text")
    if product.base_unit_id != item.unit_id:
        product.base_unit = item.unit
        update_fields.append("base_unit")
    if product.status != EntityStatus.ACTIVE:
        product.status = EntityStatus.ACTIVE
        update_fields.append("status")
    if update_fields:
        update_fields.append("updated_at")
        product.save(update_fields=update_fields)
    item.product = product
    item.save(update_fields=["product", "updated_at"])
    return product


def sync_supplier_products_to_qdrant(items, *, force: bool = False, batch_size: int = 128) -> tuple[int, int]:
    from qdrant_client.models import PointStruct

    products = list(items)
    if not products:
        return 0, 0
    client = qdrant_client()
    collection = ensure_qdrant_collection()
    existing = {}
    ids = [str(item.id) for item in products]
    for start in range(0, len(ids), batch_size):
        points = client.retrieve(
            collection_name=collection, ids=ids[start : start + batch_size], with_payload=True, with_vectors=False
        )
        existing.update({str(point.id): point for point in points})
    pending = []
    skipped = 0
    for item in products:
        text = build_supplier_product_embedding_text(item)
        source_hash = embedding_source_hash(text) if text else ""
        point = existing.get(str(item.id))
        payload = point.payload if point and point.payload else {}
        if not text or (not force and payload.get("sourceHash") == source_hash):
            skipped += 1
        else:
            pending.append((item, text, source_hash))
    processed = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = create_embeddings([text for _, text, _ in batch])
        points = [
            PointStruct(
                id=str(item.id),
                vector=vector,
                payload={
                    "recordType": "supplierProduct",
                    "supplierProductId": str(item.id),
                    "productId": str(item.product_id) if item.product_id else None,
                    "sourceHash": source_hash,
                    "embeddingModel": catalog_embedding_model(),
                },
            )
            for (item, _, source_hash), vector in zip(batch, vectors, strict=True)
        ]
        client.upsert(collection_name=collection, points=points, wait=True)
        processed += len(points)
    return processed, skipped


def vector_supplier_product_scores(query_text: str, *, limit: int | None = None) -> dict[str, float]:
    if not catalog_vector_search_enabled() or not query_text.strip():
        return {}
    try:
        points = (
            qdrant_client(search=True)
            .query_points(
                collection_name=catalog_qdrant_collection(),
                query=create_embedding(query_text, allow_download=False),
                limit=limit or int(getattr(settings, "CATALOG_VECTOR_SEARCH_TOP_K", 100) or 100),
                score_threshold=float(getattr(settings, "CATALOG_VECTOR_MIN_SCORE", 0.2) or 0.2),
                with_payload=["supplierProductId"],
                with_vectors=False,
            )
            .points
        )
    except Exception as exc:
        logger.warning("Qdrant catalog vector search failed: %s", exc)
        return {}
    return {
        str(point.payload["supplierProductId"]): float(point.score)
        for point in points
        if point.payload and point.payload.get("supplierProductId")
    }
