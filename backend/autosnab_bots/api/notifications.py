"""Узкие API-вызовы бота уведомлений."""

import asyncio
import logging
from datetime import date
from decimal import Decimal
from uuid import UUID

import aiohttp

from config import AUTH_HEADERS, BACKEND_URL

logger = logging.getLogger(__name__)
CONNECT_TIMEOUT_SECONDS = 10
ACTION_TIMEOUT_SECONDS = 20


async def get_notification_status(*, chat_id: str, channel: str = "telegram") -> dict:
    """Получить из backend состояние подключения чата уведомлений."""
    if not BACKEND_URL:
        logger.error("Notification bot has no backend URL configured")
        return {"success": False, "code": "backend_unavailable"}
    timeout = aiohttp.ClientTimeout(total=CONNECT_TIMEOUT_SECONDS, connect=5, sock_read=8)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                f"{BACKEND_URL.rstrip('/')}/notifications/bot/status",
                params={"channel": channel, "chat_id": str(chat_id)},
                headers=AUTH_HEADERS,
            ) as response,
        ):
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            if response.status == 200 and isinstance(data, dict):
                return {"success": True, "data": data}
            if response.status == 403:
                return {"success": False, "code": "backend_auth_error"}
            if response.status >= 500:
                return {"success": False, "code": "backend_unavailable"}
            return {"success": False, "code": "backend_request_error"}
    except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError):
        logger.warning("Notification status request failed")
        return {"success": False, "code": "backend_unavailable"}


async def test_notification_delivery(*, chat_id: str, channel: str = "telegram") -> dict:
    """Проверить в backend, что подключённый чат готов к тестовой доставке."""
    if not BACKEND_URL:
        return {"success": False, "code": "backend_unavailable"}
    timeout = aiohttp.ClientTimeout(total=CONNECT_TIMEOUT_SECONDS, connect=5, sock_read=8)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                f"{BACKEND_URL.rstrip('/')}/notifications/bot/test",
                json={"channel": channel, "chat_id": str(chat_id)},
                headers=AUTH_HEADERS,
            ) as response,
        ):
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            if response.status == 200 and isinstance(data, dict) and data.get("ok"):
                return {"success": True, "data": data}
            if response.status == 404:
                return {"success": False, "code": "not_connected"}
            if response.status == 409:
                return {"success": False, "code": "disabled"}
            if response.status >= 500:
                return {"success": False, "code": "backend_unavailable"}
            return {"success": False, "code": "backend_request_error"}
    except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError):
        return {"success": False, "code": "backend_unavailable"}


async def connect_notification_chat(
    *,
    chat_id: str,
    token: str,
    destination_type: str,
    title: str = "",
    channel: str = "telegram",
    user_id: str = "",
    username: str = "",
    display_name: str = "",
) -> dict:
    """Погасить одноразовый токен и подключить полученный чат в Telegram/MAX."""
    if not BACKEND_URL:
        logger.error("Notification bot has no backend URL configured")
        return {"success": False, "code": "backend_unavailable"}
    timeout = aiohttp.ClientTimeout(
        total=CONNECT_TIMEOUT_SECONDS,
        connect=5,
        sock_read=8,
    )
    body = {
        "channel": channel,
        "destination_type": destination_type,
        "chat_id": str(chat_id),
        "token": token,
        "title": title[:255],
    }
    if user_id:
        body["user_id"] = str(user_id)
    if username:
        body["username"] = str(username)[:255]
    if display_name:
        body["display_name"] = str(display_name)[:255]
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                f"{BACKEND_URL.rstrip('/')}/notifications/bot/connect",
                json=body,
                headers=AUTH_HEADERS,
            ) as response,
        ):
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            if response.status in (200, 201):
                if isinstance(data, dict) and data.get("id"):
                    result = {"success": True}
                    if response.status == 200 or data.get("reused"):
                        result["reused"] = True
                    return result
                logger.error("Notification chat connect returned an unexpected response")
                return {"success": False, "code": "invalid_backend_response"}
            if response.status == 400:
                code = "link_invalid"
                if isinstance(data, dict):
                    code = data.get("code", code)
                    if code == "link_invalid":
                        if "destination_type" in data:
                            code = "wrong_chat_type"
                        elif "chat_id" in data:
                            detail = str(data["chat_id"])
                            code = "already_connected" if "подключ" in detail.lower() else "invalid_chat_id"
                return {"success": False, "code": code}
            if response.status == 403:
                logger.error("Notification chat connect authorization failed: HTTP 403")
                return {"success": False, "code": "backend_auth_error"}
            if response.status >= 500:
                logger.warning(
                    "Notification chat connect backend unavailable: HTTP %s",
                    response.status,
                )
                return {"success": False, "code": "backend_unavailable"}
            logger.warning("Notification chat connect failed: HTTP %s", response.status)
            return {"success": False, "code": "backend_request_error"}
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Notification chat connect timed out")
        return {"success": False, "code": "backend_unavailable"}
    except aiohttp.ClientError as exc:
        logger.warning("Notification chat connect connection error: %s", type(exc).__name__)
        return {"success": False, "code": "backend_unavailable"}


