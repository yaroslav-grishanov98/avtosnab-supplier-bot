import json
import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import NotificationChannel, NotificationDelivery, NotificationDeliveryStatus
from .services import enqueue_daily_delivery_checklists, enqueue_pending_deliveries, record_notification_action

logger = logging.getLogger(__name__)


_CURRENCY_LABELS = {"RUB": "₽", "BYN": "BYN", "USD": "$", "EUR": "€"}
_TENDER_DISTRIBUTION_VENUE_LIMIT = 10


def _format_notification_number(value, *, group_thousands=False):
    """Отформатировать число без лишних нулей после запятой."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)

    integer, _, fraction = format(number, "f").partition(".")
    fraction = fraction.rstrip("0")
    if group_thousands:
        sign = "-" if integer.startswith("-") else ""
        digits = integer.lstrip("-")
        integer = sign + f"{int(digits):,}".replace(",", " ")
    return f"{integer},{fraction}" if fraction else integer


def _format_notification_unit(value):
    """Сократить единицу измерения до понятной подписи в сообщении."""
    unit = str(value or "").strip()
    normalized = unit.casefold().replace("ё", "е")
    aliases = {
        "килограмм": "кг",
        "килограмма": "кг",
        "килограммов": "кг",
        "kg": "кг",
        "грамм": "г",
        "грамма": "г",
        "граммов": "г",
        "г": "г",
        "литр": "л",
        "литра": "л",
        "литров": "л",
        "l": "л",
        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитров": "мл",
        "ml": "мл",
        "штука": "шт",
        "штуки": "шт",
        "штук": "шт",
        "piece": "шт",
        "pcs": "шт",
    }
    return aliases.get(normalized, unit)


def _format_delivery_date(value):
    """Показать дату поставки в кратком русском формате."""
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{parsed.day} {months[parsed.month - 1]}"


def _supplier_orders_url(payload):
    """Сформировать ссылку поставщика на заявку."""
    base_url = getattr(
        settings,
        "SUPPLIER_ORDERS_URL",
        "https://avtosnab.testant.online/supplier/cabinet/companies",
    ).rstrip("/")
    supplier_id = payload.get("supplier_id")
    if not supplier_id:
        return base_url
    query = urlencode({"search": payload.get("order_number", "")})
    return f"{base_url}/{supplier_id}/orders?{query}"


def _open_request_keyboard(payload):
    """Создать клавиатуру с кнопкой открытия заявки."""
    return [[{"text": "Открыть заявку", "url": _supplier_orders_url(payload)}]]


def _supplier_request_mini_app_url(payload, destination):
    """Сформировать ссылку Telegram Mini App для заявки."""
    if destination.channel != NotificationChannel.TELEGRAM:
        return None
    from .telegram_miniapp import create_supplier_request_launch_token

    token = create_supplier_request_launch_token(
        request_id=payload["request_id"],
        supplier_id=payload["supplier_id"],
        chat_id=destination.chat_id,
    )
    link_prefix = getattr(settings, "NOTIFICATION_TELEGRAM_MINI_APP_LINK", "").strip()
    if not link_prefix:
        bot_username = getattr(settings, "NOTIFICATION_TELEGRAM_MINI_APP_BOT_USERNAME", "TEST_AUTOSNAB_BOT")
        link_prefix = f"https://t.me/{bot_username.lstrip('@')}?startapp="
    return f"{link_prefix}{token}"


def _render_tender_distribution(payload):
    """Tender ТЗ §32-33 (catalog docs/adr/0013): the product is chosen for
    venues — never «you won the tender» or «order confirmed»."""
    venues = payload.get("venues") or []
    count = len(venues)
    product = payload.get("product_name", "")
    price = _format_notification_number(payload.get("price"), group_thousands=True)
    currency = _CURRENCY_LABELS.get(payload.get("currency") or "RUB", payload.get("currency"))
    if count == 1:
        lines = [
            "Ваш товар выбран для заведения",
            f"Ваш товар «{product}» добавлен в справочник {venues[0]}.",
            f"Цена {price} {currency} зафиксирована для этого заведения.",
        ]
    else:
        noun = "заведения" if count % 10 == 1 and count % 100 != 11 else "заведений"
        shown = ", ".join(venues[:_TENDER_DISTRIBUTION_VENUE_LIMIT])
        if count > _TENDER_DISTRIBUTION_VENUE_LIMIT:
            shown += f" и ещё {count - _TENDER_DISTRIBUTION_VENUE_LIMIT}"
        lines = [
            f"Ваш товар выбран для {count} {noun}",
            f"Ваш товар «{product}» добавлен в справочник заведений: {shown}.",
            f"Цена {price} {currency} зафиксирована для этих заведений.",
        ]
    lines.append(f"Тендер №{payload.get('tender_number')} «{payload.get('tender_name')}».")
    lines.append("Это не является заказом.")
    return "\n".join(lines)


def render_message(delivery):
    """Собрать текст и кнопки уведомления по типу события."""
    payload = delivery.payload
    event = delivery.event
    if event == "daily_delivery":
        deliveries = payload.get("deliveries", [])
        if not deliveries:
            return "На сегодня поставок нет.", None
        venues = list(dict.fromkeys(row.get("venue", "Заведение") for row in deliveries))
        date_label = _format_delivery_date(payload.get("date", ""))
        lines = [
            "Сегодня ожидаются поставки",
            "Заведение: " + ", ".join(venues),
            f"Дата: {date_label}",
            "",
            "Нажмите кнопку, чтобы открыть карточки и подтвердить получение заявок.",
        ]
        sections = payload.get("sections") or []
        if not sections:
            fallback = {}
            for row in deliveries:
                request_id = row.get("request_id")
                if not request_id:
                    continue
                name = row.get("supplier") or "Поставщик"
                fallback.setdefault(name, set()).add(request_id)
            sections = [
                {"key": str(index), "name": name, "request_ids": sorted(request_ids)}
                for index, (name, request_ids) in enumerate(fallback.items(), start=1)
            ]
        buttons = [[{"text": "✅ Подтвердить ВСЕ ЗАЯВКИ", "callback_data": f"dc:{payload.get('date')}:all"}]]
        for section in sections:
            request_count = section.get("request_count") or len(section.get("request_ids", []))
            buttons.append(
                [
                    {
                        "text": f"{section.get('name', 'Подразделение')} — {request_count} · Подтвердить получение",
                        "callback_data": f"dc:{payload.get('date')}:{section.get('key')}",
                    }
                ]
            )
        return "\n".join(lines), buttons

    venue = payload.get("venue", "Заведение")
    order_number = payload.get("order_number", "")
    if event == "supplier_new_request":
        lines = [f"Новая заявка №{payload.get('request_number') or order_number}", venue]
        if order_number and payload.get("request_number"):
            lines.append(f"Заказ №{order_number}")
        if payload.get("delivery_address"):
            lines.append(f"Адрес доставки: {payload['delivery_address']}")
        if payload.get("total_amount"):
            amount = _format_notification_number(payload["total_amount"], group_thousands=True)
            currency = payload.get("currency") or "RUB"
            currency_label = _CURRENCY_LABELS.get(currency, currency)
            lines.append(f"Сумма: {amount} {currency_label}")
            minimum = payload.get("minimum_order_amount")
            if minimum not in (None, "") and Decimal(str(payload["total_amount"])) < Decimal(str(minimum)):
                shortfall = Decimal(str(minimum)) - Decimal(str(payload["total_amount"]))
                minimum_label = _format_notification_number(minimum, group_thousands=True)
                shortfall_label = _format_notification_number(shortfall, group_thousands=True)
                lines.append(
                    f"⚠️ Не набрана минимальная сумма {minimum_label} {currency_label}: "
                    f"не хватает {shortfall_label} {currency_label}."
                )
        grouped = {}
        for item in payload.get("items", []):
            grouped.setdefault(item.get("subdivision") or "Без подразделения", []).append(item)
        for subdivision, items in grouped.items():
            lines.append(f"\n{subdivision}")
            lines.extend(
                f"• {item['name']} — {_format_notification_number(item['quantity'])} "
                f"{_format_notification_unit(item.get('unit', ''))}".rstrip()
                for item in items
            )
        if payload.get("buyer_comment"):
            lines.append(f"\nКомментарий заведения: {payload['buyer_comment']}")
        callback_prefix = f"supplier_action:{payload['request_id']}"
        partial_button = {"text": "Принять частично", "callback_data": f"{callback_prefix}:partial"}
        miniapp_url = _supplier_request_mini_app_url(payload, delivery.destination)
        if miniapp_url:
            partial_button = {"text": "Принять частично", "url": miniapp_url}
        return "\n".join(lines), [
            [{"text": "Принять", "callback_data": f"{callback_prefix}:accept"}],
            [partial_button],
            [{"text": "Отказать", "callback_data": f"{callback_prefix}:decline"}],
            [{"text": "Открыть заявку", "url": miniapp_url or _supplier_orders_url(payload)}],
        ]
    if event == "supplier_tender_distribution":
        return _render_tender_distribution(payload), None
    if event == "supplier_request_cancelled":
        request_number = payload.get("request_number") or order_number
        lines = [f"Заявка №{request_number} отменена заведением.", venue]
        cancelled_items = payload.get("cancelled_items", [])
        if cancelled_items:
            lines.append("Отменены позиции: " + ", ".join(cancelled_items))
        return "\n".join(lines), None
    if event == "supplier_response":
        lines = [
            f"Ответ поставщика по заявке №{payload.get('request_number') or order_number}",
            payload.get("supplier", "Поставщик"),
            payload.get("status_label", "обновлён"),
            venue,
        ]
        dates = payload.get("delivery_dates", [])
        if dates:
            lines.append("Дата поставки: " + ", ".join(dates))
        elif payload.get("delivery_date_unknown"):
            lines.append("Дата поставки: пока не определена")
        if payload.get("items"):
            lines.append("\nПозиции:")
            lines.extend(
                f"• {item['name']} — {_format_notification_number(item['quantity'])} "
                f"{_format_notification_unit(item.get('unit', ''))}".rstrip()
                for item in payload["items"]
            )
        if payload.get("comment"):
            lines.append(f"\nКомментарий поставщика: {payload['comment']}")
        return "\n".join(lines), None
    if event == "order_submitted":
        lines = [f"Заказ {order_number} отправлен."]
        lines.extend(
            f"{supplier}: " + ", ".join(item["name"] for item in items)
            for supplier, items in payload.get("suppliers", {}).items()
        )
        return "\n".join(lines), None
    return str(payload.get("text", "Уведомление АвтоСнаб")), None


def _split_notification_text(text, limit=3800):
    """Разделить длинное сообщение по строкам в пределах лимита провайдера."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def send_provider_message(delivery, text, keyboard):
    """Отправить уведомление в Telegram или MAX и вернуть ID сообщения."""
    channel = delivery.destination.channel
    if channel == NotificationChannel.TELEGRAM:
        token = settings.NOTIFICATION_TELEGRAM_BOT_TOKEN
        if not token:
            raise RuntimeError("NOTIFICATION_TELEGRAM_BOT_TOKEN is not configured")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
    else:
        token = settings.NOTIFICATION_MAX_BOT_TOKEN
        if not token:
            raise RuntimeError("NOTIFICATION_MAX_BOT_TOKEN is not configured")
        url = f"{settings.NOTIFICATION_MAX_API_BASE}/messages?chat_id={delivery.destination.chat_id}"

    headers = {"Content-Type": "application/json"}
    if channel == NotificationChannel.MAX:
        headers["Authorization"] = token
    last_message_id = ""
    chunks = _split_notification_text(text)
    for index, chunk in enumerate(chunks):
        chunk_keyboard = keyboard if index == len(chunks) - 1 else None
        if channel == NotificationChannel.TELEGRAM:
            body = {"chat_id": delivery.destination.chat_id, "text": chunk}
            if chunk_keyboard:
                body["reply_markup"] = {"inline_keyboard": chunk_keyboard}
        else:
            body = {"text": chunk}
            if chunk_keyboard:
                body["attachments"] = [
                    {
                        "type": "inline_keyboard",
                        "payload": {
                            "buttons": [
                                [
                                    {"type": "callback", "text": button["text"], "payload": button["callback_data"]}
                                    for button in row
                                ]
                                for row in chunk_keyboard
                            ]
                        },
                    }
                ]
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=settings.NOTIFICATION_HTTP_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"{channel} delivery failed: {exc}") from exc

        if channel == NotificationChannel.TELEGRAM:
            if not result.get("ok"):
                raise RuntimeError(f"Telegram rejected notification: {result.get('description', 'unknown error')}")
            last_message_id = str(result.get("result", {}).get("message_id", ""))
        else:
            last_message_id = str(result.get("message", {}).get("body", {}).get("mid", ""))
    return last_message_id


