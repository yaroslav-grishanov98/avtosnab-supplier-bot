"""Отдельный Telegram-процесс для привязки чатов уведомлений."""

import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from api.notifications import (
    confirm_delivery_receipt,
    connect_notification_chat,
    get_delivery_review,
    get_notification_status,
    get_supplier_request_for_chat,
    respond_to_supplier_request,
)
from keyboards.orders import request_detail_kb
from keyboards.supplier_actions import supplier_action_rows
from notification_state import configure_notification_state, durable_notification_state
from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from texts.orders import STATUS_LABELS

from config import NOTIFICATION_CABINET_URL, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)
NOTIFICATION_TELEGRAM_BOT_TOKEN = os.environ.get("NOTIFICATION_TELEGRAM_BOT_TOKEN", "").strip() or TELEGRAM_BOT_TOKEN

LINK_REQUIRED_TEXT = "Откройте ссылку подключения из АвтоСнаба, чтобы привязать этот чат."
WELCOME_TEXT = (
    "👋 Это бот уведомлений АвтоСнаб\n\n"
    "Здесь вы получите:\n"
    "• новые заявки;\n"
    "• ответы поставщиков;\n"
    "• уведомления об отменах;\n"
    "• ежедневный список ожидаемых поставок.\n\n"
    "Подключение занимает несколько шагов. Компания и роль определятся автоматически.\n\n"
    "Что хотите подключить?"
)
CONNECTED_TEXT = (
    "✅ Уведомления подключены\n\n"
    "Теперь сюда будут приходить сообщения по заявкам.\n"
    "Ниже отправлено проверочное сообщение.\n\n"
    "Настроить типы уведомлений можно в кабинете."
)
PERSONAL_ONBOARDING_TEXT = (
    "Шаг 1 из 3\n\n"
    "Откройте кабинет АвтоСнаб и нажмите «Подключить» напротив Telegram или MAX.\n\n"
    "После запуска ссылки нажмите «Проверить подключение»."
)
GROUP_ONBOARDING_TEXT = (
    "Шаг 1 из 3\n\n"
    "1. В кабинете выберите «Групповой чат».\n"
    "2. Нажмите «Подключить группу».\n"
    "3. Добавьте этого бота в нужную группу.\n"
    "4. Запустите полученную ссылку внутри группы.\n\n"
    "После подключения бот отправит проверочное сообщение."
)
HOW_IT_WORKS_TEXT = (
    "Как это работает\n\n"
    "1. В кабинете создаётся одноразовая ссылка.\n"
    "2. Вы открываете её в личном чате или запускаете внутри группы.\n"
    "3. Компания и роль подставляются автоматически.\n"
    "4. После подключения вы управляете типами уведомлений в кабинете.\n\n"
    "Ссылка действует 60 минут и используется один раз."
)
ERROR_TEXTS = {
    "link_invalid": "Ссылка уже использована или устарела. Создайте новую ссылку в АвтоСнабе и откройте её.",
    "backend_auth_error": "Не удалось подключить чат из-за ошибки авторизации. Обратитесь к администратору.",
    "backend_unavailable": "Сервис АвтоСнаб временно недоступен. Попробуйте открыть ссылку ещё раз позже.",
    "invalid_backend_response": "Не удалось подтвердить подключение. Попробуйте создать новую ссылку в АвтоСнабе.",
    "backend_request_error": "Не удалось подключить чат. Создайте новую ссылку в АвтоСнабе и попробуйте ещё раз.",
    "wrong_chat_type": "Эта ссылка предназначена для другого типа чата. Создайте нужную ссылку в кабинете.",
    "already_connected": "Этот чат уже подключён. Откройте настройки, чтобы проверить уведомления.",
    "invalid_chat_id": "Не удалось определить группу. Запустите ссылку внутри нужной группы ещё раз.",
    "bot_no_permission": "Дайте боту право отправлять сообщения в группе и запустите ссылку ещё раз.",
}
PENDING_ACTION_KEY = "pending_supplier_notification_action"
DELIVERY_REVIEW_QUEUE_KEY = "delivery_review_queue"
MINSK_TZ = ZoneInfo("Europe/Minsk")
RUSSIAN_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Собрать главное меню онбординга Telegram-бота."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Подключить личные уведомления", callback_data="onboarding:personal")],
            [InlineKeyboardButton("Подключить групповой чат", callback_data="onboarding:group")],
            [InlineKeyboardButton("Как это работает", callback_data="onboarding:how")],
            [InlineKeyboardButton("Открыть кабинет", url=NOTIFICATION_CABINET_URL)],
            [InlineKeyboardButton("Проверить подключение", callback_data="onboarding:status")],
        ]
    )


