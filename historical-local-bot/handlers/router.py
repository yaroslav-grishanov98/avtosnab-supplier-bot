"""
Главный диспетчер с новым онбордингом.
"""

from models import UserContext, UserState
from fsm import StateManager, get_state, set_state, get_data, set_data, clear_data
from handlers import menu, onboarding
from texts.onboarding import REGISTRATION_COMPLETE_TEXT, WELCOME_TEXT, WHAT_ELSE_TEXT
from keyboards.onboarding import after_registration_kb, binding_retry_kb, welcome_kb
from keyboards.menu import main_menu_v2_kb
from api.backend import resolve_supplier_binding, get_supplier_companies
from handlers.common.messenger import telegram_messenger
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

msg = telegram_messenger


def _get_selected_company_id(chat_id: str) -> str | None:
    """Получить ID выбранной компании из FSM."""
    data = get_data(chat_id)
    return data.get("selected_company_id")


def _set_selected_company_id(chat_id: str, company_id: str) -> None:
    """Сохранить ID выбранной компании в FSM."""
    set_data(chat_id, "selected_company_id", company_id)


def _get_companies(chat_id: str) -> list | None:
    """Получить список компаний из FSM."""
    data = get_data(chat_id)
    return data.get("companies")


def _set_companies(chat_id: str, companies: list) -> None:
    """Сохранить список компаний в FSM."""
    set_data(chat_id, "companies", companies)


def _restore_supplier_id(context: UserContext) -> None:
    """Восстановить supplier_id из FSM, если он не установлен в context."""
    if not context.supplier_id:
        selected_id = _get_selected_company_id(context.chat_id)
        if selected_id:
            context.supplier_id = selected_id


async def _send_binding_lookup_error(context: UserContext, result: dict) -> None:
    """Не путать техническую ошибку backend с отсутствием регистрации."""
    error = result.get("error") or {}
    code = error.get("code", "backend_unavailable")
    if code == "backend_auth_error":
        text = (
            "Не удалось проверить авторизацию из-за ошибки соединения с сервисом. "
            "Попробуйте ещё раз чуть позже."
        )
    else:
        text = "Сервис авторизации временно недоступен. Попробуйте проверить ещё раз через минуту."
    await msg.send_message(context.chat_id, text, reply_markup=binding_retry_kb())


