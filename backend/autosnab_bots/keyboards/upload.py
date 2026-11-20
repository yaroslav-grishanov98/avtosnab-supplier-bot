from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import SUPPLIER_COMPANIES_URL


def upload_result_kb(has_changes: bool, needs_review: bool = False) -> InlineKeyboardMarkup:
    """Actions after an asynchronous upload has reached a terminal state."""
    rows = []
    if needs_review:
        rows.append([InlineKeyboardButton("⚠️ Посмотреть загрузку", callback_data="prices_history")])
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "📦 Открыть загруженные товары",
                    web_app=WebAppInfo(url=SUPPLIER_COMPANIES_URL),
                )
            ],
            [InlineKeyboardButton("🔎 Найти товар в боте", callback_data="search_catalog")],
            [InlineKeyboardButton("📎 Загрузить ещё", callback_data="upload_new_price")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def pending_upload_kb() -> InlineKeyboardMarkup:
    """Клавиатура для отложенной загрузки."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, обработать", callback_data="process_pending_upload")],
            [InlineKeyboardButton("Позже", callback_data="supplier_main_menu")],
        ]
    )


def registration_required_kb() -> InlineKeyboardMarkup:
    """Клавиатура при требовании регистрации."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Написать менеджеру", callback_data="contact_manager")],
            [InlineKeyboardButton("Отмена", callback_data="cancel_registration")],
        ]
    )


# Алиасы
upload_result_keyboard = upload_result_kb
pending_upload_keyboard = pending_upload_kb
registration_required_keyboard = registration_required_kb