def _back_menu_keyboard() -> InlineKeyboardMarkup:
    """Собрать кнопки возврата в главное меню и открытия кабинета."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Открыть кабинет", url=NOTIFICATION_CABINET_URL)],
            [InlineKeyboardButton("Назад", callback_data="onboarding:menu")],
            [InlineKeyboardButton("Главное меню", callback_data="onboarding:menu")],
        ]
    )


def _personal_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Собрать кнопки шага подключения личных уведомлений."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Открыть кабинет", url=NOTIFICATION_CABINET_URL)],
            [InlineKeyboardButton("Как это работает", callback_data="onboarding:how")],
            [InlineKeyboardButton("Проверить подключение", callback_data="onboarding:status")],
            [InlineKeyboardButton("Назад", callback_data="onboarding:menu")],
            [InlineKeyboardButton("Главное меню", callback_data="onboarding:menu")],
        ]
    )


def _group_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Собрать кнопки шага подключения группового чата."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Открыть кабинет", url=NOTIFICATION_CABINET_URL)],
            [InlineKeyboardButton("Не получается подключить группу", callback_data="onboarding:group_help")],
            [InlineKeyboardButton("Проверить подключение", callback_data="onboarding:status")],
            [InlineKeyboardButton("Назад", callback_data="onboarding:menu")],
            [InlineKeyboardButton("Главное меню", callback_data="onboarding:menu")],
        ]
    )


def _group_fallback_keyboard() -> InlineKeyboardMarkup:
    """Собрать кнопки ручного подключения группы по Chat ID."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Подключить вручную по Chat ID", callback_data="onboarding:group_manual")],
            [InlineKeyboardButton("Назад", callback_data="onboarding:group")],
            [InlineKeyboardButton("Главное меню", callback_data="onboarding:menu")],
        ]
    )


async def _show_onboarding_message(query, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    """Показать экран мастера в существующем сообщении Telegram."""
    await query.edit_message_text(text=text, reply_markup=reply_markup)


async def onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработать кнопки главного меню и мастера подключения Telegram."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    action = str(query.data or "").split(":", 1)[-1]
    if action == "menu":
        await _show_onboarding_message(query, WELCOME_TEXT, _main_menu_keyboard())
    elif action == "personal":
        await _show_onboarding_message(query, PERSONAL_ONBOARDING_TEXT, _personal_onboarding_keyboard())
    elif action == "group":
        await _show_onboarding_message(query, GROUP_ONBOARDING_TEXT, _group_onboarding_keyboard())
    elif action == "group_help":
        await _show_onboarding_message(
            query,
            "Если не получается подключить группу:\n\n"
            "1. Добавьте бота в группу.\n"
            "2. Дайте ему право отправлять сообщения.\n"
            "3. Запустите ссылку из кабинета внутри группы.\n\n"
            "Если ссылка не открывается, используйте /getgroupid и подключите группу вручную по Chat ID.",
            _group_fallback_keyboard(),
        )
    elif action == "group_manual":
        await _show_onboarding_message(
            query,
            "Ручное подключение группы\n\n"
            "1. Добавьте бота в группу и дайте ему право отправлять сообщения.\n"
            "2. Внутри группы отправьте /getgroupid.\n"
            "3. Скопируйте Chat ID и укажите его в кабинете АвтоСнаб.\n\n"
            "После сохранения настроек отправьте /status для проверки.",
            _back_menu_keyboard(),
        )
    elif action == "how":
        await _show_onboarding_message(query, HOW_IT_WORKS_TEXT, _back_menu_keyboard())
    elif action == "status":
        result = await get_notification_status(chat_id=str(query.message.chat_id))
        if not result.get("success"):
            text = "Не удалось проверить подключение. Попробуйте ещё раз через несколько секунд."
        elif result.get("data", {}).get("connected"):
            text = "✅ Этот чат подключён к уведомлениям АвтоСнаба."
        else:
            text = "Чат пока не подключён. Откройте кабинет и создайте одноразовую ссылку."
        await _show_onboarding_message(query, text, _back_menu_keyboard())


def _action_error_text(code: str) -> str:
    return {
        "already_answered": "По этой заявке уже ответили.",
        "chat_not_linked": "Этот чат больше не подключён к поставщику.",
        "invalid_action_data": "Не получилось принять ответ. Проверьте дату и количество.",
        "request_not_found": "Заявка уже недоступна.",
        "request_not_available": "Не удалось загрузить заявку. Возможно, на неё уже ответили.",
        "backend_unavailable": "Сервис временно недоступен. Попробуйте нажать кнопку ещё раз позже.",
    }.get(code, "Не получилось обработать ответ. Попробуйте ещё раз.")


def _delivery_receipt_error_text(code: str) -> str:
    return {
        "chat_not_linked": "Этот чат больше не подключён к заведению.",
        "no_delivery": "Для этой даты нет подтверждённых поставок.",
        "invalid_receipt_data": "Не удалось подтвердить получение. Обновите чек-лист.",
        "backend_unavailable": "Сервис временно недоступен. Попробуйте ещё раз позже.",
    }.get(code, "Не удалось подтвердить получение.")


def _parse_delivery_date(value: str) -> date | None:
    today = datetime.now(MINSK_TZ).date()
    last_allowed_date = date(today.year, 12, 31)
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value.strip(), date_format).date()
            return parsed if today <= parsed <= last_allowed_date else None
        except ValueError:
            continue
    return None


def _delivery_date_error_text() -> str:
    today = datetime.now(MINSK_TZ).date()
    last_allowed_date = date(today.year, 12, 31)
    return f"Выберите дату от {today:%d.%m.%Y} до {last_allowed_date:%d.%m.%Y} включительно."


def _supplier_action_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(supplier_action_rows(request_id))


