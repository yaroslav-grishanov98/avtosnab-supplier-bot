"""
Обработчик загрузки прайс-листа.
Полный цикл: скачивание → upload → чанки → AI → confirm/reject.
"""

import logging
from pathlib import Path

from api.backend import (
    create_text_price_upload,
    create_upload_file,
    get_pending_upload,
    get_upload_stats,
    mark_pending_upload_status,
    normalize_upload_in_backend,
    reject_upload,
)
from fsm import StateManager
from keyboards import main_menu, upload_result_kb
from models import UserContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from texts import PROCESSING_STARTED_TEXT, REJECTION_TEXT, UPLOAD_NEW_PRICE_TEXT

from handlers.common.messenger import telegram_messenger
from handlers.upload_monitor import start_upload_monitor
from utils import langfuse_track

msg = telegram_messenger
logger = logging.getLogger(__name__)

OCR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff", ".bmp", ".gif"}
CSV_EXTENSIONS = {".csv"}


async def handle(context: UserContext) -> None:
    """Полный цикл загрузки файла."""
    extension = Path(context.file_name or "").suffix.lower()
    if extension in OCR_EXTENSIONS:
        await _handle_ocr_document(context)
        return

    processing_msg = await msg.send_message(context.chat_id, PROCESSING_STARTED_TEXT)

    # FSM is useful for recovery, but a temporary backend failure must not lose the file.
    try:
        await StateManager.set(
            "telegram",
            context.telegram_user_id,
            context.chat_id,
            "price_processing",
            {"source": "file_upload", "file_id": context.file_id},
        )
    except Exception as exc:
        await langfuse_track(
            "price_upload_fsm_unavailable",
            {"chat_id": context.chat_id, "error": str(exc)},
            level="WARNING",
        )

    try:
        file = await msg.get_file(context.file_id)
        file_bytes = await file.download_as_bytearray()
        upload = await create_upload_file(
            file_bytes,
            context.file_name,
            context.supplier_id,
            context.message_id,
            context.mime_type,
        )
    except Exception as exc:
        await langfuse_track(
            "price_upload_transport_failed",
            {"chat_id": context.chat_id, "file_name": context.file_name, "error": str(exc)},
            level="ERROR",
        )
        await msg.edit_message_text(
            context.chat_id,
            processing_msg.message_id,
            "Не удалось передать файл на сервер. Попробуйте ещё раз.",
        )
        return
    context.upload_id = upload.get("id") if isinstance(upload, dict) else None
    if not context.upload_id:
        await msg.edit_message_text(
            context.chat_id,
            processing_msg.message_id,
            "Сервер не создал загрузку. Попробуйте ещё раз через несколько секунд.",
        )
        return

    # The backend uses the same asynchronous normalization pipeline as the web
    # cabinet. Do not run a second local AI pass over the same spreadsheet.
    try:
        await normalize_upload_in_backend(context.upload_id)
    except Exception as exc:
        await langfuse_track(
            "price_upload_normalize_start_failed",
            {"chat_id": context.chat_id, "upload_id": context.upload_id, "error": str(exc)},
            level="ERROR",
        )
        await msg.edit_message_text(
            context.chat_id,
            processing_msg.message_id,
            "Файл принят, но обработку не удалось запустить. Повторите попытку позже.",
        )
        return
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "price_processing",
        {
            "upload_id": context.upload_id,
            "supplier_id": context.supplier_id,
            "file_name": context.file_name,
            "source": "telegram",
        },
    )
    await msg.edit_message_text(
        context.chat_id,
        processing_msg.message_id,
        f"Файл «{context.file_name or 'прайс'}» принят ✅\n"
        "Обработка продолжается на сервере. Можете закрыть бот — я напишу, когда всё будет готово.",
    )
    start_upload_monitor(context, context.upload_id, _notify_upload_complete, _notify_upload_failed)


