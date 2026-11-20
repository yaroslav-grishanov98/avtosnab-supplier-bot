import json
import logging
import re
import urllib.request

import instructor
from django.conf import settings
from openai import OpenAI
from pydantic import ValidationError

from api.llm.connector import LLMConnector
from api.llm.runtime import LLMRuntime, get_llm_runtime, llm_primary_provider_configured
from api.v1.suppliers.ai_contracts import ParsedSupplierCatalog

logger = logging.getLogger("autosnab.suppliers.ai_client")


def request_ai_normalization_payload(
    *,
    api_key,
    base_url,
    model,
    timeout_seconds,
    upload,
    input_rows,
    phase="normalization",
    unit_codes=None,
):
    system_prompt = _system_prompt(phase, unit_codes=unit_codes)
    if bool(getattr(settings, "LLM_CONNECTOR_ENABLED", False)):
        if llm_primary_provider_configured():
            connector = LLMConnector()
        else:
            runtime = get_llm_runtime()
            connector = LLMConnector(
                LLMRuntime(
                    provider=runtime.provider,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    anthropic_version=runtime.anthropic_version,
                )
            )
        result = connector.structured(
            scenario=f"supplier_price_{phase}",
            system_prompt=system_prompt,
            payload={
                "supplier": upload.supplier.display_name,
                "upload_id": str(upload.id),
                "phase": phase,
                "rows": input_rows,
            },
            response_model=ParsedSupplierCatalog,
            max_tokens=_json_fallback_max_tokens(input_rows),
        )
        return result.model_dump(mode="json")
    if _provider() == "anthropic":
        return _request_ai_normalization_payload_anthropic(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            phase=phase,
            upload=upload,
            input_rows=input_rows,
            system_prompt=system_prompt,
            unit_codes=unit_codes,
        ).model_dump(mode="json")

    client_kwargs = {
        "api_key": api_key,
        "timeout": timeout_seconds,
        "max_retries": 0,
    }
    if base_url:
        client_kwargs["base_url"] = base_url

    openai_client = OpenAI(**client_kwargs)
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": (
                f"Поставщик: {upload.supplier.display_name}\n"
                f"Upload ID: {upload.id}\n\n"
                f"Фаза: {phase}\n"
                "Строки прайса после первичного Python-разбора:\n" + json.dumps(input_rows, ensure_ascii=False)
            ),
        },
    ]

    if not _use_instructor():
        return _request_ai_normalization_payload_json_fallback(
            client=openai_client,
            model=model,
            phase=phase,
            upload=upload,
            input_rows=input_rows,
            messages=messages,
            base_url=base_url,
            unit_codes=unit_codes,
        ).model_dump(mode="json")

    try:
        logger.info(
            "supplier_price_ai.instructor.start upload_id=%s supplier_id=%s phase=%s model=%s rows=%s base_url=%s",
            upload.id,
            getattr(upload, "supplier_id", None),
            phase,
            model,
            len(input_rows),
            base_url or "default-openai",
        )
        parsed_catalog = instructor.from_openai(openai_client).chat.completions.create(
            model=model,
            response_model=ParsedSupplierCatalog,
            temperature=0,
            messages=messages,
        )
    except Exception as error:
        logger.warning(
            "supplier_price_ai.instructor.failed upload_id=%s phase=%s model=%s rows=%s error_type=%s error=%s",
            upload.id,
            phase,
            model,
            len(input_rows),
            error.__class__.__name__,
            str(error)[:500],
        )
        parsed_catalog = _request_ai_normalization_payload_json_fallback(
            client=openai_client,
            model=model,
            phase=phase,
            upload=upload,
            input_rows=input_rows,
            messages=messages,
            base_url=base_url,
            unit_codes=unit_codes,
        )
    return parsed_catalog.model_dump(mode="json")


def _provider():
    return get_llm_runtime().provider


def _use_instructor():
    return bool(getattr(settings, "AI_NORMALIZATION_USE_INSTRUCTOR", False))


def _system_prompt(phase, *, unit_codes=None):
    allowed_units = ", ".join(unit_codes) if unit_codes else "шт, кг, г, л, мл"
    if phase == "ai_cleanup":
        return (
            "Ты ревизор второй AI-дочистки прайс-листов AutoSnab. "
            "Перед тобой исходные cells и результат первичного Python-разбора. "
            "Проверь каждую строку семантически, исправь ошибки нормализации, единиц, фасовки, веса, объема, "
            "SKU и названия товара. Не переписывай уверенные поля без причины. "
            "Не изменяй цену, кратность заказа, минимальное количество и статус наличия -- эти поля "
            "приходят только из первичного разбора, твои значения для них игнорируются независимо от того, "
            "что ты вернёшь. Не выдумывай артикул, бренд или упаковку. "
            "Если данных недостаточно, оставь warnings и низкий confidence. "
            f"Сохраняй row_number из входа. unit_code используй только из набора: {allowed_units}. "
            "В comment кратко укажи причину существенного исправления."
        )
    return (
        "Ты интеллектуальный парсер прайс-листов поставщиков для AutoSnab. "
        "Перед тобой строки таблицы или текста поставщика, где колонки могут быть перепутаны, "
        "а название, артикул, вес, единица и цена могут быть записаны в одной ячейке. "
        "Разбери каждую входную строку семантически и заполни строгую структуру. "
        "Не выдумывай цену, артикул, бренд или упаковку. Если цена указана с валютой, верни только число. "
        f"Сохраняй row_number из входа. unit_code используй только из набора: {allowed_units}."
    )