async def confirm_delivery_receipt(
    *,
    chat_id: str,
    delivery_date: date,
    supplier_request_id: str | None = None,
    result: str = "complete",
    comment: str | None = None,
    telegram_user_id: str | None = None,
    telegram_user_name: str | None = None,
) -> dict:
    """Передать подтверждение поставки из чек-листа Telegram в backend."""
    if not BACKEND_URL:
        logger.error("Notification bot has no backend URL configured")
        return {"success": False, "code": "backend_unavailable"}

    body = {
        "channel": "telegram",
        "chat_id": str(chat_id),
        "delivery_date": delivery_date.isoformat(),
        "result": result,
        "telegram_user_id": str(telegram_user_id or ""),
        "telegram_user_name": str(telegram_user_name or "")[:255],
    }
    if supplier_request_id:
        body["supplier_request_id"] = str(supplier_request_id)
    if comment:
        body["comment"] = comment[:1000]
    timeout = aiohttp.ClientTimeout(total=ACTION_TIMEOUT_SECONDS, connect=5, sock_read=15)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                f"{BACKEND_URL.rstrip('/')}/notifications/bot/deliveries/confirm",
                json=body,
                headers=AUTH_HEADERS,
            ) as response,
        ):
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            if response.status == 200 and isinstance(data, dict) and data.get("ok"):
                return {"success": True, **data}
            if response.status == 403:
                return {"success": False, "code": "chat_not_linked"}
            if response.status == 404:
                return {"success": False, "code": "no_delivery"}
            if response.status == 400:
                return {"success": False, "code": "invalid_receipt_data"}
            logger.warning("Delivery receipt backend returned HTTP %s", response.status)
            return {"success": False, "code": "backend_unavailable"}
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Delivery receipt backend request timed out")
        return {"success": False, "code": "backend_unavailable"}
    except aiohttp.ClientError as exc:
        logger.warning("Delivery receipt backend connection error: %s", type(exc).__name__)
        return {"success": False, "code": "backend_unavailable"}


async def get_delivery_review(
    *, chat_id: str, delivery_date: date, scope: str = "all", channel: str = "telegram"
) -> dict:
    """Загрузить заявки для последовательного просмотра перед подтверждением прихода."""
    if not BACKEND_URL:
        logger.error("Notification bot has no backend URL configured")
        return {"success": False, "code": "backend_unavailable"}
    timeout = aiohttp.ClientTimeout(total=ACTION_TIMEOUT_SECONDS, connect=5, sock_read=15)
    params = {
        "channel": channel,
        "chat_id": str(chat_id),
        "delivery_date": delivery_date.isoformat(),
        "scope": str(scope),
    }
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                f"{BACKEND_URL.rstrip('/')}/notifications/bot/deliveries/review",
                params=params,
                headers=AUTH_HEADERS,
            ) as response,
        ):
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            if response.status == 200 and isinstance(data, dict):
                return {"success": True, "data": data}
            if response.status == 403:
                return {"success": False, "code": "chat_not_linked"}
            if response.status == 404:
                return {"success": False, "code": "no_delivery"}
            if response.status == 400:
                return {"success": False, "code": "invalid_receipt_data"}
            logger.warning("Delivery review backend returned HTTP %s", response.status)
            return {"success": False, "code": "backend_unavailable"}
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Delivery review backend request timed out")
        return {"success": False, "code": "backend_unavailable"}
    except aiohttp.ClientError as exc:
        logger.warning("Delivery review backend connection error: %s", type(exc).__name__)
        return {"success": False, "code": "backend_unavailable"}


