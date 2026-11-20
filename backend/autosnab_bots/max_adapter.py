"""
Точка входа MAX-бота AutoSnab Supplier.
Принимает сообщения из MAX, преобразует в UserContext, передаёт в dispatch.
"""

from handlers.router import dispatch
from normalizer import normalize_max


async def handle_max_message(payload: dict) -> None:
    """
    Обработать входящее сообщение от MAX.
    payload — словарь с данными сообщения от MAX API.
    """
    user_context = normalize_max(payload)
    await dispatch(user_context)


# Notification delivery uses ``max_notifications_main.py``. This adapter remains
# the shared domain entry point for the future full supplier MAX conversation.
