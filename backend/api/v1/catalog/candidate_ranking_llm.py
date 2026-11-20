import logging

from django.conf import settings

from api.llm import LLMConnector, LLMConnectorError, LLMRuntime, get_llm_runtime
from api.v1.catalog.ai_contracts import CandidateRankingBatch

logger = logging.getLogger("autosnab.catalog.candidate_ranking_llm")

_MASTER_SYSTEM_PROMPT = (
    "Ты помогаешь ранжировать похожие товары для Мастер-справочника AutoSnab. "
    "Тебе передан список строк, для каждой — исходное название товара и список кандидатов "
    "(id, canonical_name) из справочника, уже отобранных векторным поиском как потенциально "
    "похожие. Для каждой входной строки верни ranked_candidate_ids — подмножество ПЕРЕДАННЫХ "
    "id этой строки, от самого похожего к менее похожему. Никогда не придумывай id, которого "
    "не было в списке кандидатов этой строки. Если ни один кандидат не кажется уверенным "
    "совпадением — верни пустой список для этой строки, не гадай."
)

# TASK-122: same task, different target — ранжирует товары компании
# (VenueProductMatrixItem) вместо мастер-товаров, кандидаты пришли из
# pg_trgm (_venue_import_candidates), а не векторного поиска.
_VENUE_SYSTEM_PROMPT = (
    "Ты помогаешь ранжировать похожие товары в справочнике товаров компании (заведения) AutoSnab. "
    "Тебе передан список строк, для каждой — исходное название товара из импортируемого файла и "
    "список кандидатов (id, canonical_name) из справочника ЭТОЙ ЖЕ компании, уже отобранных "
    "текстовым поиском как потенциально похожие. Для каждой входной строки верни "
    "ranked_candidate_ids — подмножество ПЕРЕДАННЫХ id этой строки, от самого похожего к менее "
    "похожему. Никогда не придумывай id, которого не было в списке кандидатов этой строки. Если "
    "ни один кандидат не кажется уверенным совпадением — верни пустой список для этой строки, не "
    "гадай."
)

_DEFAULT_CHUNK_SIZE = 30


class CandidateRankingError(Exception):
    """Raised when the candidate-ranking provider can't be reached or returns nothing usable."""


def _max_tokens_for_chunk(chunk):
    """A full chunk's ranking output is a row_number plus up to
    MASTER_PRODUCT_MATCH_TOP_K (5) short UUID strings per row — smaller per row than
    api.v1.suppliers.ai_client._json_fallback_max_tokens's own scenario, but still
    needs real headroom: LLMConnector.structured's default max_tokens=1200 is well
    under what a full 30-row chunk serializes to, so the response gets cut off
    (finish_reason=length) and the whole chunk silently fails."""
    return max(500, min(4096, len(chunk) * 150 + 300))


def _chunk_size(setting_name):
    try:
        value = int(getattr(settings, setting_name, _DEFAULT_CHUNK_SIZE) or _DEFAULT_CHUNK_SIZE)
    except (TypeError, ValueError):
        return _DEFAULT_CHUNK_SIZE
    return value if value > 0 else _DEFAULT_CHUNK_SIZE


def _rerank(*, rows, scenario, system_prompt, chunk_size_setting, timeout_setting):
    """Shared implementation behind rerank_candidates/rerank_venue_candidates.

    rows: [{"row_number", "raw_name", "candidates": [{"id", "canonical_name"}]}].
    Returns {row_number: [candidate_id, ...]} for rows the model ranked with at least
    one valid (offered) id; rows it couldn't rank are simply absent from the result —
    the caller keeps its original (search) candidate order for those, this is
    not an error. Raises CandidateRankingError only when every chunk failed — callers
    treat that as "skip ranking entirely for this batch", not as a reason to fail
    the whole import preview.

    Mirrors api.v1.catalog.unit_llm_client.suggest_unit_ids's shape and chunking
    (added there in TASK-75/112/113 this same session) — same lesson applied here
    from the start instead of as a follow-up fix.
    """
    if not rows:
        return {}

    valid_ids_by_row = {row["row_number"]: {c["id"] for c in row["candidates"]} for row in rows}

    timeout_seconds = int(getattr(settings, timeout_setting, 20) or 20)
    if str(getattr(settings, "LLM_PRIMARY_PROVIDER", "") or "").strip():
        connector = LLMConnector()
    else:
        runtime = get_llm_runtime()
        connector = LLMConnector(
            LLMRuntime(
                provider=runtime.provider,
                api_key=runtime.api_key,
                base_url=runtime.base_url,
                model=runtime.model,
                timeout_seconds=timeout_seconds,
                anthropic_version=runtime.anthropic_version,
            )
        )

    chunk_size = _chunk_size(chunk_size_setting)
    chunks = [rows[start : start + chunk_size] for start in range(0, len(rows), chunk_size)]
    chunks_total = len(chunks)
    logger.info("candidate_ranking_llm.start scenario=%s rows=%s chunks=%s", scenario, len(rows), chunks_total)

    ranked = {}
    chunks_failed = 0
    for chunk_index, chunk in enumerate(chunks, start=1):
        try:
            batch = connector.structured(
                scenario=scenario,
                system_prompt=system_prompt,
                payload={"rows": chunk},
                response_model=CandidateRankingBatch,
                max_tokens=_max_tokens_for_chunk(chunk),
            )
        except LLMConnectorError as error:
            chunks_failed += 1
            logger.warning(
                "candidate_ranking_llm.chunk_failed scenario=%s chunk=%s/%s rows=%s error=%s",
                scenario,
                chunk_index,
                chunks_total,
                len(chunk),
                error,
            )
            continue
        for ranking in batch.rankings:
            valid_ids = valid_ids_by_row.get(ranking.row_number)
            if not valid_ids:
                continue
            filtered = list(dict.fromkeys(cid for cid in ranking.ranked_candidate_ids if cid in valid_ids))
            if filtered:
                ranked[ranking.row_number] = filtered

    if chunks_failed == chunks_total:
        raise CandidateRankingError(
            f"Ранжирование кандидатов недоступно: все {chunks_total} чанков завершились с ошибкой"
        )

    logger.info(
        "candidate_ranking_llm.done scenario=%s rows=%s ranked=%s chunks=%s chunks_failed=%s",
        scenario,
        len(rows),
        len(ranked),
        chunks_total,
        chunks_failed,
    )
    return ranked


def rerank_candidates(*, rows):
    """Мастер-справочник (TASK-75 §40-42). See _rerank for the shared contract."""
    return _rerank(
        rows=rows,
        scenario="master_product_candidate_ranking",
        system_prompt=_MASTER_SYSTEM_PROMPT,
        chunk_size_setting="MASTER_PRODUCT_CANDIDATE_RANKING_CHUNK_SIZE",
        timeout_setting="MASTER_PRODUCT_CANDIDATE_RANKING_TIMEOUT_SECONDS",
    )


def rerank_venue_candidates(*, rows):
    """Справочник компании (TASK-122, raw ТЗ §1/§40-42). See _rerank for the
    shared contract — independent scenario name and settings from
    rerank_candidates, same behaviour otherwise."""
    return _rerank(
        rows=rows,
        scenario="venue_product_matrix_candidate_ranking",
        system_prompt=_VENUE_SYSTEM_PROMPT,
        chunk_size_setting="VENUE_IMPORT_CANDIDATE_RANKING_CHUNK_SIZE",
        timeout_setting="VENUE_IMPORT_CANDIDATE_RANKING_TIMEOUT_SECONDS",
    )
