"""
Обработчики раздела «Мои компании».
"""

from api.backend import get_supplier_companies, get_supplier_detail
from keyboards.menu import main_menu_v2_kb
from models import UserContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from texts.companies import COMPANIES_LIST_TEXT, COMPANY_DETAIL_TEXT

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


async def handle_my_companies(context: UserContext) -> None:
    """
    Показать список компаний поставщика.
    Вызывается по кнопке «Мои компании» из главного меню.
    """
    companies = await get_supplier_companies(context.supplier_id)

    # BotUserBinding already proves that the user is linked to this supplier.
    # The service token cannot call the JWT-only /suppliers/mine endpoint, so
    # never turn an authorization error into the false "no companies" state.
    if not companies and context.supplier_id:
        companies = [{
            "id": context.supplier_id,
            "display_name": context.supplier_name or "Поставщик",
        }]

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
    company = await get_supplier_detail(company_id)

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

    context.supplier_id = company_id
    await handle_products_list(context)
