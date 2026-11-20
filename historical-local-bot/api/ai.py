import asyncio
import json
from urllib.parse import urlsplit

import aiohttp
from utils import langfuse_track

from config import AI_API_KEY, AI_BASE_URL, AI_MODEL, AI_PROVIDER, AI_TRANSCRIPTION_MODEL

SYSTEM_PROMPT = """Ты нормализатор прайсов HoReCa для AutoSnab. Верни только JSON без markdown.

Схема:
{
  "schema_version": "supplier_price_ai.v1",
  "processing_version": "n8n-demo-2026-06-18",
  "supplier_name": "...",
  "products": [
    {
      "row_number": 1,
      "raw_name": "...",
      "normalized_name": "...",
      "supplier_sku": "...",
      "price": "185.50",
      "currency": "RUB",
      "unit_code": "шт",
      "package_info": "400 мл ж/б",
      "brand": "Aroy-D",
      "volume": "400",
      "volume_unit": "мл",
      "min_order_qty": "1",
      "multiplicity": "1",
      "category_path": ["..."],
      "comment": "...",
      "availability_status": "available",
      "confidence": 0.94,
      "warnings": []
    }
  ]
}

Правила:
- Обработай только строки из текущего чанка. Не добавляй строки, которых нет во входе.
- row_number верни значение row_number из входа.
- unit_code один из: шт, кг, г, л, мл. Если единица неясна — unit_code пустой, confidence ниже 0.8, warning unit_unknown.
- Цена только числом в RUB, разделитель точка.
- Не выдумывай supplier_sku, если артикула нет.
- raw_name сохраняет исходное название.
- normalized_name без цены."""


def _transport_metadata(model: str) -> dict:
    """Вернуть безопасную диагностику AI endpoint без ключа и текста пользователя."""
    return {
        "provider": AI_PROVIDER,
        "model": model,
        "endpoint_host": urlsplit(AI_BASE_URL).hostname or "unknown",
    }


async def normalize_chunk(supplier_name: str, upload_id: str, chunk: dict) -> dict:
    if not AI_API_KEY or AI_API_KEY == "mock":
        await langfuse_track(
            "ai_normalize_error",
            {"error_type": "credentials_not_configured", **_transport_metadata(AI_MODEL)},
            level="ERROR",
        )
        raise RuntimeError("AI provider credentials are not configured")
    user_prompt = (
        f"Поставщик: {supplier_name}\n"
        f"Upload ID: {upload_id}\n"
        f"Источник: {chunk.get('source_channel', 'telegram')}\n"
        f"Чанк: {chunk.get('chunk_index', 1)}/{chunk.get('chunk_total', 1)}\n"
        f"Строки: {chunk.get('chunk_start_row', '')}-{chunk.get('chunk_end_row', '')}\n\n"
        f"Данные прайса:\n{chunk['ai_input_text']}"
    )

    payload = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    await langfuse_track(
        "ai_normalize_start",
        {
            "supplier": supplier_name,
            "upload_id": upload_id,
            "chunk_index": chunk.get("chunk_index"),
            "rows_count": len(user_prompt),
        },
    )

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{AI_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {AI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=110),
                ) as resp:
                    if resp.status >= 400:
                        resp.raise_for_status()
                    result = await resp.json()
                    content = result["choices"][0]["message"]["content"]
                    parsed = json.loads(content)

                    await langfuse_track(
                        "ai_normalize_done",
                        {
                            "attempt": attempt + 1,
                            "products_count": len(parsed.get("products", [])),
                            "tokens_used": result.get("usage", {}).get("total_tokens", 0),
                            **_transport_metadata(AI_MODEL),
                        },
                    )

                    return parsed
        except Exception as exc:
            await langfuse_track(
                "ai_normalize_error",
                {
                    "attempt": attempt + 1,
                    "error_type": type(exc).__name__,
                    "status": getattr(exc, "status", None),
                    **_transport_metadata(AI_MODEL),
                },
                level="ERROR",
            )
            if attempt == 2:
                raise
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════
# Speech-to-text (голосовой ввод)
# ═══════════════════════════════════════════════════════════════


async def speech_to_text(audio_bytes: bytes) -> str:
    """Распознать речь через настроенный OpenAI-совместимый API."""
    if not AI_API_KEY or AI_API_KEY == "mock":
        await langfuse_track(
            "speech_to_text_error",
            {
                "error_type": "credentials_not_configured",
                **_transport_metadata(AI_TRANSCRIPTION_MODEL),
            },
            level="ERROR",
        )
        return ""
    url = f"{AI_BASE_URL}/audio/transcriptions"
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename="voice.ogg", content_type="audio/ogg")
    form.add_field("model", AI_TRANSCRIPTION_MODEL)
    form.add_field("language", "ru")

    headers = {"Authorization": f"Bearer {AI_API_KEY}"}

    await langfuse_track("speech_to_text_start", {"audio_size": len(audio_bytes)})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data.get("text", "")

                    await langfuse_track(
                        "speech_to_text_done",
                        {
                            "text_length": len(text),
                            "text": text[:100],
                        },
                    )

                    return text

                await langfuse_track(
                    "speech_to_text_error",
                    {"status": resp.status, **_transport_metadata(AI_TRANSCRIPTION_MODEL)},
                    level="ERROR",
                )
                return ""
    except Exception as exc:
        await langfuse_track(
            "speech_to_text_error",
            {
                "error_type": type(exc).__name__,
                **_transport_metadata(AI_TRANSCRIPTION_MODEL),
            },
            level="ERROR",
        )
        return ""
