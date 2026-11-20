"""
Обработчики главного меню, приветствий и личного кабинета.
"""

from fsm import set_state
from keyboards import (
    greeting_kb,
    help_kb,
    main_menu_kb,
    main_menu_v2_kb,
    welcome_kb,
)
from models import UserContext, UserState
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from texts import (
    GREETING_TEXT,
    MAIN_MENU_TEXT,
    MAIN_MENU_TEXT_V2,
    REGISTRATION_REQUIRED_TEXT,
    THANKS_TEXT,
    WELCOME_TEXT,
)

from config import SUPPLIER_COMPANIES_URL
from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


# ═══════════════════════════════════════════════════════════════
# Главное меню
# ═══════════════════════════════════════════════════════════════


async def show_main_menu(context: UserContext) -> None:
    """Старое главное меню (4 кнопки)."""
    await msg.send_message(
        context.chat_id,
        MAIN_MENU_TEXT,
        reply_markup=main_menu_kb(),
    )


async def show_main_menu_v2(context: UserContext) -> None:
    """Рабочее меню: загрузка, поиск товара и кабинет."""
    set_state(context.chat_id, UserState.MAIN_MENU)
    await msg.send_message(
        context.chat_id,
        MAIN_MENU_TEXT_V2,
        reply_markup=main_menu_v2_kb(),
    )


async def show_main_menu_unregistered(context: UserContext) -> None:
    """Меню для незарегистрированного пользователя."""
    await msg.send_message(
        context.chat_id,
        REGISTRATION_REQUIRED_TEXT,
        reply_markup=help_kb(),
    )


# ═══════════════════════════════════════════════════════════════
# Приветствие
# ═══════════════════════════════════════════════════════════════


async def show_welcome(context: UserContext) -> None:
    """Второе сообщение после /start — предложение авторизоваться."""
    await msg.send_message(
        context.chat_id,
        WELCOME_TEXT,
        reply_markup=welcome_kb(),
    )


async def handle_greeting(context: UserContext) -> None:
    """Ответ на приветствие."""
    if context.is_bound:
        await msg.send_message(
            context.chat_id,
            GREETING_TEXT,
            reply_markup=greeting_kb(),
        )
    else:
        await msg.send_message(
            context.chat_id,
            ("Здравствуйте! Чтобы загрузить прайс, сначала привяжите вашу компанию.\n\nВыберите действие:"),
            reply_markup=help_kb(),
        )


async def handle_thanks(context: UserContext) -> None:
    """Ответ на благодарность."""
    await msg.send_message(
        context.chat_id,
        THANKS_TEXT,
        reply_markup=main_menu_kb(),
    )


# ═══════════════════════════════════════════════════════════════
# Личный кабинет
# ═══════════════════════════════════════════════════════════════


async def handle_open_cabinet(context: UserContext) -> None:
    """Открыть личный кабинет через Mini App."""
    if not context.is_bound:
        await show_welcome(context)
        return

    cabinet_url = SUPPLIER_COMPANIES_URL

    await msg.send_message(
        context.chat_id,
        "Нажмите кнопку, чтобы открыть личный кабинет.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🏢 Мой кабинет",
                        web_app=WebAppInfo(url=cabinet_url),
                    )
                ],
                [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
            ]
        ),
    )
