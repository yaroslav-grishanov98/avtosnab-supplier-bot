import base64
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from accounting.dto import AccessReport, AccessStep, Capabilities
from accounting.models import (
    AccountingConnection,
    AccountingConnectionEvent,
    AccountingDepartment,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionStatus,
    LinkStatus,
    Provider,
    SnapshotStatus,
    SyncKind,
    SyncStatus,
    SyncTrigger,
)
from accounting.services import (
    Actor,
    ConnectionCheckFailed,
    ConnectionConflict,
    ConnectionDisabled,
    DepartmentInUse,
    SyncThrottled,
    assign_department,
    can_manage_accounting,
    check_access_for,
    create_connection,
    disconnect,
    managed_venue_ids,
    replace_credentials,
    request_manual_sync,
)
from accounting.test_support.sync import make_connection, make_venue
from core.models import User, UserRole, VenueUser, VenueUserRole

SECRET_KEY = base64.b64encode(b"0" * 32).decode("ascii")


class PassingAdapter:
    def check_access(self):
        return AccessReport(ok=True, steps=[AccessStep("login", "ok", "Вход выполнен.")])

    def capabilities(self):
        return Capabilities(True, True, True, True, False, source_version="test-1")


class FailingAdapter(PassingAdapter):
    def check_access(self):
        return AccessReport(
            ok=False,
            steps=[AccessStep("login", "failed", "Не удалось войти.")],
            error_code="auth_error",
            hint="Проверьте логин и пароль.",
        )


@contextmanager
def adapter_context(adapter):
    yield adapter


@contextmanager
def acquired_guard(*args, **kwargs):
    class Guard:
        acquired = True

    yield Guard()