def update_provider_message(delivery, text, keyboard):
    """Обновить ранее отправленное сообщение и его кнопки."""
    channel = delivery.destination.channel
    if not delivery.provider_message_id:
        return
    if channel == NotificationChannel.TELEGRAM:
        token = settings.NOTIFICATION_TELEGRAM_BOT_TOKEN
        if not token:
            return
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        body = {
            "chat_id": delivery.destination.chat_id,
            "message_id": delivery.provider_message_id,
            "text": text,
            "reply_markup": {"inline_keyboard": keyboard or []},
        }
        headers = {"Content-Type": "application/json"}
    else:
        token = settings.NOTIFICATION_MAX_BOT_TOKEN
        if not token:
            return
        url = f"{settings.NOTIFICATION_MAX_API_BASE}/messages?message_id={delivery.provider_message_id}"
        buttons = [
            [
                (
                    {"type": "link", "text": button["text"], "url": button["url"]}
                    if button.get("url")
                    else {"type": "callback", "text": button["text"], "payload": button["callback_data"]}
                )
                for button in row
            ]
            for row in (keyboard or [])
        ]
        body = {
            "text": text,
            "attachments": ([{"type": "inline_keyboard", "payload": {"buttons": buttons}}] if buttons else []),
        }
        headers = {"Authorization": token, "Content-Type": "application/json"}
    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST" if channel == NotificationChannel.TELEGRAM else "PUT",
    )
    try:
        with urlopen(request, timeout=settings.NOTIFICATION_HTTP_TIMEOUT_SECONDS):
            return
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not update {channel} notification: {exc}") from exc


