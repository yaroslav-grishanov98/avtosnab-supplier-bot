"""Сценарии общего каталога и добавления товаров текстом."""

import logging

from api.backend import create_text_price_upload, get_supplier_products
from fsm import StateManager
from handlers.common.messenger import telegram_messenger
from keyboards import back_to_menu_kb, main_menu
from models import UserContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

bot = telegram_messenger
logger = logging.getLogger(__name__)


async def ask_catalog_query(context: UserContext) -> None:
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "awaiting_catalog_search",
        {"source": "catalog_search"},
    )
    await bot.send_message(
        context.chat_id,
        "Напишите название товара. Я поищу среди товаров текущей компании.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отменить поиск", callback_data="cancel_current_action")],
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
        ]),
    )


async def handle_catalog_query(context: UserContext) -> None:
    query = context.text.strip()
    if len(query) < 2:
        await bot.send_message(context.chat_id, "Введите хотя бы два символа.")
        return

    try:
        result = await get_supplier_products(context.supplier_id, query=query, limit=10)
    except Exception:
        logger.exception(
            "Ошибка поиска по каталогу supplier_id=%s chat_id=%s",
            context.supplier_id,
            context.chat_id,
        )
        await bot.send_message(
            context.chat_id,
            "Не удалось выполнить поиск. Попробуйте ещё раз чуть позже.",
            reply_markup=main_menu(),
        )
        await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")
        return
    products = result.get("results", [])
    if not products:
        await bot.send_message(
            context.chat_id,
            f"По запросу «{query}» ничего не найдено. Попробуйте другое название.",
            reply_markup=main_menu(),
        )
    else:
        lines = [f"Нашёл по запросу «{query}»:", ""]
        for product in products[:10]:
            name = product.get("supplier_normalized_name") or product.get("supplier_raw_name") or "Товар"
            price = product.get("current_price")
            unit = product.get("unit")
            if isinstance(price, dict):
                currency = price.get("currency") or "руб"
                price = price.get("price")
            else:
                currency = product.get("currency") or "руб"
            if isinstance(unit, dict):
                unit = unit.get("code")
            price_text = f" — {price} ₽" if price is not None else ""
            unit_text = f"/{unit}" if unit else ""
            if price is not None and currency != "руб":
                price_text = f" — {price} {currency}"
            lines.append(f"• {name}{price_text}{unit_text}")
        product_rows = [
            [InlineKeyboardButton(
                f"⚙️ {(p.get('supplier_normalized_name') or p.get('supplier_raw_name') or 'Товар')[:35]}",
                callback_data=f"product_manage:{p['id']}",
            )]
            for p in products[:8]
            if p.get("id")
        ]
        await bot.send_message(
            context.chat_id,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                product_rows + [[InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")]]
            ),
        )

    await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")


async def ask_product_text(context: UserContext) -> None:
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "awaiting_product_text",
        {"source": "product_text"},
    )
    await bot.send_message(
        context.chat_id,
        "Напишите одну позицию или вставьте весь список. Каждую позицию лучше начать с новой строки.\n\n"
        "Пример:\nМолоко 3,2% — 95 руб/л\nСыр Российский — 720 руб/кг",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отменить", callback_data="cancel_current_action")],
            [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
        ]),
    )


async def handle_product_text(context: UserContext) -> None:
    price_text = context.text.strip()
    if len(price_text) < 3:
        await bot.send_message(context.chat_id, "Добавьте название товара и цену.")
        return

    result = await create_text_price_upload(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        context.message_id,
        price_text,
    )
    upload = result.get("upload") or {}
    upload_id = upload.get("id") or upload.get("upload_id")
    if not upload_id:
        await bot.send_message(
            context.chat_id,
            "Не удалось принять список. Проверьте строки и попробуйте ещё раз.",
            reply_markup=main_menu(),
        )
        return

    await bot.send_message(
        context.chat_id,
        "Товары приняты ✓\nПроверяю названия и цены. Результат появится в истории загрузок.",
        reply_markup=main_menu(),
    )
