from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def request_list_kb(requests: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for request in requests[:10]:
        icon = "🆕" if request.get("status") == "new" else "📄"
        count = request.get("item_count", 0)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {request['number']} · {count} поз.",
                    callback_data=f"request_open:{request['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def request_detail_kb(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"request_open:{request_id}")],
            [InlineKeyboardButton("⬅️ К заявкам", callback_data="orders")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )
