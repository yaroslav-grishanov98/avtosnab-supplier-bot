import base64
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounting.models import (
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    ConnectionStatus,
    LinkStatus,
    SnapshotStatus,
    SyncKind,
    SyncStatus,
    SyncTrigger,
)
from accounting.selectors import get_venue_stock
from accounting.test_support.sync import link, make_connection
from core.models import EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole

SECRET_KEY = base64.b64encode(b"0" * 32).decode("ascii")
SERVICE_TOKEN = "test-service-token-for-accounting"
LOGIN = "iiko-api-login-XYZ"
PASSWORD = "iiko-api-password-XYZ"


def bearer(user):
    return f"Bearer {RefreshToken.for_user(user).access_token}"


def make_user(role=UserRole.VENUE_USER):
    return User.objects.create(email=f"user-{uuid4()}@example.com", full_name="Пользователь", role=role)


def make_venue(name):
    return Venue.objects.create(legal_name=name, display_name=name)


def membership(venue, user, role):
    return VenueUser.objects.create(venue=venue, user=user, role=role, status=EntityStatus.ACTIVE)


@override_settings(
    ACCOUNTING_SECRET_KEYS={1: SECRET_KEY},
    ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1,
    SERVICE_API_TOKEN=SERVICE_TOKEN,
    ACCOUNTING_MANUAL_SYNC_THROTTLE_SECONDS=60,
    ACCOUNTING_STOCK_STALE_AFTER_MINUTES=180,
)
class AccountingApiTests(APITestCase):
    def setUp(self):
        self.venue = make_venue("Моё заведение")
        self.other_venue = make_venue("Чужое заведение")
        self.owner, self.manager, self.stranger = make_user(), make_user(), make_user()
        membership(self.venue, self.owner, VenueUserRole.OWNER)
        membership(self.venue, self.manager, VenueUserRole.MANAGER)
        membership(self.other_venue, self.stranger, VenueUserRole.OWNER)

        self.connection = make_connection(catalog_synced_at=timezone.now())
        self.connection.set_credentials(LOGIN, PASSWORD)
        self.connection.save()
        link(self.connection, self.venue)
        self.foreign_connection = make_connection(base_url="https://foreign.iiko.example/resto")
        link(self.foreign_connection, self.other_venue)

    def as_user(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=bearer(user))

    def as_service(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {SERVICE_TOKEN}")

    def status_url(self, venue=None):
        return reverse("v1:accounting:venue-status", args=[(venue or self.venue).id])

    def sync_url(self, connection=None, venue=None):
        return reverse(
            "v1:accounting:venue-connection-sync", args=[(venue or self.venue).id, (connection or self.connection).id]
        )

    def job_url(self, job, venue=None):
        return reverse("v1:accounting:venue-sync-job", args=[(venue or self.venue).id, job.job_id])

    def make_job(self, connection=None, **fields):
        fields.setdefault("kind", SyncKind.STOCK)
        fields.setdefault("trigger", SyncTrigger.MANUAL)
        return AccountingSyncJob.objects.create(connection=connection or self.connection, **fields)

    # -- access ------------------------------------------------------------------------------------------------

    def test_non_member_gets_403(self):
        self.as_user(self.stranger)
        self.assertEqual(self.client.get(self.status_url()).status_code, 403)
        self.client.credentials()
        self.assertIn(self.client.get(self.status_url()).status_code, (401, 403))

    def test_member_of_other_venue_cannot_use_foreign_venue_path(self):
        job = self.make_job()
        self.as_user(self.stranger)
        for response in (
            self.client.get(self.status_url()),
            self.client.post(self.sync_url()),
            self.client.get(self.job_url(job)),
        ):
            self.assertEqual(response.status_code, 403)
        self.assertEqual(AccountingSyncJob.objects.filter(connection=self.connection).count(), 1)

    def test_foreign_connection_in_own_venue_path_returns_404(self):
        self.as_user(self.owner)
        response = self.client.post(self.sync_url(connection=self.foreign_connection))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AccountingSyncJob.objects.filter(connection=self.foreign_connection).exists())

    def test_foreign_job_id_returns_404(self):
        foreign_job = self.make_job(connection=self.foreign_connection)
        self.as_user(self.owner)
        self.assertEqual(self.client.get(self.job_url(foreign_job)).status_code, 404)
        self.assertEqual(self.client.get(self.job_url(foreign_job, venue=self.venue)).status_code, 404)

    def test_unknown_venue_returns_404_for_staff_and_service_token(self):
        ghost = Venue(id=uuid4())
        self.as_service()
        self.assertEqual(self.client.get(self.status_url(ghost)).status_code, 404)

    # -- status ----------------------------------------------------------------------------------------------------

    def test_status_all_returns_only_users_venues(self):
        self.as_user(self.owner)
        response = self.client.get(reverse("v1:accounting:status-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["venue_id"] for item in response.json()], [str(self.venue.id)])

    def test_status_all_needs_a_user_jwt(self):
        self.client.credentials()
        self.assertEqual(self.client.get(reverse("v1:accounting:status-list")).status_code, 401)
        self.as_service()
        self.assertIn(self.client.get(reverse("v1:accounting:status-list")).status_code, (401, 403))

    def test_manager_sees_can_manage_false_owner_true(self):
        for user, expected in ((self.owner, True), (self.manager, False)):
            self.as_user(user)
            self.assertIs(self.client.get(self.status_url()).json()["can_manage"], expected)
            (item,) = self.client.get(reverse("v1:accounting:status-list")).json()
            self.assertIs(item["can_manage"], expected)

    def test_platform_admin_reads_any_venue_and_manages(self):
        self.as_user(make_user(role=UserRole.ADMIN))
        body = self.client.get(self.status_url()).json()
        self.assertIs(body["can_manage"], True)
        self.assertEqual(body["venue_id"], str(self.venue.id))

    def test_status_body_shape(self):
        self.as_user(self.owner)
        body = self.client.get(self.status_url()).json()
        self.assertEqual(
            set(body),
            {
                "venue_id", "state", "reason_code", "connection_id", "provider", "last_success_at",
                "stale_since", "warehouses_count", "can_manage",
            },
        )  # fmt: skip
        self.assertEqual(
            (body["state"], body["connection_id"], body["provider"]),
            ("needs_department_link", str(self.connection.id), "iiko"),
        )

    def test_service_token_can_read_status_but_not_trigger_sync(self):
        self.as_service()
        response = self.client.get(self.status_url())
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["can_manage"], False)
        self.assertEqual(self.client.post(self.sync_url()).status_code, 403)
        self.assertFalse(AccountingSyncJob.objects.filter(connection=self.connection).exists())

    # -- manual sync -------------------------------------------------------------------------------------------------

    @patch("accounting.services.enqueue_job")
    def test_sync_returns_202_with_job_id_and_429_with_retry_after(self, enqueue):
        self.as_user(self.manager)  # a manager may start a sync; only managing needs the owner
        first = self.client.post(self.sync_url())
        self.assertEqual(first.status_code, 202)
        job = AccountingSyncJob.objects.get(pk=first.json()["job_id"])
        self.assertEqual(
            (first.json()["status"], first.json()["kind"], first.json()["trigger"]),
            ("queued", "stock", "manual"),
        )
        self.assertEqual(job.requested_by, self.manager)
        enqueue.assert_called_once_with(job)

        AccountingSyncJob.objects.filter(pk=job.pk).update(status=SyncStatus.SUCCESS, finished_at=timezone.now())
        second = self.client.post(self.sync_url())
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second["Retry-After"], str(second.json()["retry_after"]))
        self.assertGreaterEqual(second.json()["retry_after"], 1)
        self.assertEqual(AccountingSyncJob.objects.filter(connection=self.connection).count(), 1)

    @patch("accounting.services.enqueue_job")
    def test_a_running_job_is_returned_instead_of_a_new_one(self, enqueue):
        running = self.make_job(status=SyncStatus.RUNNING, started_at=timezone.now())
        self.as_user(self.owner)
        response = self.client.post(self.sync_url())
        self.assertEqual((response.status_code, response.json()["job_id"]), (202, str(running.job_id)))
        enqueue.assert_not_called()

    @patch("accounting.services.enqueue_job")
    def test_sync_of_a_disabled_connection_is_409(self, enqueue):
        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save()
        self.as_user(self.owner)
        response = self.client.post(self.sync_url())
        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()

    def test_job_status_is_returned_to_a_member(self):
        job = self.make_job(status=SyncStatus.PARTIAL, error_code="warehouse_failed", finished_at=timezone.now())
        self.as_user(self.manager)
        body = self.client.get(self.job_url(job)).json()
        self.assertEqual(
            (body["job_id"], body["connection_id"], body["status"], body["error_code"]),
            (str(job.job_id), str(self.connection.id), "partial", "warehouse_failed"),
        )
        self.assertIsNone(body["started_at"])

    # -- no secrets, no stale data after a disconnect --------------------------------------------------------------

    @patch("accounting.services.enqueue_job")
    def test_responses_never_contain_secrets(self, enqueue):
        secrets = (
            "password",
            "pass_hash",
            "ciphertext",
            "credentials",
            LOGIN,
            PASSWORD,
            self.connection.credentials().pass_hash,
        )
        job = self.make_job()
        self.as_user(self.owner)
        responses = [
            self.client.get(reverse("v1:accounting:status-list")),
            self.client.get(self.status_url()),
            self.client.get(self.job_url(job)),
            self.client.post(self.sync_url()),
            self.client.post(self.sync_url()),  # the second one is rejected
        ]
        text = "\n".join(response.content.decode() for response in responses).lower()
        for secret in secrets:
            self.assertNotIn(secret.lower(), text)

    def test_disconnected_status_hides_stock_through_selectors(self):
        wh = AccountingWarehouse.objects.create(
            connection=self.connection, venue=self.venue, external_id="wh", name="Кухня"
        )
        AccountingStockSnapshot.objects.create(
            connection=self.connection, venue=self.venue, warehouse=wh, taken_at=timezone.now(),
            status=SnapshotStatus.COMPLETE,
        )  # fmt: skip
        self.as_user(self.owner)
        self.assertEqual(self.client.get(self.status_url()).json()["state"], "active")

        self.connection.status = ConnectionStatus.DISABLED
        self.connection.save()
        AccountingVenueLink.objects.filter(connection=self.connection).update(status=LinkStatus.INACTIVE)
        body = self.client.get(self.status_url()).json()
        self.assertEqual((body["state"], body["connection_id"], body["last_success_at"]), ("disconnected", None, None))
        self.assertEqual(get_venue_stock(self.venue.id), [])

    def test_our_error_bodies_are_russian(self):
        self.as_user(self.owner)
        not_found = self.client.post(self.sync_url(connection=self.foreign_connection))
        self.assertEqual(not_found.status_code, 404)
        self.assertRegex(json.dumps(not_found.json(), ensure_ascii=False), "[А-Яа-я]")

        with patch("accounting.services.enqueue_job"):
            self.client.post(self.sync_url())
        AccountingSyncJob.objects.update(status=SyncStatus.SUCCESS, finished_at=timezone.now())
        throttled = self.client.post(self.sync_url())
        self.assertEqual(throttled.status_code, 429)
        self.assertRegex(throttled.json()["detail"], "[А-Яа-я]")
        self.assertNotRegex(throttled.json()["detail"], "[A-Za-z]{4,}")

        missing_job = self.client.get(reverse("v1:accounting:venue-sync-job", args=[self.venue.id, uuid4()]))
        self.assertEqual(missing_job.status_code, 404)
        self.assertRegex(missing_job.json()["detail"], "[А-Яа-я]")

    def test_stale_state_comes_with_stale_since(self):
        wh = AccountingWarehouse.objects.create(
            connection=self.connection, venue=self.venue, external_id="wh", name="Кухня"
        )
        old = timezone.now() - timedelta(minutes=400)
        AccountingStockSnapshot.objects.create(
            connection=self.connection, venue=self.venue, warehouse=wh, taken_at=old, synced_at=old,
            status=SnapshotStatus.COMPLETE,
        )  # fmt: skip
        self.as_user(self.owner)
        body = self.client.get(self.status_url()).json()
        self.assertEqual((body["state"], body["reason_code"], body["warehouses_count"]), ("stale", "stock_stale", 1))
        self.assertIsNotNone(body["stale_since"])
