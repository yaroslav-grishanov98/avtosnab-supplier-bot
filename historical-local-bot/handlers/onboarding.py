"""
Онбординг: регистрация, добавление компании.
"""

from fsm import clear_data, set_state
from keyboards.onboarding import after_company_added_kb
from models import UserContext, UserState
from texts.onboarding import COMPANY_ADDED_TEXT, UPLOAD_PRICE_OFFER_TEXT

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


# ═══════════════════════════════════════════════════════════════
# Callback от бэкенда: компания добавлена
# ═══════════════════════════════════════════════════════════════


async def notify_company_added(chat_id: str, company_name: str, inn: str) -> None:
    """
    Вызывается ИЗВНЕ (из API-роутера/вебхука), когда бэкенд подтверждает
    успешное добавление компании.
    """
    set_state(chat_id, UserState.MAIN_MENU)
    clear_data(chat_id)

    await msg.send_message(
        chat_id,
        COMPANY_ADDED_TEXT.format(company_name=company_name),
        reply_markup=after_company_added_kb(),
    )


# ═══════════════════════════════════════════════════════════════
# Обработка кнопок после добавления компании
# ═══════════════════════════════════════════════════════════════


async def handle_upload_price_offer(context: UserContext) -> None:
    """Пользователь нажал «Да, загрузить прайс»."""
    set_state(context.chat_id, UserState.PENDING_PRICE_UPLOAD)
    await msg.send_message(context.chat_id, UPLOAD_PRICE_OFFER_TEXT)


async def handle_upload_price_later(context: UserContext) -> None:
    """Пользователь нажал «Позже» — показываем главное меню."""
    from handlers.menu import show_main_menu_v2

    await show_main_menu_v2(context)
