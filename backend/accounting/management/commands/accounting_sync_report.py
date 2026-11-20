"""Lag report of the accounting connections: who has not synced for too long, and who needs an admin.

One line per connection: status, time since the last successful sync, consecutive failures and flags. A flag is
raised for LAG (an ACTIVE connection whose last success is older than ``--max-lag-factor`` x its sync interval, or
that never succeeded although it is that old), NEEDS_ATTENTION (the connection stopped, e.g. its password was
rejected) and DISABLED. Only the connection id, provider and host are printed: no credentials, URLs or venue names.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounting.models import AccountingConnection, ConnectionStatus

LAG = "LAG"
NEVER_SYNCED = "NEVER_SYNCED"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
DISABLED = "DISABLED"


def _age(delta: timedelta | None) -> str:
    if delta is None:
        return "никогда"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} мин"
    hours = minutes // 60
    return f"{hours} ч" if hours < 48 else f"{hours // 24} д"


def connection_flags(connection: AccountingConnection, now, max_lag_factor: float) -> list[str]:
    if connection.status == ConnectionStatus.DISABLED:
        return [DISABLED]
    if connection.status == ConnectionStatus.NEEDS_ATTENTION:
        return [NEEDS_ATTENTION]
    allowed = timedelta(minutes=max(1, connection.sync_interval_minutes) * max_lag_factor)
    if connection.last_success_at is None:
        return [NEVER_SYNCED] if now - connection.created_at > allowed else []
    return [LAG] if now - connection.last_success_at > allowed else []


class Command(BaseCommand):
    help = "Report accounting connections that lag behind their sync interval or need attention."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-lag-factor",
            type=float,
            default=2.0,
            help="Lag is flagged when the last success is older than this many sync intervals (default 2).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        factor = options["max_lag_factor"]
        counts = {LAG: 0, NEVER_SYNCED: 0, NEEDS_ATTENTION: 0, DISABLED: 0}
        total = 0
        for connection in AccountingConnection.objects.order_by("created_at", "id"):
            total += 1
            flags = connection_flags(connection, now, factor)
            for flag in flags:
                counts[flag] += 1
            since = None if connection.last_success_at is None else now - connection.last_success_at
            self.stdout.write(
                f"{connection.id}  {connection.provider:10}  {connection.host or '-':32}  {connection.status:16}  "
                f"успех: {_age(since):>9}  ошибок подряд: {connection.consecutive_failures:3}  {' '.join(flags)}".rstrip()
            )
        self.stdout.write(
            f"Всего подключений: {total}; отстают: {counts[LAG]}; ни разу не синхронизировались: {counts[NEVER_SYNCED]}; "
            f"требуют внимания: {counts[NEEDS_ATTENTION]}; отключены: {counts[DISABLED]}."
        )
