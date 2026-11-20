"""
Промпты для GPT-структурирования прайс-листов.
"""

import json
import logging

import aiohttp

from config import AI_API_KEY, AI_BASE_URL, AI_MODEL

logger = logging.getLogger(__name__)

PRICE_LIST_SYSTEM_PROMPT = """Ты помощник для обработки прайс-листов.
Извлеки из текста товарные позиции и верни только JSON.

Схема ответа:
{
  "items": [
    {
      "name": "Название товара",
      "price": 100.00,
      "unit": "шт",
      "quantity": 1,
      "currency": "RUB"
    }
  ]
}

Правила:
- Цена только числом, разделитель точка
- unit_code один из: шт, кг, г, л, мл, уп, кор
- Если единица не указана — ставь "шт"
- Если количество не указано — ставь 1
- Не придумывай товары, которых нет в тексте
- Игнорируй заголовки, реквизиты компании, номера телефонов"""


async def structure_price_list(raw_text: str) -> list[dict]:
    """
    Преобразовать распознанный текст прайса в список товаров через GPT.
    """
    payload = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PRICE_LIST_SYSTEM_PROMPT},
            {"role": "user", "content": f"Текст прайса:\n{raw_text[:120000]}"},
        ],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {AI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                result = await resp.json()
                content = result["choices"][0]["message"]["content"]
                data = json.loads(content)
                return data.get("items", [])
    except Exception:
        logger.exception("Не удалось структурировать распознанный прайс через AI")
        return []
