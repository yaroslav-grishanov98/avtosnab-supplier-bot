"""ТЗ §8/§20 ENRICH stage. Chunked LLMConnector client, same shape and
per-chunk try/except pattern as api.v1.catalog.candidate_ranking_llm and
api.v1.catalog.unit_llm_client (TASK-75/112/113 lesson: build the chunking
in from the start)."""

import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from api.llm import LLMConnector, LLMConnectorError, LLMRuntime, get_llm_runtime
from api.v1.suppliers.product_import_contracts import SupplierProductEnrichmentBatch
from api.v1.suppliers.product_import_hints import has_country_hint
from core.models import EntityStatus, Unit

logger = logging.getLogger("autosnab.suppliers.product_import_llm")

_SYSTEM_PROMPT = (
    "Ты помогаешь безопасно дозаполнить карточки товаров поставщика AutoSnab при импорте. "
    "Тебе передан список строк rows (row_number, name, needs_unit, needs_country) и закрытый список единиц "
    "units (code, name). Для строки с needs_unit=true можешь предложить unit -- ТОЛЬКО значение поля code из "
    "units: единицу, в которой товар ПРОДАЁТСЯ (штука, упаковка, банка, бутылка, пакет, коробка; для весового "
    "товара -- кг). Для строки с needs_country=true можешь предложить country_of_origin -- СТРАНУ (не регион, "
    "не область, не город), ТОЛЬКО если она явно указана в названии; никогда по стереотипу вроде 'этот "
    "продукт обычно из страны X'. Поле, которое для строки не запрошено, оставляй null. Никогда не предлагай "
    "цену, кратность заказа, минимальное количество, вес/объём или статус наличия -- этих полей в схеме "
    "ответа нет специально. Если не уверен -- оставь поле пустым (null), не гадай."
)


# Модель иногда возвращает текст вместо null для «не знаю».
_EMPTY_COUNTRY_MARKERS = {"null", "none", "n/a", "na", "-", "—", "unknown", "неизвестно", "не указано"}


# Единицы продажи, которые имеет смысл предлагать модели (TASK-126): на прогоне ~2800 строк из «хвоста» списка
# (рулон, лоток, ведро…) модель выбирала единицу по созвучию («лайм штука» -> «рулон»).
_SALE_UNIT_CODES = ("кг", "г", "л", "мл", "шт", "уп", "бан", "бут", "пак", "кор", "пач")


class SupplierProductEnrichmentError(Exception):
    pass


def _chunk_size():
    try:
        value = int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE", 10) or 10)
    except (TypeError, ValueError):
        return 10
    return value if value > 0 else 10


def _max_workers():
    try:
        value = int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS", 16) or 16)
    except (TypeError, ValueError):
        return 16
    return value if value > 0 else 16


def _chunk_retries():
    try:
        value = int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_RETRIES", 1))
    except (TypeError, ValueError):
        return 1
    return max(value, 0)


def _connector():
    timeout_seconds = int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_TIMEOUT_SECONDS", 40) or 40)
    if str(getattr(settings, "LLM_PRIMARY_PROVIDER", "") or "").strip():
        return LLMConnector()
    runtime = get_llm_runtime()
    return LLMConnector(
        LLMRuntime(
            provider=runtime.provider,
            api_key=runtime.api_key,
            base_url=runtime.base_url,
            model=runtime.model,
            timeout_seconds=timeout_seconds,
            anthropic_version=runtime.anthropic_version,
        )
    )


def enrich_supplier_product_import_rows(*, rows):
    if not rows or not bool(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED", False)):
        return {}

    connector = _connector()
    active_units = list(Unit.objects.filter(status=EntityStatus.ACTIVE).order_by("code"))
    units = [{"code": unit.code, "name": unit.name} for unit in active_units if unit.code in _SALE_UNIT_CODES]
    unit_id_by_code = {unit.code: str(unit.id) for unit in active_units if unit.code in _SALE_UNIT_CODES}
    rows_by_number = {row["row_number"]: row for row in rows}
    chunk_size = _chunk_size()
    chunks = [rows[start : start + chunk_size] for start in range(0, len(rows), chunk_size)]
    chunks_total = len(chunks)
    logger.info("product_import_llm.start rows=%s chunks=%s", len(rows), chunks_total)

    def run_chunk(chunk_index, chunk):
        # Чанки независимы, LLMConnector.structured без общего состояния (клиент на каждый вызов) --
        # безопасно гонять параллельно; в БД потоки не ходят. Хвост задержек модели длинный
        # (замер 2026-09-30, 8 потоков: ~11% чанков ловили таймаут) -- упавший чанк повторяем.
        attempts = 1 + _chunk_retries()
        for attempt in range(1, attempts + 1):
            try:
                return connector.structured(
                    scenario="supplier_product_import_enrichment",
                    system_prompt=_SYSTEM_PROMPT,
                    payload={"rows": chunk, "units": units},
                    response_model=SupplierProductEnrichmentBatch,
                    max_tokens=max(500, min(4096, len(chunk) * 120 + 300)),
                )
            except LLMConnectorError as error:
                logger.warning(
                    "product_import_llm.chunk_failed chunk=%s/%s attempt=%s/%s rows=%s error=%s",
                    chunk_index,
                    chunks_total,
                    attempt,
                    attempts,
                    len(chunk),
                    error,
                )
        return None

    with ThreadPoolExecutor(max_workers=min(_max_workers(), chunks_total)) as executor:
        batches = list(executor.map(run_chunk, range(1, chunks_total + 1), chunks))

    enriched = {}
    chunks_failed = 0
    # Результаты собираем в порядке чанков -- итог детерминирован независимо от скорости потоков.
    for batch in batches:
        if batch is None:
            chunks_failed += 1
            continue
        for item in batch.items:
            row = rows_by_number.get(item.row_number)
            if row is None:
                continue
            fields = {}
            # Единицу принимаем только для строки, где она запрошена, и только код из закрытого списка.
            # Строка без флагов needs_* (старый вызов) считается запросившей оба поля.
            if row.get("needs_unit", True) and item.unit in unit_id_by_code:
                fields["unit_id"] = unit_id_by_code[item.unit]
            country = (item.country_of_origin or "").strip()
            if (
                row.get("needs_country", True)
                and country
                and country.lower() not in _EMPTY_COUNTRY_MARKERS
                and has_country_hint(row.get("name") or "")
            ):
                fields["country_of_origin"] = country
            if fields:
                enriched[item.row_number] = fields

    if chunks_failed == chunks_total:
        raise SupplierProductEnrichmentError(
            f"Дозаполнение недоступно: все {chunks_total} чанков завершились с ошибкой"
        )

    logger.info(
        "product_import_llm.done rows=%s enriched=%s chunks=%s chunks_failed=%s",
        len(rows),
        len(enriched),
        chunks_total,
        chunks_failed,
    )
    return enriched
