import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.models import EntityStatus, UserRole, VenueUser
from suppliers.models import SupplierUser

from .models import (
    NotificationActionLog,
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryReceipt,
    NotificationDestination,
    NotificationDestinationType,
    NotificationEvent,
    NotificationInboxDelivery,
    NotificationInboxEvent,
    NotificationInboxEventType,
    NotificationLinkToken,
    NotificationReceiptResult,
    NotificationRule,
    NotificationVenueScope,
)

logger = logging.getLogger(__name__)

BUYER_EVENTS = (
    NotificationEvent.ORDER_SUBMITTED,
    NotificationEvent.SUPPLIER_RESPONSE,
    NotificationEvent.DAILY_DELIVERY,
)
SUPPLIER_EVENTS = (
    NotificationEvent.SUPPLIER_NEW_REQUEST,
    NotificationEvent.SUPPLIER_REQUEST_CANCELLED,
    NotificationEvent.SUPPLIER_TENDER_DISTRIBUTION,
)


def record_venue_inbox_event(*, venue_id, event_type, payload, idempotency_key, object_type="", object_id=""):
    """Сохранить бизнес-событие заведения независимо от каналов доставки."""
    notification, _ = NotificationInboxEvent.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "venue_id": venue_id,
            "event_type": event_type,
            "payload": payload,
            "object_type": object_type,
            "object_id": str(object_id or ""),
        },
    )
    return notification


def link_inbox_event_deliveries(*, notification, deliveries):
    """Связать событие ленты с отправками Telegram и MAX без повторов."""
    NotificationInboxDelivery.objects.bulk_create(
        [
            NotificationInboxDelivery(notification=notification, delivery=delivery)
            for delivery in deliveries
            if delivery is not None
        ],
        ignore_conflicts=True,
    )


def accessible_venue_ids(user):
    if user.role == UserRole.ADMIN:
        from core.models import Venue

        return set(Venue.objects.filter(status=EntityStatus.ACTIVE).values_list("id", flat=True))
    ids = set(VenueUser.objects.filter(user=user, status=EntityStatus.ACTIVE).values_list("venue_id", flat=True))
    if user.venue_id:
        ids.add(user.venue_id)
    return ids


def accessible_supplier_ids(user):
    if user.role == UserRole.ADMIN:
        from suppliers.models import Supplier

        return set(Supplier.objects.filter(status=EntityStatus.ACTIVE).values_list("id", flat=True))
    return set(SupplierUser.objects.filter(user=user, status=EntityStatus.ACTIVE).values_list("supplier_id", flat=True))


def create_destination(*, user, values):
    """Создать групповой чат и правила уведомлений для пользователя."""
    from suppliers.models import Supplier

    audience = values["audience"]
    if audience == NotificationAudience.SUPPLIER:
        if values["supplier_id"] not in accessible_supplier_ids(user):
            raise ValidationError({"supplier_id": "У вас нет доступа к этой компании-поставщику."})
        supplier = Supplier.objects.get(pk=values["supplier_id"])
        venue_ids = []
    else:
        supplier = None
        venue_ids = set(values["venue_ids"])
        if not venue_ids.issubset(accessible_venue_ids(user)):
            raise ValidationError({"venue_ids": "Выбрано заведение, к которому у вас нет доступа."})

    try:
        with transaction.atomic():
            destination = NotificationDestination.objects.create(
                created_by=user,
                audience=audience,
                supplier=supplier,
                channel=values["channel"],
                destination_type=values["destination_type"],
                chat_id=values["chat_id"],
                title=values.get("title", ""),
            )
            if venue_ids:
                NotificationVenueScope.objects.bulk_create(
                    [NotificationVenueScope(destination=destination, venue_id=venue_id) for venue_id in venue_ids]
                )
            events = BUYER_EVENTS if audience == NotificationAudience.BUYER else SUPPLIER_EVENTS
            NotificationRule.objects.bulk_create(
                [NotificationRule(destination=destination, event=event) for event in events]
            )
            record_notification_action(destination=destination, action="connected", actor_name=user.full_name)
            return destination
    except IntegrityError as exc:
        raise ValidationError({"chat_id": "Этот чат уже подключён."}) from exc


