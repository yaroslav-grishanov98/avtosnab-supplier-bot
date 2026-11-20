"""Зарегистрировать вебхук MAX-бота уведомлений.

Запускайте после появления публичного HTTPS-адреса:
  python register_max_webhook.py
"""

import asyncio
import os

import aiohttp


async def main():
    """Создать подписку MAX на события бота."""
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    webhook_url = os.getenv("MAX_PUBLIC_WEBHOOK_URL", "").strip()
    secret = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
    if not token or not webhook_url:
        raise SystemExit("MAX_BOT_TOKEN and MAX_PUBLIC_WEBHOOK_URL are required")
    body = {"url": webhook_url, "update_types": ["bot_started", "message_created", "message_callback"]}
    if secret:
        body["secret"] = secret
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{os.getenv('MAX_BOT_API_BASE', 'https://platform-api2.max.ru').rstrip('/')}/subscriptions",
            json=body,
            headers={"Authorization": token},
        ) as response:
            print(response.status, await response.text())
            response.raise_for_status()


if __name__ == "__main__":
    asyncio.run(main())
