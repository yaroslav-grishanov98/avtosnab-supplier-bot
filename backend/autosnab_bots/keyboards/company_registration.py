from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def company_confirmation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data="company_inn_confirm"),
                InlineKeyboardButton("↩️ Отмена", callback_data="company_inn_cancel"),
            ]
        ]
    )


def inn_retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="supplier_main_menu")]])
