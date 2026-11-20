from api.backend import (
    get_pending_upload,
    mark_pending_upload_status,
    register_supplier_binding,
    save_pending_upload,
)
from fsm import StateManager
from keyboards import help_keyboard, main_menu, registration_required_keyboard
from models import UserContext
from telegram import Bot

from config import BOT_TOKEN

bot = Bot(BOT_TOKEN)


async def handle_deep_link(context: UserContext):
    token = context.text  # start_payload || text
    result = await register_supplier_binding(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        context.username,
        context.first_name,
        context.last_name,
        token,
    )

    if result.get("success"):
        supplier_name = result.get("binding", {}).get("supplier_name", "поставщик")
        # Проверяем pending upload
        pending = await get_pending_upload(context.chat_id)
        if pending and pending.get("file_id"):
            await bot.send_message(
                context.chat_id,
                f"Готово! Компания «{supplier_name}» привязана.\n\nМы нашли ваш файл — начинаю обработку.",
            )
            await mark_pending_upload_status(
                pending["id"],
                "price_processing",
                result["binding"]["supplier_id"],
                {
                    "source": "registration_success",
                    "file_id": pending["file_id"],
                    "file_name": pending["file_name"],
                },
            )
            # Восстанавливаем контекст и запускаем обработку
            context.supplier_id = result["binding"]["supplier_id"]
            context.supplier_name = supplier_name
            context.file_id = pending["file_id"]
            context.file_name = pending["file_name"]
            context.mime_type = pending.get("mime_type")
            context.message_id = pending.get("message_id", "")
            from handlers.file_upload import handle

            return await handle(context)
        else:
            await bot.send_message(
                context.chat_id,
                f"Готово! Компания «{supplier_name}» привязана.\n\nТеперь можете загрузить прайс.",
                reply_markup=main_menu(),
            )
    else:
        code = result.get("code", "")
        if code == "SUPPLIER_INACTIVE":
            text = "Эта компания сейчас неактивна. Свяжитесь с менеджером АвтоСнаб — он поможет разобраться."
        else:
            text = "Ссылка недействительна или устарела. Вернитесь в личный кабинет и нажмите «Присоединиться к Telegram-боту» ещё раз."
        await bot.send_message(context.chat_id, text, reply_markup=registration_required_keyboard())


async def handle_unregistered_file_upload(context: UserContext):
    """Незарегистрированный пользователь прислал файл → сохраняем как pending"""
    await save_pending_upload(
        {
            "telegram_user_id": context.telegram_user_id,
            "chat_id": context.chat_id,
        },
        {
            "file_id": context.file_id,
            "file_name": context.file_name,
            "message_id": context.message_id,
            "mime_type": context.mime_type,
        },
    )
    await bot.send_message(
        context.chat_id,
        "Привет! Чтобы загрузить прайс, сначала нужно привязать вашу компанию.\n\n"
        "Зайдите в личный кабинет поставщика и нажмите «Присоединиться к Telegram-боту» — "
        "вы автоматически попадёте сюда уже привязанным.\n\n"
        "Если нет доступа к кабинету — свяжитесь с менеджером.",
        reply_markup=registration_required_keyboard(),
    )


async def handle_cancel(context: UserContext):
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "registration_cancelled",
        {"source": "cancel_registration", "callback_data": context.callback_data},
    )
    await bot.send_message(
        context.chat_id,
        "Регистрация отменена.\n\nВаш файл сохранён — когда будете готовы, можно продолжить.",
        reply_markup=help_keyboard(),
    )


async def handle_invite_code(context: UserContext):
    """Обработка invite-кода в состоянии waiting_invite_code"""
    return await handle_deep_link(context)


async def handle_upload_new_price_unregistered(context: UserContext):
    await bot.send_message(context.chat_id, "Сначала привяжите компанию.", reply_markup=help_keyboard())


async def handle_show_products_unregistered(context: UserContext):
    await bot.send_message(context.chat_id, "Сначала привяжите компанию.", reply_markup=help_keyboard())


async def handle_open_cabinet_unregistered(context: UserContext):
    await bot.send_message(context.chat_id, "Сначала привяжите компанию.", reply_markup=help_keyboard())
