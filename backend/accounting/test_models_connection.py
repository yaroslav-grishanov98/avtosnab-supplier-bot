import base64
import hashlib

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.views.debug import ExceptionReporter

from accounting.models import (
    AccountingConnection,
    AccountingConnectionEvent,
    AccountingDepartment,
    AccountingVenueLink,
    ConnectionEventAction,
    ConnectionStatus,
    CredentialsNotSet,
    IikoCredentials,
    LinkStatus,
    Provider,
)
from accounting.secrets import SecretError
from core.models import User, UserRole, Venue

SECRET_KEY = base64.b64encode(bytes([7]) * 32).decode("ascii")
LOGIN = "ivanov-login"
PASSWORD = "pa55w0rd-Секрет"


def error_report(call, expected):
    """The HTML of a production error report (mail_admins, DEBUG off; frame locals included) for call().

    sensitive_variables() is honoured only when DEBUG is False, which is how tests run.

    Built from a plain try/except: assertRaises clears the traceback frames, which
    would make a "secret is absent" assertion pass vacuously.
    """
    try:
        call()
    except expected as exc:
        return ExceptionReporter(None, type(exc), exc, exc.__traceback__).get_traceback_html()
    raise AssertionError(f"{expected.__name__} was not raised")


@override_settings(ACCOUNTING_SECRET_KEYS={1: SECRET_KEY}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1)
class AccountingConnectionModelTests(TestCase):
    def make_connection(self, base_url="https://one.iiko.it:443/resto", **extra):
        return AccountingConnection.objects.create(provider=Provider.IIKO, base_url=base_url, **extra)

    def make_venue(self, name):
        return Venue.objects.create(display_name=name)

    def link(self, connection, venue, status=LinkStatus.ACTIVE, provider=Provider.IIKO):
        return AccountingVenueLink.objects.create(connection=connection, venue=venue, provider=provider, status=status)

    def test_for_venue_returns_only_linked_connections(self):
        venue_a = self.make_venue("A")
        venue_b = self.make_venue("B")
        conn_a = self.make_connection("https://a.iiko.it/resto")
        conn_b = self.make_connection("https://b.iiko.it/resto")
        self.link(conn_a, venue_a)
        self.link(conn_b, venue_b, status=LinkStatus.PENDING)

        self.assertEqual(list(AccountingConnection.objects.for_venue(venue_a.id)), [conn_a])
        self.assertEqual(list(AccountingConnection.objects.for_venue(venue_b.id)), [conn_b])

    def test_for_venue_ignores_inactive_links(self):
        venue = self.make_venue("A")
        connection = self.make_connection()
        self.link(connection, venue, status=LinkStatus.INACTIVE)

        self.assertFalse(AccountingConnection.objects.for_venue(venue.id).exists())

    def test_one_active_connection_per_venue_and_provider(self):
        venue = self.make_venue("A")
        self.link(self.make_connection("https://a.iiko.it/resto"), venue)
        second = self.make_connection("https://b.iiko.it/resto")

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.link(second, venue, status=LinkStatus.PENDING)

    def test_inactive_link_does_not_block_a_new_connection(self):
        venue = self.make_venue("A")
        self.link(self.make_connection("https://a.iiko.it/resto"), venue, status=LinkStatus.INACTIVE)

        self.link(self.make_connection("https://b.iiko.it/resto"), venue)

        self.assertEqual(AccountingVenueLink.objects.filter(venue=venue).count(), 2)

    def test_same_connection_and_venue_pair_is_unique(self):
        venue = self.make_venue("A")
        connection = self.make_connection()
        self.link(connection, venue, status=LinkStatus.INACTIVE)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.link(connection, venue, status=LinkStatus.INACTIVE)

    def test_host_is_derived_and_lowercased(self):
        connection = self.make_connection("https://Demo.IIKO.it:443/Resto")

        self.assertEqual(connection.host, "demo.iiko.it")
        connection.refresh_from_db()
        self.assertEqual(connection.host, "demo.iiko.it")

    def test_for_venue_requires_the_live_link_to_be_for_that_venue(self):
        venue_a = self.make_venue("A")
        venue_b = self.make_venue("B")
        connection = self.make_connection()
        self.link(connection, venue_a, status=LinkStatus.INACTIVE)
        self.link(connection, venue_b, status=LinkStatus.ACTIVE)

        self.assertFalse(AccountingConnection.objects.for_venue(venue_a.id).exists())
        self.assertEqual(list(AccountingConnection.objects.for_venue(venue_b.id)), [connection])

    def test_for_venue_lists_a_shared_connection_once_per_venue(self):
        venue_a = self.make_venue("A")
        venue_b = self.make_venue("B")
        connection = self.make_connection()
        self.link(connection, venue_a)
        self.link(connection, venue_b, status=LinkStatus.PENDING)

        self.assertEqual(list(AccountingConnection.objects.for_venue(venue_a.id)), [connection])
        self.assertEqual(list(AccountingConnection.objects.for_venue(venue_b.id)), [connection])

    def test_link_provider_is_taken_from_the_connection(self):
        venue = self.make_venue("A")
        connection = self.make_connection()

        link = self.link(connection, venue, provider=Provider.STOREHOUSE)

        self.assertEqual(link.provider, Provider.IIKO)
        link.refresh_from_db()
        self.assertEqual(link.provider, Provider.IIKO)

    def test_link_provider_stays_in_sync_on_update(self):
        venue = self.make_venue("A")
        link = self.link(self.make_connection(), venue)
        AccountingVenueLink.objects.filter(pk=link.pk).update(provider=Provider.STOREHOUSE)
        link.refresh_from_db()

        link.save(update_fields=["status"])

        link.refresh_from_db()
        self.assertEqual(link.provider, Provider.IIKO)

    def test_mismatching_link_provider_cannot_dodge_the_live_link_guard(self):
        venue = self.make_venue("A")
        self.link(self.make_connection("https://a.iiko.it/resto"), venue)
        second = self.make_connection("https://b.iiko.it/resto")

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.link(second, venue, status=LinkStatus.PENDING, provider=Provider.STOREHOUSE)

    def test_base_url_without_host_is_rejected(self):
        for base_url in ("https:///resto", "", "   ", "/resto"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                self.make_connection(base_url)
        self.assertFalse(AccountingConnection.objects.exists())

    def test_unparsable_base_url_is_rejected(self):
        for base_url in ("https://[bad/resto", "https://one.iiko.it:notaport/resto"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                self.make_connection(base_url)

    def test_base_url_with_userinfo_is_rejected_without_echoing_it(self):
        for base_url in (
            "https://svc-user:s3cr3t-pass@one.iiko.it/resto",
            "https://svc-user@one.iiko.it/resto",
            "svc-user:s3cr3t-pass@one.iiko.it/resto",
            "https://@one.iiko.it/resto",
        ):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError) as caught:
                self.make_connection(base_url)
            self.assertNotIn("svc-user", str(caught.exception))
            self.assertNotIn("s3cr3t-pass", str(caught.exception))
        self.assertFalse(AccountingConnection.objects.exists())

    def test_bad_base_url_on_update_is_rejected_and_not_saved(self):
        connection = self.make_connection("https://one.iiko.it/resto")

        connection.base_url = "https://svc-user:s3cr3t-pass@two.iiko.it/resto"
        with self.assertRaises(ValueError):
            connection.save(update_fields=["base_url"])

        connection.refresh_from_db()
        self.assertEqual(connection.base_url, "https://one.iiko.it/resto")
        self.assertEqual(connection.host, "one.iiko.it")

    @override_settings(ACCOUNTING_SECRET_KEYS={}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1)
    def test_set_credentials_failure_hides_login_and_hash_in_error_reports(self):
        connection = self.make_connection()

        report = error_report(lambda: connection.set_credentials(LOGIN, PASSWORD), ImproperlyConfigured)

        self.assertIn("set_credentials", report)
        self.assertNotIn(LOGIN, report)
        self.assertNotIn(PASSWORD, report)
        self.assertNotIn(hashlib.sha1(PASSWORD.encode("utf-8")).hexdigest(), report)

    def test_set_credentials_rejects_unencodable_password_without_leaking_it(self):
        connection = self.make_connection()

        with self.assertRaises(ValueError) as caught:
            connection.set_credentials(LOGIN, "pw-\ud800")

        self.assertNotIn("pw-", str(caught.exception))
        self.assertIsNone(connection.credentials_ciphertext)

    def test_host_follows_base_url_on_update(self):
        connection = self.make_connection("https://one.iiko.it/resto")

        connection.base_url = "http://Two.Example.com:8080/resto"
        connection.save(update_fields=["base_url"])

        connection.refresh_from_db()
        self.assertEqual(connection.host, "two.example.com")

    def test_credentials_are_sealed_not_plaintext(self):
        connection = self.make_connection()
        connection.set_credentials(LOGIN, PASSWORD)
        connection.save()
        connection.refresh_from_db()

        ciphertext = bytes(connection.credentials_ciphertext)
        credentials = connection.credentials()

        self.assertNotIn(LOGIN.encode(), ciphertext)
        self.assertNotIn(PASSWORD.encode(), ciphertext)
        self.assertNotIn(credentials.pass_hash.encode(), ciphertext)
        self.assertEqual(connection.credentials_key_version, 1)
        self.assertEqual(credentials.login, LOGIN)
        self.assertRegex(credentials.pass_hash, r"^[0-9a-f]{40}$")
        self.assertNotEqual(credentials.pass_hash, PASSWORD)

    def test_pass_hash_is_sha1_of_password(self):
        connection = self.make_connection()
        connection.set_credentials(LOGIN, "secret")

        # iiko expects the SHA-1 hex digest of the password.
        self.assertEqual(connection.credentials().pass_hash, "e5e9fa1ba31ecd1ae84f75caaa474f3a663f05f4")

    def test_credentials_bound_to_row(self):
        source = self.make_connection("https://a.iiko.it/resto")
        source.set_credentials(LOGIN, PASSWORD)
        source.save()
        other = self.make_connection("https://b.iiko.it/resto")
        other.credentials_ciphertext = source.credentials_ciphertext
        other.credentials_key_version = source.credentials_key_version
        other.save()
        other.refresh_from_db()

        with self.assertRaises(SecretError):
            other.credentials()

    def test_credentials_without_set_raises(self):
        connection = self.make_connection()

        with self.assertRaises(CredentialsNotSet) as caught:
            connection.credentials()

        self.assertIsInstance(caught.exception, SecretError)

    def test_set_credentials_rejects_empty_values(self):
        connection = self.make_connection()

        for login, password in (("", PASSWORD), (LOGIN, "")):
            with self.subTest(login=login), self.assertRaises(ValueError):
                connection.set_credentials(login, password)
        self.assertIsNone(connection.credentials_ciphertext)

    def test_clear_credentials_wipes_columns(self):
        connection = self.make_connection()
        connection.set_credentials(LOGIN, PASSWORD)
        connection.save()

        connection.clear_credentials()
        connection.save()
        connection.refresh_from_db()

        self.assertIsNone(connection.credentials_ciphertext)
        self.assertIsNone(connection.credentials_key_version)
        with self.assertRaises(CredentialsNotSet):
            connection.credentials()

    def test_ciphertext_and_key_version_are_set_together(self):
        connection = self.make_connection()
        connection.credentials_ciphertext = b"x" * 40
        connection.credentials_key_version = None

        with self.assertRaises(IntegrityError), transaction.atomic():
            connection.save()

    def test_connection_repr_has_no_secrets(self):
        connection = self.make_connection()
        connection.set_credentials(LOGIN, PASSWORD)
        connection.save()
        credentials = connection.credentials()

        for text in (repr(connection), str(connection), repr(credentials), str(credentials)):
            self.assertNotIn(LOGIN, text)
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(credentials.pass_hash, text)
            self.assertNotIn(bytes(connection.credentials_ciphertext).hex(), text)

    def test_iiko_credentials_stays_a_named_tuple(self):
        credentials = IikoCredentials(login="a", pass_hash="b")

        self.assertEqual(tuple(credentials), ("a", "b"))
        self.assertEqual((credentials.login, credentials.pass_hash), ("a", "b"))

    def test_defaults(self):
        connection = self.make_connection()

        self.assertEqual(connection.status, ConnectionStatus.ACTIVE)
        self.assertEqual(connection.consecutive_failures, 0)
        self.assertEqual(connection.capabilities, {})
        self.assertIsNone(connection.next_sync_at)


class AccountingDepartmentModelTests(TestCase):
    def setUp(self):
        self.connection = AccountingConnection.objects.create(
            provider=Provider.IIKO, base_url="https://a.iiko.it/resto"
        )
        self.venue = Venue.objects.create(display_name="A")

    def test_one_venue_can_own_several_departments(self):
        # A department venue must be linked to the connection (composite FK, 0003_composite_fks).
        AccountingVenueLink.objects.create(connection=self.connection, venue=self.venue, status=LinkStatus.ACTIVE)
        for index in range(2):
            AccountingDepartment.objects.create(
                connection=self.connection,
                external_id=f"dep-{index}",
                name=f"Department {index}",
                department_type="DEPARTMENT",
                venue=self.venue,
            )

        self.assertEqual(self.venue.accounting_departments.count(), 2)

    def test_department_venue_is_optional(self):
        department = AccountingDepartment.objects.create(connection=self.connection, external_id="dep-1", name="D")

        self.assertIsNone(department.venue)

    def test_external_id_is_unique_per_connection(self):
        AccountingDepartment.objects.create(connection=self.connection, external_id="dep-1", name="D")
        other = AccountingConnection.objects.create(provider=Provider.IIKO, base_url="https://b.iiko.it/resto")

        AccountingDepartment.objects.create(connection=other, external_id="dep-1", name="D")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccountingDepartment.objects.create(connection=self.connection, external_id="dep-1", name="Again")


class AccountingConnectionEventModelTests(TestCase):
    def setUp(self):
        self.connection = AccountingConnection.objects.create(
            provider=Provider.IIKO, base_url="https://a.iiko.it/resto"
        )

    def test_event_records_actor_label_for_django_admin(self):
        event = AccountingConnectionEvent.objects.create(
            connection=self.connection,
            actor_label="admin-login",
            action=ConnectionEventAction.CREATED,
            details={"base_url": self.connection.base_url},
        )
        event.refresh_from_db()

        self.assertEqual(event.actor_label, "admin-login")
        self.assertIsNone(event.actor_user)
        self.assertEqual(event.action, "created")
        self.assertEqual(event.details, {"base_url": "https://a.iiko.it/resto"})

    def test_event_keeps_label_after_user_is_deleted(self):
        user = User.objects.create(email="owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        event = AccountingConnectionEvent.objects.create(
            connection=self.connection,
            actor_label=user.email,
            actor_user=user,
            action=ConnectionEventAction.STATUS_CHANGED,
        )

        user.delete()
        event.refresh_from_db()

        self.assertIsNone(event.actor_user)
        self.assertEqual(event.actor_label, "owner@example.com")
        self.assertEqual(event.details, {})

    def test_action_values(self):
        self.assertEqual(
            {value for value, _ in ConnectionEventAction.choices},
            {"created", "credentials_replaced", "disconnected", "department_assigned", "status_changed"},
        )
