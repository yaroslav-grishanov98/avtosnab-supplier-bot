"""Account/company events shared by the cabinet and supplier bot."""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import EntityStatus
from notifications.models import (
    NotificationAudience,
    NotificationChannel,
    NotificationDestination,
    NotificationDestinationType,
    NotificationRule,
)
from notifications.services import SUPPLIER_EVENTS
from suppliers.models import BotUserBinding, BotUserState, SourceChannel

logger = logging.getLogger(__name__)


def ensure_supplier_notification_destination(*, user, supplier, binding) -> NotificationDestination | None:
    """Use the supplier bot's personal chat as its notification destination."""
    destination = (
        NotificationDestination.objects.select_for_update()
        .filter(channel=NotificationChannel.TELEGRAM, chat_id=binding.chat_id, is_active=True)
        .first()
    )
    if destination is not None and (
        destination.audience != NotificationAudience.SUPPLIER or destination.created_by_id != user.id
    ):
        logger.warning(
            "supplier_bot.notification_destination_conflict chat_id=%s destination_id=%s audience=%s owner_id=%s",
            binding.chat_id,
            destination.id,
            destination.audience,
            destination.created_by_id,
        )
        return None

    values = {
        "created_by": user,
        "audience": NotificationAudience.SUPPLIER,
        "supplier": supplier,
        "channel": NotificationChannel.TELEGRAM,
        "destination_type": NotificationDestinationType.PERSONAL,
        "chat_id": binding.chat_id,
        "connected_user_id": binding.external_user_id or "",
        "connected_username": binding.username or "",
        "is_active": True,
    }
    if destination is None:
        destination = NotificationDestination.objects.create(**values)
    else:
        for field, value in values.items():
            setattr(destination, field, value)
        destination.save(update_fields=[*values.keys(), "updated_at"])

    for event in SUPPLIER_EVENTS:
        NotificationRule.objects.get_or_create(
            destination=destination,
            event=event,
            defaults={"is_enabled": True},
        )
    return destination


def notify_linked_supplier_chats(*, supplier, text: str) -> int:
    """Send an event to every active Telegram chat linked to a supplier."""
    chat_ids = list(
        BotUserBinding.objects.filter(
            supplier=supplier,
            status=EntityStatus.ACTIVE,
            channel=SourceChannel.TELEGRAM,
        )
        .exclude(chat_id="")
        .values_list("chat_id", flat=True)
        .distinct()
    )

    for chat_id in chat_ids:

        def send_notification(chat_id=chat_id):
            sent, error = send_supplier_bot_message(chat_id=chat_id, text=text)
            if not sent:
                logger.warning(
                    "supplier_bot.event.notify_failed chat_id=%s supplier_id=%s error=%s",
                    chat_id,
                    supplier.id,
                    error,
                )

        transaction.on_commit(send_notification)

    return len(chat_ids)


def send_supplier_bot_message(*, chat_id: str, text: str) -> tuple[bool, str | None]:
    """Send a best-effort message through the main supplier Telegram bot."""
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return False, "TELEGRAM_BOT_TOKEN not configured"

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            return True, None
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("supplier_bot.send.http_error chat_id=%s status=%s body=%s", chat_id, error.code, body)
        return False, f"telegram_http_{error.code}"
    except (urllib.error.URLError, TimeoutError) as error:
        logger.warning("supplier_bot.send.network_error chat_id=%s error=%s", chat_id, error)
        return False, "telegram_network_error"


@transaction.atomic
def select_company_for_linked_account(*, user, supplier, notify: bool) -> int:
    """Select a newly created company in every active chat linked to user."""
    bindings = list(
        BotUserBinding.objects.select_for_update().filter(
            user=user,
            status=EntityStatus.ACTIVE,
            channel=SourceChannel.TELEGRAM,
        )
    )
    for binding in bindings:
        binding.supplier = supplier
        binding.metadata = {
            **(binding.metadata or {}),
            "selected_company_at": timezone.now().isoformat(),
            "selected_company_source": "company_created",
        }
        binding.save(update_fields=["supplier", "metadata", "updated_at"])
        ensure_supplier_notification_destination(user=user, supplier=supplier, binding=binding)
        state, _ = BotUserState.objects.select_for_update().get_or_create(
            platform=binding.channel,
            chat_id=binding.chat_id,
            defaults={"telegram_user_id": binding.external_user_id},
        )
        state.telegram_user_id = binding.external_user_id
        state.state = "supplier_registered"
        state.payload = {
            **(state.payload if isinstance(state.payload, dict) else {}),
            "selected_company_id": str(supplier.id),
        }
        state.save(update_fields=["telegram_user_id", "state", "payload", "updated_at"])

        if notify:
            chat_id = binding.chat_id
            company_name = supplier.display_name

            def send_confirmation(chat_id=chat_id, company_name=company_name):
                sent, error = send_supplier_bot_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ Вы добавили компанию «{company_name}»\n\n"
                        "Компания выбрана в боте. Теперь можно загрузить прайс или открыть главное меню."
                    ),
                )
                if not sent:
                    logger.warning(
                        "supplier_bot.company_created.notify_failed chat_id=%s supplier_id=%s error=%s",
                        chat_id,
                        supplier.id,
                        error,
                    )

            transaction.on_commit(send_confirmation)
    return len(bindings)
