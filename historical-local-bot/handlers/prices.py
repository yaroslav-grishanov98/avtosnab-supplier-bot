"""
Обработчики раздела «Прайсы».
"""

from api.backend import get_upload_history
from keyboards.menu import main_menu_v2_kb
from models import UserContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from texts.prices import (
    PRICES_EMPTY_TEXT,
    PRICES_HISTORY_TEXT,
    PRICES_MENU_TEXT,
    PRICES_UPLOAD_DETAIL_TEXT,
)

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


async def handle_price_lists(context: UserContext) -> None:
    """Показать меню прайсов: загрузить новый или история."""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📎 Загрузить новый прайс", callback_data="upload_new_price")],
            [InlineKeyboardButton("📋 История загрузок", callback_data="prices_history")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )
    await msg.send_message(context.chat_id, PRICES_MENU_TEXT, reply_markup=keyboard)


async def handle_prices_history(context: UserContext) -> None:
    """Показать историю загрузок прайсов."""
    if not context.supplier_id:
        await msg.send_message(context.chat_id, PRICES_EMPTY_TEXT, reply_markup=main_menu_v2_kb())
        return

    uploads = await get_upload_history(context.supplier_id, limit=10)
    if not uploads:
        await msg.send_message(context.chat_id, PRICES_EMPTY_TEXT, reply_markup=main_menu_v2_kb())
        return

    keyboard = []
    for upload in uploads:
        upload_id = upload.get("id", "")
        file_name = upload.get("source_file_name") or "Без названия"
        status = upload.get("status", "—")
        status_emoji = {"applied": "✅", "processing": "⏳", "needs_review": "⚠️", "failed": "❌"}.get(status, "📎")
        keyboard.append(
            [InlineKeyboardButton(f"{status_emoji} {file_name}", callback_data=f"price_detail_{upload_id}")]
        )
    keyboard.append(
        [
            InlineKeyboardButton("📎 Загрузить новый", callback_data="upload_new_price"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
        ]
    )
    await msg.send_message(context.chat_id, PRICES_HISTORY_TEXT, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_price_detail(context: UserContext, upload_id: str) -> None:
    """Показать детали конкретной загрузки."""
    from api.backend import get_upload_stats

    stats = await get_upload_stats(upload_id)

    detail_text = PRICES_UPLOAD_DETAIL_TEXT.format(
        date=stats.get("date", "—"),
        file_name=stats.get("file", "—"),
        status=stats.get("status", "—"),
        total=stats.get("items_total", 0),
        new=stats.get("products_created", 0),
        updated=stats.get("prices_created", 0),
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Список товаров", callback_data="products")],
            [InlineKeyboardButton("◀️ К истории", callback_data="prices_history")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )
    await msg.send_message(context.chat_id, detail_text, reply_markup=keyboard)