@shared_task(name="notifications.dispatch_delivery")
def dispatch_notification_delivery(delivery_id):
    """Доставить уведомление с арендой записи и повторными попытками."""
    lease_token = uuid.uuid4()
    with transaction.atomic():
        delivery = (
            NotificationDelivery.objects.select_for_update()
            .select_related("destination")
            .filter(pk=delivery_id, status=NotificationDeliveryStatus.PENDING)
            .first()
        )
        if delivery is None or not delivery.destination.is_active:
            return
        delivery.attempts += 1
        delivery.status = NotificationDeliveryStatus.PROCESSING
        delivery.available_at = timezone.now() + timedelta(minutes=5)
        delivery.queued_at = None
        delivery.lease_token = lease_token
        delivery.save(update_fields=["attempts", "status", "available_at", "queued_at", "lease_token", "updated_at"])

    try:
        text, keyboard = render_message(delivery)
        if settings.NOTIFICATION_DELIVERY_DRY_RUN:
            provider_message_id = "dry-run"
        else:
            provider_message_id = send_provider_message(delivery, text, keyboard)
    except Exception as exc:
        error = str(exc)[:1000]
        if delivery.attempts >= 6:
            status = NotificationDeliveryStatus.FAILED
            available_at = delivery.available_at
        else:
            status = NotificationDeliveryStatus.PENDING
            available_at = timezone.now() + timedelta(seconds=min(60 * (2 ** (delivery.attempts - 1)), 1800))
        NotificationDelivery.objects.filter(
            pk=delivery.pk,
            status=NotificationDeliveryStatus.PROCESSING,
            lease_token=lease_token,
        ).update(
            status=status,
            available_at=available_at,
            last_error=error,
            lease_token=None,
            updated_at=timezone.now(),
        )
        record_notification_action(
            destination=delivery.destination,
            action="delivery_failed",
            details={
                "delivery_id": str(delivery.id),
                "attempt": delivery.attempts,
                "retry_scheduled": status == NotificationDeliveryStatus.PENDING,
                "error": error,
            },
        )
        logger.warning("Notification delivery %s failed: %s", delivery.id, exc)
        return

    sent_at = timezone.now()
    updated = NotificationDelivery.objects.filter(
        pk=delivery.pk,
        status=NotificationDeliveryStatus.PROCESSING,
        lease_token=lease_token,
    ).update(
        status=NotificationDeliveryStatus.SENT,
        sent_at=sent_at,
        provider_message_id=provider_message_id,
        last_error="",
        lease_token=None,
        updated_at=sent_at,
    )
    if not updated:
        logger.warning("Notification delivery %s completed after its lease expired", delivery.id)
        return
    record_notification_action(
        destination=delivery.destination,
        action="delivery_sent",
        details={"delivery_id": str(delivery.id), "attempt": delivery.attempts},
    )


