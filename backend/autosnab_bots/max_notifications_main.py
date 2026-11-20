"""Вебхук MAX-бота уведомлений.

Сервис принимает события MAX, подключает чаты по одноразовым ссылкам и
обрабатывает действия чек-листа поставок. Состояние заказов хранится в API.
"""

import hmac
import logging
import os
from datetime import date

import aiohttp
from aiohttp import web
from api.notifications import get_delivery_review, get_notification_status

from config import AUTH_HEADERS, BACKEND_URL, MAX_BOT_TOKEN, NOTIFICATION_CABINET_URL

logger = logging.getLogger("autosnab.max_notifications")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
MAX_API_BASE = os.getenv("MAX_BOT_API_BASE", "https://platform-api2.max.ru").rstrip("/")
WEBHOOK_PATH = os.getenv("MAX_NOTIFICATIONS_WEBHOOK_PATH", "webhooks/max/notifications").strip("/")
WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "")
PORT = int(os.getenv("MAX_BOT_PORT", "8090"))
REVIEW_QUEUES = {}
WELCOME_TEXT = (
    "👋 Это бот уведомлений АвтоСнаб\n\n"
    "Здесь вы получите новые заявки, ответы поставщиков, уведомления об отменах "
    "и ежедневный список ожидаемых поставок.\n\n"
    "Компания и роль определятся автоматически. Что хотите подключить?"
)
PERSONAL_ONBOARDING_TEXT = (
    "Шаг 1 из 3\n\nОткройте кабинет АвтоСнаб и нажмите «Подключить» напротив Telegram или MAX.\n\n"
    "После запуска ссылки нажмите «Проверить подключение»."
)
GROUP_ONBOARDING_TEXT = (
    "Шаг 1 из 3\n\n1. В кабинете выберите «Групповой чат».\n2. Нажмите «Подключить группу».\n"
    "3. Добавьте этого бота в нужную группу.\n4. Запустите ссылку внутри группы.\n\n"
    "После подключения бот отправит проверочное сообщение."
)
HOW_IT_WORKS_TEXT = (
    "Как это работает\n\n1. В кабинете создаётся одноразовая ссылка.\n"
    "2. Вы открываете её в личном чате или запускаете внутри группы.\n"
    "3. Компания и роль подставляются автоматически.\n"
    "4. Типы уведомлений меняются в кабинете.\n\nСсылка действует 60 минут и используется один раз."
)


def _main_menu_buttons():
    """Собрать inline-кнопки главного меню MAX-бота."""
    return [
        [{"type": "callback", "text": "Подключить личные уведомления", "payload": "onboarding:personal"}],
        [{"type": "callback", "text": "Подключить групповой чат", "payload": "onboarding:group"}],
        [{"type": "callback", "text": "Как это работает", "payload": "onboarding:how"}],
        [{"type": "link", "text": "Открыть кабинет", "url": NOTIFICATION_CABINET_URL}],
        [{"type": "callback", "text": "Проверить подключение", "payload": "onboarding:status"}],
    ]


def _back_menu_buttons():
    """Собрать кнопки возврата в меню MAX-бота."""
    return [
        [{"type": "link", "text": "Открыть кабинет", "url": NOTIFICATION_CABINET_URL}],
        [{"type": "callback", "text": "Назад", "payload": "onboarding:menu"}],
        [{"type": "callback", "text": "Главное меню", "payload": "onboarding:menu"}],
    ]


def _group_menu_buttons():
    """Собрать кнопки шага подключения группового чата MAX."""
    return [
        [{"type": "link", "text": "Открыть кабинет", "url": NOTIFICATION_CABINET_URL}],
        [{"type": "callback", "text": "Не получается подключить группу", "payload": "onboarding:group_help"}],
        [{"type": "callback", "text": "Проверить подключение", "payload": "onboarding:status"}],
        [{"type": "callback", "text": "Назад", "payload": "onboarding:menu"}],
        [{"type": "callback", "text": "Главное меню", "payload": "onboarding:menu"}],
    ]


async def _send_status_message(chat_id: str):
    """Проверить подключение MAX-чата и отправить понятный результат."""
    result = await get_notification_status(chat_id=chat_id, channel="max")
    if not result.get("success"):
        text = "Не удалось проверить подключение. Попробуйте ещё раз через несколько секунд."
    elif result.get("data", {}).get("connected"):
        text = "✅ Этот чат подключён к уведомлениям АвтоСнаба."
    else:
        text = "Чат пока не подключён. Откройте кабинет и создайте одноразовую ссылку."
    await send_message(chat_id, text, _back_menu_buttons())


