from keyboards import back_to_menu_keyboard, main_menu, unsupported_keyboard
from models import UserContext
from telegram import Bot

from config import BOT_TOKEN

bot = Bot(BOT_TOKEN)

UNSUPPORTED_RESPONSES = {
    "voice": "К сожалению, голосовые сообщения я пока не понимаю.\n\nОтправьте прайс файлом (Excel, PDF, Word, фото) или напишите позиции текстом.",
    "sticker": "Стикеры я не обрабатываю.\n\nОтправьте прайс файлом или текстом.",
    "animation": "GIF и анимации я не обрабатываю.\n\nОтправьте прайс файлом или текстом.",
    "video": "Видео я пока не обрабатываю.\n\nОтправьте прайс файлом или текстом.",
    "contact": "Контакты я не обрабатываю.\n\nОтправьте прайс файлом или текстом.",
    "location": "Геолокации я не обрабатываю.\n\nОтправьте прайс файлом или текстом.",
    "unsupported_file": "Этот формат файла я пока не могу обработать.\n\nОтправьте прайс в одном из форматов:\n• Excel (.xlsx, .csv)\n• PDF\n• Фото прайса\n• Или просто напишите позиции текстом",
    "unsupported_message": "Не совсем понял, что вы отправили.\n\nЧтобы загрузить прайс — отправьте файл или напишите позиции текстом.\nИли выберите действие из меню.",
}

QUESTION_RESPONSE = (
    "Этот бот помогает загружать прайс-листы в систему АвтоСнаб.\n\n"
    "Что я умею:\n"
    "• Принять ваш прайс — файлом, фото или текстом\n"
    "• Распознать товары и цены\n"
    "• Показать результат для проверки\n\n"
    "Как обновить цены?\n"
    "Просто отправьте новый прайс — я сравню с текущими данными.\n\n"
    "Если что-то не получается — напишите менеджеру."
)

WANTS_UPLOAD_RESPONSE = (
    "Отправьте файл с вашим прайсом — я распознаю товары и цены.\n\nПодходят: Excel, PDF, Word, фото таблицы или текст."
)


async def handle_unsupported(context: UserContext):
    text = UNSUPPORTED_RESPONSES.get(context.message_type, UNSUPPORTED_RESPONSES["unsupported_message"])
    await bot.send_message(context.chat_id, text, reply_markup=unsupported_keyboard())


async def handle_question(context: UserContext):
    await bot.send_message(context.chat_id, QUESTION_RESPONSE, reply_markup=unsupported_keyboard())


async def handle_wants_upload(context: UserContext):
    await bot.send_message(context.chat_id, WANTS_UPLOAD_RESPONSE, reply_markup=back_to_menu_keyboard())


async def handle_unknown_text(context: UserContext):
    await bot.send_message(
        context.chat_id,
        "Не совсем понял. Выберите действие из меню или отправьте файл с прайсом.",
        reply_markup=main_menu(),
    )


async def handle_unknown_text_unregistered(context: UserContext):
    await bot.send_message(
        context.chat_id,
        "Привет! Чтобы загрузить прайс, сначала нужно привязать вашу компанию.\n\nОтправьте файл или выберите действие.",
        reply_markup=unsupported_keyboard(),
    )


async def handle_fallback(context: UserContext):
    await bot.send_message(context.chat_id, "Что хотите сделать?", reply_markup=main_menu())


async def handle_fallback_unregistered(context: UserContext):
    await bot.send_message(context.chat_id, "Сначала привяжите компанию.", reply_markup=unsupported_keyboard())