@shared_task(name="notifications.enqueue_daily_delivery_checklists")
def enqueue_daily_delivery_checklists_task():
    """Поставить в очередь ежедневные чек-листы поставок."""
    return enqueue_daily_delivery_checklists()


@shared_task(name="notifications.enqueue_pending_deliveries")
def enqueue_pending_deliveries_task():
    """Поставить в очередь ожидающие доставки уведомлений."""
    return enqueue_pending_deliveries()


@shared_task(name="notifications.remove_supplier_action_buttons")
def remove_supplier_action_buttons(supplier_request_id, action_label):
    """Обновить сообщения поставщика после его ответа по заявке."""
    from core.models import OrderItemStatus, SupplierOrderRequest

    from .models import NotificationDelivery, NotificationSupplierAction

    supplier_request = (
        SupplierOrderRequest.objects.select_related("order", "supplier").filter(pk=supplier_request_id).first()
    )
    action = NotificationSupplierAction.objects.filter(supplier_request_id=supplier_request_id).first()
    if supplier_request is None or action is None:
        return
    response = action.response or {}
    status_labels = {
        "confirmed": "Поставщик подтвердил заявку",
        "partially_confirmed": "Поставщик принял часть заявки",
        "declined": "Поставщик отказался от заявки",
    }
    lines = [
        f"Заявка №{supplier_request.number}",
        status_labels.get(supplier_request.status, f"Заявка {action_label}"),
        supplier_request.order.venue.display_name,
    ]
    delivery_date = response.get("confirmed_delivery_date")
    if delivery_date:
        lines.append(f"Дата поставки: {delivery_date}")
    elif response.get("delivery_date_unknown"):
        lines.append("Дата поставки: пока не определена")
    lines.append("\nПозиции:")
    items = supplier_request.items.select_related("requested_unit").order_by("created_at", "id")
    for item in items:
        unit = f" {item.requested_unit.name}" if item.requested_unit_id else ""
        if item.status == OrderItemStatus.CONFIRMED:
            confirmed_qty = _format_notification_number(item.confirmed_qty)
            lines.append(f"• {item.requested_name} — {confirmed_qty}{unit}")
        elif item.status == OrderItemStatus.CANCELLED_BY_VENUE:
            lines.append(f"• {item.requested_name} — отменено заведением")
        else:
            lines.append(f"• {item.requested_name} — не поставляется")
    if response.get("comment"):
        lines.append(f"\nКомментарий поставщика: {response['comment']}")
    if response.get("responded_by"):
        lines.append(f"Ответил: {response['responded_by']}")
    payload = {
        "supplier_id": str(supplier_request.supplier_id),
        "order_number": supplier_request.order.order_number,
    }
    deliveries = NotificationDelivery.objects.filter(
        event="supplier_new_request",
        payload__request_id=str(supplier_request_id),
        status=NotificationDeliveryStatus.SENT,
    ).select_related("destination")
    for delivery in deliveries:
        update_provider_message(delivery, "\n".join(lines), _open_request_keyboard(payload))


