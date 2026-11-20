import re

from models import SUPPORTED_MIME, UserContext


def normalize(update) -> UserContext:
    """Полный аналог Normalize Incoming Telegram Message"""

    # --- Извлечение данных ---
    if update.callback_query:
        callback = update.callback_query
        msg = callback.message
        from_user = callback.from_user
    elif update.message:
        callback = None
        msg = update.message
        from_user = msg.from_user
    else:
        callback = None
        msg = update
        from_user = getattr(update, "from_user", None) or {}

    chat_id = str(getattr(msg.chat, "id", "") or getattr(msg, "chat_id", "") or getattr(from_user, "id", ""))
    external_user_id = str(getattr(from_user, "id", ""))
    username = getattr(from_user, "username", "") or ""
    first_name = getattr(from_user, "first_name", "") or ""
    last_name = getattr(from_user, "last_name", "") or ""
    message_id = str(getattr(msg, "message_id", "") or getattr(msg, "id", ""))
    text = str(getattr(msg, "text", "") or getattr(msg, "caption", "")).strip()
    callback_data = str(getattr(callback, "data", "")).strip() if callback else ""

    # --- Файлы ---
    document = getattr(msg, "document", None)
    photo = getattr(msg, "photo", None)
    voice = getattr(msg, "voice", None)
    audio = getattr(msg, "audio", None)
    video_note = getattr(msg, "video_note", None)
    video = getattr(msg, "video", None)
    sticker = getattr(msg, "sticker", None)
    animation = getattr(msg, "animation", None)
    contact = getattr(msg, "contact", None)
    location = getattr(msg, "location", None)

    file_id = getattr(document, "file_id", None) if document else None
    file_name = getattr(document, "file_name", None) if document else None
    mime_type = getattr(document, "mime_type", None) if document else None

    if not file_id and photo and len(photo) > 0:
        best_photo = photo[-1]
        file_id = getattr(best_photo, "file_id", None)
        file_name = f"photo_{message_id}.jpg"
        mime_type = "image/jpeg"

    if not file_id and (voice or audio):
        audio_file = voice or audio
        file_id = getattr(audio_file, "file_id", None)
        file_name = getattr(audio_file, "file_name", None) or f"voice_{message_id}.ogg"
        mime_type = getattr(audio_file, "mime_type", None) or "audio/ogg"

    # --- message_type ---
    if file_id and mime_type in SUPPORTED_MIME:
        message_type = "supported_file"
    elif file_id and document:
        message_type = "unsupported_file"
    elif voice or audio:
        message_type = "voice"
    elif sticker:
        message_type = "sticker"
    elif animation:
        message_type = "animation"
    elif video_note or video:
        message_type = "video"
    elif contact:
        message_type = "contact"
    elif location:
        message_type = "location"
    elif text:
        message_type = "text"
    else:
        message_type = "unknown"

    # --- route ---
    route = "unknown"
    start_match = re.match(r"^/start\s+(\S+)", text)
    start_payload = start_match.group(1) if start_match else ""

    if start_payload:
        route = "deep_link_register"
    elif callback_data in ("register_supplier", "start_registration"):
        route = "callback_register_supplier"
    elif callback_data == "cancel_registration":
        route = "callback_cancel_registration"
    elif callback_data == "cancel_current_action":
        route = "cancel_current_action"
    elif callback_data == "process_pending_upload":
        route = "callback_process_pending_upload"
    elif callback_data in ("upload_new_price", "update_price_upload"):
        route = "callback_upload_new_price"
    elif callback_data == "confirm_upload":
        route = "callback_confirm_upload"
    elif callback_data == "reject_upload":
        route = "callback_reject_upload"
    elif callback_data == "show_products":
        route = "show_products"
    elif callback_data == "add_products":
        route = "add_products"
    elif callback_data == "search_catalog":
        route = "search_catalog"
    elif callback_data.startswith("search_not_relevant:"):
        route = "search_not_relevant"
    elif callback_data.startswith("product_manage:"):
        route = "product_manage"
    elif callback_data.startswith("product_price:"):
        route = "product_price"
    elif callback_data.startswith("product_delete:"):
        route = "product_delete"
    elif callback_data.startswith("pdelok:"):
        route = "product_delete_confirm"
    elif callback_data in ("contact_manager", "help"):
        route = "contact_manager"
    elif callback_data in ("supplier_main_menu", "main_menu"):
        route = "supplier_main_menu"
    elif callback_data == "open_cabinet":
        route = "open_cabinet"
    elif message_type == "supported_file":
        route = "file_upload"
    elif message_type == "unsupported_file":
        route = "unsupported_format"
    elif message_type == "voice":
        route = "voice_message"
    elif message_type in ("sticker", "animation", "video", "contact", "location"):
        route = "unsupported_message"
    elif re.search(r"^(измени|обнови|поменяй|поставь)\b", text, re.IGNORECASE) and re.search(
        r"цен", text, re.IGNORECASE
    ):
        route = "update_price_command"
    elif re.search(r"^(найди|найти|поиск|ищу)\b", text, re.IGNORECASE):
        route = "search_catalog"
    elif re.search(r"^(добавить|добавь|внести)\s+(товар|товары|позици)\b", text, re.IGNORECASE):
        route = "add_products"
    elif re.search(r"^(покажи|список|товары|каталог|мои товары|мой прайс)\b", text, re.IGNORECASE):
        route = "show_products"
    elif re.match(r"^(меню|старт|start|menu|help|помощь|/?start|/?help|/?menu)$", text, re.IGNORECASE):
        route = "supplier_main_menu"
    elif re.search(
        r"^(привет|здравствуй|добрый|доброе|добрый день|добрый вечер|доброе утро|хай|hi|hello|йо)\b",
        text,
        re.IGNORECASE,
    ):
        route = "greeting"
    elif re.search(
        r"^(спасибо|благодар|пасиб|thx|thanks|ок|ладно|понял|хорошо|отлично|супер|круто|класс|ура)\b",
        text,
        re.IGNORECASE,
    ):
        route = "thanks"
    elif re.search(r"\?\s*$", text) or re.search(
        r"^(как|что|где|когда|зачем|почему|можно|а можно|скажи|подскажи|расскажи)\b", text, re.IGNORECASE
    ):
        route = "question"
    elif re.search(r"^(загрузи|отправ|прайс|файл|excel|эксель|xlsx|csv|pdf)\b", text, re.IGNORECASE):
        route = "wants_upload"
    elif text:
        route = "unknown_text"

    return UserContext(
        chat_id=chat_id,
        telegram_user_id=external_user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        message_id=message_id,
        text=text,
        callback_data=callback_data,
        callback_query_id=str(getattr(callback, "id", "")),
        is_callback_query=bool(callback),
        route=route,
        message_type=message_type,
        file_id=file_id,
        file_name=file_name,
        mime_type=mime_type,
        has_file=bool(file_id),
    )


def normalize_max(payload: dict) -> UserContext:
    """
    Преобразовать сообщение от MAX в UserContext.
    payload — словарь от MAX API.
    """
    chat_id = str(payload.get("chat_id", ""))
    external_user_id = str(payload.get("user_id", ""))
    text = str(payload.get("text", "")).strip()
    file_id = payload.get("file_id")
    file_name = payload.get("file_name")
    mime_type = payload.get("mime_type")
    has_file = bool(file_id)

    if payload.get("voice"):
        message_type = "voice"
    elif has_file:
        message_type = "supported_file" if mime_type in SUPPORTED_MIME else "unsupported_file"
    elif text:
        message_type = "text"
    else:
        message_type = "unknown"

    return UserContext(
        chat_id=chat_id,
        telegram_user_id=external_user_id,
        username=payload.get("username", ""),
        first_name=payload.get("first_name", ""),
        last_name=payload.get("last_name", ""),
        message_id=str(payload.get("message_id", "")),
        text=text,
        callback_data=payload.get("callback_data", ""),
        is_callback_query=bool(payload.get("callback_data")),
        route="max_message",
        message_type=message_type,
        file_id=file_id,
        file_name=file_name,
        mime_type=mime_type,
        has_file=has_file,
    )
