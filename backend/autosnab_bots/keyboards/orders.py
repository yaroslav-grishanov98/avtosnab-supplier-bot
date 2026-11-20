from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.supplier_actions import supplier_action_rows


def request_list_kb(requests: list[dict], *, offset=0, count=None, limit=10) -> InlineKeyboardMarkup:
    rows = []
    for request in requests[:10]:
        icon = "🆕" if not request.get("viewed_at") and request.get("status") == "submitted" else "📄"
        item_count = request.get("item_count", 0)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {request['number']} · {item_count} поз.",
                    callback_data=f"request_open:{request['id']}:{offset}",
                )
            ]
        )
    pages = []
    if offset:
        pages.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"orders:{max(0, offset - limit)}"))
    if count is not None and offset + limit < count:
        pages.append(InlineKeyboardButton("Далее ➡️", callback_data=f"orders:{offset + limit}"))
    if pages:
        rows.append(pages)
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"orders:{offset}")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def request_detail_kb(
    request_id: str, offset=0, *, can_respond=False, response_unavailable_reason=""
) -> InlineKeyboardMarkup:
    actions = supplier_action_rows(request_id) if can_respond else []
    if response_unavailable_reason == "chat_not_connected":
        actions.append([InlineKeyboardButton("Подключить уведомления", callback_data="onboarding:menu")])
    return InlineKeyboardMarkup(
        actions
        + [
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"request_open:{request_id}:{offset}")],
            [InlineKeyboardButton("⬅️ К заявкам", callback_data=f"orders:{offset}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )
