"""Lifecycle operations for accounting-system connections.

The admin and the future REST write API must use these functions instead of
writing connection, link, department, or credential fields directly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from accounting.adapters import get_adapter
from accounting.dto import AccessReport, AccessStep, AdapterError, Capabilities
from accounting.locks import sync_guard
from accounting.models import (
    AccountingConnection,
    AccountingConnectionEvent,
    AccountingDepartment,
    AccountingStockLevel,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionEventAction,
    ConnectionStatus,
    LinkStatus,
    SyncKind,
    SyncStatus,
    SyncTrigger,
)
from accounting.tasks import enqueue_job
from core.models import EntityStatus, UserRole, VenueUser, VenueUserRole

if TYPE_CHECKING:
    from core.models import User, Venue

logger = logging.getLogger(__name__)

_DISABLED_MESSAGE = "Подключение отключено: сначала замените учётные данные и заново назначьте подразделения."


@dataclass(frozen=True)
class Actor:
    label: str
    user: User | None = None


class ConnectionCheckFailed(Exception):
    def __init__(self, report: AccessReport):
        super().__init__("Не удалось проверить подключение к учётной системе.")
        self.report = report


class ConnectionConflict(Exception):
    pass


class ConnectionBusy(Exception):
    pass


class ConnectionDisabled(Exception):
    """The connection was disconnected: nothing may revive its links or queue syncs until credentials are replaced."""


class DepartmentInUse(Exception):
    pass


class SyncThrottled(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Ручную синхронизацию можно повторить позже.")
        self.retry_after = retry_after


def can_manage_accounting(user: User, venue_id) -> bool:
    if user.role == UserRole.ADMIN and user.status == EntityStatus.ACTIVE:
        return True
    return VenueUser.objects.filter(
        user=user,
        venue_id=venue_id,
        role=VenueUserRole.OWNER,
        status=EntityStatus.ACTIVE,
        venue__status=EntityStatus.ACTIVE,
    ).exists()


def managed_venue_ids(user: User, venue_ids) -> set:
    """Of ``venue_ids`` the ones ``can_manage_accounting`` allows, in one query: for listing without N+1."""
    ids = list(venue_ids)
    if user.role == UserRole.ADMIN and user.status == EntityStatus.ACTIVE:
        return set(ids)
    return set(
        VenueUser.objects.filter(
            user=user,
            venue_id__in=ids,
            role=VenueUserRole.OWNER,
            status=EntityStatus.ACTIVE,
            venue__status=EntityStatus.ACTIVE,
        ).values_list("venue_id", flat=True)
    )


def _failed_report(error: AdapterError) -> AccessReport:
    return AccessReport(
        ok=False,
        steps=[AccessStep(name="connection", status="failed", message=error.message)],
        error_code=error.code,
        hint="Проверьте адрес, доступ с серверов АвтоСнаб и права пользователя учётной системы.",
    )


@sensitive_variables("login", "password")
def _check_access_and_capabilities(provider: str, base_url: str, login: str, password: str):
    candidate = AccountingConnection(provider=provider, base_url=base_url)
    try:
        candidate.full_clean(exclude=("host", "credentials_ciphertext", "credentials_key_version"))
        candidate.set_credentials(login, password)
        with get_adapter(candidate) as adapter:
            report = adapter.check_access()
            capabilities = adapter.capabilities() if report.ok else Capabilities.unsupported()
    except AdapterError as exc:
        return _failed_report(exc), Capabilities.unsupported()
    except (ValidationError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Параметры подключения заполнены некорректно."
        return (
            AccessReport(
                ok=False,
                steps=[AccessStep(name="address", status="failed", message=message)],
                error_code="invalid_connection_settings",
                hint="Исправьте адрес сервера, логин или пароль.",
            ),
            Capabilities.unsupported(),
        )
    return report, capabilities


@sensitive_variables("login", "password")
def check_access_for(provider: str, base_url: str, login: str, password: str) -> AccessReport:
    report, _ = _check_access_and_capabilities(provider, base_url, login, password)
    return report


def check_connection_access(connection: AccountingConnection) -> AccessReport:
    """Check an already sealed connection, used by the admin action."""
    try:
        with get_adapter(connection) as adapter:
            return adapter.check_access()
    except AdapterError as exc:
        return _failed_report(exc)


def _event(connection, actor: Actor, action: str, details: dict | None = None):
    return AccountingConnectionEvent.objects.create(
        connection=connection,
        actor_label=actor.label,
        actor_user=actor.user,
        action=action,
        details=details or {},
    )


@sensitive_variables("login", "password")
def create_connection(
    *,
    venue: Venue,
    actor: Actor,
    provider: str,
    base_url: str,
    login: str,
    password: str,
    _verified: tuple[AccessReport, Capabilities] | None = None,
) -> AccountingConnection:
    report, capabilities = _verified or _check_access_and_capabilities(provider, base_url, login, password)
    if not report.ok:
        raise ConnectionCheckFailed(report)

    try:
        with transaction.atomic():
            connection = AccountingConnection(
                provider=provider,
                base_url=base_url,
                capabilities=capabilities.to_json(),
                server_version=capabilities.source_version,
                created_by=actor.user,
            )
            connection.set_credentials(login, password)
            connection.save()
            AccountingVenueLink.objects.create(
                connection=connection,
                venue=venue,
                status=LinkStatus.PENDING,
            )
            _event(connection, actor, ConnectionEventAction.CREATED, {"venue_id": str(venue.id)})
            job = AccountingSyncJob.objects.create(
                connection=connection,
                kind=SyncKind.CATALOG,
                trigger=SyncTrigger.CONNECT,
                requested_by=actor.user,
            )
    except IntegrityError as exc:
        raise ConnectionConflict("У заведения уже есть подключение этого типа.") from exc

    enqueue_job(job)
    return connection


@sensitive_variables("login", "password")
def replace_credentials(
    connection: AccountingConnection,
    actor: Actor,
    login: str,
    password: str,
    *,
    _verified: tuple[AccessReport, Capabilities] | None = None,
) -> None:
    report, capabilities = _verified or _check_access_and_capabilities(
        connection.provider, connection.base_url, login, password
    )
    if not report.ok:
        raise ConnectionCheckFailed(report)
    with transaction.atomic():
        locked = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
        previous_status = locked.status
        locked.set_credentials(login, password)
        locked.status = ConnectionStatus.ACTIVE
        locked.error_code = ""
        locked.stale_reason = ""
        locked.consecutive_failures = 0
        locked.capabilities = capabilities.to_json()
        locked.server_version = capabilities.source_version
        locked.next_sync_at = timezone.now()
        locked.save(
            update_fields=[
                "credentials_ciphertext",
                "credentials_key_version",
                "status",
                "error_code",
                "stale_reason",
                "consecutive_failures",
                "capabilities",
                "server_version",
                "next_sync_at",
                "updated_at",
            ]
        )
        _event(locked, actor, ConnectionEventAction.CREDENTIALS_REPLACED)
        if previous_status != locked.status:
            _event(
                locked,
                actor,
                ConnectionEventAction.STATUS_CHANGED,
                {"from": previous_status, "to": locked.status},
            )
    connection.refresh_from_db()


def disconnect(connection: AccountingConnection, actor: Actor) -> None:
    with transaction.atomic():
        locked = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
        previous_status = locked.status
        locked.clear_credentials()
        locked.status = ConnectionStatus.DISABLED
        locked.next_sync_at = None
        locked.save(
            update_fields=[
                "credentials_ciphertext",
                "credentials_key_version",
                "status",
                "next_sync_at",
                "updated_at",
            ]
        )
        locked.venue_links.update(status=LinkStatus.INACTIVE)
        _event(locked, actor, ConnectionEventAction.DISCONNECTED)
        if previous_status != locked.status:
            _event(
                locked,
                actor,
                ConnectionEventAction.STATUS_CHANGED,
                {"from": previous_status, "to": locked.status},
            )
    connection.refresh_from_db()


def _department_has_stock(connection_id, external_department_id: str) -> bool:
    warehouses = AccountingWarehouse.objects.filter(
        connection_id=connection_id,
        external_department_id=external_department_id,
    )
    warehouse_ids = warehouses.values("id")
    return (
        AccountingStockSnapshot.objects.filter(warehouse_id__in=warehouse_ids).exists()
        or AccountingStockLevel.objects.filter(warehouse_id__in=warehouse_ids).exists()
    )


def assign_department(
    connection: AccountingConnection,
    external_department_id: str,
    venue: Venue | None,
    actor: Actor,
) -> AccountingDepartment:
    with sync_guard(connection.id, connection.host, settings.ACCOUNTING_HOST_SLOTS) as guard:
        if not guard.acquired:
            raise ConnectionBusy("Подключение сейчас синхронизируется; повторите назначение позже.")
        try:
            with transaction.atomic():
                # A disconnected connection must not get a live link again (BE2-04): its data would look current.
                if (
                    AccountingConnection.objects.select_for_update().get(pk=connection.pk).status
                    == ConnectionStatus.DISABLED
                ):
                    raise ConnectionDisabled(_DISABLED_MESSAGE)
                department = AccountingDepartment.objects.select_for_update().get(
                    connection=connection,
                    external_id=external_department_id,
                )
                if department.venue_id != getattr(venue, "id", None) and department.venue_id is not None:
                    if _department_has_stock(connection.id, external_department_id):
                        raise DepartmentInUse("Нельзя перенести подразделение: по его складам уже сохранены остатки.")
                if venue is not None:
                    link, _ = AccountingVenueLink.objects.get_or_create(
                        connection=connection,
                        venue=venue,
                        defaults={"status": LinkStatus.ACTIVE},
                    )
                    if link.status != LinkStatus.ACTIVE:
                        link.status = LinkStatus.ACTIVE
                        link.save(update_fields=["status", "updated_at"])
                department.venue = venue
                department.save(update_fields=["venue", "updated_at"])
                AccountingWarehouse.objects.filter(
                    connection=connection,
                    external_department_id=external_department_id,
                ).update(venue=venue)
                _event(
                    connection,
                    actor,
                    ConnectionEventAction.DEPARTMENT_ASSIGNED,
                    {
                        "external_department_id": external_department_id,
                        "venue_id": str(venue.id) if venue else None,
                    },
                )
        except IntegrityError as exc:
            raise ConnectionConflict("Заведение уже связано с другим подключением этого типа.") from exc
    return department


def request_manual_sync(
    connection: AccountingConnection,
    actor: Actor,
    kind: str = SyncKind.STOCK,
) -> AccountingSyncJob:
    with transaction.atomic():
        locked = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
        if locked.status == ConnectionStatus.DISABLED:
            raise ConnectionDisabled(_DISABLED_MESSAGE)
        running = (
            locked.sync_jobs.filter(status__in=(SyncStatus.QUEUED, SyncStatus.RUNNING)).order_by("queued_at").first()
        )
        if running is not None:
            return running
        latest = locked.sync_jobs.filter(trigger=SyncTrigger.MANUAL).order_by("-queued_at").first()
        now = timezone.now()
        if latest is not None:
            elapsed = (now - latest.queued_at).total_seconds()
            remaining = settings.ACCOUNTING_MANUAL_SYNC_THROTTLE_SECONDS - elapsed
            if remaining > 0:
                raise SyncThrottled(max(1, math.ceil(remaining)))
        job = AccountingSyncJob.objects.create(
            connection=locked,
            kind=kind,
            trigger=SyncTrigger.MANUAL,
            requested_by=actor.user,
        )
    enqueue_job(job)
    return job
