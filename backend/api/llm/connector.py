import json
import logging
import re
import time
import urllib.request
from typing import TypeVar
from urllib.parse import urlparse

from openai import DefaultHttpxClient, OpenAI
from pydantic import BaseModel

from .runtime import LLMRuntime, get_llm_runtimes

logger = logging.getLogger("autosnab.llm.connector")
ResponseT = TypeVar("ResponseT", bound=BaseModel)


class LLMConnectorError(RuntimeError):
    pass


class LLMConnector:
    """Provider-neutral structured-output connector.

    Business modules own prompts and response models. This class owns provider
    transport, JSON extraction, validation, timeouts and common logging.
    It never writes business data to the database.
    """

    def __init__(
        self,
        runtime: LLMRuntime | None = None,
        *,
        runtimes: list[LLMRuntime] | None = None,
        openai_factory=OpenAI,
        urlopen=None,
    ):
        if runtime is not None and runtimes is not None:
            raise ValueError("Pass runtime or runtimes, not both.")
        self.runtimes = runtimes or ([runtime] if runtime else get_llm_runtimes())
        self.openai_factory = openai_factory
        self.urlopen = urlopen or urllib.request.urlopen

    def structured(
        self,
        *,
        scenario: str,
        system_prompt: str,
        payload: dict,
        response_model: type[ResponseT],
        max_tokens: int = 1200,
    ) -> ResponseT:
        errors = []
        for index, runtime in enumerate(self.runtimes):
            if not runtime.api_key:
                errors.append("credentials are not configured")
                continue
            logger.info(
                "llm.start scenario=%s provider=%s model=%s attempt=%s",
                scenario,
                runtime.provider,
                runtime.model,
                index + 1,
            )
            started = time.perf_counter()
            usage = {}
            try:
                if runtime.provider == "anthropic":
                    raw = self._anthropic(runtime, system_prompt, payload, response_model, max_tokens, usage)
                elif runtime.provider == "openai":
                    raw = self._openai(runtime, system_prompt, payload, response_model, max_tokens, usage)
                else:
                    raise ValueError(f"Unsupported LLM provider: {runtime.provider}")
                result = response_model.model_validate(raw)
            except Exception as error:
                errors.append(f"{runtime.provider}/{runtime.model}: {error.__class__.__name__}")
                logger.warning(
                    "llm.failed scenario=%s provider=%s model=%s error_type=%s finish_reason=%s",
                    scenario,
                    runtime.provider,
                    runtime.model,
                    error.__class__.__name__,
                    usage.get("finish_reason"),
                )
                continue
            logger.info(
                "llm.finish scenario=%s provider=%s model=%s prompt_tokens=%s completion_tokens=%s "
                "finish_reason=%s duration=%.2f",
                scenario,
                runtime.provider,
                runtime.model,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("finish_reason"),
                time.perf_counter() - started,
            )
            return result
        raise LLMConnectorError(f"All LLM providers failed for {scenario}: {'; '.join(errors)}")

    def _openai(self, runtime, system_prompt, payload, response_model, max_tokens, usage_sink=None):
        kwargs = {
            "api_key": runtime.api_key,
            "timeout": runtime.timeout_seconds,
            "max_retries": 0,
        }
        if runtime.base_url:
            kwargs["base_url"] = runtime.base_url
        is_local = (urlparse(runtime.base_url).hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        if is_local:
            kwargs["http_client"] = DefaultHttpxClient(trust_env=False)
        client = self.openai_factory(**kwargs)
        schema = response_model.model_json_schema()
        response_format = (
            {"type": "json_object"}
            if is_local
            else {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": _strict_json_schema(schema),
                },
            }
        )
        if is_local:
            system_prompt = (
                f"{system_prompt}\nReturn only one JSON object matching this JSON Schema: "
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        completion = client.chat.completions.create(
            model=runtime.model,
            temperature=0,
            max_tokens=max_tokens,
            response_format=response_format,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        completion_usage = getattr(completion, "usage", None)
        if usage_sink is not None and completion_usage is not None:
            usage_sink["prompt_tokens"] = getattr(completion_usage, "prompt_tokens", None)
            usage_sink["completion_tokens"] = getattr(completion_usage, "completion_tokens", None)
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise ValueError("LLM returned no choices.")
        if usage_sink is not None:
            usage_sink["finish_reason"] = getattr(choices[0], "finish_reason", None)
        return _json_object(getattr(choices[0].message, "content", ""))

    def _anthropic(self, runtime, system_prompt, payload, response_model, max_tokens, usage_sink=None):
        base_url = runtime.base_url or "https://api.anthropic.com/v1"
        url = base_url if base_url.endswith("/messages") else f"{base_url}/messages"
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        body = {
            "model": runtime.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": f"{system_prompt}\nReturn only JSON matching this JSON Schema: {schema}",
            "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": runtime.api_key,
                "anthropic-version": runtime.anthropic_version,
                "content-type": "application/json",
            },
            method="POST",
        )
        with self.urlopen(request, timeout=runtime.timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        response_usage = response_payload.get("usage") or {}
        if usage_sink is not None:
            usage_sink["prompt_tokens"] = response_usage.get("input_tokens")
            usage_sink["completion_tokens"] = response_usage.get("output_tokens")
        content = "".join(
            str(part.get("text") or "")
            for part in response_payload.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text"
        )
        return _json_object(content)


def _strict_json_schema(node):
    """Recursively bring a Pydantic JSON schema in line with OpenAI's strict json_schema
    contract: every object node (including nested ones under $defs) needs
    additionalProperties: false and every one of its properties listed in required.

    TASK-113: also drops any anyOf branch that carries a regex `pattern` — Pydantic
    represents Decimal as anyOf[number, pattern-constrained string, ...] (to also accept a
    decimal-as-string), and OpenAI's strict mode cannot handle that combination: confirmed
    live, the model returns 0 completion tokens (finish_reason=length) for any schema
    containing it, for any prompt/input. The provider only ever emits a JSON number for a
    numeric field, so the string variant is never needed for parsing the response anyway.
    Dropping down to a single remaining branch unwraps the anyOf entirely, merging its
    keys with the node's own (title/default/description)."""
    if isinstance(node, dict):
        node = {key: _strict_json_schema(value) for key, value in node.items()}
        if isinstance(node.get("anyOf"), list):
            node["anyOf"] = [branch for branch in node["anyOf"] if "pattern" not in branch]
            if len(node["anyOf"]) == 1:
                only_branch = node.pop("anyOf")[0]
                node = {**only_branch, **node}
        if isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        return node
    if isinstance(node, list):
        return [_strict_json_schema(item) for item in node]
    return node


def _json_object(value):
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object.")
    return payload