async def dispatch(context: UserContext) -> None:
    state = get_state(context.chat_id)
    text = context.text
    callback = context.callback_data

    # Восстанавливаем supplier_id из FSM при каждом сообщении
    _restore_supplier_id(context)

    # Для сообщений после /start привязка должна восстанавливаться из backend,
    # иначе файлы и кнопки обрабатываются как запросы незарегистрированного пользователя.
    binding_lookup = None
    if not context.supplier_id:
        binding_lookup = await resolve_supplier_binding(
            platform="telegram",
            telegram_user_id=context.telegram_user_id,
            chat_id=context.chat_id,
        )
        if binding_lookup.get("exists"):
            context.binding = binding_lookup.get("binding") or {}
            context.supplier_id = context.binding.get("supplier_id") or binding_lookup.get("supplier_id")
            context.supplier_name = context.binding.get("supplier_name") or binding_lookup.get("supplier_name")

    if context.supplier_id:
        persisted_state = await StateManager.get(
            "telegram",
            context.telegram_user_id,
            context.chat_id,
        )
        context.state = persisted_state.get("state", state)
        context.bot_user_state = persisted_state
        persisted_payload = persisted_state.get("payload") or {}
        persisted_company_id = persisted_payload.get("selected_company_id")
        if persisted_company_id:
            _set_selected_company_id(context.chat_id, persisted_company_id)
            context.supplier_id = persisted_company_id
            context.selected_company_id = persisted_company_id
        if context.state == "price_processing":
            from handlers.file_upload import resume_upload_monitor

            resume_upload_monitor(context, persisted_payload)
    else:
        context.state = state

    # Техническая ошибка backend — это не отсутствие регистрации. Системные
    # /help и /stop остаются доступны даже при недоступном backend.
    if (
        binding_lookup
        and binding_lookup.get("error")
        and text not in ("/help", "/stop", "/start", "/menu")
        and callback != "check_authorization"
    ):
        await _send_binding_lookup_error(context, binding_lookup)
        return

    scenario_hints = {
        "awaiting_price_file": "Сейчас я жду прайс. Отправьте XLSX, CSV, PDF или фотографию товара.",
        "awaiting_catalog_search": "Сейчас я жду название товара. Напишите его текстом, например: сыр.",
        "awaiting_product_text": "Сейчас я жду список товаров текстом. Каждую позицию начните с новой строки.",
        "awaiting_product_price": "Сейчас я жду новую цену. Отправьте только число, например: 590.",
        "price_processing": "Прайс уже обрабатывается на сервере. Можете закрыть чат — я сам пришлю результат.",
    }

    # Во время пошагового сценария голосовое или случайное вложение не должно
    # уводить пользователя в другой обработчик.
    if context.state in scenario_hints and context.message_type == "voice":
        await msg.send_message(context.chat_id, scenario_hints[context.state])
        return

    # ═══════════════════════════════════════════════════════════
    # Голосовое сообщение
    # ═══════════════════════════════════════════════════════════
    if context.message_type == "voice":
        from handlers.voice import handle_voice
        await handle_voice(context)
        return

    # ═══════════════════════════════════════════════════════════
    # 0. Системные команды
    # ═══════════════════════════════════════════════════════════

    if text == "/stop":
        clear_data(context.chat_id)
        set_state(context.chat_id, UserState.START)
        await msg.send_message(
            context.chat_id,
            "Бот остановлен. Чтобы начать заново, нажмите /start"
        )
        return

    if text == "/help":
        await msg.send_message(
            context.chat_id,
            "Я AI-помощник АвтоСнаб.\n\n"
            "Помогаю с рутинными задачами:\n"
            "• Принимаю и загружаю прайс-листы\n"
            "• Слежу за товарами и ценами\n"
            "• Сообщаю о новых заявках\n\n"
            "Работаю и текстом, и голосом."
        )
        return

    if text.lower() in ("/cancel", "отмена", "отменить"):
        await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")
        set_state(context.chat_id, UserState.MAIN_MENU)
        await msg.send_message(context.chat_id, "Действие отменено. Что хотите сделать?", reply_markup=main_menu_v2_kb())
        return

    # /menu — только для привязанных пользователей
    if text == "/menu":
        binding = binding_lookup or await resolve_supplier_binding(
            platform="telegram",
            telegram_user_id=context.telegram_user_id,
            chat_id=context.chat_id,
        )
        if binding.get("error"):
            await _send_binding_lookup_error(context, binding)
            return
        if not binding.get("exists"):
            await msg.send_message(
                context.chat_id,
                "Сначала нужно зарегистрироваться. Нажмите /start",
                reply_markup=welcome_kb(),
            )
            return
        from handlers.menu import show_main_menu_v2
        await show_main_menu_v2(context)
        return

    # ═══════════════════════════════════════════════════════════
    # 1. /start — проверяем привязку и показываем выбор компании
    # ═══════════════════════════════════════════════════════════
    if text == "/start":
        binding = binding_lookup or await resolve_supplier_binding(
            platform="telegram",
            telegram_user_id=context.telegram_user_id,
            chat_id=context.chat_id,
        )

        if binding.get("error"):
            await _send_binding_lookup_error(context, binding)
            return

        if binding.get("exists"):
            supplier_name = binding.get("binding", {}).get("supplier_name", "")
            first_name = context.first_name or supplier_name or "пользователь"

            context.supplier_id = binding["binding"].get("supplier_id")
            context.supplier_name = supplier_name
            context.binding = binding.get("binding")

            companies = await get_supplier_companies(context.supplier_id)
            _set_companies(context.chat_id, companies)

            selected_id = _get_selected_company_id(context.chat_id)

            if len(companies) > 1 and not selected_id:
                set_state(context.chat_id, UserState.SELECTING_COMPANY)
                keyboard_rows = []
                for c in companies[:10]:
                    name = c.get("display_name") or c.get("legal_name", "Без названия")
                    keyboard_rows.append([
                        InlineKeyboardButton(
                            f"🏢 {name}",
                            callback_data=f"select_company_{c['id']}"
                        )
                    ])
                keyboard_rows.append([
                    InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
                ])
                await msg.send_message(
                    context.chat_id,
                    f"С возвращением, {first_name}! 👋\n\n"
                    f"У вас несколько компаний. Выберите, с какой работать:",
                    reply_markup=InlineKeyboardMarkup(keyboard_rows),
                )
            else:
                if selected_id:
                    context.supplier_id = selected_id
                elif companies:
                    company_id = companies[0]["id"]
                    _set_selected_company_id(context.chat_id, company_id)
                    context.supplier_id = company_id
                    await StateManager.set(
                        "telegram",
                        context.telegram_user_id,
                        context.chat_id,
                        UserState.MAIN_MENU,
                        {"selected_company_id": company_id},
                    )
                set_state(context.chat_id, UserState.MAIN_MENU)
                from handlers.checklist import show_checklist
                await msg.send_message(context.chat_id, f"С возвращением, {first_name}! 👋")
                await show_checklist(context, company_created=bool(companies or context.supplier_id))
        else:
            set_state(context.chat_id, UserState.START)
            from handlers.checklist import show_checklist
            await show_checklist(context, company_created=False)
        return

    # ═══════════════════════════════════════════════════════════
    # 2. Callback-кнопки
    # ═══════════════════════════════════════════════════════════
    if context.is_callback_query:
        if callback == "cancel_current_action":
            await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")
            set_state(context.chat_id, UserState.MAIN_MENU)
            await msg.send_message(
                context.chat_id,
                "Действие отменено. Что хотите сделать?",
                reply_markup=main_menu_v2_kb(),
            )
            return
        if callback == "check_authorization":
            binding = await resolve_supplier_binding(
                platform="telegram",
                telegram_user_id=context.telegram_user_id,
                chat_id=context.chat_id,
            )
            if binding.get("error"):
                await _send_binding_lookup_error(context, binding)
                return
            if not binding.get("exists"):
                set_state(context.chat_id, UserState.START)
                await msg.send_message(
                    context.chat_id,
                    "Авторизация пока не завершена. Войдите в АвтоСнаб или повторите проверку позже.",
                    reply_markup=welcome_kb(),
                )
                return

            context.binding = binding.get("binding") or {}
            context.supplier_id = context.binding.get("supplier_id") or binding.get("supplier_id")
            context.supplier_name = context.binding.get("supplier_name") or binding.get("supplier_name")
            if context.supplier_id:
                _set_selected_company_id(context.chat_id, context.supplier_id)
                await StateManager.set(
                    "telegram",
                    context.telegram_user_id,
                    context.chat_id,
                    UserState.MAIN_MENU,
                    {"selected_company_id": context.supplier_id},
                )
            set_state(context.chat_id, UserState.MAIN_MENU)
            from handlers.checklist import show_checklist
            await msg.send_message(context.chat_id, "Авторизация подтверждена ✅")
            await show_checklist(context, company_created=bool(context.supplier_id))
            return

        if callback == "onboarding_status":
            from handlers.checklist import show_checklist
            await show_checklist(context, company_created=bool(context.supplier_id))
            return

        if callback in ("main_menu", "supplier_main_menu"):
            await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")
            from handlers.menu import show_main_menu_v2
            await show_main_menu_v2(context)
            return

        if callback.startswith("select_company_"):
            company_id = callback.replace("select_company_", "")
            _set_selected_company_id(context.chat_id, company_id)
            context.supplier_id = company_id
            context.selected_company_id = company_id
            await StateManager.set(
                "telegram",
                context.telegram_user_id,
                context.chat_id,
                UserState.MAIN_MENU,
                {"selected_company_id": company_id},
            )

            company_name = "Компания"
            companies = _get_companies(context.chat_id)
            if companies:
                for c in companies:
                    if c["id"] == company_id:
                        company_name = c.get("display_name") or c.get("legal_name", "Компания")
                        break

            set_state(context.chat_id, UserState.MAIN_MENU)
            from handlers.checklist import show_checklist
            await msg.send_message(context.chat_id, f"✅ Вы работаете с компанией «{company_name}».")
            await show_checklist(context, company_created=True)
            return

        if callback == "switch_company":
            companies = _get_companies(context.chat_id) or await get_supplier_companies(context.supplier_id)
            _set_companies(context.chat_id, companies)
            if not companies:
                await msg.send_message(context.chat_id, "У вас нет добавленных компаний.")
                return
            set_state(context.chat_id, UserState.SELECTING_COMPANY)
            keyboard_rows = []
            for c in companies[:10]:
                name = c.get("display_name") or c.get("legal_name", "Без названия")
                keyboard_rows.append([
                    InlineKeyboardButton(f"🏢 {name}", callback_data=f"select_company_{c['id']}")
                ])
            keyboard_rows.append([
                InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
            ])
            await msg.send_message(
                context.chat_id,
                "Выберите компанию для работы:",
                reply_markup=InlineKeyboardMarkup(keyboard_rows),
            )
            return

        if callback == "what_else":
            await msg.send_message(context.chat_id, WHAT_ELSE_TEXT, reply_markup=main_menu_v2_kb())
            return

        if callback == "upload_price_offer":
            await onboarding.handle_upload_price_offer(context)
            return

        if callback == "upload_price_later":
            await onboarding.handle_upload_price_later(context)
            return

        # Новая кнопка: посмотреть каталог
        if callback == "view_catalog":
            from handlers.products import handle_show
            await handle_show(context)
            return

        if callback == "add_company_by_inn":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📝 Открыть форму создания компании",
                    web_app=WebAppInfo(url="https://avtosnab.testant.online/supplier/cabinet/companies")
                )],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ])
            await msg.send_message(
                context.chat_id,
                "Нажмите кнопку, чтобы открыть форму создания компании.",
                reply_markup=keyboard,
            )
            return

        if callback == "my_companies":
            from handlers.companies import handle_my_companies
            await handle_my_companies(context)
            return

        if callback.startswith("company_detail_"):
            company_id = callback.replace("company_detail_", "")
            from handlers.companies import handle_company_detail
            await handle_company_detail(context, company_id)
            return

        if callback.startswith("company_products_"):
            company_id = callback.replace("company_products_", "")
            from handlers.companies import handle_company_products
            await handle_company_products(context, company_id)
            return

        if callback == "products":
            from handlers.products import handle_show
            await handle_show(context)
            return

        if callback == "price_lists":
            from handlers.prices import handle_price_lists
            await handle_price_lists(context)
            return

        if callback == "prices_history":
            from handlers.prices import handle_prices_history
            await handle_prices_history(context)
            return

        if callback.startswith("price_detail_"):
            upload_id = callback.replace("price_detail_", "")
            from handlers.prices import handle_price_detail
            await handle_price_detail(context, upload_id)
            return

        if callback == "orders":
            from handlers.orders import handle_list
            await handle_list(context)
            return

        if callback.startswith("request_open:"):
            request_id = callback.split(":", 1)[1]
            from handlers.orders import handle_detail
            await handle_detail(context, request_id)
            return

        if callback in ("upload_new_price", "update_price_upload"):
            from handlers.file_upload import handle_upload_new_price
            await handle_upload_new_price(context)
            return

        if callback == "show_products":
            from handlers.products import handle_show
            await handle_show(context)
            return

        if callback == "open_cabinet":
            await menu.handle_open_cabinet(context)
            return

        if callback in ("contact_manager", "help"):
            from handlers.contact import handle
            await handle(context)
            return

        if callback == "confirm_upload":
            from handlers.file_upload import handle_confirm
            await handle_confirm(context)
            return

        if callback == "reject_upload":
            from handlers.file_upload import handle_reject
            await handle_reject(context)
            return

        if callback == "process_pending_upload":
            from handlers.file_upload import handle_pending_upload
            await handle_pending_upload(context)
            return

        if callback == "cancel_registration":
            from handlers.registration import handle_cancel
            await handle_cancel(context)
            return

        if callback == "fill_manually":
            await msg.send_message(
                context.chat_id,
                "Ручное создание компании доступно в личном кабинете: "
                "https://avtosnab.testant.online/supplier/cabinet/companies",
            )
            return

    # ═══════════════════════════════════════════════════════════
    # 3. Обработка по состоянию
    # ═══════════════════════════════════════════════════════════

    # direct-register переводит backend-состояние в supplier_registered.
    # Если push-уведомление Telegram не дошло, первое действие пользователя
    # восстанавливает онбординг без повторной регистрации.
    if context.supplier_id and context.state == "supplier_registered" and state == UserState.START:
        set_state(context.chat_id, UserState.MAIN_MENU)
        from handlers.checklist import show_checklist
        await msg.send_message(context.chat_id, REGISTRATION_COMPLETE_TEXT)
        await show_checklist(context, company_created=True)
        return


    # Маршруты старого сценария, сохранённые после обновления онбординга.
    if context.supplier_id and context.state == "awaiting_catalog_search":
        if context.message_type == "text":
            from handlers.catalog import handle_catalog_query
            await handle_catalog_query(context)
        else:
            await msg.send_message(context.chat_id, scenario_hints[context.state])
        return

    if context.supplier_id and context.state == "awaiting_product_text":
        if context.message_type == "text":
            from handlers.catalog import handle_product_text
            await handle_product_text(context)
        else:
            await msg.send_message(context.chat_id, scenario_hints[context.state])
        return

    if context.supplier_id and context.state == "awaiting_product_price":
        if context.message_type == "text":
            from handlers.products import handle_new_price
            await handle_new_price(context)
        else:
            await msg.send_message(context.chat_id, scenario_hints[context.state])
        return

    if context.supplier_id and context.state == "awaiting_price_file" and context.route != "file_upload":
        await msg.send_message(context.chat_id, scenario_hints[context.state])
        return

    if context.supplier_id and context.state == "price_processing" and not context.is_callback_query:
        await msg.send_message(context.chat_id, scenario_hints[context.state])
        return

    if context.route == "deep_link_register":
        from handlers.registration import handle_deep_link
        await handle_deep_link(context)
        return

    if context.route == "file_upload":
        if not context.supplier_id:
            from handlers.registration import handle_unregistered_file_upload
            await handle_unregistered_file_upload(context)
            return
        supported_upload_mime = {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
            "application/csv",
            "application/vnd.ms-excel",
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/tiff",
            "image/bmp",
            "image/gif",
        }
        if context.mime_type not in supported_upload_mime:
            await msg.send_message(
                context.chat_id,
                "Сейчас я обрабатываю XLSX, CSV, PDF и фотографии товара.",
                reply_markup=main_menu_v2_kb(),
            )
            return
        from handlers.file_upload import handle
        await handle(context)
        return

    registered_routes = {
        "show_products": ("handlers.products", "handle_show"),
        "add_products": ("handlers.catalog", "ask_product_text"),
        "search_catalog": ("handlers.catalog", "ask_catalog_query"),
        "search_not_relevant": ("handlers.catalog", "handle_not_relevant"),
        "update_price_command": ("handlers.products", "handle_update_price"),
        "product_manage": ("handlers.products", "handle_manage"),
        "product_price": ("handlers.products", "ask_new_price"),
        "product_delete": ("handlers.products", "ask_delete"),
        "product_delete_confirm": ("handlers.products", "confirm_delete"),
        "callback_upload_new_price": ("handlers.file_upload", "handle_upload_new_price"),
        "open_cabinet": ("handlers.menu", "handle_open_cabinet"),
        "contact_manager": ("handlers.contact", "handle"),
        "greeting": ("handlers.menu", "handle_greeting"),
        "thanks": ("handlers.menu", "handle_thanks"),
        "question": ("handlers.fallback", "handle_question"),
        "wants_upload": ("handlers.fallback", "handle_wants_upload"),
        "unknown_text": ("handlers.fallback", "handle_unknown_text"),
        "unsupported_message": ("handlers.fallback", "handle_unsupported"),
        "unsupported_format": ("handlers.fallback", "handle_unsupported"),
    }
    unregistered_routes = {
        "supplier_main_menu": ("handlers.menu", "show_main_menu_unregistered"),
        "contact_manager": ("handlers.contact", "handle"),
        "greeting": ("handlers.menu", "handle_greeting"),
        "thanks": ("handlers.menu", "handle_thanks"),
        "question": ("handlers.fallback", "handle_question"),
        "wants_upload": ("handlers.fallback", "handle_wants_upload"),
        "unknown_text": ("handlers.fallback", "handle_unknown_text_unregistered"),
        "callback_upload_new_price": ("handlers.registration", "handle_upload_new_price_unregistered"),
        "show_products": ("handlers.registration", "handle_show_products_unregistered"),
        "add_products": ("handlers.registration", "handle_show_products_unregistered"),
        "search_catalog": ("handlers.catalog", "ask_catalog_query"),
        "search_not_relevant": ("handlers.catalog", "handle_not_relevant"),
        "open_cabinet": ("handlers.registration", "handle_open_cabinet_unregistered"),
        "unsupported_message": ("handlers.fallback", "handle_unsupported"),
        "unsupported_format": ("handlers.fallback", "handle_unsupported"),
    }

    route_target = (
        registered_routes.get(context.route)
        if context.supplier_id
        else unregistered_routes.get(context.route)
    )
    if route_target:
        import importlib
        module_name, function_name = route_target
        handler = getattr(importlib.import_module(module_name), function_name)
        await handler(context)
        return

    # ═══════════════════════════════════════════════════════════
    # 4. Fallback
    # ═══════════════════════════════════════════════════════════
    await msg.send_message(
        context.chat_id,
        "Я вас не понял. Используйте меню или команды:\n"
        "/start — начать работу\n"
        "/menu — главное меню\n"
        "/help — помощь\n"
        "/stop — остановить",
        reply_markup=main_menu_v2_kb(),
    )
