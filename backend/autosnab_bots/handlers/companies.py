"""
Обработчики раздела «Мои компании».
"""

from api.backend import get_supplier_companies
from api.company_accounts import CompanyAPIError, error_text, select_company
from fsm import StateManager, get_data, set_data, set_state
from keyboards.menu import main_menu_v2_kb
from models import UserContext, UserState
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from texts.companies import COMPANIES_LIST_TEXT, COMPANY_DETAIL_TEXT

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


async def activate_company(context, company_id):
    """Update local selection only after the backend accepts the company."""
    try:
        result = await select_company(context, company_id)
    except CompanyAPIError as exc:
        await msg.send_message(context.chat_id, error_text(exc.code), reply_markup=main_menu_v2_kb())
        return False
    context.supplier_id = str(company_id)
    context.selected_company_id = str(company_id)
    context.binding = result.get("binding")
    context.supplier_name = (result.get("company") or {}).get("display_name")
    set_data(context.chat_id, "selected_company_id", str(company_id))
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        UserState.MAIN_MENU,
        {"selected_company_id": str(company_id), "companies": get_data(context.chat_id).get("companies", [])},
    )
    set_state(context.chat_id, UserState.MAIN_MENU)
    return True


async def _account_companies(context):
    try:
        companies = await get_supplier_companies(context)
    except CompanyAPIError as exc:
        await msg.send_message(context.chat_id, error_text(exc.code), reply_markup=main_menu_v2_kb())
        return None
    set_data(context.chat_id, "companies", companies)
    return companies


async def handle_my_companies(context: UserContext) -> None:
    """
    Показать список компаний поставщика.
    Вызывается по кнопке «Мои компании» из главного меню.
    """
    companies = await _account_companies(context)
    if companies is None:
        return

    if not companies:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Добавить компанию по ИНН", callback_data="add_company_by_inn")],
                [InlineKeyboardButton("📝 Заполнить вручную", callback_data="fill_manually")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ]
        )
        await msg.send_message(
            context.chat_id,
            "У вас пока нет добавленных компаний. Хотите добавить?",
            reply_markup=keyboard,
        )
        return

    keyboard = []
    for company in companies[:10]:
        company_name = company.get("display_name") or company.get("legal_name", "Без названия")
        company_id = company.get("id", "")
        keyboard.append([InlineKeyboardButton(f"🏢 {company_name}", callback_data=f"company_detail_{company_id}")])
    keyboard.append(
        [
            InlineKeyboardButton("➕ Добавить компанию", callback_data="add_company_by_inn"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
        ]
    )

    await msg.send_message(
        context.chat_id,
        COMPANIES_LIST_TEXT.format(count=len(companies)),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_company_detail(context: UserContext, company_id: str) -> None:
    """
    Показать карточку компании.
    """
    companies = await _account_companies(context)
    if companies is None:
        return
    company = next((c for c in companies if str(c.get("id")) == str(company_id)), None)

    if not company:
        await msg.send_message(
            context.chat_id,
            "Компания не найдена.",
            reply_markup=main_menu_v2_kb(),
        )
        return

    detail_text = COMPANY_DETAIL_TEXT.format(
        name=company.get("display_name") or company.get("legal_name", "—"),
        inn=company.get("inn", "—"),
        legal_name=company.get("legal_name", "—"),
        phone=company.get("phone", "—"),
        email=company.get("email", "—"),
        status=company.get("status", "—"),
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Работать с компанией", callback_data=f"select_company_{company_id}")],
            [InlineKeyboardButton("📋 Товары компании", callback_data=f"company_products_{company_id}")],
            [InlineKeyboardButton("◀️ К списку компаний", callback_data="my_companies")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )

    await msg.send_message(
        context.chat_id,
        detail_text,
        reply_markup=keyboard,
    )


async def handle_company_products(context: UserContext, company_id: str) -> None:
    """
    Показать товары конкретной компании.
    """
    from handlers.products import handle_products_list

    if not await activate_company(context, company_id):
        return
    await handle_products_list(context)
