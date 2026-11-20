"""Integration state of a venue, for the cabinet: connected or not, and if not, what is missing.

``statuses_for_venues`` answers for many venues with a constant number of queries (links with their connections,
warehouse counts, the last stored stock snapshot), so the list endpoint has no N+1.

States, first match wins:

* NOT_CONNECTED — the venue has no link to any connection.
* DISCONNECTED — it had links, but none is live (links are inactive or the connection is disabled).
* NEEDS_ATTENTION — the live connection stopped (credentials rejected, repeated failures): an admin must act.
* FIRST_SYNC_PENDING — connected, but nothing to show yet: the catalogue was never loaded, or no stock snapshot
  has been stored for the venue.
* NEEDS_DEPARTMENT_LINK — the catalogue is loaded but no warehouse belongs to the venue: an admin has not assigned
  an iiko department to it.
* STALE — the last stored stock snapshot is older than ACCOUNTING_STOCK_STALE_AFTER_MINUTES.
* ACTIVE — otherwise.

Only a *live* link counts: ACTIVE or PENDING link of a connection that is not DISABLED (ADR-0020: the database does
not look at link status, readers do).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import models
from django.db.models import Count, Max

from accounting.models import (
    AccountingStockSnapshot,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionStatus,
    LinkStatus,
    SnapshotStatus,
)

REASON_DISCONNECTED = "disconnected"
REASON_NO_DEPARTMENT = "no_department_assigned"
REASON_FIRST_SYNC = "first_sync_pending"
REASON_STOCK_STALE = "stock_stale"
REASON_NEEDS_ATTENTION = "needs_attention"


class ConnectionState(models.TextChoices):
    NOT_CONNECTED = "not_connected", "Не подключено"
    NEEDS_DEPARTMENT_LINK = "needs_department_link", "Не назначено подразделение"
    FIRST_SYNC_PENDING = "first_sync_pending", "Ждём первой синхронизации"
    ACTIVE = "active", "Работает"
    STALE = "stale", "Данные устарели"
    NEEDS_ATTENTION = "needs_attention", "Нужно внимание администратора"
    DISCONNECTED = "disconnected", "Отключено"


@dataclass(frozen=True)
class VenueIntegrationStatus:
    venue_id: UUID
    state: str
    reason_code: str | None = None
    connection_id: UUID | None = None
    provider: str | None = None
    last_success_at: datetime | None = None
    stale_since: datetime | None = None
    warehouses_count: int = 0


_LIVE_LINK = (LinkStatus.ACTIVE, LinkStatus.PENDING)


def compute_status(venue_id: UUID, now: datetime) -> VenueIntegrationStatus:
    return statuses_for_venues([venue_id], now)[venue_id]


def statuses_for_venues(venue_ids: Iterable[UUID], now: datetime) -> dict[UUID, VenueIntegrationStatus]:
    ids = list(dict.fromkeys(venue_ids))
    result = {venue_id: VenueIntegrationStatus(venue_id, ConnectionState.NOT_CONNECTED) for venue_id in ids}
    if not ids:
        return result

    links_by_venue: dict[UUID, list[AccountingVenueLink]] = {}
    for link in (
        AccountingVenueLink.objects.filter(venue_id__in=ids).select_related("connection").order_by("-created_at", "-id")
    ):
        links_by_venue.setdefault(link.venue_id, []).append(link)
    if not links_by_venue:
        return result

    warehouse_counts = dict(
        AccountingWarehouse.objects.filter(venue_id__in=links_by_venue, is_present_in_source=True)
        .values("venue_id")
        .annotate(total=Count("id"))
        .values_list("venue_id", "total")
    )
    last_stored = dict(
        AccountingStockSnapshot.objects.filter(
            venue_id__in=links_by_venue, status__in=(SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL)
        )
        .values("venue_id")
        .annotate(last=Max("synced_at"))
        .values_list("venue_id", "last")
    )
    stale_after = timedelta(minutes=settings.ACCOUNTING_STOCK_STALE_AFTER_MINUTES)

    for venue_id, links in links_by_venue.items():
        live = [
            link for link in links if link.status in _LIVE_LINK and link.connection.status != ConnectionStatus.DISABLED
        ]
        if not live:
            result[venue_id] = VenueIntegrationStatus(
                venue_id, ConnectionState.DISCONNECTED, reason_code=REASON_DISCONNECTED
            )
            continue
        # V1: one live connection per provider; the newest link speaks for the venue.
        connection = live[0].connection
        base = {
            "venue_id": venue_id,
            "connection_id": connection.id,
            "provider": connection.provider,
            "warehouses_count": warehouse_counts.get(venue_id, 0),
        }
        last = last_stored.get(venue_id)
        if connection.status == ConnectionStatus.NEEDS_ATTENTION:
            result[venue_id] = VenueIntegrationStatus(
                state=ConnectionState.NEEDS_ATTENTION,
                reason_code=connection.error_code or REASON_NEEDS_ATTENTION,
                last_success_at=last,
                **base,
            )
        elif base["warehouses_count"] == 0 and connection.catalog_synced_at is not None:
            result[venue_id] = VenueIntegrationStatus(
                state=ConnectionState.NEEDS_DEPARTMENT_LINK, reason_code=REASON_NO_DEPARTMENT, **base
            )
        elif last is None:
            result[venue_id] = VenueIntegrationStatus(
                state=ConnectionState.FIRST_SYNC_PENDING, reason_code=REASON_FIRST_SYNC, **base
            )
        elif now - last > stale_after:
            result[venue_id] = VenueIntegrationStatus(
                state=ConnectionState.STALE,
                reason_code=REASON_STOCK_STALE,
                last_success_at=last,
                stale_since=last + stale_after,
                **base,
            )
        else:
            result[venue_id] = VenueIntegrationStatus(state=ConnectionState.ACTIVE, last_success_at=last, **base)
    return result
