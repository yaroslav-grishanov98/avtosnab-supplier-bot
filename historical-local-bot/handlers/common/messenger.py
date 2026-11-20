"""
Общий слой отправки сообщений для Telegram и MAX.
Все handler'ы используют этот интерфейс вместо прямого вызова Bot.
"""

import logging

from telegram import Bot, InlineKeyboardMarkup

from config import TELEGRAM_BOT_TOKEN
from utils import langfuse_track

logger = logging.getLogger(__name__)


class Messenger:
    """
    Единый интерфейс отправки сообщений.
    Сейчас реализован только Telegram. MAX будет добавлен позже.
    """

    def __init__(self, platform: str = "telegram"):
        self.platform = platform
        if platform == "telegram":
            self._bot = Bot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
        else:
            self._bot = None  # MAX-бот будет добавлен позже

    def _require_bot(self) -> Bot:
        if self._bot is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Создайте локальный .env перед запуском бота.")
        return self._bot

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ):
        """Отправить текстовое сообщение."""
        if self.platform == "telegram":
            result = await self._require_bot().send_message(chat_id, text, reply_markup=reply_markup)
            await langfuse_track("telegram_send_message", {"chat_id": chat_id, "text": text})
            return result
        else:
            # TODO: реализовать отправку через MAX API
            return None

    async def edit_message_text(self, chat_id: str, message_id: str, text: str) -> None:
        """Редактировать сообщение."""
        if self.platform == "telegram":
            try:
                await self._require_bot().edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                await langfuse_track("telegram_edit_message", {"chat_id": chat_id, "message_id": message_id, "text": text})
            except Exception as exc:
                logger.exception(
                    "Не удалось отредактировать Telegram-сообщение chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
                await langfuse_track("telegram_edit_message_error", {"chat_id": chat_id, "error": str(exc)}, level="ERROR")

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        """Удалить сообщение."""
        if self.platform == "telegram":
            try:
                await self._require_bot().delete_message(chat_id, message_id)
                await langfuse_track("telegram_delete_message", {"chat_id": chat_id, "message_id": message_id})
            except Exception as exc:
                logger.exception(
                    "Не удалось удалить Telegram-сообщение chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
                await langfuse_track("telegram_delete_message_error", {"chat_id": chat_id, "error": str(exc)}, level="ERROR")

    async def get_file(self, file_id: str):
        """Получить файл по ID."""
        if self.platform == "telegram":
            result = await self._require_bot().get_file(file_id)
            await langfuse_track("telegram_get_file", {"file_id": file_id})
            return result
        return None


# Глобальный экземпляр для Telegram (обратная совместимость)
telegram_messenger = Messenger("telegram")
