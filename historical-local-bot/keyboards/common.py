from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def back_to_menu_kb() -> InlineKeyboardMarkup:
    """Кнопка «Главное меню»."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
        ]
    )


def unsupported_kb() -> InlineKeyboardMarkup:
    """Клавиатура при неподдерживаемом типе."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📎 Загрузить прайс", callback_data="upload_new_price")],
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
            [InlineKeyboardButton("❓ Помощь", callback_data="contact_manager")],
        ]
    )


def products_list_kb() -> InlineKeyboardMarkup:
    """Клавиатура после списка товаров."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📎 Загрузить прайс", callback_data="upload_new_price")],
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
        ]
    )


def contact_kb() -> InlineKeyboardMarkup:
    """Клавиатура после контактов."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
        ]
    )


# Алиасы
back_to_menu_keyboard = back_to_menu_kb
unsupported_keyboard = unsupported_kb
products_list_keyboard = products_list_kb
contact_keyboard = contact_kb
