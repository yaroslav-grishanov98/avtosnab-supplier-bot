import logging
import re

from api.backend import delete_supplier_product, get_supplier_products, patch_product_price
from fsm import StateManager
from models import UserContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger
logger = logging.getLogger(__name__)


def _product_name(product: dict) -> str:
    return product.get("supplier_normalized_name") or product.get("supplier_raw_name") or "Товар"


async def _find_product(context: UserContext, product_id: str) -> dict | None:
    result = await get_supplier_products(context.supplier_id, limit=100)
    return next((item for item in result.get("results", []) if str(item.get("id")) == product_id), None)


async def handle_show(context: UserContext):
    result = await get_supplier_products(context.supplier_id, limit=100)
    products = result.get("results", [])

    if not products:
        text = "Товаров пока нет. Загрузите прайс — и они появятся в каталоге компании."
    else:
        total = result.get("count")
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = len(products)
        text = (
            f"В каталоге компании: {total} товаров.\n\n"
            "Чтобы проверить цену, изменить или удалить позицию, найдите её по названию. "
            "Полный каталог доступен в личном кабинете."
        )

    await msg.send_message(
        context.chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔎 Найти товар", callback_data="search_catalog")],
                [InlineKeyboardButton("📎 Загрузить товары", callback_data="upload_new_price")],
                [InlineKeyboardButton("🏢 Личный кабинет", callback_data="open_cabinet")],
            ]
        ),
    )


async def handle_update_price(context: UserContext):
    """Обработка update_price_command"""
    text = context.text

    price_match = re.search(r"(?:на|=)?\s*(\d+[\d\s]*(?:[,.]\d+)?)\s*(?:руб|р|₽)?\s*$", text, re.IGNORECASE)
    if not price_match:
        await msg.send_message(context.chat_id, "Не удалось выделить цену. Напишите: обнови цену на ТОВАР на 500 руб")
        return

    price = float(price_match[1].replace(" ", "").replace(",", "."))
    if price <= 0:
        await msg.send_message(context.chat_id, "Цена должна быть положительным числом.")
        return

    product_query = re.sub(r"^(измени|обнови|поменяй|поставь)\s+", "", text, flags=re.IGNORECASE)
    product_query = re.sub(r"цену\s+(на\s+)?", "", product_query, flags=re.IGNORECASE)
    product_query = re.sub(
        r"\s+на\s+\d+[\d\s]*(?:[,.]\d+)?\s*(?:руб|р|₽)?\s*$", "", product_query, flags=re.IGNORECASE
    ).strip()

    if not product_query:
        await msg.send_message(context.chat_id, "Не удалось выделить товар.")
        return

    result = await get_supplier_products(context.supplier_id, query=product_query, limit=5)
    products = result.get("results", [])

    if len(products) == 0:
        await msg.send_message(context.chat_id, "Не нашёл такой товар. Попробуйте уточнить название.")
    elif len(products) > 1:
        lines = ["Нашёл несколько похожих товаров. Уточните, какой именно:"]
        for i, p in enumerate(products, 1):
            name = p.get("supplier_normalized_name") or p.get("supplier_raw_name", "—")
            lines.append(f"{i}. {name}")
        await msg.send_message(context.chat_id, "\n".join(lines))
    else:
        product = products[0]
        await patch_product_price(product["id"], f"{price:.2f}")
        product_name = product.get("supplier_normalized_name") or product.get("supplier_raw_name", "товар")
        await msg.send_message(context.chat_id, f"✅ Цена обновлена: {product_name} — {price:.2f} RUB")


async def handle_products_list(context: UserContext):
    """Показать список товаров (вызывается из companies.py)."""
    await handle_show(context)


async def handle_manage(context: UserContext):
    product_id = context.callback_data.partition(":")[2]
    try:
        product = await _find_product(context, product_id)
    except Exception:
        logger.exception("Не удалось загрузить товар product_id=%s", product_id)
        product = None
    if not product:
        await msg.send_message(context.chat_id, "Товар не найден или уже удалён.")
        return
    name = _product_name(product)
    await msg.send_message(
        context.chat_id,
        f"Товар: {name}\n\nЧто изменить?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Изменить цену", callback_data=f"product_price:{product_id}")],
                [InlineKeyboardButton("🗑 Удалить товар", callback_data=f"product_delete:{product_id}")],
                [InlineKeyboardButton("◀️ К товарам", callback_data="show_products")],
            ]
        ),
    )


async def ask_new_price(context: UserContext):
    product_id = context.callback_data.partition(":")[2]
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "awaiting_product_price",
        {"product_id": product_id},
    )
    await msg.send_message(
        context.chat_id,
        "Введите новую цену числом. Например: 590",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Отменить", callback_data="cancel_current_action")],
                [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
            ]
        ),
    )


async def handle_new_price(context: UserContext):
    raw_price = context.text.strip().replace(" ", "").replace(",", ".")
    try:
        price = float(raw_price)
    except ValueError:
        await msg.send_message(context.chat_id, "Введите цену числом. Например: 590")
        return
    if price <= 0:
        await msg.send_message(context.chat_id, "Цена должна быть больше нуля.")
        return
    payload = (context.bot_user_state or {}).get("payload") or {}
    product_id = str(payload.get("product_id") or "")
    if not product_id:
        await msg.send_message(context.chat_id, "Не удалось определить товар. Откройте «Мои товары» ещё раз.")
        return
    try:
        await patch_product_price(product_id, f"{price:.2f}")
    except Exception:
        logger.exception("Не удалось обновить цену product_id=%s", product_id)
        await msg.send_message(context.chat_id, "Не удалось изменить цену. Попробуйте ещё раз позже.")
        return
    await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")
    await msg.send_message(
        context.chat_id,
        f"✅ Цена обновлена: {price:.2f} руб. Изменение уже доступно в веб-кабинете.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 Мои товары", callback_data="show_products")]]),
    )


async def ask_delete(context: UserContext):
    product_id = context.callback_data.partition(":")[2]
    try:
        product = await _find_product(context, product_id)
    except Exception:
        logger.exception("Не удалось загрузить товар перед удалением product_id=%s", product_id)
        product = None
    if not product:
        await msg.send_message(context.chat_id, "Товар не найден или уже удалён.")
        return
    await msg.send_message(
        context.chat_id,
        f"Удалить товар «{_product_name(product)}»? Он также исчезнет из веб-кабинета.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Да, удалить", callback_data=f"pdelok:{product_id}")],
                [InlineKeyboardButton("Отмена", callback_data=f"product_manage:{product_id}")],
            ]
        ),
    )


async def confirm_delete(context: UserContext):
    product_id = context.callback_data.partition(":")[2]
    try:
        await delete_supplier_product(product_id)
    except Exception:
        logger.exception("Не удалось удалить товар product_id=%s", product_id)
        await msg.send_message(context.chat_id, "Не удалось удалить товар. Попробуйте ещё раз позже.")
        return
    await msg.send_message(
        context.chat_id,
        "✅ Товар удалён. В веб-кабинете список тоже обновится.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 Мои товары", callback_data="show_products")]]),
    )
