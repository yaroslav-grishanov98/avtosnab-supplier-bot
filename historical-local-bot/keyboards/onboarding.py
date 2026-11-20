from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo


def welcome_kb() -> InlineKeyboardMarkup:
    """Кнопка авторизации через Mini App."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔐 Авторизоваться в АвтоСнаб",
            web_app=WebAppInfo(url="https://avtosnab.testant.online/supplier/login")
        )],
        [InlineKeyboardButton("🔄 Проверить авторизацию", callback_data="check_authorization")],
    ])

def binding_retry_kb() -> InlineKeyboardMarkup:
    """Повторить проверку привязки без повторной регистрации."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Проверить авторизацию", callback_data="check_authorization")],
        [InlineKeyboardButton("🆘 Помощь", callback_data="help")],
    ])


def onboarding_checklist_kb(next_step: str) -> InlineKeyboardMarkup:
    """One primary action for the next unfinished onboarding step."""
    if next_step == "authorize":
        return welcome_kb()
    if next_step == "create_company":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🏢 Создать компанию",
                web_app=WebAppInfo(url="https://avtosnab.testant.online/supplier/cabinet/companies"),
            )],
        ])
    if next_step == "upload_price":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📎 Загрузить прайс", callback_data="upload_new_price")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="supplier_main_menu")],
        ])
    if next_step == "wait_for_price":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="supplier_main_menu")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти товар", callback_data="search_catalog")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="supplier_main_menu")],
    ])


def after_registration_kb() -> InlineKeyboardMarkup:
    """Следующие действия после успешной привязки компании."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📎 Загрузить товары", callback_data="upload_new_price")],
        [InlineKeyboardButton("🔎 Найти товар", callback_data="search_catalog")],
        [InlineKeyboardButton("🏢 Личный кабинет", callback_data="open_cabinet")],
    ])



def after_company_added_kb() -> InlineKeyboardMarkup:
    """Клавиатура после добавления компании."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Загрузить каталог", callback_data="upload_price_offer")],
        [InlineKeyboardButton("📋 Посмотреть каталог", callback_data="view_catalog")],
        [InlineKeyboardButton("↩️ Позже", callback_data="upload_price_later")],
    ])