async def _handle_ocr_document(context: UserContext) -> None:
    """OCR photo/PDF and send recognized text to the common backend pipeline."""
    from api.ocr import recognize_document

    processing_msg = await msg.send_message(
        context.chat_id,
        "📄 Распознаю документ. Это может занять около минуты...",
    )
    file = await msg.get_file(context.file_id)
    file_bytes = await file.download_as_bytearray()
    result = await recognize_document(bytes(file_bytes), context.file_name or "document")

    if not result.get("success"):
        await msg.edit_message_text(
            context.chat_id,
            processing_msg.message_id,
            f"❌ Не удалось распознать документ: {result.get('error', 'Неизвестная ошибка')}",
        )
        return

    raw_text = result.get("raw_text", "")
    backend_result = await create_text_price_upload(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        context.message_id,
        raw_text,
    )
    upload = backend_result.get("upload") or {}
    upload_id = upload.get("id") or upload.get("upload_id")
    if not upload_id:
        await langfuse_track(
            "ocr_backend_upload_failed",
            {"chat_id": context.chat_id, "response_keys": list(backend_result.keys())},
            level="ERROR",
        )
        await msg.edit_message_text(
            context.chat_id,
            processing_msg.message_id,
            "❌ Текст распознан, но backend не принял товары. Попробуйте позже.",
        )
        return

    context.upload_id = upload_id
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "price_processing",
        {
            "upload_id": upload_id,
            "supplier_id": context.supplier_id,
            "file_name": context.file_name,
            "source": "google_ocr",
        },
    )
    await langfuse_track(
        "ocr_backend_upload_created",
        {"chat_id": context.chat_id, "upload_id": upload_id, "text_length": len(raw_text)},
    )
    await msg.edit_message_text(
        context.chat_id,
        processing_msg.message_id,
        "✅ Документ распознан и отправлен на обработку. "
        "Можете закрыть бот — я пришлю результат автоматически.",
    )
    start_upload_monitor(context, upload_id, _notify_upload_complete, _notify_upload_failed)

def resume_upload_monitor(context: UserContext, payload: dict) -> bool:
    """Resume monitoring saved in backend FSM after the user returns."""
    upload_id = str(payload.get("upload_id") or "")
    if not upload_id:
        return False
    context.upload_id = upload_id
    context.supplier_id = payload.get("supplier_id") or context.supplier_id
    context.file_name = payload.get("file_name") or context.file_name
    start_upload_monitor(context, upload_id, _notify_upload_complete, _notify_upload_failed)
    return True

async def _notify_upload_complete(context: UserContext, stats: dict) -> None:
    """Send a terminal success/partial-success notification."""
    await send_upload_result(context, stats)
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "price_processed",
        {"upload_id": context.upload_id, "supplier_id": context.supplier_id},
    )


async def _notify_upload_failed(context: UserContext, stats: dict) -> None:
    """Send a user-friendly terminal failure notification."""
    detail = stats.get("error_message") or stats.get("detail") or "Проверьте формат и содержимое файла."
    await msg.send_message(
        context.chat_id,
        f"Не удалось обработать файл «{context.file_name or 'прайс'}» ❌\n\n{detail}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📎 Загрузить повторно", callback_data="upload_new_price")],
                [InlineKeyboardButton("❓ Помощь", callback_data="contact_manager")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="supplier_main_menu")],
            ]
        ),
    )
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "idle",
        {"upload_id": context.upload_id, "supplier_id": context.supplier_id, "status": "failed"},
    )