@shared_task(name="notifications.update_cancelled_supplier_messages")
def update_cancelled_supplier_messages(supplier_request_id):
    """Обновить сообщения поставщика после отмены позиций заведением."""
    from core.models import OrderItemStatus, SupplierOrderRequest

    from .models import NotificationDelivery

    supplier_request = (
        SupplierOrderRequest.objects.select_related("order", "order__venue").filter(pk=supplier_request_id).first()
    )
    if supplier_request is None:
        return
    cancelled_names = list(
        supplier_request.items.filter(status=OrderItemStatus.CANCELLED_BY_VENUE).values_list(
            "requested_name", flat=True
        )
    )
    has_pending = supplier_request.items.filter(status=OrderItemStatus.SUBMITTED).exists()
    original_deliveries = NotificationDelivery.objects.filter(
        event="supplier_new_request",
        payload__request_id=str(supplier_request_id),
        status=NotificationDeliveryStatus.SENT,
    ).select_related("destination")
    for delivery in original_deliveries:
        text, keyboard = render_message(SimpleNamespace(event="supplier_new_request", payload=delivery.payload))
        if cancelled_names:
            text += "\n\nЗаведение отменило позиции: " + ", ".join(cancelled_names)
        if not has_pending:
            text = (
                f"Заявка №{supplier_request.number} отменена заведением.\n{supplier_request.order.venue.display_name}"
            )
            keyboard = _open_request_keyboard(delivery.payload)
        update_provider_message(delivery, text, keyboard)


def kick_update_cancelled_supplier_messages(supplier_request_id):
    """Безопасно поставить обновление отменённых позиций в очередь."""
    try:
        update_cancelled_supplier_messages.delay(supplier_request_id)
    except Exception:
        logger.exception("Could not enqueue cancelled supplier message update for %s", supplier_request_id)


def kick_remove_supplier_action_buttons(supplier_request_id, action_label):
    """Безопасно поставить обновление ответа поставщика в очередь."""
    try:
        remove_supplier_action_buttons.delay(supplier_request_id, action_label)
    except Exception:
        logger.exception("Could not enqueue action button removal for %s", supplier_request_id)