def _json_schema_prompt(*, unit_codes=None):
    allowed_units = ", ".join(unit_codes) if unit_codes else "шт, кг, г, л, мл"
    return (
        "Return only valid JSON, no markdown. "
        'Schema: {"products":[{"row_number":1,"raw_name":"","normalized_name":"",'
        '"price":0,"currency":"RUB","unit_code":"шт","package_info":null,'
        '"brand":null,"volume":null,"volume_unit":null,"weight":null,"weight_unit":null,'
        '"min_order_qty":null,"multiplicity":null,"supplier_sku":null,"category_path":[],'
        '"availability_status":"available","confidence":0.95,"comment":null,"warnings":[]}]}. '
        "Use source row_number. Do not invent missing price/SKU/brand/package. "
        f"Allowed unit_code values: {allowed_units}."
    )


def _request_ai_normalization_payload_json_fallback(
    *,
    client,
    model,
    phase,
    upload,
    input_rows,
    messages,
    base_url,
    unit_codes=None,
):
    fallback_messages = [
        {
            "role": "system",
            "content": _json_schema_prompt(unit_codes=unit_codes),
        },
        {
            "role": "user",
            "content": (
                f"Supplier: {upload.supplier.display_name}\n"
                f"Phase: {phase}\n"
                "Rows JSON:\n" + json.dumps(input_rows, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]
    logger.info(
        "supplier_price_ai.json_fallback.start upload_id=%s supplier_id=%s phase=%s model=%s rows=%s base_url=%s",
        upload.id,
        getattr(upload, "supplier_id", None),
        phase,
        model,
        len(input_rows),
        base_url or "default-openai",
    )
    completion = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=_json_fallback_max_tokens(input_rows),
        messages=fallback_messages,
    )
    content = _completion_content(completion)
    payload = _json_object_from_text(content)
    try:
        parsed_catalog = ParsedSupplierCatalog.model_validate(payload)
    except ValidationError:
        logger.warning(
            "supplier_price_ai.json_fallback.validation_failed upload_id=%s phase=%s content=%s",
            upload.id,
            phase,
            content[:1000],
        )
        raise
    logger.info(
        "supplier_price_ai.json_fallback.finish upload_id=%s phase=%s rows=%s products=%s",
        upload.id,
        phase,
        len(input_rows),
        len(parsed_catalog.products),
    )
    return parsed_catalog


def _request_ai_normalization_payload_anthropic(
    *,
    api_key,
    base_url,
    model,
    timeout_seconds,
    phase,
    upload,
    input_rows,
    system_prompt,
    unit_codes=None,
):
    url = _anthropic_messages_url(base_url)
    request_payload = {
        "model": model,
        "max_tokens": _json_fallback_max_tokens(input_rows),
        "temperature": 0,
        "system": f"{system_prompt}\n\n{_json_schema_prompt(unit_codes=unit_codes)}",
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Supplier: {upload.supplier.display_name}\n"
                    f"Phase: {phase}\n"
                    "Rows JSON:\n" + json.dumps(input_rows, ensure_ascii=False, separators=(",", ":"))
                ),
            }
        ],
    }
    logger.info(
        "supplier_price_ai.anthropic.start upload_id=%s supplier_id=%s phase=%s model=%s rows=%s url=%s",
        upload.id,
        getattr(upload, "supplier_id", None),
        phase,
        model,
        len(input_rows),
        url,
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": str(getattr(settings, "AI_NORMALIZATION_ANTHROPIC_VERSION", "2023-06-01")),
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response_payload = json.loads(response.read().decode("utf-8"))
    content = _anthropic_completion_content(response_payload)
    payload = _json_object_from_text(content)
    parsed_catalog = ParsedSupplierCatalog.model_validate(payload)
    logger.info(
        "supplier_price_ai.anthropic.finish upload_id=%s phase=%s rows=%s products=%s",
        upload.id,
        phase,
        len(input_rows),
        len(parsed_catalog.products),
    )
    return parsed_catalog


def _anthropic_messages_url(base_url):
    url = str(base_url or "https://api.anthropic.com/v1").rstrip("/")
    if url.endswith("/messages"):
        return url
    return f"{url}/messages"


def _anthropic_completion_content(payload):
    parts = payload.get("content") or []
    text_parts = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            text_parts.append(str(part.get("text") or ""))
    content = "".join(text_parts).strip()
    if not content:
        raise ValueError("Anthropic normalization returned empty content.")
    return content


def _json_fallback_max_tokens(input_rows):
    return max(500, min(4000, len(input_rows) * 260 + 240))


def _completion_content(completion):
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise ValueError("AI JSON fallback returned no choices.")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not content:
        raise ValueError("AI JSON fallback returned empty content.")
    return content


def _json_object_from_text(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])
