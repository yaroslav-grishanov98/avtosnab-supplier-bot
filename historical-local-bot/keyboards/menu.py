from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    """Единое рабочее меню поставщика."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Входящие заявки", callback_data="orders")],
            [InlineKeyboardButton("📎 Загрузить товары", callback_data="upload_new_price")],
            [InlineKeyboardButton("🔎 Найти товар", callback_data="search_catalog")],
            [InlineKeyboardButton("🏢 Личный кабинет", callback_data="open_cabinet")],
        ]
    )


def main_menu_v2_kb() -> InlineKeyboardMarkup:
    """Компактное главное меню поставщика без служебных шагов онбординга."""
    return main_menu_kb()


def greeting_kb() -> InlineKeyboardMarkup:
    """Клавиатура для приветствия."""
    return main_menu_kb()


def help_kb() -> InlineKeyboardMarkup:
    """Клавиатура помощи."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Связаться с менеджером", callback_data="contact_manager")],
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
        ]
    )


# Алиасы для обратной совместимости
main_menu = main_menu_kb
greeting_keyboard = greeting_kb
help_keyboard = help_kb
