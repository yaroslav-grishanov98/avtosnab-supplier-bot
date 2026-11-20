from datetime import datetime


def _format_datetime(value: str | None) -> str:
    if not value:
        return "не указан"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _format_quantity(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def format_request_list(requests: list[dict]) -> str:
    if not requests:
        return "📥 Входящие заявки\n\nНовых заявок пока нет."
    new_count = sum(1 for request in requests if request.get("status") == "new")
    return (
        "📥 Входящие заявки\n\n"
        f"Всего: {len(requests)} · новых: {new_count}\n"
        "Выберите заявку, чтобы посмотреть позиции."
    )


def format_request_detail(request: dict) -> str:
    lines = [
        f"📄 Заявка {request['number']}",
        "",
        f"Покупатель: {request['buyer_name']}",
        f"Получена: {_format_datetime(request.get('created_at'))}",
        f"Ответить до: {_format_datetime(request.get('deadline'))}",
    ]
    if request.get("delivery_address"):
        lines.append(f"Доставка: {request['delivery_address']}")
    lines.extend(["", "Позиции:"])
    for item in request.get("items", []):
        details = " · ".join(value for value in (item.get("brand"), item.get("sku")) if value)
        suffix = f" ({details})" if details else ""
        lines.append(
            f"{item['position']}. {item['name']}{suffix} — "
            f"{_format_quantity(item['quantity'])} {item['unit']}"
        )
    if request.get("comment"):
        lines.extend(["", f"Комментарий: {request['comment']}"])
    return "\n".join(lines)