async def send_upload_result(context: UserContext, stats: dict) -> None:
    """Show a final success or partial-success notification."""
    def count(key: str) -> int:
        try:
            return int(stats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    status = str(stats.get("status") or "completed").lower()
    needs_review = count("needs_review")
    failed_rows = count("items_failed") or count("errors_count")
    partial = status in {"completed_with_errors", "needs_review"} or needs_review > 0 or failed_rows > 0
    has_changes = count("products_created") > 0 or count("products_updated") > 0 or count("prices_created") > 0

    file_name = context.file_name or stats.get("source_file_name") or stats.get("file_name") or "прайс"
    lines = [
        "Файл обработан частично ⚠️" if partial else "Файл обработан ✅",
        f"📄 {file_name}",
        f"🏢 Загружено в каталог: {context.supplier_name or 'выбранная компания'}",
        "",
    ]
    lines.append(f"📊 Всего позиций: {count('items_total')}")
    if count("products_created") > 0:
        lines.append(f"🆕 Добавлено товаров: {count('products_created')}")
    if count("products_updated") > 0:
        lines.append(f"🔄 Обновлено товаров: {count('products_updated')}")
    if count("prices_created") > 0:
        lines.append(f"💰 Обновлено цен: {count('prices_created')}")
    if count("items_matched") > 0:
        lines.append(f"📌 Без изменений: {count('items_matched')}")
    if needs_review > 0:
        lines.append(f"⚠️ Требуют проверки: {needs_review}")
    if failed_rows > 0:
        lines.append(f"❌ Не обработано строк: {failed_rows}")
    if not has_changes and not partial:
        lines.extend(["", "Изменений нет — данные уже актуальны."])

    await msg.send_message(
        context.chat_id,
        "\n".join(lines),
        reply_markup=upload_result_kb(has_changes, needs_review=partial),
    )

async def handle_confirm(context: UserContext) -> None:
    """Подтверждение загрузки."""
    state = await StateManager.get("telegram", context.telegram_user_id, context.chat_id)
    payload = state.get("payload", {})
    upload_id = payload.get("upload_id")
    if not upload_id:
        await msg.send_message(context.chat_id, "Ошибка: не найден upload_id.")
        return
    stats = await get_upload_stats(upload_id)
    await send_summary(context, stats)
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "price_processed",
        {"upload_id": upload_id},
    )
    try:
        await msg.delete_message(context.chat_id, context.message_id)
    except Exception:
        logger.exception(
            "Не удалось удалить сообщение подтверждения chat_id=%s message_id=%s",
            context.chat_id,
            context.message_id,
        )


async def handle_reject(context: UserContext) -> None:
    """Отклонение загрузки."""
    state = await StateManager.get("telegram", context.telegram_user_id, context.chat_id)
    payload = state.get("payload", {})
    upload_id = payload.get("upload_id")
    if upload_id:
        await reject_upload(upload_id)
    await msg.send_message(context.chat_id, REJECTION_TEXT, reply_markup=upload_result_kb(False))
    await StateManager.set("telegram", context.telegram_user_id, context.chat_id, "idle")
    try:
        await msg.delete_message(context.chat_id, context.message_id)
    except Exception:
        logger.exception(
            "Не удалось удалить сообщение отклонения chat_id=%s message_id=%s",
            context.chat_id,
            context.message_id,
        )


async def handle_pending_upload(context: UserContext) -> None:
    """Обработка отложенной загрузки."""
    pending = await get_pending_upload(context.chat_id)
    if pending and pending.get("file_id"):
        await mark_pending_upload_status(
            pending["id"],
            "price_processing",
            context.supplier_id or "",
            {"source": "process_pending", "file_id": pending["file_id"], "file_name": pending["file_name"]},
        )
        context.file_id = pending["file_id"]
        context.file_name = pending["file_name"]
        context.mime_type = pending.get("mime_type")
        context.message_id = pending.get("message_id", "")
        return await handle(context)
    else:
        await msg.send_message(context.chat_id, "Нет отложенных загрузок.", reply_markup=main_menu())


async def handle_upload_new_price(context: UserContext) -> None:
    """Приглашение загрузить новый прайс."""
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        "awaiting_price_file",
        {"supplier_id": context.supplier_id},
    )
    await msg.send_message(
        context.chat_id,
        UPLOAD_NEW_PRICE_TEXT,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Отменить", callback_data="cancel_current_action")],
                [InlineKeyboardButton("Главное меню", callback_data="supplier_main_menu")],
            ]
        ),
    )


async def send_summary(context: UserContext, stats: dict) -> None:
    """Финальная статистика после подтверждения."""
    total = int(stats.get("items_total", 0))
    created = int(stats.get("products_created", 0))
    updated = int(stats.get("products_updated", 0))
    prices = int(stats.get("prices_created", 0))
    needs_review = int(stats.get("needs_review", 0))
    lines = ["Прайс загружен и подтверждён! ✅"]
    if total > 0:
        lines.extend(
            [
                "",
                f"📊 Всего позиций: {total}",
                f"🆕 Новых товаров: {created}",
                f"🔄 Обновлено: {updated}",
                f"💰 Новых цен: {prices}",
            ]
        )
        if needs_review > 0:
            lines.append(f"⚠️ Требуют проверки: {needs_review}")
    if context.supplier_id:
        lines.extend(["", f"📋 Ваши товары: https://avtosnab.testant.online/supplier/{context.supplier_id}/"])
    await msg.send_message(context.chat_id, "\n".join(lines))