async def respond_to_supplier_request(
    *,
    request_id: str,
    chat_id: str,
    action: str,
    confirmed_delivery_date: date | None = None,
    delivery_date_unknown: bool = False,
    item_quantities: dict[str, Decimal] | None = None,
    comment: str | None = None,
    responded_by: str | None = None,
) -> dict:
    """Передать в backend ответ поставщика, пришедший из inline-кнопки."""
    if not BACKEND_URL:
        logger.error("Notification bot has no backend URL configured")
        return {"success": False, "code": "backend_unavailable"}

    try:
        UUID(request_id)
    except (TypeError, ValueError, AttributeError):
        return {"success": False, "code": "invalid_request"}
    if action not in {"accept", "partial", "decline"}:
        return {"success": False, "code": "invalid_action"}
    if action in {"accept", "partial"} and confirmed_delivery_date is None and not delivery_date_unknown:
        return {"success": False, "code": "delivery_date_required"}

    body = {"channel": "telegram", "chat_id": str(chat_id), "action": action}
    if confirmed_delivery_date is not None:
        body["confirmed_delivery_date"] = confirmed_delivery_date.isoformat()
    if delivery_date_unknown:
        body["delivery_date_unknown"] = True
    if item_quantities is not None:
        body["item_quantities"] = {str(item_id): str(quantity) for item_id, quantity in item_quantities.items()}
    if comment:
        body["comment"] = comment[:1000]
    if responded_by:
        body["responded_by"] = responded_by[:255]

    timeout = aiohttp.ClientTimeout(total=ACTION_TIMEOUT_SECONDS, connect=5, sock_read=15)
    url = f"{BACKEND_URL.rstrip('/')}/notifications/bot/supplier-requests/{request_id}/respond"
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(url, json=body, headers=AUTH_HEADERS) as response,
        ):
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            if response.status == 200 and isinstance(data, dict) and data.get("ok"):
                result = {"success": True}
                if isinstance(data.get("status"), str):
                    result["status"] = data["status"]
                return result
            if response.status == 409:
                return {"success": False, "code": "already_answered"}
            if response.status == 403:
                logger.error("Supplier action authorization rejected for a linked chat")
                return {"success": False, "code": "chat_not_linked"}
            if response.status == 400:
                return {"success": False, "code": "invalid_action_data"}
            if response.status == 404:
                return {"success": False, "code": "request_not_found"}
            logger.warning("Supplier action backend returned HTTP %s", response.status)
            return {"success": False, "code": "backend_unavailable"}
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Supplier action backend request timed out")
        return {"success": False, "code": "backend_unavailable"}
    except aiohttp.ClientError as exc:
        logger.warning("Supplier action backend connection error: %s", type(exc).__name__)
        return {"success": False, "code": "backend_unavailable"}


async def get_supplier_request_for_chat(*, request_id: str, chat_id: str) -> dict:
    """Получить позиции заявки только для подключённого чата поставщика."""
    if not BACKEND_URL:
        return {"success": False, "code": "backend_unavailable"}
    try:
        UUID(request_id)
    except (TypeError, ValueError, AttributeError):
        return {"success": False, "code": "invalid_request"}

    timeout = aiohttp.ClientTimeout(total=ACTION_TIMEOUT_SECONDS, connect=5, sock_read=15)
    url = f"{BACKEND_URL.rstrip('/')}/notifications/bot/supplier-requests/{request_id}"
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                url,
                params={"channel": "telegram", "chat_id": str(chat_id)},
                headers=AUTH_HEADERS,
            ) as response,
        ):
            if response.status != 200:
                return {"success": False, "code": "request_not_available"}
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                return {"success": False, "code": "invalid_backend_response"}
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return {"success": False, "code": "invalid_backend_response"}
            return {"success": True, "data": data}
    except (asyncio.TimeoutError, TimeoutError):
        return {"success": False, "code": "backend_unavailable"}
    except aiohttp.ClientError as exc:
        logger.warning("Supplier request lookup connection error: %s", type(exc).__name__)
        return {"success": False, "code": "backend_unavailable"}
