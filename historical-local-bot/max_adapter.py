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


# TODO: добавить HTTP-сервер для приёма вебхуков от MAX
# или polling-цикл для получения сообщений
