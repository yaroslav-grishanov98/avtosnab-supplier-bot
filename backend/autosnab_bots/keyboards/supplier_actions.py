"""One reply callback contract for incoming requests and notifications."""

from telegram import InlineKeyboardButton


def supplier_action_rows(request_id: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton("Принять", callback_data=f"supplier_action:{request_id}:accept"),
            InlineKeyboardButton("Принять частично", callback_data=f"supplier_action:{request_id}:partial"),
        ],
        [InlineKeyboardButton("Отказать", callback_data=f"supplier_action:{request_id}:decline")],
    ]