def create_link_token(*, user, audience, channel, destination_type="personal", supplier_id=None):
    """Выдать временный одноразовый токен подключения бота."""
    if audience == NotificationAudience.SUPPLIER:
        if supplier_id not in accessible_supplier_ids(user):
            raise ValidationError({"supplier_id": "У вас нет доступа к этой компании-поставщику."})
    elif audience == NotificationAudience.BUYER:
        if supplier_id is not None:
            raise ValidationError({"supplier_id": "Для покупателя это поле не используется."})
        if not accessible_venue_ids(user):
            raise ValidationError("Сначала привяжите аккаунт к заведению.")
    else:
        raise ValidationError({"audience": "Выберите покупателя или поставщика."})

    ttl = int(getattr(settings, "NOTIFICATION_LINK_TTL_MINUTES", 60))
    return NotificationLinkToken.objects.create(
        created_by=user,
        audience=audience,
        supplier_id=supplier_id,
        channel=channel,
        destination_type=destination_type,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(minutes=ttl),
    )


@transaction.atomic
def record_notification_action(*, destination, action, actor_id="", actor_name="", details=None):
    """Сохранить действие, которое показывается во вкладке истории настроек."""
    return NotificationActionLog.objects.create(
        destination=destination,
        created_by=destination.created_by if destination else None,
        channel=destination.channel,
        destination_type=destination.destination_type,
        chat_id=destination.chat_id,
        action=action,
        actor_id=str(actor_id or "")[:255],
        actor_name=str(actor_name or "")[:255],
        details=details or {},
    )


@transaction.atomic
def connect_chat_from_bot(
    *, channel, chat_id, token, destination_type="personal", title="", user_id="", username="", display_name=""
):
    """Подключить чат Telegram/MAX по действующему одноразовому токену."""
    if (
        channel == NotificationChannel.TELEGRAM
        and destination_type == NotificationDestinationType.GROUP
        and (not str(chat_id).startswith("-") or not str(chat_id)[1:].isdigit())
    ):
        raise ValidationError({"chat_id": "Для группы Telegram укажите числовой ID, например -10028216382."})
    link = (
        NotificationLinkToken.objects.select_for_update()
        .filter(token=token, channel=channel, used_at__isnull=True, expires_at__gt=timezone.now())
        .select_related("created_by")
        .first()
    )
    if link is None:
        raise ValidationError({"token": "Ссылка устарела или уже использована."})
    if link.destination_type != destination_type:
        raise ValidationError({"destination_type": "Тип чата не совпадает с выданной ссылкой."})
    existing = NotificationDestination.objects.filter(channel=channel, chat_id=str(chat_id)).first()
    if existing is not None:
        if (
            existing.audience != link.audience
            or existing.supplier_id != link.supplier_id
            or existing.destination_type != destination_type
        ):
            raise ValidationError({"chat_id": "Этот чат уже подключён к другой компании или роли."})
        existing.is_active = True
        existing.title = title or existing.title
        existing.connected_user_id = str(user_id or existing.connected_user_id)[:255]
        existing.connected_username = str(username or existing.connected_username)[:255]
        existing.connected_user_name = str(display_name or existing.connected_user_name)[:255]
        existing.save(
            update_fields=[
                "is_active",
                "title",
                "connected_user_id",
                "connected_username",
                "connected_user_name",
                "updated_at",
            ]
        )
        link.used_at = timezone.now()
        link.save(update_fields=["used_at", "updated_at"])
        record_notification_action(
            destination=existing,
            action="reconnected",
            actor_id=user_id,
            actor_name=display_name or username,
            details={"username": username, "source": "bot"},
        )
        record_notification_action(
            destination=existing,
            action="test_delivery",
            actor_id=user_id,
            actor_name=display_name or username,
            details={"source": "bot", "kind": "connection_confirmation"},
        )
        existing._notification_reused = True
        return existing

    try:
        destination = NotificationDestination.objects.create(
            created_by=link.created_by,
            audience=link.audience,
            supplier_id=link.supplier_id,
            channel=channel,
            destination_type=destination_type,
            chat_id=chat_id,
            title=title,
            connected_user_id=str(user_id or "")[:255],
            connected_username=str(username or "")[:255],
            connected_user_name=str(display_name or "")[:255],
        )
    except IntegrityError as exc:
        raise ValidationError({"chat_id": "Этот чат уже подключён."}) from exc

    if link.audience == NotificationAudience.BUYER:
        from core.models import Venue

        venues = Venue.objects.filter(pk__in=accessible_venue_ids(link.created_by), status=EntityStatus.ACTIVE)
        NotificationVenueScope.objects.bulk_create(
            [NotificationVenueScope(destination=destination, venue=venue) for venue in venues]
        )
        events = BUYER_EVENTS
    else:
        events = SUPPLIER_EVENTS
    NotificationRule.objects.bulk_create([NotificationRule(destination=destination, event=event) for event in events])
    link.used_at = timezone.now()
    link.save(update_fields=["used_at", "updated_at"])
    record_notification_action(
        destination=destination,
        action="connected",
        actor_id=user_id,
        actor_name=display_name or username,
        details={"username": username, "source": "bot"},
    )
    record_notification_action(
        destination=destination,
        action="test_delivery",
        actor_id=user_id,
        actor_name=display_name or username,
        details={"source": "bot", "kind": "connection_confirmation"},
    )
    return destination


