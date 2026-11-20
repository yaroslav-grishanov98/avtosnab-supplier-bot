"""
Точка входа Telegram-бота AutoSnab Supplier.
"""

import logging

from handlers.router import dispatch
from logging_setup import configure_logging
from normalizer import normalize
from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from config import TELEGRAM_BOT_TOKEN
from utils import langfuse_flush, langfuse_track

logger = logging.getLogger(__name__)
GENERIC_ERROR_TEXT = (
    "Не получилось выполнить действие из-за технической ошибки. "
    "Попробуйте ещё раз или вернитесь в /menu."
)


async def setup_bot(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "🚀 Начать работу"),
                BotCommand("stop", "🛑 Стоп"),
                BotCommand("help", "❓ Помощь"),
            ]
        )
    except Exception:
        logger.exception("Не удалось установить команды Telegram-бота")
    await langfuse_track("bot_started", {"platform": "telegram"})


async def shutdown_bot(app: Application) -> None:
    await langfuse_track("bot_stopped", {"platform": "telegram"})
    await langfuse_flush()


async def handle_update(update, context) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception as exc:
            logger.warning("Не удалось подтвердить callback Telegram: %s", type(exc).__name__)
    user_context = normalize(update)
    await dispatch(user_context)


async def handle_error(update, context) -> None:
    """Записать необработанную ошибку и не оставлять пользователя без ответа."""
    error = getattr(context, "error", None)
    if error is not None:
        logger.error(
            "Необработанная ошибка Telegram update",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Telegram вызвал error handler без объекта ошибки")

    chat = getattr(update, "effective_chat", None) if update is not None else None
    if chat is None:
        return
    try:
        await context.bot.send_message(chat_id=chat.id, text=GENERIC_ERROR_TEXT)
    except Exception:
        logger.exception("Не удалось отправить пользователю сообщение об ошибке")


def build_application() -> Application:
    """Собрать приложение отдельно, чтобы конфигурацию можно было проверить тестом."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в локальном .env")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(8)
        .post_init(setup_bot)
        .post_shutdown(shutdown_bot)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL, handle_update))
    app.add_handler(CallbackQueryHandler(handle_update))
    app.add_error_handler(handle_error)
    return app


def main() -> None:
    log_file = configure_logging()
    app = build_application()
    logger.info("Бот запущен; журнал: %s", log_file)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