@override_settings(
    ACCOUNTING_SECRET_KEYS={1: SECRET_KEY},
    ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1,
)
class ConnectionServiceTests(TestCase):
    def setUp(self):
        self.venue = make_venue("Заведение")
        self.actor = Actor("admin-login")

    @patch("accounting.services.get_adapter")
    def test_create_connection_saves_nothing_when_check_fails(self, get_adapter):
        get_adapter.return_value = adapter_context(FailingAdapter())
        with self.assertRaises(ConnectionCheckFailed):
            create_connection(
                venue=self.venue,
                actor=self.actor,
                provider=Provider.IIKO,
                base_url="https://one.example/resto",
                login="login",
                password="secret",
            )
        self.assertFalse(AccountingConnection.objects.exists())
        self.assertFalse(AccountingSyncJob.objects.exists())

    def test_access_report_is_russian_and_hides_inputs(self):
        secret_url = "not-a-url.internal"
        with self.assertNoLogs("accounting.services"):
            report = check_access_for(Provider.IIKO, secret_url, "private-login", "private-password")
        rendered = repr(report)
        self.assertFalse(report.ok)
        self.assertIn("некорректно", rendered.lower())
        for secret in (secret_url, "private-login", "private-password"):
            self.assertNotIn(secret, rendered)

    @patch("accounting.services.enqueue_job")
    @patch("accounting.services.get_adapter")
    def test_create_connection_seals_credentials_links_and_enqueues_catalog(self, get_adapter, enqueue):
        get_adapter.return_value = adapter_context(PassingAdapter())
        connection = create_connection(
            venue=self.venue,
            actor=self.actor,
            provider=Provider.IIKO,
            base_url="https://one.example/resto",
            login="client-login",
            password="client-secret",
        )
        self.assertNotIn(b"client-login", bytes(connection.credentials_ciphertext))
        self.assertNotIn(b"client-secret", bytes(connection.credentials_ciphertext))
        self.assertEqual(connection.capabilities["source_version"], "test-1")
        self.assertEqual(connection.venue_links.get().status, LinkStatus.PENDING)
        job = connection.sync_jobs.get()
        self.assertEqual((job.kind, job.trigger), (SyncKind.CATALOG, SyncTrigger.CONNECT))
        enqueue.assert_called_once_with(job)
        self.assertEqual(connection.events.get().actor_label, "admin-login")

    @patch("accounting.services.enqueue_job")
    @patch("accounting.services.get_adapter")
    def test_create_connection_rejects_second_live_provider_link(self, get_adapter, enqueue):
        get_adapter.side_effect = lambda *args, **kwargs: adapter_context(PassingAdapter())
        for index in range(2):
            if index == 1:
                expected = self.assertRaises(ConnectionConflict)
            else:
                expected = nullcontext()
            with expected:
                create_connection(
                    venue=self.venue,
                    actor=self.actor,
                    provider=Provider.IIKO,
                    base_url=f"https://{index}.example/resto",
                    login="login",
                    password="secret",
                )
        self.assertEqual(AccountingConnection.objects.count(), 1)

    @patch("accounting.services.get_adapter")
    def test_replace_credentials_resets_needs_attention(self, get_adapter):
        get_adapter.return_value = adapter_context(PassingAdapter())
        connection = make_connection(status=ConnectionStatus.NEEDS_ATTENTION, consecutive_failures=4)
        connection.set_credentials("old", "old-secret")
        connection.save()
        replace_credentials(connection, self.actor, "new", "new-secret")
        connection.refresh_from_db()
        self.assertEqual(connection.status, ConnectionStatus.ACTIVE)
        self.assertEqual(connection.consecutive_failures, 0)
        self.assertEqual(connection.credentials().login, "new")

    def test_disconnect_wipes_credentials_but_keeps_inventory(self):
        connection = make_connection()
        connection.set_credentials("login", "secret")
        connection.save()
        AccountingVenueLink.objects.create(connection=connection, venue=self.venue, status=LinkStatus.ACTIVE)
        warehouse = AccountingWarehouse.objects.create(
            connection=connection, venue=self.venue, external_id="w", name="W"
        )
        disconnect(connection, self.actor)
        connection.refresh_from_db()
        self.assertEqual(connection.status, ConnectionStatus.DISABLED)
        self.assertIsNone(connection.credentials_ciphertext)
        self.assertTrue(AccountingWarehouse.objects.filter(pk=warehouse.pk).exists())
        self.assertEqual(connection.venue_links.get().status, LinkStatus.INACTIVE)

    @patch("accounting.services.sync_guard", acquired_guard)
    def test_assign_department_sets_venue_link_and_warehouses(self):
        connection = make_connection()
        AccountingDepartment.objects.create(connection=connection, external_id="d", name="D")
        warehouse = AccountingWarehouse.objects.create(
            connection=connection, external_id="w", external_department_id="d", name="W"
        )
        result = assign_department(connection, "d", self.venue, self.actor)
        warehouse.refresh_from_db()
        self.assertEqual(result.venue, self.venue)
        self.assertEqual(warehouse.venue, self.venue)
        self.assertEqual(connection.venue_links.get(venue=self.venue).status, LinkStatus.ACTIVE)

    @patch("accounting.services.sync_guard", acquired_guard)
    def test_reassign_is_blocked_when_department_has_stock_history(self):
        connection = make_connection()
        AccountingVenueLink.objects.create(connection=connection, venue=self.venue, status=LinkStatus.ACTIVE)
        department = AccountingDepartment.objects.create(
            connection=connection, external_id="d", name="D", venue=self.venue
        )
        warehouse = AccountingWarehouse.objects.create(
            connection=connection, venue=self.venue, external_id="w", external_department_id="d", name="W"
        )
        snapshot = AccountingStockSnapshot.objects.create(
            connection=connection,
            venue=self.venue,
            warehouse=warehouse,
            taken_at=timezone.now(),
            status=SnapshotStatus.COMPLETE,
        )
        self.assertTrue(snapshot.pk)
        with self.assertRaises(DepartmentInUse):
            assign_department(connection, "d", make_venue("Другое"), self.actor)
        department.refresh_from_db()
        self.assertEqual(department.venue, self.venue)

    @patch("accounting.services.sync_guard", acquired_guard)
    def test_assign_department_is_refused_on_a_disconnected_connection(self):
        connection = make_connection()
        AccountingVenueLink.objects.create(connection=connection, venue=self.venue, status=LinkStatus.ACTIVE)
        AccountingDepartment.objects.create(connection=connection, external_id="d", name="D")
        disconnect(connection, self.actor)
        with self.assertRaises(ConnectionDisabled):
            assign_department(connection, "d", self.venue, self.actor)
        # A disconnected connection must never look live again through a reactivated link.
        self.assertEqual(connection.venue_links.get().status, LinkStatus.INACTIVE)
        self.assertIsNone(AccountingDepartment.objects.get(connection=connection).venue)

    @patch("accounting.services.enqueue_job")
    def test_manual_sync_is_refused_on_a_disconnected_connection(self, enqueue):
        connection = make_connection()
        disconnect(connection, self.actor)
        with self.assertRaises(ConnectionDisabled):
            request_manual_sync(connection, self.actor)
        self.assertFalse(AccountingSyncJob.objects.filter(connection=connection).exists())
        enqueue.assert_not_called()

    @patch("accounting.services.enqueue_job")
    def test_manual_sync_is_still_allowed_when_the_connection_needs_attention(self, enqueue):
        connection = make_connection()
        connection.status = ConnectionStatus.NEEDS_ATTENTION
        connection.save()
        job = request_manual_sync(connection, self.actor)
        enqueue.assert_called_once_with(job)

    @patch("accounting.services.enqueue_job")
    def test_manual_sync_returns_running_job_and_throttles_recent_finished(self, enqueue):
        connection = make_connection()
        running = AccountingSyncJob.objects.create(
            connection=connection, kind=SyncKind.CATALOG, trigger=SyncTrigger.CONNECT
        )
        self.assertEqual(request_manual_sync(connection, self.actor), running)
        running.status = SyncStatus.SUCCESS
        running.finished_at = timezone.now()
        running.trigger = SyncTrigger.MANUAL
        running.save()
        with self.assertRaises(SyncThrottled):
            request_manual_sync(connection, self.actor)
        running.queued_at = timezone.now() - timedelta(minutes=2)
        running.save()
        job = request_manual_sync(connection, self.actor)
        self.assertEqual(job.trigger, SyncTrigger.MANUAL)
        enqueue.assert_called_once_with(job)

    def test_can_manage_accounting_owner_and_admin_only(self):
        owner = User.objects.create(email="owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        manager = User.objects.create(email="manager@example.com", full_name="Manager", role=UserRole.VENUE_USER)
        admin = User.objects.create(email="admin@example.com", full_name="Admin", role=UserRole.ADMIN)
        VenueUser.objects.create(venue=self.venue, user=owner, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=self.venue, user=manager, role=VenueUserRole.MANAGER)
        self.assertTrue(can_manage_accounting(owner, self.venue.id))
        self.assertTrue(can_manage_accounting(admin, self.venue.id))
        self.assertFalse(can_manage_accounting(manager, self.venue.id))
        self.assertFalse(can_manage_accounting(owner, make_venue("Чужое").id))

    def test_managed_venue_ids_agrees_with_can_manage_accounting(self):
        other = make_venue("Другое")
        archived = make_venue("В архиве")
        archived.status = "archived"
        archived.save()
        owner = User.objects.create(email="o2@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        manager = User.objects.create(email="m2@example.com", full_name="Manager", role=UserRole.VENUE_USER)
        admin = User.objects.create(email="a2@example.com", full_name="Admin", role=UserRole.ADMIN)
        VenueUser.objects.create(venue=self.venue, user=owner, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=archived, user=owner, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=other, user=manager, role=VenueUserRole.MANAGER)
        ids = [self.venue.id, other.id, archived.id]
        for user in (owner, manager, admin):
            self.assertEqual(
                managed_venue_ids(user, ids), {venue_id for venue_id in ids if can_manage_accounting(user, venue_id)}
            )
        self.assertEqual(managed_venue_ids(owner, ids), {self.venue.id})
        self.assertEqual(managed_venue_ids(owner, []), set())

    def test_event_details_never_need_secrets(self):
        connection = make_connection()
        event = AccountingConnectionEvent.objects.create(
            connection=connection,
            actor_label="admin-login",
            action="status_changed",
            details={"from": "active", "to": "disabled"},
        )
        self.assertNotIn("secret", str(event.details).lower())