def _delivery_date_keyboard(request_id: str, action: str) -> InlineKeyboardMarkup:
    action_code = _action_code(action)
    today = datetime.now(MINSK_TZ).date()
    rows = []
    for offset in range(7):
        delivery_date = today + timedelta(days=offset)
        if delivery_date.year != today.year:
            continue
        if offset == 0:
            label = f"Сегодня — {delivery_date.day} {RUSSIAN_MONTHS[delivery_date.month - 1]}"
        elif offset == 1:
            label = f"Завтра — {delivery_date.day} {RUSSIAN_MONTHS[delivery_date.month - 1]}"
        else:
            label = f"{delivery_date.day} {RUSSIAN_MONTHS[delivery_date.month - 1]}"
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"supplier_date:{request_id}:{action_code}:{delivery_date:%Y-%m-%d}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton("Другая дата", callback_data=f"supplier_custom:{request_id}:{action_code}")],
            [
                InlineKeyboardButton(
                    "Не готов назвать точную дату поставки",
                    callback_data=f"supplier_no_date:{request_id}:{action_code}",
                )
            ],
            [InlineKeyboardButton("Отмена", callback_data=f"supplier_cancel:{request_id}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _show_delivery_date_picker(message, state: dict) -> None:
    request_id = state["request_id"]
    state.update(_request_card_state(message, request_id))
    await message.edit_reply_markup(reply_markup=None)
    prompt = await message.reply_text(
        "Через сколько сможете поставить товар? Выберите дату или укажите, что пока не готовы назвать точную дату.",
        reply_markup=_delivery_date_keyboard(request_id, state["action"]),
    )
    state["step"] = "delivery_date"
    state["prompt_message_id"] = prompt.message_id
    state["origin_message_id"] = message.message_id


def _action_code(action: str) -> str:
    return "p" if action == "partial" else "a"


def _action_from_code(code: str) -> str | None:
    return {"a": "accept", "p": "partial"}.get(code)


def _responded_by(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Участник чата"
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return name or (f"@{user.username}" if user.username else "Участник чата")


def _confirmation_keyboard(request_id: str, action: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Подтвердить", callback_data=f"supplier_confirm:{request_id}:{action}")]]
    if action == "partial":
        rows.append(
            [InlineKeyboardButton("Добавить комментарий", callback_data=f"supplier_partial_comment:{request_id}")]
        )
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"supplier_cancel:{request_id}")])
    return InlineKeyboardMarkup(rows)


def _delivery_result_keyboard(delivery_date: date, request_id: str) -> InlineKeyboardMarkup:
    """Показать варианты результата для одной открытой заявки."""
    prefix = f"dr:{delivery_date:%Y-%m-%d}:{request_id}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Приехала полностью", callback_data=f"{prefix}:complete")],
            [InlineKeyboardButton("Приехала частично", callback_data=f"{prefix}:partial")],
        ]
    )


def _format_delivery_card(row: dict, delivery_date: date) -> str:
    """Собрать подробную карточку одной поставки для проверки пользователем."""
    lines = [
        f"Заявка №{row.get('request_number') or row.get('order_number') or '—'}",
        row.get("supplier", "Поставщик"),
        f"Дата поставки: {delivery_date:%d.%m.%Y}",
    ]
    if row.get("venue"):
        lines.append(f"Заведение: {row['venue']}")
    if row.get("delivery_address"):
        lines.append(f"Адрес доставки: {row['delivery_address']}")
    if row.get("total_amount") not in (None, "", "0", "0.00"):
        lines.append(f"Сумма заказа: {row['total_amount']} ₽")
    grouped = {}
    for product in row.get("products", []):
        grouped.setdefault(product.get("subdivision") or "Без подразделения", []).append(product)
    for subdivision, products in grouped.items():
        lines.append(f"\n{subdivision}:")
        for index, product in enumerate(products, start=1):
            quantity = product.get("quantity", "")
            unit = product.get("unit", "")
            lines.append(f"{index}. {product.get('name', 'Товар')} — {quantity} {unit}".rstrip())
    if row.get("comment"):
        lines.append(f"\nПримечание: {row['comment']}")
    lines.append("\nПроверьте поставку и выберите результат:")
    return "\n".join(lines)