def _valid_secret(request: web.Request) -> bool:
    """Проверить секретный заголовок входящего вебхука, если он настроен."""
    if not WEBHOOK_SECRET:
        return True
    supplied = request.headers.get("X-Max-Secret", "")
    return hmac.compare_digest(supplied, WEBHOOK_SECRET)


async def _max_request(method: str, path: str, *, params=None, json=None):
    """Выполнить авторизованный запрос к API MAX."""
    headers = {"Authorization": MAX_BOT_TOKEN}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, f"{MAX_API_BASE}{path}", params=params, json=json, headers=headers
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"MAX API {response.status}: {body}")
            return body


async def send_message(chat_id: str, text: str, buttons=None):
    """Отправить сообщение в чат MAX с необязательной клавиатурой."""
    payload = {"text": text[:3900]}
    if buttons:
        payload["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
    return await _max_request("POST", "/messages", params={"chat_id": chat_id}, json=payload)


async def connect_chat(chat_id: str, token: str, destination_type: str, user: dict, title: str = ""):
    """Завершить подключение MAX-чата через backend по одноразовому токену."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(
            f"{BACKEND_URL.rstrip('/')}/notifications/bot/connect",
            json={
                "channel": "max",
                "destination_type": destination_type,
                "chat_id": str(chat_id),
                "token": token,
                "title": title[:255],
                "user_id": str(user.get("user_id") or user.get("id") or ""),
                "username": str(user.get("username") or ""),
                "display_name": str(user.get("name") or user.get("first_name") or ""),
            },
            headers=AUTH_HEADERS,
        ) as response:
            return response.status, await response.json(content_type=None)


async def confirm_receipt(
    chat_id: str,
    delivery_date: str,
    request_id: str | None,
    user: dict,
    result: str = "complete",
):
    """Передать в backend результат проверки поставки из MAX."""
    payload = {
        "channel": "max",
        "chat_id": str(chat_id),
        "delivery_date": delivery_date,
        "result": result,
        "telegram_user_id": str(user.get("user_id") or user.get("id") or ""),
        "telegram_user_name": str(user.get("name") or user.get("username") or "")[:255],
    }
    if request_id:
        payload["supplier_request_id"] = request_id
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(
            f"{BACKEND_URL.rstrip('/')}/notifications/bot/deliveries/confirm", json=payload, headers=AUTH_HEADERS
        ) as response:
            return response.status, await response.json(content_type=None)


async def supplier_action(chat_id: str, request_id: str, action: str, user: dict):
    """Передать в backend ответ поставщика по заявке."""
    payload = {
        "channel": "max",
        "chat_id": str(chat_id),
        "action": action,
        "responded_by": str(user.get("name") or user.get("username") or "Участник чата")[:255],
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(
            f"{BACKEND_URL.rstrip('/')}/notifications/bot/supplier-requests/{request_id}/respond",
            json=payload,
            headers=AUTH_HEADERS,
        ) as response:
            return response.status, await response.json(content_type=None)


def _format_delivery_card(row: dict, delivery_date: str) -> str:
    """Собрать подробную карточку поставки для MAX."""
    lines = [
        f"Заявка №{row.get('request_number') or row.get('order_number') or '—'}",
        row.get("supplier", "Поставщик"),
        f"Дата поставки: {delivery_date}",
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
            lines.append(
                f"{index}. {product.get('name', 'Товар')} — {product.get('quantity', '')} "
                f"{product.get('unit', '')}".rstrip()
            )
    if row.get("comment"):
        lines.append(f"\nПримечание: {row['comment']}")
    lines.append("\nПроверьте поставку и выберите результат:")
    return "\n".join(lines)


def _delivery_card_buttons(delivery_date: str, request_id: str):
    """Сформировать кнопки результата поставки для MAX."""
    prefix = f"dr:{delivery_date}:{request_id}"
    return [
        [{"type": "callback", "text": "Приехала полностью", "payload": f"{prefix}:complete"}],
        [{"type": "callback", "text": "Приехала частично", "payload": f"{prefix}:partial"}],
    ]


async def _send_next_review_card(chat_id: str):
    """Отправить следующую карточку MAX из очереди проверки."""
    queue = REVIEW_QUEUES.get(chat_id)
    if not queue:
        return
    index = queue["index"]
    if index >= len(queue["rows"]):
        REVIEW_QUEUES.pop(chat_id, None)
        await send_message(chat_id, "Готово. Все выбранные заявки проверены.")
        return
    row = queue["rows"][index]
    await send_message(
        chat_id,
        _format_delivery_card(row, queue["date"]),
        _delivery_card_buttons(queue["date"], row["request_id"]),
    )


async def handle_update(request: web.Request):
    """Обработать событие MAX: запуск бота, команду или callback-кнопку."""
    if not _valid_secret(request):
        raise web.HTTPUnauthorized()
    update = await request.json()
    update_type = update.get("update_type", "")
    message = update.get("message") or update.get("message_created") or {}
    callback = update.get("callback") or update.get("message_callback") or {}
    user = update.get("user") or message.get("sender") or message.get("user") or {}
    recipient = message.get("recipient") or {}
    chat_id = str(message.get("chat_id") or recipient.get("chat_id") or update.get("chat_id") or "")
    text = str(message.get("body", {}).get("text") or message.get("text") or "").strip()
    payload = str(update.get("payload") or message.get("payload") or "").strip()

    is_start_event = update_type in {"bot_started", "message_created"} and (
        text.startswith("/start") or (bool(payload) and not callback)
    )
    if is_start_event:
        token = payload or (text.split(maxsplit=1)[1] if len(text.split()) > 1 else "")
        if token:
            destination_type = (
                "group" if str(recipient.get("chat_type") or "").lower() in {"group", "supergroup"} else "personal"
            )
            await send_message(chat_id, "Проверяю подключение…")
            status, body = await connect_chat(chat_id, token, destination_type, user, recipient.get("name", ""))
            if status in (200, 201):
                is_reused = status == 200 or (isinstance(body, dict) and bool(body.get("reused")))
                await send_message(
                    chat_id,
                    "Этот чат уже был подключён. Настройки уведомлений доступны в кабинете."
                    if is_reused
                    else "✅ Уведомления подключены\n\n"
                    "Теперь сюда будут приходить сообщения по заявкам.\n"
                    "Ниже отправлено проверочное сообщение.",
                    _back_menu_buttons(),
                )
            elif (
                status == 400
                and isinstance(body, dict)
                and (body.get("code") == "wrong_chat_type" or "destination_type" in body)
            ):
                await send_message(chat_id, "Эта ссылка предназначена для другого типа чата.", _back_menu_buttons())
            elif (
                status == 400
                and isinstance(body, dict)
                and (body.get("code") == "already_connected" or "chat_id" in body)
            ):
                await send_message(
                    chat_id,
                    "Этот чат уже подключён. Откройте настройки, чтобы проверить уведомления.",
                    _back_menu_buttons(),
                )
            else:
                await send_message(
                    chat_id,
                    "Ссылка уже использована или устарела. Создайте новую ссылку в кабинете.",
                    _back_menu_buttons(),
                )
            return web.json_response({"ok": True})
        await send_message(chat_id, WELCOME_TEXT, _main_menu_buttons())
        return web.json_response({"ok": True})

    if text.lower().startswith("/menu") or text.lower().startswith("/help"):
        await send_message(
            chat_id, WELCOME_TEXT if text.lower().startswith("/menu") else HOW_IT_WORKS_TEXT, _main_menu_buttons()
        )
        return web.json_response({"ok": True})
    if text.lower().startswith("/status"):
        await _send_status_message(chat_id)
        return web.json_response({"ok": True})
    if text.lower().startswith("/settings"):
        await send_message(
            chat_id, "Типы уведомлений и список заведений изменяются в кабинете АвтоСнаб.", _back_menu_buttons()
        )
        return web.json_response({"ok": True})
    if text.lower().startswith("/getgroupid"):
        await send_message(chat_id, f"ID этой группы: {chat_id}\nУкажите его в настройках уведомлений АвтоСнаба.")
        return web.json_response({"ok": True})

    callback_payload = str(callback.get("payload") or callback.get("data") or "")
    if callback_payload.startswith("onboarding:"):
        action = callback_payload.split(":", 1)[1]
        if action == "menu":
            await send_message(chat_id, WELCOME_TEXT, _main_menu_buttons())
        elif action == "personal":
            await send_message(chat_id, PERSONAL_ONBOARDING_TEXT, _back_menu_buttons())
        elif action == "group":
            await send_message(chat_id, GROUP_ONBOARDING_TEXT, _group_menu_buttons())
        elif action == "group_help":
            await send_message(
                chat_id,
                "Если не получается подключить группу:\n\n"
                "1. Добавьте бота в группу.\n"
                "2. Дайте ему право отправлять сообщения.\n"
                "3. Запустите ссылку из кабинета внутри группы.\n\n"
                "Если ссылка не открывается, используйте /getgroupid и подключите группу вручную по Chat ID.",
                [
                    [
                        {
                            "type": "callback",
                            "text": "Подключить вручную по Chat ID",
                            "payload": "onboarding:group_manual",
                        }
                    ],
                    [{"type": "callback", "text": "Назад", "payload": "onboarding:group"}],
                    [{"type": "callback", "text": "Главное меню", "payload": "onboarding:menu"}],
                ],
            )
        elif action == "group_manual":
            await send_message(
                chat_id,
                "Ручное подключение группы\n\n"
                "1. Добавьте бота в группу и дайте ему право отправлять сообщения.\n"
                "2. Внутри группы отправьте /getgroupid.\n"
                "3. Скопируйте Chat ID и укажите его в кабинете АвтоСнаб.\n\n"
                "После сохранения настроек отправьте /status для проверки.",
                _back_menu_buttons(),
            )
        elif action == "how":
            await send_message(chat_id, HOW_IT_WORKS_TEXT, _back_menu_buttons())
        elif action == "status":
            await _send_status_message(chat_id)
        return web.json_response({"ok": True})
    if callback_payload.startswith("dc:"):
        parts = callback_payload.split(":")
        if len(parts) != 3:
            return web.json_response({"ok": False}, status=400)
        _, delivery_date, scope = parts
        try:
            date.fromisoformat(delivery_date)
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        review = await get_delivery_review(
            chat_id=chat_id,
            delivery_date=date.fromisoformat(delivery_date),
            scope=scope,
            channel="max",
        )
        callback_id = callback.get("callback_id") or update.get("callback_id")
        if not review.get("success"):
            if callback_id:
                await _max_request(
                    "POST",
                    "/answers",
                    params={"callback_id": callback_id},
                    json={"message": {"text": "Не удалось открыть карточки поставок."}},
                )
            return web.json_response({"ok": True})
        data = review["data"]
        REVIEW_QUEUES[chat_id] = {
            "date": data.get("date", delivery_date),
            "rows": data.get("deliveries", []),
            "index": 0,
        }
        if callback_id:
            await _max_request(
                "POST",
                "/answers",
                params={"callback_id": callback_id},
                json={"message": {"text": "Открываю карточки заявок…"}},
            )
        await _send_next_review_card(chat_id)
        return web.json_response({"ok": True})
    if callback_payload.startswith(("dr:", "delivery_receipt:")):
        parts = callback_payload.split(":")
        _, delivery_date, request_id, result = (parts + ["", "complete"])[:4]
        if result not in {"complete", "partial", "discrepancy"}:
            result = "complete"
        backend_result = "discrepancy" if result in {"partial", "discrepancy"} else "complete"
        try:
            date.fromisoformat(delivery_date)
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        status, body = await confirm_receipt(
            chat_id,
            delivery_date,
            request_id if request_id and request_id != "all" else None,
            user,
            backend_result,
        )
        callback_id = callback.get("callback_id") or update.get("callback_id")
        if callback_id:
            if status == 200 and body.get("ok"):
                message = (
                    "Поставка отмечена частично."
                    if result == "partial"
                    else "Поставка отмечена с расхождением."
                    if result == "discrepancy"
                    else "Поставка отмечена полностью."
                )
            else:
                message = "Не удалось сохранить результат поставки."
            await _max_request(
                "POST", "/answers", params={"callback_id": callback_id}, json={"message": {"text": message}}
            )
        queue = REVIEW_QUEUES.get(chat_id)
        if status == 200 and body.get("ok") and queue:
            rows = queue.get("rows", [])
            index = int(queue.get("index", 0))
            if index < len(rows) and rows[index].get("request_id") == (request_id or ""):
                queue["index"] = index + 1
                await _send_next_review_card(chat_id)
        return web.json_response({"ok": True})
    if callback_payload.startswith("supplier_action:"):
        _, request_id, action = (callback_payload.split(":", 2) + [""])[:3]
        if action == "decline":
            status, body = await supplier_action(chat_id, request_id, action, user)
            await send_message(
                chat_id,
                "Ответ принят: заявка отклонена."
                if status == 200 and body.get("ok")
                else "Не удалось обработать ответ.",
            )
        else:
            await send_message(
                chat_id,
                "Для подтверждения или частичного подтверждения откройте заявку в кабинете и укажите дату поставки.",
            )
        return web.json_response({"ok": True})
    return web.json_response({"ok": True})


async def health(_request):
    """Вернуть технический статус MAX-сервиса."""
    return web.json_response({"ok": True, "service": "max-notifications"})


def main():
    """Запустить HTTP-сервис вебхука MAX."""
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post(f"/{WEBHOOK_PATH}", handle_update)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