def update_destination_rules(*, destination, rows):
    """Обновить разрешённые события и время ежедневного уведомления."""
    allowed = set(BUYER_EVENTS if destination.audience == NotificationAudience.BUYER else SUPPLIER_EVENTS)
    submitted = {row["event"]: row for row in rows}
    if not set(submitted).issubset(allowed):
        raise ValidationError({"rules": "В настройках указано уведомление, недоступное для этого чата."})
    for event, row in submitted.items():
        rule, _ = NotificationRule.objects.get_or_create(destination=destination, event=event)
        if "is_enabled" in row:
            rule.is_enabled = row["is_enabled"]
        if "send_time" in row and event == NotificationEvent.DAILY_DELIVERY:
            rule.send_time = row["send_time"]
        rule.save()
    record_notification_action(destination=destination, action="settings_updated", details={"events": list(submitted)})


def enqueue_notification(*, destination, event, payload, idempotency_key):
    rule = NotificationRule.objects.filter(destination=destination, event=event, is_enabled=True).first()
    if rule is None:
        return None
    delivery, created = NotificationDelivery.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={"destination": destination, "event": event, "payload": payload},
    )
    if created:
        record_notification_action(
            destination=destination,
            action="delivery_queued",
            details={"event": event, "delivery_id": str(delivery.id)},
        )
        transaction.on_commit(lambda: kick_notification_delivery(delivery.id))
    return delivery


def kick_notification_delivery(delivery_id):
    from .tasks import dispatch_notification_delivery

    now = timezone.now()
    reserved = (
        NotificationDelivery.objects.filter(
            pk=delivery_id,
            status="pending",
            available_at__lte=now,
        )
        .filter(Q(queued_at__isnull=True) | Q(queued_at__lte=now - timedelta(minutes=5)))
        .update(queued_at=now)
    )
    if not reserved:
        return False
    try:
        dispatch_notification_delivery.delay(str(delivery_id))
    except Exception:
        NotificationDelivery.objects.filter(pk=delivery_id, status="pending", queued_at=now).update(queued_at=None)
        logger.exception("Could not enqueue notification delivery %s", delivery_id)
        return False
    return True


