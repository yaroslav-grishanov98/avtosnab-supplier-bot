import logging

from django.conf import settings

from api.llm import LLMConnector, LLMConnectorError, LLMRuntime, get_llm_runtime
from api.v1.catalog.ai_contracts import UnitSuggestionBatch

logger = logging.getLogger("autosnab.catalog.unit_llm_client")

_MASTER_SYSTEM_PROMPT = (
    "Ты помогаешь определить единицу измерения товара для Мастер-справочника AutoSnab. "
    "Тебе передан закрытый список допустимых единиц (id, code, name) и список строк с "
    "названием товара и исходным текстом единицы измерения, который не совпал со справочником. "
    "Для каждой входной строки верни ровно одну запись в suggestions: unit_id — ТОЛЬКО id из "
    "переданного списка единиц, если уверен в соответствии, иначе unit_id: null. Никогда не "
    "придумывай unit_id, которого нет в списке, и не пропускай row_number входных строк."
)

# TASK-122 п.1: same task, different target — подсказывает единицу для товара
# компании (VenueProductMatrixItem.requested_unit_label), не Мастер-справочника.
_VENUE_SYSTEM_PROMPT = (
    "Ты помогаешь определить единицу измерения товара в справочнике товаров компании "
    "(заведения) AutoSnab. Тебе передан закрытый список допустимых единиц (id, code, name) и "
    "список строк с названием товара и исходным текстом единицы измерения, который не совпал со "
    "справочником. Для каждой входной строки верни ровно одну запись в suggestions: unit_id — "
    "ТОЛЬКО id из переданного списка единиц, если уверен в соответствии, иначе unit_id: null. "
    "Никогда не придумывай unit_id, которого нет в списке, и не пропускай row_number входных строк."
)

_DEFAULT_CHUNK_SIZE = 30


class UnitSuggestionError(Exception):
    """Raised when the unit-suggestion provider can't be reached or returns nothing usable."""


def _chunk_size(setting_name):
    try:
        value = int(getattr(settings, setting_name, _DEFAULT_CHUNK_SIZE) or _DEFAULT_CHUNK_SIZE)
    except (TypeError, ValueError):
        return _DEFAULT_CHUNK_SIZE
    return value if value > 0 else _DEFAULT_CHUNK_SIZE


def _suggest_unit_ids(*, rows, units, scenario, system_prompt, chunk_size_setting, timeout_setting):
    """Shared implementation behind suggest_unit_ids/suggest_venue_unit_ids.

    rows: [{"row_number", "raw_name", "unit_label"}], units: [{"id", "code", "name"}].
    Returns {row_number: unit_id} for rows the model matched with confidence;
    rows it couldn't match are simply absent from the result (not an error).
    Raises UnitSuggestionError only when every chunk fails — callers treat
    that as "skip this batch", not as a reason to fail the whole preview.
    """
    if not rows:
        return {}

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
    logger.info("unit_llm_client.start scenario=%s rows=%s chunks=%s", scenario, len(rows), chunks_total)

    matched = {}
    chunks_failed = 0
    for chunk_index, chunk in enumerate(chunks, start=1):
        try:
            batch = connector.structured(
                scenario=scenario,
                system_prompt=system_prompt,
                payload={"units": units, "rows": chunk},
                response_model=UnitSuggestionBatch,
            )
        except LLMConnectorError as error:
            chunks_failed += 1
            logger.warning(
                "unit_llm_client.chunk_failed scenario=%s chunk=%s/%s rows=%s error=%s",
                scenario,
                chunk_index,
                chunks_total,
                len(chunk),
                error,
            )
            continue
        matched.update(
            {
                suggestion.row_number: suggestion.unit_id
                for suggestion in batch.suggestions
                if suggestion.unit_id is not None
            }
        )

    if chunks_failed == chunks_total:
        raise UnitSuggestionError(
            f"Подсказка единицы измерения недоступна: все {chunks_total} чанков завершились с ошибкой"
        )

    logger.info(
        "unit_llm_client.done scenario=%s rows=%s matched=%s chunks=%s chunks_failed=%s",
        scenario,
        len(rows),
        len(matched),
        chunks_total,
        chunks_failed,
    )
    return matched


def suggest_unit_ids(*, rows, units):
    """Мастер-справочник (TASK-75 §35-39). Signature and behaviour unchanged
    by the TASK-122 refactor — see _suggest_unit_ids for the shared contract."""
    return _suggest_unit_ids(
        rows=rows,
        units=units,
        scenario="master_catalog_unit_suggestion",
        system_prompt=_MASTER_SYSTEM_PROMPT,
        chunk_size_setting="MASTER_CATALOG_UNIT_LLM_CHUNK_SIZE",
        timeout_setting="MASTER_CATALOG_UNIT_LLM_TIMEOUT_SECONDS",
    )


def suggest_venue_unit_ids(*, rows, units):
    """Справочник компании (TASK-122 п.1, raw ТЗ §1/§35-39). Independent
    scenario/settings from suggest_unit_ids, same behaviour otherwise."""
    return _suggest_unit_ids(
        rows=rows,
        units=units,
        scenario="venue_product_matrix_unit_suggestion",
        system_prompt=_VENUE_SYSTEM_PROMPT,
        chunk_size_setting="VENUE_IMPORT_UNIT_LLM_CHUNK_SIZE",
        timeout_setting="VENUE_IMPORT_UNIT_LLM_TIMEOUT_SECONDS",
    )
