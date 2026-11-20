import asyncio
import hashlib
import json
import logging
import re

from config import LANGFUSE_CAPTURE_CONTENT, LANGFUSE_ENABLED, LANGFUSE_HASH_SALT

UNIT_ALIASES = {
    "шт": "шт",
    "штука": "шт",
    "штуки": "шт",
    "pcs": "шт",
    "pc": "шт",
    "кг": "кг",
    "kg": "кг",
    "килограмм": "кг",
    "г": "г",
    "гр": "г",
    "gram": "г",
    "g": "г",
    "л": "л",
    "литр": "л",
    "l": "л",
    "мл": "мл",
    "ml": "мл",
}


def clean_value(value, max_length: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False).replace(r"\s+", " ").strip()[:max_length]
    return str(value).strip()[:max_length]


def normalize_decimal(value, fallback=None) -> str:
    if value is None or value == "":
        return fallback
    normalized = re.sub(r"[^\d.]", "", str(value).replace(",", "."))
    if not normalized:
        return fallback
    try:
        return f"{float(normalized):.2f}"
    except ValueError:
        return fallback


def normalize_unit(value: str) -> str:
    raw = str(value).strip().lower()
    return UNIT_ALIASES.get(raw, raw)


def normalize_confidence(value) -> str:
    try:
        return f"{max(0.0, min(1.0, float(value))):.4f}"
    except (ValueError, TypeError):
        return "0.8000"


def strip_code_fence(text: str) -> str:
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def pick_ai_text(response: dict) -> str:
    paths = [
        "ai_raw_output",
        "output",
        "text",
        "content",
        ["message", "content"],
        ["choices", 0, "message", "content"],
        ["data", "choices", 0, "message", "content"],
        ["body", "choices", 0, "message", "content"],
        ["response", "choices", 0, "message", "content"],
    ]
    for path in paths:
        if isinstance(path, str):
            value = response.get(path)
        else:
            obj = response
            for key in path:
                obj = (
                    obj.get(key)
                    if isinstance(obj, dict)
                    else (obj[key] if isinstance(obj, list) and len(obj) > key else None)
                )
            value = obj
        if value:
            return value if isinstance(value, str) else json.dumps(value)
    return ""


logger = logging.getLogger(__name__)
_langfuse_client = None


def _hash_identifier(value: object) -> str:
    raw = f"{LANGFUSE_HASH_SALT}:{value}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _sanitize_langfuse_value(key: str, value):
    lowered = key.lower()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize_langfuse_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_langfuse_value(key, item) for item in value[:50]]

    text = str(value)
    if any(part in lowered for part in ("chat_id", "user_id", "telegram_id", "file_id")):
        return _hash_identifier(text)
    if not LANGFUSE_CAPTURE_CONTENT and any(
        part in lowered for part in ("text", "content", "prompt", "message", "input", "output")
    ):
        return {"redacted": True, "length": len(text)}
    return text[:1000]


def _get_langfuse_client():
    global _langfuse_client
    if not LANGFUSE_ENABLED:
        return None
    if _langfuse_client is None:
        from langfuse import get_client

        _langfuse_client = get_client()
    return _langfuse_client


def _emit_langfuse_event(name: str, payload: dict, level: str) -> None:
    client = _get_langfuse_client()
    if client is None:
        return
    metadata = {str(key): _sanitize_langfuse_value(str(key), value) for key, value in payload.items()}
    observation = client.start_observation(
        name=f"autosnab.bot.{name}",
        as_type="span",
        metadata=metadata,
        level=level,
    )
    observation.end()


async def langfuse_track(name: str, payload: dict | None = None, level: str = "DEFAULT") -> None:
    """Send one bot event to Langfuse without blocking or breaking the bot."""
    if not LANGFUSE_ENABLED:
        return
    try:
        await asyncio.to_thread(_emit_langfuse_event, name, payload or {}, level)
    except Exception as exc:
        logger.warning("Langfuse event %s was not sent: %s", name, exc)


async def langfuse_flush() -> None:
    """Flush queued Langfuse events during tests and graceful shutdown."""
    if not LANGFUSE_ENABLED:
        return
    try:
        client = _get_langfuse_client()
        if client is not None:
            await asyncio.to_thread(client.flush)
    except Exception as exc:
        logger.warning("Langfuse flush failed: %s", exc)
