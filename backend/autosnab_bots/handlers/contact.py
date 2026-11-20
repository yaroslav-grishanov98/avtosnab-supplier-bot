from models import UserContext
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_TOKEN, FRONTEND_BASE_URL

bot = Bot(BOT_TOKEN)


async def handle(context: UserContext):
    await bot.send_message(
        context.chat_id,
        "📞 Свяжитесь с менеджером АвтоСнаб:\n\n"
        "• Телефон: +7 (XXX) XXX-XX-XX\n"
        "• Email: support@avtosnab.ru\n"
        f"• Через личный кабинет: {FRONTEND_BASE_URL}/\n\n"
        "Менеджер поможет с регистрацией и загрузкой прайса.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
            ]
        ),
    )
