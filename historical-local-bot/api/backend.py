"""
API-клиент для бэкенда AutoSnab.
"""

import asyncio
import json
import logging

import aiohttp

from config import (
    AUTH_HEADERS,
    BACKEND_URL,
    DEMO_AUTH_MODE,
    DEMO_SUPPLIER_ID,
    DEMO_SUPPLIER_NAME,
    MOCK_MODE,
    MOCK_USER_BOUND,
)

logger = logging.getLogger(__name__)
BINDING_LOOKUP_TIMEOUT_SECONDS = 10

# ═══════════════════════════════════════════════════════════════
# Bot User Bindings
# ═══════════════════════════════════════════════════════════════


async def resolve_supplier_binding(platform: str, telegram_user_id: str, chat_id: str) -> dict:
    """Проверить, привязан ли Telegram-пользователь к поставщику."""
    if DEMO_AUTH_MODE and DEMO_SUPPLIER_ID:
        return {
            "exists": True,
            "binding": {
                "supplier_id": DEMO_SUPPLIER_ID,
                "supplier_name": DEMO_SUPPLIER_NAME,
            },
        }
    if MOCK_MODE:
        if MOCK_USER_BOUND:
            return {
                "exists": True,
                "binding": {
                    "supplier_id": "mock123",
                    "supplier_name": "Тестовый поставщик",
                },
            }
        return {"exists": False}

    url = f"{BACKEND_URL}/bot-user-bindings/resolve"
    params = {"platform": platform, "telegram_user_id": telegram_user_id, "chat_id": chat_id}
    timeout = aiohttp.ClientTimeout(
        total=BINDING_LOOKUP_TIMEOUT_SECONDS,
        connect=min(5, BINDING_LOOKUP_TIMEOUT_SECONDS),
        sock_read=min(8, BINDING_LOOKUP_TIMEOUT_SECONDS),
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        logger.error("Binding lookup returned invalid JSON")
                        return {
                            "exists": False,
                            "error": {"code": "invalid_backend_response", "status": 200},
                        }
                    return data if isinstance(data, dict) else {
                        "exists": False,
                        "error": {"code": "invalid_backend_response", "status": 200},
                    }

                if resp.status == 404:
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        data = None
                    if (
                        isinstance(data, dict)
                        and data.get("exists") is False
                        and data.get("code") == "BOT_USER_BINDING_NOT_FOUND"
                    ):
                        return {"exists": False}
                    logger.warning("Binding lookup unexpected not-found response")
                    return {
                        "exists": False,
                        "error": {"code": "backend_request_error", "status": 404},
                    }

                if resp.status in (401, 403):
                    logger.error("Binding lookup authorization failed: HTTP %s", resp.status)
                    return {
                        "exists": False,
                        "error": {"code": "backend_auth_error", "status": resp.status},
                    }

                if resp.status >= 500:
                    logger.warning("Binding lookup backend unavailable: HTTP %s", resp.status)
                    return {
                        "exists": False,
                        "error": {"code": "backend_unavailable", "status": resp.status},
                    }

                logger.warning("Binding lookup unexpected response: HTTP %s", resp.status)
                return {
                    "exists": False,
                    "error": {"code": "backend_request_error", "status": resp.status},
                }
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Binding lookup timed out")
        return {"exists": False, "error": {"code": "backend_timeout"}}
    except aiohttp.ClientError as exc:
        logger.warning("Binding lookup connection error: %s", type(exc).__name__)
        return {"exists": False, "error": {"code": "backend_unavailable"}}


async def register_supplier_binding(
    platform: str,
    telegram_user_id: str,
    chat_id: str,
    username: str,
    first_name: str,
    last_name: str,
    link_token: str,
) -> dict:
    """Привязать Telegram-пользователя к поставщику по link_token."""
    if MOCK_MODE:
        return {"success": True, "binding": {"supplier_id": "mock123", "supplier_name": "Тестовый поставщик"}}

    url = f"{BACKEND_URL}/bot-user-bindings/register"
    body = {
        "platform": platform,
        "telegram_user_id": telegram_user_id,
        "chat_id": chat_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "link_token": link_token,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=AUTH_HEADERS) as resp:
            return await resp.json()


# ═══════════════════════════════════════════════════════════════
# Pending Actions
# ═══════════════════════════════════════════════════════════════


async def get_pending_upload(chat_id: str) -> dict | None:
    if MOCK_MODE:
        return None
    url = f"{BACKEND_URL}/bot-user-bindings/pending-actions"
    params = {
        "platform": "telegram",
        "chat_id": chat_id,
        "action_type": "upload_price",
        "status": "ready_for_processing,pending_registration,waiting_registration",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
            data = await resp.json()
            return data[0] if data else None


async def save_pending_upload(user_data: dict, file_info: dict) -> dict:
    if MOCK_MODE:
        return {"success": True}
    url = f"{BACKEND_URL}/bot-user-bindings/pending-actions"
    body = {
        "platform": "telegram",
        "telegram_user_id": user_data["telegram_user_id"],
        "chat_id": user_data["chat_id"],
        "action_type": "upload_price",
        "file_id": file_info["file_id"],
        "file_name": file_info["file_name"],
        "message_id": file_info["message_id"],
        "mime_type": file_info["mime_type"],
        "status": "pending_registration",
        "raw_payload": json.dumps({"source": "telegram", "received_at": file_info.get("received_at", "")}),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=AUTH_HEADERS) as resp:
            return await resp.json()


async def mark_pending_upload_status(pending_action_id: str, status: str, supplier_id: str, payload: dict) -> dict:
    if MOCK_MODE:
        return {"status": "updated"}
    url = f"{BACKEND_URL}/bot-user-bindings/pending-actions/{pending_action_id}"
    body = {"status": status, "supplier_id": supplier_id, "raw_payload": json.dumps(payload)}
    async with aiohttp.ClientSession() as session:
        async with session.patch(url, json=body, headers=AUTH_HEADERS) as resp:
            return await resp.json()


# ═══════════════════════════════════════════════════════════════
# Uploads
# ═══════════════════════════════════════════════════════════════


async def create_upload_file(
    file_bytes: bytes,
    file_name: str,
    supplier_id: str,
    telegram_message_id: str,
    mime_type: str,
    execution_id: str = "",
) -> dict:
    if MOCK_MODE:
        return {"id": "mock-upload-123", "status": "uploaded"}
    url = f"{BACKEND_URL}/suppliers/uploads"
    form = aiohttp.FormData()
    form.add_field("supplier_id", str(supplier_id))
    form.add_field("source_channel", "telegram")
    form.add_field("source_file_name", file_name)
    form.add_field("telegram_message_id", str(telegram_message_id))
    form.add_field("n8n_execution_id", execution_id)
    form.add_field("source_file", file_bytes, filename=file_name, content_type=mime_type or "application/octet-stream")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, headers=AUTH_HEADERS) as resp:
            return await resp.json()


async def normalize_upload_in_backend(upload_id: str) -> dict:
    if MOCK_MODE:
        return {"status": "applied"}
    url = f"{BACKEND_URL}/suppliers/uploads/{upload_id}/normalize"
    body = {"use_ai": True, "ai_mode": "ambiguous", "run_async": True, "force": True}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=AUTH_HEADERS, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            return await resp.json()


async def apply_ai_result(upload_id: str, ai_payload: dict) -> dict:
    if MOCK_MODE:
        return {"status": "applied"}
    url = f"{BACKEND_URL}/suppliers/uploads/{upload_id}/apply-ai-result"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=ai_payload, headers=AUTH_HEADERS) as resp:
            return await resp.json()


