import base64
import json
import logging
import urllib.request

from django.conf import settings
from openai import OpenAI

logger = logging.getLogger("autosnab.catalog.vision_client")

_RECOGNITION_PROMPT = (
    "Ты помогаешь покупателю найти товар в каталоге продуктов питания и хозтоваров по фото. "
    "Определи, что изображено, и верни короткую поисковую фразу на русском языке "
    "(2-5 слов: тип товара, и бренд/сорт, если он виден на упаковке), без пояснений и кавычек. "
    "Если на фото нет узнаваемого товара для закупки — верни пустую строку."
)

MAX_RECOGNITION_TOKENS = 40


class PhotoSearchError(Exception):
    """Raised when the vision provider can't be reached or isn't configured."""


def recognize_product_search_query(*, image_bytes, content_type):
    """Ask the configured vision-capable model what product is on the photo.

    Reuses the same AI_NORMALIZATION_* provider settings as supplier price
    normalization (api.v1.suppliers.ai_client) — same credentials, same
    OpenAI-compatible/Anthropic-compatible split — since this project only
    has the one AI gateway configured, not a separate vision provider.
    """
    provider = str(getattr(settings, "AI_NORMALIZATION_PROVIDER", "openai") or "openai").strip().lower()
    api_key = _api_key()
    model = str(
        getattr(settings, "OPENAI_NORMALIZATION_MODEL", "")
        or getattr(settings, "AI_NORMALIZATION_MODEL", "gpt-4o-mini")
    )
    base_url = _base_url()
    timeout_seconds = int(getattr(settings, "AI_NORMALIZATION_TIMEOUT_SECONDS", 60) or 60)

    try:
        if provider == "anthropic":
            return _recognize_anthropic(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
                image_bytes=image_bytes,
                content_type=content_type,
            )
        return _recognize_openai(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            image_bytes=image_bytes,
            content_type=content_type,
        )
    except PhotoSearchError:
        raise
    except Exception as error:
        raise PhotoSearchError(f"Распознавание фото недоступно: {error}") from error


def _api_key():
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        api_key = str(getattr(settings, "AI_NORMALIZATION_API_KEY", "") or "").strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    if not api_key:
        raise PhotoSearchError("Поиск по фото не настроен: нет AI-ключа.")
    return api_key


def _base_url():
    base_url = str(getattr(settings, "OPENAI_BASE_URL", "") or "").strip()
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    return base_url or None


def _recognize_openai(*, api_key, base_url, model, timeout_seconds, image_bytes, content_type):
    client_kwargs = {"api_key": api_key, "timeout": timeout_seconds, "max_retries": 0}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)

    data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    completion = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=MAX_RECOGNITION_TOKENS,
        messages=[
            {"role": "system", "content": _RECOGNITION_PROMPT},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": data_url}}],
            },
        ],
    )
    text = (completion.choices[0].message.content or "").strip().strip('"«»')
    logger.info("catalog_photo_search.openai.finish model=%s query=%r", model, text)
    return text


def _recognize_anthropic(*, api_key, base_url, model, timeout_seconds, image_bytes, content_type):
    url = _anthropic_messages_url(base_url)
    request_payload = {
        "model": model,
        "max_tokens": MAX_RECOGNITION_TOKENS,
        "temperature": 0,
        "system": _RECOGNITION_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        },
                    }
                ],
            }
        ],
    }
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
    parts = response_payload.get("content") or []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text")
    text = text.strip().strip('"«»')
    logger.info("catalog_photo_search.anthropic.finish model=%s query=%r", model, text)
    return text


def _anthropic_messages_url(base_url):
    url = str(base_url or "https://api.anthropic.com/v1").rstrip("/")
    if url.endswith("/messages"):
        return url
    return f"{url}/messages"
