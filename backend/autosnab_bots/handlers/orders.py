"""Incoming requests read from the company-scoped backend, never demo SQLite."""

from api.orders import OrderAPIError, error_text, get_request, list_requests
from keyboards.orders import request_detail_kb, request_list_kb
from models import UserContext
from texts.orders import (
    STATUS_LABELS,
    format_request_detail,
    format_request_items,
    format_request_list,
    format_request_summary,
)

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


async def handle_list(context: UserContext, offset=0) -> None:
    try:
        page = await list_requests(context, offset)
        if offset and not page["results"] and page["count"]:
            offset = ((page["count"] - 1) // page["limit"]) * page["limit"]
            page = await list_requests(context, offset)
    except OrderAPIError as exc:
        await msg.send_message(context.chat_id, error_text(exc.code), reply_markup=request_list_kb([], offset=offset))
        return
    await msg.send_message(
        context.chat_id,
        format_request_list(page["results"], count=page["count"], offset=page["offset"]),
        reply_markup=request_list_kb(page["results"], offset=page["offset"], count=page["count"], limit=page["limit"]),
    )


async def handle_detail(context: UserContext, request_id: str, offset=0) -> None:
    try:
        request = await get_request(context, request_id)
    except OrderAPIError as exc:
        await msg.send_message(context.chat_id, error_text(exc.code), reply_markup=request_list_kb([], offset=offset))
        return
    text = format_request_detail(request)
    # At most 3600 UTF-16 code units, including text with astral characters.
    if len(text) > 1800:
        # A single final summary owns the status and actions, so updating it
        # cannot leave a contradictory status in an earlier positions chunk.
        items = format_request_items(request)
        summary = format_request_summary(request)
        if len(summary) > 1800:
            # Preserve unusually long source names/addresses in the preceding
            # details while keeping the sole live status card within Telegram limits.
            items = "\n".join(line for line in summary.splitlines() if not line.startswith("Статус: ")) + "\n\n" + items
            summary = "\n".join(
                [
                    f"📄 Заявка {str(request['number'])[:120]}",
                    f"Покупатель: {str(request['buyer_name'])[:120]}…",
                    f"Поставщик: {str(request.get('supplier_name', ''))[:120]}…",
                    f"Статус: {STATUS_LABELS.get(request.get('status'), 'Не указан')}",
                    "Полные сведения и позиции заявки — в сообщениях выше.",
                ]
            )
        chunks = [items[index : index + 1800] for index in range(0, len(items), 1800)]
        chunks.append(summary)
    else:
        chunks = [text]
    for index, chunk in enumerate(chunks):
        await msg.send_message(
            context.chat_id,
            chunk,
            reply_markup=request_detail_kb(
                request_id,
                offset,
                can_respond=request.get("can_respond") is True,
                response_unavailable_reason=request.get("response_unavailable_reason", ""),
            )
            if index == len(chunks) - 1
            else None,
        )