async def _send_next_delivery_card(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> None:
    """Отправить следующую карточку из текущей очереди проверки."""
    queue = context.chat_data.get(DELIVERY_REVIEW_QUEUE_KEY)
    if not queue:
        return
    rows = queue.get("deliveries", [])
    index = int(queue.get("index", 0))
    if index >= len(rows):
        context.chat_data.pop(DELIVERY_REVIEW_QUEUE_KEY, None)
        await context.bot.send_message(chat_id=chat_id, text="Готово. Все выбранные заявки проверены.")
        return
    delivery_date = date.fromisoformat(queue["date"])
    row = rows[index]
    await context.bot.send_message(
        chat_id=chat_id,
        text=_format_delivery_card(row, delivery_date),
        reply_markup=_delivery_result_keyboard(delivery_date, row["request_id"]),
    )


async def delivery_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Открыть карточки заявок из ежедневного чек-листа по очереди."""
    query = update.callback_query
    message = query.message if query else None
    parts = (query.data or "").split(":") if query else []
    if query is None or message is None or len(parts) != 3 or parts[0] != "dc":
        if query:
            await query.answer("Не удалось открыть карточки поставок.", show_alert=True)
        return
    try:
        delivery_date = date.fromisoformat(parts[1])
    except ValueError:
        await query.answer("Дата поставки указана неверно.", show_alert=True)
        return
    review = await get_delivery_review(
        chat_id=str(message.chat_id),
        delivery_date=delivery_date,
        scope=parts[2],
    )
    if not review.get("success"):
        await query.answer(_delivery_receipt_error_text(review.get("code", "")), show_alert=True)
        return
    data = review["data"]
    rows = data.get("deliveries", [])
    if not rows:
        await query.answer("В выбранной группе нет заявок.", show_alert=True)
        return
    await query.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramError as exc:
        logger.warning("Could not remove checklist buttons: %s", type(exc).__name__)
    context.chat_data[DELIVERY_REVIEW_QUEUE_KEY] = {
        "date": data.get("date", delivery_date.isoformat()),
        "scope": parts[2],
        "deliveries": rows,
        "index": 0,
    }
    await _send_next_delivery_card(context, str(message.chat_id))


def _decline_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Отказать без комментария", callback_data=f"supplier_decline:{request_id}:send")],
            [InlineKeyboardButton("Добавить комментарий", callback_data=f"supplier_decline:{request_id}:comment")],
            [InlineKeyboardButton("Отмена", callback_data=f"supplier_cancel:{request_id}")],
        ]
    )


async def _show_action_confirmation(message, state: dict) -> None:
    request_id = state["request_id"]
    delivery_date = state["confirmed_delivery_date"]
    date_label = "без точной даты" if state.get("delivery_date_unknown") else f"на {delivery_date:%d.%m.%Y}"
    prompt = await message.reply_text(
        (
            f"Проверьте ответ: частичная поставка {date_label}. "
            "Нажмите «Добавить комментарий», если нужно что-то пояснить, затем подтвердите."
            if state["action"] == "partial"
            else f"Подтвердить поставку {date_label}?"
        ),
        reply_markup=_confirmation_keyboard(request_id, state["action"]),
    )
    state["step"] = "confirm"
    state["prompt_message_id"] = prompt.message_id


async def delivery_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохранить результат проверки одной поставки из ежедневного чек-листа."""
    query = update.callback_query
    message = query.message if query else None
    parts = (query.data or "").split(":") if query else []
    if query is None or message is None or len(parts) not in {3, 4} or parts[0] not in {"dr", "delivery_receipt"}:
        if query:
            await query.answer("Не удалось распознать подтверждение.", show_alert=True)
        return
    try:
        delivery_date = date.fromisoformat(parts[1])
    except ValueError:
        await query.answer("Дата поставки указана неверно.", show_alert=True)
        return
    request_id = None if parts[2] == "all" else parts[2]
    requested_result = parts[3] if len(parts) == 4 else "complete"
    if requested_result not in {"complete", "partial", "discrepancy"}:
        await query.answer("Результат поставки указан неверно.", show_alert=True)
        return
    backend_result = "discrepancy" if requested_result in {"partial", "discrepancy"} else "complete"
    result = await confirm_delivery_receipt(
        chat_id=str(message.chat_id),
        delivery_date=delivery_date,
        supplier_request_id=request_id,
        result=backend_result,
        telegram_user_id=str(query.from_user.id) if query.from_user else "",
        telegram_user_name=_responded_by(update),
    )
    if not result.get("success"):
        await query.answer(_delivery_receipt_error_text(result.get("code", "")), show_alert=True)
        return
    if requested_result == "partial":
        await query.answer("Поставка отмечена частично.")
    elif requested_result == "discrepancy":
        await query.answer("Поставка отмечена с расхождением.")
    else:
        await query.answer("Поставка отмечена полностью.")
    markup = message.reply_markup
    if markup is None:
        return
    button_prefixes = {
        f"dr:{parts[1]}:{parts[2]}:",
        f"delivery_receipt:{parts[1]}:{parts[2]}:",
    }
    rows = []
    for row in markup.inline_keyboard:
        remaining = [
            button
            for button in row
            if button.callback_data != query.data
            and not any((button.callback_data or "").startswith(prefix) for prefix in button_prefixes)
        ]
        if remaining:
            rows.append(remaining)
    await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    queue = context.chat_data.get(DELIVERY_REVIEW_QUEUE_KEY)
    if queue and queue.get("date") == delivery_date.isoformat():
        current_rows = queue.get("deliveries", [])
        current_index = int(queue.get("index", 0))
        if current_index < len(current_rows) and current_rows[current_index].get("request_id") == request_id:
            queue["index"] = current_index + 1
            await _send_next_delivery_card(context, str(message.chat_id))


async def _submit_supplier_action(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict) -> dict:
    result = await respond_to_supplier_request(
        request_id=state["request_id"],
        chat_id=state["chat_id"],
        action=state["action"],
        confirmed_delivery_date=state.get("confirmed_delivery_date"),
        delivery_date_unknown=state.get("delivery_date_unknown", False),
        item_quantities=state.get("item_quantities"),
        comment=state.get("comment"),
        responded_by=_responded_by(update),
    )
    if result.get("success") and "card_text" in state:
        result["card_refreshed"] = False
        label = STATUS_LABELS.get(result.get("status"))
        if label:
            updated = "\n".join(
                f"Статус: {label}" if line.startswith("Статус: ") else line for line in state["card_text"].splitlines()
            )
            try:
                await context.bot.edit_message_text(
                    chat_id=state["chat_id"],
                    message_id=state["origin_message_id"],
                    text=updated,
                    reply_markup=request_detail_kb(state["request_id"], state["card_offset"]),
                )
                result["card_refreshed"] = True
            except TelegramError as exc:
                logger.warning("Could not refresh supplier request card: %s", type(exc).__name__)
    return result


def _request_card_state(message, request_id: str) -> dict:
    markup = getattr(message, "reply_markup", None)
    text = getattr(message, "text", None)
    if not isinstance(markup, InlineKeyboardMarkup) or not isinstance(text, str):
        return {}
    prefix = f"request_open:{request_id}:"
    for row in markup.inline_keyboard:
        for button in row:
            callback = button.callback_data or ""
            if callback.startswith(prefix):
                offset = callback[len(prefix) :]
                if offset.isdigit() and 0 <= int(offset) <= 1000000000:
                    return {"card_text": text, "card_offset": int(offset)}
    return {}


def _action_success_text(result: dict) -> str:
    if result.get("card_refreshed") is False:
        return "Ответ принят. Откройте заявку заново через /menu, чтобы увидеть актуальный статус."
    return "Ответ принят. Статус заявки сохранён."


async def _restore_supplier_actions(context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    if not state.get("origin_message_id"):
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=state["chat_id"],
            message_id=state["origin_message_id"],
            reply_markup=request_detail_kb(state["request_id"], state["card_offset"], can_respond=True)
            if "card_offset" in state
            else _supplier_action_keyboard(state["request_id"]),
        )
    except TelegramError as exc:
        logger.warning("Could not restore supplier action buttons: %s", type(exc).__name__)


async def supplier_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message if query else None
    if query is None or message is None:
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[0] != "supplier_date":
        await query.answer("Не удалось распознать дату.", show_alert=True)
        return
    request_id, action = parts[1], _action_from_code(parts[2])
    delivery_date = _parse_delivery_date(parts[3])
    if action is None or delivery_date is None:
        await query.answer(_delivery_date_error_text(), show_alert=True)
        return
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("Could not acknowledge supplier date callback: %s", type(exc).__name__)
    state = context.user_data.get(PENDING_ACTION_KEY, {})
    if (
        state.get("request_id") != request_id
        or state.get("chat_id") != str(message.chat_id)
        or state.get("action") != action
        or (action == "partial" and not state.get("items"))
    ):
        await message.reply_text("Этот выбор уже устарел. Нажмите кнопку ответа на заявке ещё раз.")
        return
    if action == "partial":
        state["delivery_date_unknown"] = False
        state["confirmed_delivery_date"] = delivery_date
        await _prompt_for_partial_quantities(message, state)
        return
    state["delivery_date_unknown"] = False
    state["confirmed_delivery_date"] = delivery_date
    await _show_action_confirmation(message, state)


async def supplier_custom_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message if query else None
    if query is None or message is None:
        return
    parts = (query.data or "").split(":")
    action = _action_from_code(parts[2]) if len(parts) == 3 and parts[0] == "supplier_custom" else None
    if action is None:
        await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("Could not acknowledge custom date callback: %s", type(exc).__name__)
    state = context.user_data.get(PENDING_ACTION_KEY)
    if (
        not state
        or state.get("request_id") != parts[1]
        or state.get("chat_id") != str(message.chat_id)
        or state.get("action") != action
    ):
        await message.reply_text("Этот выбор уже устарел. Нажмите кнопку ответа на заявке ещё раз.")
        return
    await message.edit_reply_markup(reply_markup=None)
    prompt = await message.reply_text(
        f"Ответьте на это сообщение датой поставки в формате ДД.ММ.ГГГГ. Например: {datetime.now(MINSK_TZ):%d.%m.%Y}.",
        reply_markup=ForceReply(selective=True),
    )
    state["step"] = "delivery_date"
    state["prompt_message_id"] = prompt.message_id


async def supplier_no_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохранить ответ поставщика без обещания точной даты."""
    query = update.callback_query
    message = query.message if query else None
    parts = (query.data or "").split(":") if query else []
    action = _action_from_code(parts[2]) if len(parts) == 3 and parts[0] == "supplier_no_date" else None
    if query is None or message is None or action is None:
        if query:
            await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    state = context.user_data.get(PENDING_ACTION_KEY)
    if (
        not state
        or state.get("request_id") != parts[1]
        or state.get("chat_id") != str(message.chat_id)
        or state.get("action") != action
    ):
        await query.answer("Этот выбор уже устарел.", show_alert=True)
        return
    await query.answer()
    state["delivery_date_unknown"] = True
    state["confirmed_delivery_date"] = None
    if action == "partial":
        await _prompt_for_partial_quantities(message, state)
    else:
        await _show_action_confirmation(message, state)


async def supplier_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message if query else None
    if query is None or message is None:
        return
    request_id = (query.data or "").split(":")
    if len(request_id) != 2 or request_id[0] != "supplier_cancel":
        await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("Could not acknowledge supplier cancel callback: %s", type(exc).__name__)
    state = context.user_data.get(PENDING_ACTION_KEY)
    if state and state.get("request_id") == request_id[1]:
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await _restore_supplier_actions(context, state)
    await message.edit_reply_markup(reply_markup=None)
    await message.reply_text("Ответ по заявке отменён. Чтобы продолжить, нажмите кнопку на уведомлении.")


async def _prompt_for_partial_quantities(message, state: dict) -> None:
    items = state["items"]
    lines = [
        f"{index}. {item['name']} — заказано {item['quantity']} {item.get('unit', '')}".rstrip()
        for index, item in enumerate(items, start=1)
    ]
    prompt = await message.reply_text(
        "Ответьте на это сообщение количеством по одной строке в формате номер: количество. "
        "Например: 1: 10. Неуказанные позиции будут приняты полностью. "
        "Хотя бы для одной позиции укажите количество меньше заказанного.\n\n" + "\n".join(lines),
        reply_markup=ForceReply(selective=True),
    )
    state["step"] = "quantities"
    state["prompt_message_id"] = prompt.message_id


async def supplier_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработать кнопки принятия, частичного принятия или отказа поставщика."""
    query = update.callback_query
    message = query.message if query else None
    if query is None or message is None:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "supplier_action" or parts[2] not in {"accept", "partial", "decline"}:
        await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    request_id, action = parts[1], parts[2]
    chat_id = str(message.chat_id)
    try:
        await query.answer()
    except TelegramError as exc:
        # Acknowledging a callback is best-effort. A transient Telegram API timeout
        # must not prevent the actual supplier action flow from continuing.
        logger.warning("Could not acknowledge supplier action callback: %s", type(exc).__name__)

    if action == "decline":
        state = {
            "request_id": request_id,
            "chat_id": chat_id,
            "action": action,
            "origin_message_id": message.message_id,
            **_request_card_state(message, request_id),
        }
        context.user_data[PENDING_ACTION_KEY] = state
        await message.edit_reply_markup(reply_markup=None)
        await message.reply_text(
            "Подтвердите отказ. При желании добавьте причину для заведения.",
            reply_markup=_decline_keyboard(request_id),
        )
        return

    state = {"request_id": request_id, "chat_id": chat_id, "action": action}
    if action == "partial":
        details = await get_supplier_request_for_chat(request_id=request_id, chat_id=chat_id)
        if not details.get("success"):
            await message.reply_text(_action_error_text(details.get("code", "")))
            return
        items = details["data"].get("items", [])
        if not items:
            await message.reply_text("В этой заявке больше нет позиций, ожидающих ответа.")
            return
        state["items"] = items
    context.user_data[PENDING_ACTION_KEY] = state
    await _show_delivery_date_picker(message, state)


async def supplier_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message if query else None
    parts = (query.data or "").split(":") if query else []
    if query is None or message is None or len(parts) != 3 or parts[0] != "supplier_confirm":
        if query:
            await query.answer("Не удалось распознать подтверждение.", show_alert=True)
        return
    state = context.user_data.get(PENDING_ACTION_KEY)
    if (
        not state
        or state.get("request_id") != parts[1]
        or state.get("chat_id") != str(message.chat_id)
        or state.get("action") != parts[2]
        or state.get("step") != "confirm"
    ):
        await query.answer("Этот выбор уже устарел. Нажмите кнопку на заявке ещё раз.", show_alert=True)
        return
    await query.answer("Отправляю ответ…")
    result = await _submit_supplier_action(update, context, state)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await message.edit_reply_markup(reply_markup=None)
    if result.get("success"):
        await message.reply_text(_action_success_text(result))
    else:
        if result.get("code") != "already_answered":
            await _restore_supplier_actions(context, state)
        await message.reply_text(_action_error_text(result.get("code", "")))


async def supplier_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message if query else None
    parts = (query.data or "").split(":") if query else []
    if query is None or message is None or len(parts) != 3 or parts[0] != "supplier_decline":
        if query:
            await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    request_id, mode = parts[1], parts[2]
    if mode not in {"send", "comment"}:
        await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    state = context.user_data.get(PENDING_ACTION_KEY, {})
    if state.get("request_id") != request_id or state.get("chat_id") != str(message.chat_id):
        await query.answer("Этот выбор уже устарел.", show_alert=True)
        return
    state["action"] = "decline"
    context.user_data[PENDING_ACTION_KEY] = state
    await query.answer()
    await message.edit_reply_markup(reply_markup=None)
    if mode == "comment":
        state["step"] = "decline_comment"
        prompt = await message.reply_text(
            "Напишите короткую причину отказа ответом на это сообщение.",
            reply_markup=ForceReply(selective=True),
        )
        state["prompt_message_id"] = prompt.message_id
        return
    result = await _submit_supplier_action(update, context, state)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    if result.get("success"):
        await message.reply_text(_action_success_text(result))
    else:
        if result.get("code") != "already_answered":
            await _restore_supplier_actions(context, state)
        await message.reply_text(_action_error_text(result.get("code", "")))


async def supplier_partial_comment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message = query.message if query else None
    parts = (query.data or "").split(":") if query else []
    state = context.user_data.get(PENDING_ACTION_KEY) if context else None
    if query is None or message is None or len(parts) != 2 or parts[0] != "supplier_partial_comment":
        if query:
            await query.answer("Не удалось распознать действие.", show_alert=True)
        return
    if (
        not state
        or state.get("request_id") != parts[1]
        or state.get("chat_id") != str(message.chat_id)
        or state.get("action") != "partial"
        or state.get("step") != "confirm"
    ):
        await query.answer("Этот выбор уже устарел. Начните ответ заново.", show_alert=True)
        return
    await query.answer()
    await message.edit_reply_markup(reply_markup=None)
    prompt = await message.reply_text(
        "Напишите необязательный комментарий ответом на это сообщение.",
        reply_markup=ForceReply(selective=True),
    )
    state["step"] = "partial_comment"
    state["prompt_message_id"] = prompt.message_id


def _parse_partial_quantities(value: str, items: list[dict]) -> dict[str, Decimal] | None:
    quantities = {}
    by_number = {str(index): item for index, item in enumerate(items, start=1)}
    for line in value.splitlines():
        if not line.strip():
            continue
        number, separator, raw_quantity = line.partition(":")
        item = by_number.get(number.strip())
        if not separator or item is None:
            return None
        try:
            quantity = Decimal(raw_quantity.strip().replace(",", "."))
        except InvalidOperation:
            return None
        if not quantity.is_finite() or quantity < 0 or quantity > Decimal(str(item["quantity"])):
            return None
        quantities[str(item["id"])] = quantity

    has_partial_quantity = any(
        Decimal("0") < quantity < Decimal(str(by_number[number]["quantity"]))
        for number, item in by_number.items()
        if (quantity := quantities.get(str(item["id"]))) is not None
    )
    return quantities if quantities and has_partial_quantity else None


async def supplier_action_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Collect the required delivery date and, for partial acceptance, quantities."""
    state = context.user_data.get(PENDING_ACTION_KEY)
    message = update.effective_message
    if not state or message is None:
        return
    if str(message.chat_id) != state.get("chat_id"):
        return
    is_prompt_reply = message.reply_to_message is not None and message.reply_to_message.message_id == state.get(
        "prompt_message_id"
    )

    if state.get("step") == "delivery_date":
        if not is_prompt_reply:
            return
        delivery_date = _parse_delivery_date(message.text or "")
        if delivery_date is None:
            prompt = await message.reply_text(
                f"{_delivery_date_error_text()} Ответьте датой в формате ДД.ММ.ГГГГ.",
                reply_markup=ForceReply(selective=True),
            )
            state["prompt_message_id"] = prompt.message_id
            return
        state["confirmed_delivery_date"] = delivery_date
        if state["action"] == "partial":
            await _prompt_for_partial_quantities(message, state)
            return
        await _show_action_confirmation(message, state)
        return
    elif state.get("step") == "quantities":
        if not is_prompt_reply:
            return
        quantities = _parse_partial_quantities(message.text or "", state["items"])
        if quantities is None:
            if is_prompt_reply:
                prompt = await message.reply_text(
                    "Не удалось разобрать количество. Ответьте на это сообщение списком вида 1: 10, "
                    "указав хотя бы одну позицию с количеством меньше заказанного.",
                    reply_markup=ForceReply(selective=True),
                )
                state["prompt_message_id"] = prompt.message_id
            return
        state["item_quantities"] = quantities
        await _show_action_confirmation(message, state)
        return
    elif state.get("step") in {"decline_comment", "partial_comment"}:
        if not is_prompt_reply:
            return
        comment = (message.text or "").strip()
        if not comment or len(comment) > 1000:
            prompt = await message.reply_text(
                "Комментарий должен содержать от 1 до 1000 символов. Ответьте ещё раз.",
                reply_markup=ForceReply(selective=True),
            )
            state["prompt_message_id"] = prompt.message_id
            return
        state["comment"] = comment
        if state["action"] == "partial":
            await _show_action_confirmation(message, state)
            return
        result = await _submit_supplier_action(update, context, state)
    else:
        return

    context.user_data.pop(PENDING_ACTION_KEY, None)
    if result.get("success"):
        await message.reply_text(_action_success_text(result))
    else:
        await message.reply_text(_action_error_text(result.get("code", "")))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать меню или подключить чат одноразовым токеном."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    token = context.args[0].strip() if context.args else ""
    if not token or len(token) > 64:
        await message.reply_text(WELCOME_TEXT, reply_markup=_main_menu_keyboard())
        return

    if chat.type == "private":
        destination_type = "personal"
        title = ""
    elif chat.type in ("group", "supergroup"):
        destination_type = "group"
        title = chat.title or ""
    else:
        await message.reply_text("Подключить уведомления можно в личном или групповом чате.")
        return

    await message.reply_text("Проверяю подключение…")
    effective_user = update.effective_user
    result = await connect_notification_chat(
        chat_id=str(chat.id),
        token=token,
        destination_type=destination_type,
        title=title,
        user_id=str(getattr(effective_user, "id", "") or ""),
        username=(getattr(effective_user, "username", "") or ""),
        display_name=(getattr(effective_user, "full_name", "") or ""),
    )
    if result.get("success"):
        text = (
            "Этот чат уже был подключён. Настройки уведомлений доступны в кабинете."
            if result.get("reused")
            else CONNECTED_TEXT
        )
        await message.reply_text(text, reply_markup=_back_menu_keyboard())
        return

    await message.reply_text(ERROR_TEXTS.get(result.get("code"), ERROR_TEXTS["backend_request_error"]))


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать главное меню бота уведомлений."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(WELCOME_TEXT, reply_markup=_main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать краткую инструкцию по подключению уведомлений."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(HOW_IT_WORKS_TEXT, reply_markup=_back_menu_keyboard())


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверить состояние подключения текущего Telegram-чата."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    result = await get_notification_status(chat_id=str(chat.id))
    if not result.get("success"):
        text = "Не удалось проверить подключение. Попробуйте ещё раз через несколько секунд."
    elif result.get("data", {}).get("connected"):
        text = "✅ Этот чат подключён к уведомлениям АвтоСнаба."
    else:
        text = "Чат пока не подключён. Откройте кабинет и создайте одноразовую ссылку."
    await message.reply_text(text, reply_markup=_back_menu_keyboard())


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Открыть кабинет для изменения типов уведомлений."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "Типы уведомлений и список заведений изменяются в кабинете АвтоСнаб.",
            reply_markup=_back_menu_keyboard(),
        )


async def get_group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать Telegram chat ID группы для настройки уведомлений."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(
            "Эта команда работает внутри группового чата. Добавьте бота в группу и отправьте там команду."
        )
        return

    await message.reply_text(
        f"ID этой группы: {chat.id}\nСкопируйте это число и укажите его в настройках уведомлений АвтоСнаба."
    )


async def setup_bot(application: Application) -> None:
    """Настроить постоянное меню команд Telegram-бота."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Начать работу с ботом"),
            BotCommand("menu", "Главное меню"),
            BotCommand("help", "Инструкция"),
            BotCommand("status", "Проверить подключение"),
            BotCommand("settings", "Настройки уведомлений"),
            BotCommand("getgroupid", "Показать ID группового чата"),
        ]
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = getattr(context, "error", None)
    logger.error(
        "Notification bot update failed: %s",
        type(error).__name__ if error else "unknown",
    )
    message = getattr(update, "effective_message", None)
    if message is not None:
        callback_query = getattr(update, "callback_query", None)
        reply = (
            "Не удалось выполнить действие. Попробуйте нажать кнопку ещё раз."
            if callback_query is not None
            else "Не удалось обработать команду. Попробуйте открыть ссылку ещё раз."
        )
        try:
            await message.reply_text(reply)
        except Exception:
            logger.exception("Could not send notification bot error reply")


