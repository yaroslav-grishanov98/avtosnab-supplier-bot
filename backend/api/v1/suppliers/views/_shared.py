"""Helpers shared by several supplier view modules."""

import logging
from datetime import timedelta

from core.models import EntityStatus
from suppliers.models import (
    BotUserBinding,
)

logger = logging.getLogger("autosnab.suppliers.views")


STATE_REGISTRATION_CANCELLED = "registration_cancelled"


def _resolve_active_binding(*, channel, chat_id=None, telegram_user_id=None, require_active_supplier=False):
    """Matches BotUserBinding by chat_id first, falling back to
    external_user_id — a binding's chat_id can go stale (e.g. re-linked from
    a different chat) while the Telegram user_id stays the same. Callers that
    only send chat_id (like CabinetTokenView) must send telegram_user_id too
    to benefit from the fallback."""
    queryset = BotUserBinding.objects.select_related("supplier").filter(channel=channel, status=EntityStatus.ACTIVE)
    if require_active_supplier:
        queryset = queryset.filter(supplier__status=EntityStatus.ACTIVE)
    if chat_id:
        binding = queryset.filter(chat_id=chat_id).first()
        if binding:
            return binding
    if telegram_user_id:
        return queryset.filter(external_user_id=telegram_user_id).first()
    return None


# Двойной вызов (перезагрузка страницы, ретрай фронта) в течение этого окна
# после последнего успешного уведомления не шлёт сообщение повторно — тот же
# логин, тот же чат. Не "никогда не уведомлять дважды": следующий реальный
# вход после окна снова получает подтверждение.
BOT_LOGIN_CONFIRM_DEDUP_WINDOW = timedelta(seconds=60)
