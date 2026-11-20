"""Real supplier requests, scoped by bot user, chat and selected company."""

import asyncio
from uuid import UUID

import aiohttp

from config import AUTH_HEADERS, BACKEND_URL

PAGE_SIZE = 10
MAX_OFFSET = 1000000000


class OrderAPIError(Exception):
    def __init__(self, code="backend_unavailable"):
        self.code = code
        super().__init__(code)


def error_text(code):
    if code in {"BOT_USER_BINDING_NOT_FOUND", "BOT_ACCOUNT_NOT_LINKED"}:
        return "Подключите бота заново из личного кабинета АвтоСнаб."
    if code == "BOT_COMPANY_NOT_AVAILABLE":
        return "Компания больше недоступна или была переключена. Выберите компанию заново через /menu."
    if code == "not_found":
        return "Заявка не найдена или больше недоступна для выбранной компании."
    return "Не удалось загрузить заявки. Попробуйте ещё раз немного позже."


def _identity(context):
    if not context.supplier_id:
        raise OrderAPIError("BOT_COMPANY_NOT_AVAILABLE")
    return {
        "platform": "telegram",
        "telegram_user_id": str(context.telegram_user_id),
        "chat_id": str(context.chat_id),
        "supplier_id": str(context.supplier_id),
    }


async def _request(path, context, **params):
    identity = _identity(context)
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{BACKEND_URL}/bot-user-bindings/orders{path}",
                params={**identity, **params},
                headers=AUTH_HEADERS,
            ) as response:
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    raise OrderAPIError("invalid_backend_response")
                if response.status != 200:
                    code = result.get("code")
                    raise OrderAPIError(
                        code if isinstance(code, str) else "not_found" if response.status == 404 else "request_failed"
                    )
                return result
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise OrderAPIError() from exc


def _validate_row(row, context, *, details=False):
    if (
        not isinstance(row, dict)
        or not row.get("id")
        or not row.get("number")
        or str(row.get("supplier_id")) != str(context.supplier_id)
        or (details and not isinstance(row.get("items"), list))
    ):
        raise OrderAPIError("invalid_backend_response")


async def list_requests(context, offset=0):
    if not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
        raise OrderAPIError("invalid_page")
    data = await _request("", context, limit=PAGE_SIZE, offset=offset)
    if (
        not isinstance(data.get("results"), list)
        or not isinstance(data.get("count"), int)
        or data["count"] < 0
        or data.get("offset") != offset
        or data.get("limit") != PAGE_SIZE
        or len(data["results"]) > PAGE_SIZE
    ):
        raise OrderAPIError("invalid_backend_response")
    for row in data["results"]:
        _validate_row(row, context)
    return data


async def get_request(context, request_id):
    try:
        request_id = str(UUID(request_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise OrderAPIError("not_found") from exc
    data = await _request(f"/{request_id}", context)
    _validate_row(data, context, details=True)
    if str(data["id"]) != request_id:
        raise OrderAPIError("invalid_backend_response")
    return data
