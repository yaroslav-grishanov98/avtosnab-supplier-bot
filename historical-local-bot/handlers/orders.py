"""Экраны входящих заявок поставщика."""

from api.orders import get_order_repository
from handlers.common.messenger import telegram_messenger
from keyboards.orders import request_detail_kb, request_list_kb
from models import UserContext
from texts.orders import format_request_detail, format_request_list


msg = telegram_messenger


async def handle_list(context: UserContext) -> None:
    repository = get_order_repository()
    requests = repository.list_requests(context.supplier_id)
    await msg.send_message(
        context.chat_id,
        format_request_list(requests),
        reply_markup=request_list_kb(requests),
    )


async def handle_detail(context: UserContext, request_id: str) -> None:
    repository = get_order_repository()
    request = repository.get_request(request_id, context.supplier_id)
    if request is None:
        await msg.send_message(
            context.chat_id,
            "Заявка не найдена или больше недоступна.",
            reply_markup=request_list_kb(repository.list_requests(context.supplier_id)),
        )
        return
    repository.mark_viewed(request_id, context.supplier_id)
    await msg.send_message(
        context.chat_id,
        format_request_detail(request),
        reply_markup=request_detail_kb(request_id),
    )
