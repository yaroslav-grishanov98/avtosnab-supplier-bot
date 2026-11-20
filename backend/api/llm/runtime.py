from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings


@dataclass(frozen=True)
class LLMRuntime:
    provider: str
    api_key: str
    base_url: str | None
    model: str
    timeout_seconds: int
    anthropic_version: str


def _clean_base_url(value):
    value = str(value or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")]
    return value or None


def _runtime_from_prefix(prefix: str) -> LLMRuntime | None:
    provider = str(getattr(settings, f"{prefix}_PROVIDER", "") or "").strip().lower()
    base_url = _clean_base_url(getattr(settings, f"{prefix}_BASE_URL", ""))
    api_key = str(getattr(settings, f"{prefix}_API_KEY", "") or "").strip()
    model = str(getattr(settings, f"{prefix}_MODEL", "") or "").strip()
    if not provider and not base_url and not api_key and not model:
        return None
    if not provider or not model:
        raise ValueError(f"{prefix}_PROVIDER and {prefix}_MODEL must be configured together.")
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    hostname = urlparse(base_url or "").hostname
    if not api_key and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{prefix}_API_KEY is required for a non-local provider.")
    return LLMRuntime(
        provider=provider,
        api_key=api_key or "local",
        base_url=base_url,
        model=model,
        timeout_seconds=int(getattr(settings, f"{prefix}_TIMEOUT_SECONDS", 60) or 60),
        anthropic_version=str(getattr(settings, "AI_NORMALIZATION_ANTHROPIC_VERSION", "2023-06-01") or "2023-06-01"),
    )


def llm_primary_provider_configured() -> bool:
    """True when LLM_PRIMARY_PROVIDER is set — in that case LLMConnector()
    is fully self-sufficient (it reads LLM_PRIMARY_*/LLM_FALLBACK_* itself
    via get_llm_runtimes()) and callers must not require an OpenAI-specific
    api_key/base_url/model/timeout_seconds to reach it."""
    return bool(str(getattr(settings, "LLM_PRIMARY_PROVIDER", "") or "").strip())


def get_llm_runtime() -> LLMRuntime:
    """Return the one project-wide LLM runtime configuration."""
    primary = _runtime_from_prefix("LLM_PRIMARY")
    if primary:
        return primary
    provider = str(getattr(settings, "AI_NORMALIZATION_PROVIDER", "openai") or "openai").strip().lower()
    api_key = str(
        getattr(settings, "OPENAI_API_KEY", "") or getattr(settings, "AI_NORMALIZATION_API_KEY", "") or ""
    ).strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    model = str(
        getattr(settings, "OPENAI_NORMALIZATION_MODEL", "")
        or getattr(settings, "AI_NORMALIZATION_MODEL", "gpt-4o-mini")
    ).strip()
    return LLMRuntime(
        provider=provider,
        api_key=api_key,
        base_url=_clean_base_url(
            getattr(settings, "OPENAI_BASE_URL", "") or getattr(settings, "AI_NORMALIZATION_URL", "")
        ),
        model=model,
        timeout_seconds=int(getattr(settings, "AI_NORMALIZATION_TIMEOUT_SECONDS", 60) or 60),
        anthropic_version=str(getattr(settings, "AI_NORMALIZATION_ANTHROPIC_VERSION", "2023-06-01") or "2023-06-01"),
    )


def get_llm_runtimes() -> list[LLMRuntime]:
    """Return primary runtime followed by an optional fallback runtime."""
    primary = get_llm_runtime()
    fallback = _runtime_from_prefix("LLM_FALLBACK")
    if fallback and fallback != primary:
        return [primary, fallback]
    return [primary]