async def get_upload_stats(upload_id: str) -> dict:
    if MOCK_MODE:
        return {
            "items_total": 0,
            "products_created": 0,
            "products_updated": 0,
            "prices_created": 0,
            "needs_review": 0,
            "items_matched": 0,
        }
    url = f"{BACKEND_URL}/suppliers/uploads/{upload_id}"
    timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_read=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=AUTH_HEADERS) as resp:
            if resp.status != 200:
                detail = await resp.text()
                raise RuntimeError(f"upload status HTTP {resp.status}: {detail[:200]}")
            return await resp.json()


async def reject_upload(upload_id: str) -> dict:
    if MOCK_MODE:
        return {"status": "rejected"}
    url = f"{BACKEND_URL}/suppliers/uploads/{upload_id}/reject"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=AUTH_HEADERS) as resp:
            return await resp.json()


# ═══════════════════════════════════════════════════════════════
# Upload History
# ═══════════════════════════════════════════════════════════════


async def get_upload_history(supplier_id: str, limit: int = 10) -> list:
    """Получить историю загрузок прайсов."""
    if MOCK_MODE:
        return [{"id": "mock-1", "source_file_name": "price.xlsx", "status": "applied"}]
    url = f"{BACKEND_URL}/suppliers/uploads"
    params = {"supplier_id": supplier_id, "limit": limit}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
            data = await resp.json()
            return data.get("results", [])