def enqueue_for_venue(*, venue_id, event, payload, idempotency_key, inbox_event=None):
    destinations = NotificationDestination.objects.filter(
        audience=NotificationAudience.BUYER,
        is_active=True,
        venue_scopes__venue_id=venue_id,
    ).distinct()
    deliveries = _enqueue_for_destinations(
        destination_ids=list(destinations.values_list("id", flat=True)),
        event=event,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if inbox_event is not None:
        link_inbox_event_deliveries(notification=inbox_event, deliveries=deliveries)
    return deliveries


def enqueue_for_supplier(*, supplier_id, event, payload, idempotency_key):
    destinations = NotificationDestination.objects.filter(
        audience=NotificationAudience.SUPPLIER,
        supplier_id=supplier_id,
        is_active=True,
    )
    return _enqueue_for_destinations(
        destination_ids=list(destinations.values_list("id", flat=True)),
        event=event,
        payload=payload,
        idempotency_key=idempotency_key,
    )


def _enqueue_for_destinations(*, destination_ids, event, payload, idempotency_key, payload_by_destination=None):
    if not destination_ids:
        return []

    enabled_destination_ids = set(
        NotificationRule.objects.filter(
            destination_id__in=destination_ids,
            event=event,
            is_enabled=True,
        ).values_list("destination_id", flat=True)
    )
    eligible_ids = [destination_id for destination_id in destination_ids if destination_id in enabled_destination_ids]
    if not eligible_ids:
        return [None] * len(destination_ids)

    keys_by_destination = {destination_id: f"{idempotency_key}:{destination_id}" for destination_id in eligible_ids}
    NotificationDelivery.objects.bulk_create(
        [
            NotificationDelivery(
                destination_id=destination_id,
                event=event,
                payload=payload_by_destination[destination_id] if payload_by_destination is not None else payload,
                idempotency_key=delivery_key,
            )
            for destination_id, delivery_key in keys_by_destination.items()
        ],
        ignore_conflicts=True,
    )
    rows = list(
        NotificationDelivery.objects.filter(idempotency_key__in=keys_by_destination.values()).values_list(
            "idempotency_key", "id"
        )
    )
    ids_by_key = dict(rows)
    delivery_ids = tuple(ids_by_key.values())
    if delivery_ids:
        transaction.on_commit(lambda ids=delivery_ids: kick_notification_deliveries(ids))
    deliveries_by_id = NotificationDelivery.objects.in_bulk(delivery_ids)
    deliveries_by_destination = {
        destination_id: deliveries_by_id.get(ids_by_key.get(keys_by_destination[destination_id]))
        for destination_id in eligible_ids
    }
    return [deliveries_by_destination.get(destination_id) for destination_id in destination_ids]


def kick_notification_deliveries(delivery_ids):
    for delivery_id in delivery_ids:
        kick_notification_delivery(delivery_id)


def enqueue_daily_delivery_checklists(*, today=None):
    """Сформировать и поставить в очередь ежедневные чек-листы поставок."""
    from core.models import OrderItem, OrderItemStatus

    today = today or timezone.localdate()
    destinations = (
        NotificationDestination.objects.filter(
            audience=NotificationAudience.BUYER,
            is_active=True,
            rules__event=NotificationEvent.DAILY_DELIVERY,
            rules__is_enabled=True,
        )
        .prefetch_related("venue_scopes__venue", "rules")
        .distinct()
    )
    due_destinations = []
    current_time = timezone.localtime().time().replace(second=0, microsecond=0)
    for destination in destinations:
        rules = {rule.event: rule for rule in destination.rules.all()}
        rule = rules.get(NotificationEvent.DAILY_DELIVERY)
        # Чек-лист отправляется только в настроенную минуту, а не при каждом
        # запуске Beat после неё. Иначе настройка времени 09:00 приводит к
        # немедленной отправке при первом запуске задачи в 14:00.
        if rule is None or rule.send_time != current_time:
            continue
        due_destinations.append((destination, {scope.venue_id for scope in destination.venue_scopes.all()}))

    venue_ids = {venue_id for _, destination_venue_ids in due_destinations for venue_id in destination_venue_ids}
    items = list(
        OrderItem.objects.filter(
            order__venue_id__in=venue_ids,
            status=OrderItemStatus.CONFIRMED,
            confirmed_delivery_date=today,
        )
        .select_related(
            "order",
            "order__venue",
            "order__delivery_address",
            "supplier_product__supplier",
            "off_system_supplier",
            "requested_unit",
            "subdivision",
            "supplier_request__supplier",
        )
        .order_by("order__venue__display_name", "order__order_number", "created_at")
    )

    payload_by_destination = {}
    for destination, destination_venue_ids in due_destinations:
        destination_items = [item for item in items if item.order.venue_id in destination_venue_ids]
        grouped = {}
        for item in destination_items:
            supplier = (
                item.supplier_request.supplier.display_name
                if item.supplier_request_id
                else item.supplier_product.supplier.display_name
                if item.supplier_product_id
                else item.off_system_supplier.display_name
                if item.off_system_supplier_id
                else "Поставщик"
            )
            key = str(item.supplier_request_id) if item.supplier_request_id else f"order:{item.order_id}:{supplier}"
            group = grouped.setdefault(
                key,
                {
                    "request_id": str(item.supplier_request_id) if item.supplier_request_id else None,
                    "request_number": item.supplier_request.number if item.supplier_request_id else None,
                    "venue": item.order.venue.display_name,
                    "order_number": item.order.order_number,
                    "supplier": supplier,
                    "items_count": 0,
                    "products": [],
                    "total_amount": Decimal("0"),
                },
            )
            group["items_count"] += 1
            quantity = item.confirmed_qty or item.requested_qty
            price = item.confirmed_price or item.applied_price
            group["products"].append(
                {
                    "name": item.requested_name,
                    "quantity": str(quantity),
                    "unit": item.requested_unit.code if item.requested_unit else "",
                    "subdivision": item.subdivision.name if item.subdivision_id else "Без подразделения",
                    "price": str(price) if price is not None else "",
                }
            )
            if price is not None:
                group["total_amount"] += quantity * price
            if item.order.delivery_address_id:
                address = item.order.delivery_address
                group["delivery_address"] = address.address or ", ".join(
                    part
                    for part in (
                        address.locality,
                        address.street,
                        address.apartment_office,
                    )
                    if part
                )
            group["comment"] = item.order.comment or ""
        for group in grouped.values():
            group["total_amount"] = str(group["total_amount"])
        sections_by_name = {}
        for group in grouped.values():
            request_id = group.get("request_id")
            if not request_id:
                continue
            name = group.get("supplier") or "Поставщик"
            section = sections_by_name.setdefault(
                name,
                {
                    "name": name,
                    "request_ids": set(),
                    "items_count": 0,
                },
            )
            section["request_ids"].add(request_id)
            section["items_count"] += group["items_count"]
        sections = [
            {
                "key": str(index),
                "name": section["name"],
                "request_ids": sorted(section["request_ids"]),
                "request_count": len(section["request_ids"]),
                "items_count": section["items_count"],
            }
            for index, section in enumerate(sections_by_name.values(), start=1)
        ]
        checklist_payload = {
            "kind": "daily_delivery",
            "date": today.isoformat(),
            "deliveries": list(grouped.values()),
        }
        if sections:
            checklist_payload["sections"] = sections
        payload_by_destination[destination.id] = checklist_payload

    inbox_events_by_venue = {}
    for venue_id in venue_ids:
        venue_items = [item for item in items if item.order.venue_id == venue_id]
        if not venue_items:
            continue
        inbox_events_by_venue[venue_id] = record_venue_inbox_event(
            venue_id=venue_id,
            event_type=NotificationInboxEventType.DAILY_DELIVERY,
            payload={
                "date": today.isoformat(),
                "deliveries_count": len({item.supplier_request_id or item.order_id for item in venue_items}),
                "items_count": len(venue_items),
                "suppliers": sorted(
                    {
                        item.supplier_request.supplier.display_name
                        if item.supplier_request_id
                        else item.supplier_product.supplier.display_name
                        if item.supplier_product_id
                        else item.off_system_supplier.display_name
                        if item.off_system_supplier_id
                        else "Поставщик"
                        for item in venue_items
                    }
                ),
            },
            idempotency_key=f"daily-delivery-inbox:{today.isoformat()}:{venue_id}",
            object_type="venue",
            object_id=venue_id,
        )

    deliveries = _enqueue_for_destinations(
        destination_ids=[destination.id for destination, _ in due_destinations],
        event=NotificationEvent.DAILY_DELIVERY,
        payload=None,
        payload_by_destination=payload_by_destination,
        # Keep a schema version in the key so a checklist generated by the
        # previous item-per-row payload is not reused after deployment.
        idempotency_key=f"daily-delivery:v4:{today.isoformat()}",
    )
    for (destination, destination_venue_ids), delivery in zip(due_destinations, deliveries, strict=False):
        for venue_id in destination_venue_ids:
            inbox_event = inbox_events_by_venue.get(venue_id)
            if inbox_event is not None:
                link_inbox_event_deliveries(notification=inbox_event, deliveries=[delivery])
    return sum(delivery is not None for delivery in deliveries)


def get_daily_delivery_review(*, channel, chat_id, delivery_date=None, scope="all"):
    """Получить заявки для последовательной проверки прихода в чате."""
    delivery_date = delivery_date or timezone.localdate()
    destination = NotificationDestination.objects.filter(
        audience=NotificationAudience.BUYER,
        channel=channel,
        chat_id=str(chat_id),
        is_active=True,
    ).first()
    if destination is None:
        raise ValidationError({"chat_id": "Этот чат не подключён к уведомлениям заведения."})

    notification = (
        NotificationDelivery.objects.filter(
            destination=destination,
            event=NotificationEvent.DAILY_DELIVERY,
            status="sent",
            payload__date=delivery_date.isoformat(),
        )
        .order_by("-created_at")
        .first()
    )
    if notification is None:
        raise ValidationError({"detail": "Для этой даты нет ежедневного чек-листа поставок."})

    payload = notification.payload or {}
    deliveries = [row for row in payload.get("deliveries", []) if row.get("request_id")]
    sections = payload.get("sections", [])
    if scope == "all":
        selected_request_ids = {row["request_id"] for row in deliveries}
        scope_name = "Все заявки"
    else:
        section = next((item for item in sections if str(item.get("key")) == str(scope)), None)
        if section is None:
            raise ValidationError({"scope": "Поставщик в чек-листе не найден."})
        selected_request_ids = set(section.get("request_ids", []))
        scope_name = section.get("name") or "Поставщик"
    selected = [row for row in deliveries if row.get("request_id") in selected_request_ids]
    if not selected:
        raise ValidationError({"detail": "В выбранной группе нет заявок для проверки."})
    return {
        "date": payload.get("date") or delivery_date.isoformat(),
        "scope": str(scope),
        "scope_name": scope_name,
        "deliveries": selected,
    }


@transaction.atomic
def confirm_delivery_receipts(
    *,
    channel,
    chat_id,
    delivery_date=None,
    supplier_request_id=None,
    result=NotificationReceiptResult.COMPLETE,
    comment="",
    telegram_user_id="",
    telegram_user_name="",
):
    """Сохранить полную поставку или поставку с расхождением."""
    from core.models import (
        OrderItemStatus,
        Receiving,
        ReceivingDocument,
        ReceivingStatus,
        SupplierOrderRequest,
    )

    if result not in NotificationReceiptResult.values:
        raise ValidationError({"result": "Укажите complete или discrepancy."})
    comment = str(comment or "").strip()[:1000]

    destination = (
        NotificationDestination.objects.select_for_update()
        .filter(
            audience=NotificationAudience.BUYER,
            channel=channel,
            chat_id=str(chat_id),
            is_active=True,
        )
        .first()
    )
    if destination is None:
        raise ValidationError({"chat_id": "Этот чат не подключён к уведомлениям заведения."})

    delivery_date = delivery_date or timezone.localdate()
    venue_ids = list(destination.venue_scopes.values_list("venue_id", flat=True))
    requests = SupplierOrderRequest.objects.filter(
        order__venue_id__in=venue_ids,
        items__status=OrderItemStatus.CONFIRMED,
        items__confirmed_delivery_date=delivery_date,
    ).distinct()
    if supplier_request_id is not None:
        requests = requests.filter(pk=supplier_request_id)
    requests = list(
        requests.select_related("order", "order__venue", "supplier").prefetch_related("items__requested_unit")
    )
    if not requests:
        raise ValidationError({"detail": "Для этой даты нет подтверждённых поставок."})

    created = 0
    for supplier_request in requests:
        receipt, was_created = NotificationDeliveryReceipt.objects.get_or_create(
            destination=destination,
            supplier_request=supplier_request,
            defaults={
                "result": result,
                "comment": comment,
                "received_at": timezone.now(),
                "telegram_user_id": str(telegram_user_id or ""),
                "telegram_user_name": str(telegram_user_name or "")[:255],
            },
        )
        if not was_created:
            continue
        created += 1
        receiving = Receiving.objects.create(
            venue=supplier_request.order.venue,
            order=supplier_request.order,
            supplier=supplier_request.supplier,
            status=(
                ReceivingStatus.ACCEPTED_WITH_DISCREPANCY
                if result == NotificationReceiptResult.DISCREPANCY
                else ReceivingStatus.ACCEPTED
            ),
            received_at=receipt.received_at,
            comment=comment,
            raw_payload={
                "source": "telegram_notification_checklist",
                "destination_id": str(destination.id),
                "supplier_request_id": str(supplier_request.id),
                "telegram_user_id": str(telegram_user_id or ""),
                "telegram_user_name": str(telegram_user_name or "")[:255],
            },
        )
        for item in supplier_request.items.all():
            if item.status != OrderItemStatus.CONFIRMED or item.confirmed_delivery_date != delivery_date:
                continue
            ReceivingDocument.objects.create(
                receiving=receiving,
                order_item=item,
                supplier_product=item.supplier_product,
                received_name=item.requested_name,
                received_qty=item.confirmed_qty or item.requested_qty,
                received_unit=item.requested_unit,
                received_price=item.confirmed_price or item.applied_price,
                status=(
                    ReceivingStatus.ACCEPTED_WITH_DISCREPANCY
                    if result == NotificationReceiptResult.DISCREPANCY
                    else ReceivingStatus.ACCEPTED
                ),
                discrepancy_reason=comment if result == NotificationReceiptResult.DISCREPANCY else "",
                external_payload={
                    "source": "notification_checklist",
                    "result": result,
                },
            )
        record_notification_action(
            destination=destination,
            action=(
                "delivery_discrepancy" if result == NotificationReceiptResult.DISCREPANCY else "delivery_confirmed"
            ),
            actor_id=telegram_user_id,
            actor_name=telegram_user_name,
            details={
                "supplier_request_id": str(supplier_request.id),
                "delivery_date": delivery_date.isoformat(),
                "result": result,
                "comment": comment,
            },
        )
    return {"confirmed_count": created, "already_confirmed": len(requests) - created}


def enqueue_pending_deliveries(*, limit=100):
    now = timezone.now()
    NotificationDelivery.objects.filter(status="processing", available_at__lte=now).update(
        status="pending", lease_token=None, queued_at=None, updated_at=now
    )
    pending_ids = list(
        NotificationDelivery.objects.filter(status="pending", available_at__lte=now, destination__is_active=True)
        .filter(Q(queued_at__isnull=True) | Q(queued_at__lte=now - timedelta(minutes=5)))
        .order_by("available_at", "created_at")
        .values_list("id", flat=True)[:limit]
    )
    return sum(kick_notification_delivery(delivery_id) for delivery_id in pending_ids)
