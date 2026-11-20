"""Bot identity scoped company API; never fall back to the global supplier list."""

import asyncio

import aiohttp

from config import AUTH_HEADERS, BACKEND_URL


class CompanyAPIError(Exception):
    def __init__(self, code="backend_unavailable", status=None):
        self.code = code
        self.status = status
        super().__init__(code)


def identity(context):
    return {"platform": "telegram", "telegram_user_id": context.telegram_user_id, "chat_id": context.chat_id}


def error_text(code):
    if code in ("BOT_ACCOUNT_NOT_LINKED", "BOT_USER_BINDING_NOT_FOUND"):
        return "Подключите бота заново из личного кабинета АвтоСнаб, чтобы работать с компаниями своего аккаунта."
    if code == "BOT_COMPANY_NOT_AVAILABLE":
        return "Эта компания больше недоступна. Откройте «Мои компании» и выберите доступную компанию."
    return "Не удалось выполнить действие с компанией. Попробуйте ещё раз немного позже."


async def _request(method, context, **payload):
    data = {**identity(context), **payload}
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
    kwargs = {"params": data} if method == "GET" else {"json": data}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, f"{BACKEND_URL}/bot-user-bindings/companies", headers=AUTH_HEADERS, **kwargs
            ) as response:
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    raise CompanyAPIError("invalid_backend_response")
                if response.status not in (200, 201):
                    raise CompanyAPIError(result.get("code", "company_request_failed"), response.status)
                return result
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise CompanyAPIError() from exc


async def list_companies(context):
    result = await _request("GET", context)
    companies = result.get("companies")
    if not isinstance(companies, list) or any(not isinstance(c, dict) or not c.get("id") for c in companies):
        raise CompanyAPIError("invalid_backend_response")
    return companies


async def create_company(company, context):
    body = {
        "display_name": company.get("short_name") or company.get("company_name") or company.get("legal_name", ""),
        "legal_name": company.get("legal_name") or company.get("company_name", ""),
        "inn": company["inn"],
        "legal_address": company.get("legal_address") or company.get("address", ""),
    }
    for key in ("short_name", "kpp", "ogrn", "entity_type", "okved", "okpo", "registration_date"):
        if company.get(key) is not None:
            body[key] = company[key]
    if company.get("management_name"):
        body["general_director"] = company["management_name"]
    try:
        result = await _request("POST", context, company=body)
        if not isinstance(result.get("company"), dict) or not result["company"].get("id"):
            raise CompanyAPIError("invalid_backend_response")
        return {"success": True, **result}
    except CompanyAPIError as exc:
        return {"success": False, "error": {"code": exc.code, "status": exc.status}}


async def select_company(context, supplier_id):
    result = await _request("PATCH", context, supplier_id=supplier_id)
    if not isinstance(result.get("binding"), dict) or str(result["binding"].get("supplier_id")) != str(supplier_id):
        raise CompanyAPIError("invalid_backend_response")
    return result
