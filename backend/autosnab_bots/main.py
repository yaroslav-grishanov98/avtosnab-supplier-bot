"""
Точка входа Telegram-бота AutoSnab Supplier.
"""

import logging

from handlers.router import dispatch
from logging_setup import configure_logging
from normalizer import normalize
from notification_state import configure_notification_state, durable_notification_state
from notifications_main import (
    PENDING_ACTION_KEY,
    get_group_id_command,
    settings_command,
    status_command,
    supplier_action_reply,
)
from notifications_main import (
    register_handlers as register_notification_handlers,
)
from notifications_main import (
    start_command as notification_start_command,
)
from telegram import BotCommand
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from utils import langfuse_flush, langfuse_track

from config import TELEGRAM_BOT_TOKEN, validate_runtime_config

logger = logging.getLogger(__name__)
GENERIC_ERROR_TEXT = (
    "Не получилось выполнить действие из-за технической ошибки. Попробуйте ещё раз или вернитесь в /menu."
)


async def setup_bot(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "🚀 Начать работу"),
                BotCommand("menu", "📋 Главное меню"),
                BotCommand("stop", "🛑 Стоп"),
                BotCommand("help", "❓ Помощь"),
                BotCommand("status", "🔗 Проверить уведомления"),
                BotCommand("settings", "⚙️ Настройки уведомлений"),
                BotCommand("getgroupid", "👥 ID группового чата"),
            ]
        )
    except Exception:
        logger.exception("Не удалось установить команды Telegram-бота")
    await langfuse_track("bot_started", {"platform": "telegram"})


async def shutdown_bot(app: Application) -> None:
    await langfuse_track("bot_stopped", {"platform": "telegram"})
    await langfuse_flush()


@durable_notification_state
async def handle_update(update, context) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception as exc:
            logger.warning("Не удалось подтвердить callback Telegram: %s", type(exc).__name__)
    user_context = normalize(update)
    pending = getattr(context, "user_data", {}).get(PENDING_ACTION_KEY)
    command = (getattr(user_context, "text", "") or "").split(" ", 1)[0].split("@", 1)[0].lower()
    if command in {"/start", "/menu", "/stop", "/cancel"}:
        getattr(context, "chat_data", {}).pop("delivery_review_queue", None)
    if (
        pending
        and pending.get("chat_id") == str(user_context.chat_id)
        and command in {"/start", "/menu", "/stop", "/cancel"}
    ):
        context.user_data.pop(PENDING_ACTION_KEY, None)
    await dispatch(user_context)


@durable_notification_state
async def handle_notification_deep_link(update, context) -> None:
    """Подключить уведомления по прежней ссылке /start <token> в общем боте."""
    await notification_start_command(update, context)


@durable_notification_state
async def handle_notification_reply(update, context) -> None:
    """Перехватить только активный ответ на заявку, остальные сообщения пропустить."""
    pending = context.user_data.get(PENDING_ACTION_KEY)
    message = getattr(update, "effective_message", None)
    if not pending or message is None or pending.get("chat_id") != str(message.chat_id):
        return
    await supplier_action_reply(update, context)
    raise ApplicationHandlerStop


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
    configure_notification_state(app, TELEGRAM_BOT_TOKEN)
    register_notification_handlers(app, include_commands=False, include_reply_handler=False)
    app.add_handler(MessageHandler(filters.Regex(r"^/start(?:@\w+)?\s+\S+"), handle_notification_deep_link))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("getgroupid", get_group_id_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_notification_reply), group=-1)
    app.add_handler(MessageHandler(filters.ALL, handle_update))
    app.add_handler(CallbackQueryHandler(handle_update))
    app.add_error_handler(handle_error)
    return app


def main() -> None:
    validate_runtime_config()
    log_file = configure_logging()
    app = build_application()
    logger.info("Бот запущен; журнал: %s", log_file or "консоль")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