def register_handlers(
    application: Application,
    *,
    include_commands: bool = True,
    include_reply_handler: bool = True,
) -> None:
    """Подключить сценарии уведомлений к существующему Telegram-приложению."""
    if include_commands:
        application.add_handler(CommandHandler("start", durable_notification_state(start_command)))
        application.add_handler(CommandHandler("menu", durable_notification_state(menu_command)))
        application.add_handler(CommandHandler("help", durable_notification_state(help_command)))
        application.add_handler(CommandHandler("status", durable_notification_state(status_command)))
        application.add_handler(CommandHandler("settings", durable_notification_state(settings_command)))
        application.add_handler(CommandHandler("getgroupid", durable_notification_state(get_group_id_command)))
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(onboarding_callback), pattern=r"^onboarding:")
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_action_callback), pattern=r"^supplier_action:")
    )
    application.add_handler(CallbackQueryHandler(durable_notification_state(delivery_review_callback), pattern=r"^dc:"))
    application.add_handler(
        CallbackQueryHandler(
            durable_notification_state(delivery_receipt_callback), pattern=r"^(?:dr|delivery_receipt):"
        )
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_date_callback), pattern=r"^supplier_date:")
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_no_date_callback), pattern=r"^supplier_no_date:")
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_custom_date_callback), pattern=r"^supplier_custom:")
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_cancel_callback), pattern=r"^supplier_cancel:")
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_confirm_callback), pattern=r"^supplier_confirm:")
    )
    application.add_handler(
        CallbackQueryHandler(durable_notification_state(supplier_decline_callback), pattern=r"^supplier_decline:")
    )
    application.add_handler(
        CallbackQueryHandler(
            durable_notification_state(supplier_partial_comment_callback), pattern=r"^supplier_partial_comment:"
        )
    )
    if include_reply_handler:
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, durable_notification_state(supplier_action_reply))
        )


def build_application(token: str | None = None) -> Application:
    """Создать standalone-приложение для обратной совместимости и тестов."""
    bot_token = (token if token is not None else NOTIFICATION_TELEGRAM_BOT_TOKEN).strip()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в локальном .env")

    application = Application.builder().token(bot_token).concurrent_updates(4).post_init(setup_bot).build()
    configure_notification_state(application, bot_token)
    register_handlers(application)
    application.add_error_handler(handle_error)
    return application


def main() -> None:
    configure_logging()
    logger.info("Notification Telegram bot is starting")
    build_application().run_polling(allowed_updates=["message", "callback_query"])


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO; Telegram Bot API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    main()
