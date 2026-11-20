from datetime import datetime
from decimal import Decimal

STATUS_LABELS = {
    "submitted": "Ожидает ответа",
    "confirmed": "Подтверждена",
    "partially_confirmed": "Принята частично",
    "declined": "Отклонена",
    "completed": "Завершена",
}


def _format_datetime(value: str | None) -> str:
    if not value:
        return "не указан"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _format_quantity(value) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def format_request_list(requests: list[dict], *, count=None, offset=0) -> str:
    if not requests:
        return (
            "📥 Входящие заявки\n\nНа этой странице заявок нет." if offset else "📥 Входящие заявки\n\nЗаявок пока нет."
        )
    count = len(requests) if count is None else count
    return (
        f"📥 Входящие заявки\n\nВсего: {count} · показаны {offset + 1}–{offset + len(requests)}\n"
        "Выберите заявку, чтобы посмотреть позиции."
    )


def format_request_summary(request: dict) -> str:
    lines = [
        f"📄 Заявка {request['number']}",
        "",
        f"Покупатель: {request['buyer_name']}",
        *([f"Поставщик: {request['supplier_name']}"] if request.get("supplier_name") else []),
        f"Статус: {STATUS_LABELS.get(request.get('status'), request.get('status', 'Не указан'))}",
        f"Получена: {_format_datetime(request.get('created_at'))}",
    ]
    if request.get("delivery_address"):
        lines.append(f"Доставка: {request['delivery_address']}")
    reason = request.get("response_unavailable_reason")
    hint = {
        "chat_not_connected": "Чтобы ответить, подключите этот чат к уведомлениям выбранного поставщика через кабинет АвтоСнаб.",
        "request_closed": "Заявка закрыта. Ответить на неё уже нельзя.",
        "no_pending_items": "В заявке нет позиций, ожидающих ответа.",
    }.get(reason)
    if hint:
        lines.extend(["", hint])
    return "\n".join(lines)


def format_request_items(request: dict) -> str:
    lines = ["Позиции:"]
    for item in request.get("items", []):
        details = " · ".join(value for value in (item.get("brand"), item.get("sku")) if value)
        suffix = f" ({details})" if details else ""
        lines.append(
            f"{item['position']}. {item['name']}{suffix} — {_format_quantity(item['quantity'])} {item['unit']}"
        )
    if request.get("comment"):
        lines.extend(["", f"Комментарий: {request['comment']}"])
    return "\n".join(lines)


def format_request_detail(request: dict) -> str:
    return format_request_summary(request) + "\n\n" + format_request_items(request)