# ═══════════════════════════════════════════════════════════════
# Products
# ═══════════════════════════════════════════════════════════════


async def get_supplier_products(
    supplier_id: str, status: str = "active", limit: int = 20, query: str | None = None
) -> dict:
    if MOCK_MODE:
        return {"results": []}
    url = f"{BACKEND_URL}/suppliers/products"
    params = {"supplier_id": supplier_id, "status": status, "limit": limit}
    if query:
        params["q"] = query
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
            resp.raise_for_status()
            return await resp.json()


async def patch_product_price(product_id: str, price: str) -> dict:
    if MOCK_MODE:
        return {"status": "updated"}
    url = f"{BACKEND_URL}/suppliers/products/{product_id}"
    body = {"price": price, "comment": "обновлено через Telegram бот"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.patch(url, json=body, headers=AUTH_HEADERS) as resp:
            resp.raise_for_status()
            return await resp.json()


async def delete_supplier_product(product_id: str) -> dict:
    """Удалить товар через общий backend, чтобы изменение сразу увидел веб."""
    if MOCK_MODE:
        return {"deleted": True}
    url = f"{BACKEND_URL}/suppliers/products/{product_id}"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(url, headers=AUTH_HEADERS) as resp:
            resp.raise_for_status()
            if resp.status == 204:
                return {"deleted": True}
            data = await resp.json()
            return {"deleted": True, **data}


# ═══════════════════════════════════════════════════════════════
# Cabinet
# ═══════════════════════════════════════════════════════════════


async def generate_cabinet_token(platform: str, chat_id: str) -> dict:
    if MOCK_MODE:
        return {
            "token": "mock-token-abc123",
            "expires_at": "2026-07-25T12:00:00Z",
            "supplier_id": "mock123",
            "supplier_name": "Тестовый поставщик",
        }
    url = f"{BACKEND_URL}/suppliers/cabinet/token"
    body = {"platform": platform, "chat_id": chat_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=AUTH_HEADERS) as resp:
            return await resp.json()


# ═══════════════════════════════════════════════════════════════
# INN Lookup
# ═══════════════════════════════════════════════════════════════


async def lookup_inn(inn: str) -> dict:
    """Проверить компанию по ИНН через реальный API бэкенда."""
    if MOCK_MODE:
        return {
            "found": True,
            "company_name": "ООО «Ресторан Прага»",
            "inn": inn,
            "address": "г. Москва, ул. Тверская, 1",
        }

    url = f"{BACKEND_URL}/suppliers/inn-lookup"
    params = {"inn": inn}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
            if resp.status == 404:
                return {"found": False}
            data = await resp.json()
            print("DEBUG INN LOOKUP:", data)  # Временная строка для отладки
            return {
                "found": True,
                "company_name": data.get("name") or data.get("company_name", ""),
                "inn": inn,
                "address": data.get("address", {}).get("unrestricted") or data.get("address", ""),
            }


# ═══════════════════════════════════════════════════════════════
# Companies
# ═══════════════════════════════════════════════════════════════


async def get_supplier_companies(supplier_id: str | None = None) -> list:
    """Получить список компаний поставщика."""
    if DEMO_AUTH_MODE and DEMO_SUPPLIER_ID:
        return [{
            "id": DEMO_SUPPLIER_ID,
            "display_name": DEMO_SUPPLIER_NAME,
            "legal_name": DEMO_SUPPLIER_NAME,
        }]
    if MOCK_MODE:
        return [{"id": "mock-1", "display_name": "Тестовая компания", "inn": "7701234567"}]
    url = f"{BACKEND_URL}/suppliers"
    params = {}
    if supplier_id:
        params["supplier_id"] = supplier_id
    timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
                if resp.status != 200:
                    logger.warning("Companies lookup failed: HTTP %s", resp.status)
                    return []
                data = await resp.json()
                if isinstance(data, list):
                    return data
                if not isinstance(data, dict):
                    return []
                results = data.get("results", [])
                return results if isinstance(results, list) else []
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("Companies lookup connection error: %s", type(exc).__name__)
        return []


async def get_supplier_detail(company_id: str) -> dict | None:
    """Получить детальную информацию о компании."""
    if DEMO_AUTH_MODE and DEMO_SUPPLIER_ID and company_id == DEMO_SUPPLIER_ID:
        return {
            "id": DEMO_SUPPLIER_ID,
            "display_name": DEMO_SUPPLIER_NAME,
            "legal_name": DEMO_SUPPLIER_NAME,
            "status": "active",
        }
    if MOCK_MODE:
        return {
            "id": company_id,
            "display_name": "Тестовая компания",
            "inn": "7701234567",
            "legal_name": "ООО Тест",
            "phone": "+7 999 123-45-67",
            "email": "test@example.com",
            "status": "active",
        }
    url = f"{BACKEND_URL}/suppliers/{company_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=AUTH_HEADERS) as resp:
            if resp.status == 404:
                return None
            return await resp.json()

async def create_text_price_upload(
    platform: str,
    telegram_user_id: str,
    chat_id: str,
    message_id: str,
    text: str,
) -> dict:
    """Передать одну позицию или многострочный прайс в общий pipeline."""
    if MOCK_MODE:
        return {
            "upload": {"id": "mock-text-upload-123", "status": "processing"},
            "normalization": {"accepted": True},
        }

    url = f"{BACKEND_URL}/bot-user-bindings/uploads/text"
    body = {
        "platform": platform,
        "telegram_user_id": telegram_user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "use_ai": True,
        "run_async": True,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=AUTH_HEADERS) as resp:
            return await resp.json()


# ═══════════════════════════════════════════════════════════════
# Products (товары поставщика)
# ═══════════════════════════════════════════════════════════════

async def search_catalog(query: str, limit: int = 10) -> dict:
    """Гибридный поиск по общему каталогу всех поставщиков."""
    if MOCK_MODE:
        return {"count": 0, "results": [], "search_id": "mock-search-id"}

    url = f"{BACKEND_URL}/suppliers/catalog-search"
    params = {"q": query, "limit": limit, "group_offers": "true"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
            return await resp.json()

async def record_catalog_search_feedback(search_id: str, event_type: str, comment: str = "") -> dict:
    """Связать короткий пользовательский feedback с конкретной выдачей."""
    if MOCK_MODE:
        return {"success": True}

    url = f"{BACKEND_URL}/suppliers/catalog-search/events"
    body = {"search_id": search_id, "event_type": event_type, "comment": comment}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=AUTH_HEADERS) as resp:
            return await resp.json()
