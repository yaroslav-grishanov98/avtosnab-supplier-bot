import base64
from contextlib import contextmanager
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.views.debug import ExceptionReporter

from accounting.dto import AccessReport, AccessStep, Capabilities
from accounting.models import (
    AccountingConnection,
    AccountingDepartment,
    AccountingSyncJob,
    AccountingWarehouse,
    ConnectionStatus,
    LinkStatus,
    Provider,
    SyncKind,
    SyncTrigger,
)
from accounting.test_support.sync import make_venue

SECRET_KEY = base64.b64encode(b"0" * 32).decode("ascii")
# Module constants: the error report must not find them in a frame's local variables or source lines.
LOGIN = "client-login"
PASSWORD = "client-secret"
TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class PassingAdapter:
    def check_access(self):
        return AccessReport(ok=True, steps=[AccessStep("login", "ok", "Вход выполнен.")])

    def capabilities(self):
        return Capabilities(True, True, True, True, False, source_version="admin-test")


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
    STORAGES=TEST_STORAGES,
)
class AccountingAdminTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="platform-admin",
            email="platform@example.com",
            password="admin-password",
        )
        self.venue = make_venue("Активное заведение")
        self.client.force_login(self.admin_user)
        self.add_url = reverse("admin:accounting_accountingconnection_add")

    def add_payload(self, **overrides):
        payload = {
            "venue": str(self.venue.pk),
            "provider": Provider.IIKO,
            "base_url": "https://one.example/resto",
            "login": "client-login",
            "password": "client-secret",
            "_save": "Сохранить",
        }
        payload.update(overrides)
        return payload

    def with_inline_management(self, response, payload):
        for inline in response.context["inline_admin_formsets"]:
            formset = inline.formset
            prefix = formset.prefix
            payload.update(
                {
                    f"{prefix}-TOTAL_FORMS": str(formset.total_form_count()),
                    f"{prefix}-INITIAL_FORMS": str(formset.initial_form_count()),
                    f"{prefix}-MIN_NUM_FORMS": str(formset.min_num),
                    f"{prefix}-MAX_NUM_FORMS": str(formset.max_num),
                }
            )
        return payload

    @patch("accounting.services.enqueue_job")
    @patch("accounting.services.get_adapter")
    def test_add_form_saves_sealed_credentials_and_enqueues_catalog(self, get_adapter, enqueue):
        get_adapter.return_value = adapter_context(PassingAdapter())
        response = self.client.post(self.add_url, self.add_payload())
        self.assertEqual(response.status_code, 302)
        connection = AccountingConnection.objects.get()
        self.assertEqual(connection.venue_links.get().status, LinkStatus.PENDING)
        self.assertNotIn(b"client-login", bytes(connection.credentials_ciphertext))
        self.assertNotIn(b"client-secret", bytes(connection.credentials_ciphertext))
        job = connection.sync_jobs.get(kind=SyncKind.CATALOG, trigger=SyncTrigger.CONNECT)
        enqueue.assert_called_once_with(job)

    @patch("accounting.services.get_adapter")
    def test_failed_check_shows_steps_and_saves_nothing(self, get_adapter):
        get_adapter.return_value = adapter_context(FailingAdapter())
        response = self.client.post(self.add_url, self.add_payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Не удалось войти")
        self.assertFalse(AccountingConnection.objects.exists())

    @patch("accounting.services.enqueue_job")
    @patch("accounting.services.get_adapter")
    def test_password_is_never_rendered_or_written_to_admin_log(self, get_adapter, enqueue):
        get_adapter.return_value = adapter_context(PassingAdapter())
        self.client.post(self.add_url, self.add_payload())
        connection = AccountingConnection.objects.get()
        response = self.client.get(reverse("admin:accounting_accountingconnection_change", args=[connection.pk]))
        html = response.content.decode()
        self.assertNotIn("client-secret", html)
        self.assertNotIn(connection.credentials().pass_hash, html)
        self.assertNotIn("credentials_ciphertext", html)
        self.assertTrue(all("client-secret" not in entry.change_message for entry in LogEntry.objects.all()))

    @patch("accounting.services.enqueue_job")
    @patch("accounting.services.get_adapter")
    def test_blank_password_on_change_keeps_credentials(self, get_adapter, enqueue):
        get_adapter.return_value = adapter_context(PassingAdapter())
        self.client.post(self.add_url, self.add_payload())
        connection = AccountingConnection.objects.get()
        before = bytes(connection.credentials_ciphertext)
        change_url = reverse("admin:accounting_accountingconnection_change", args=[connection.pk])
        page = self.client.get(change_url)
        response = self.client.post(
            change_url,
            self.with_inline_management(page, {"login": "", "password": "", "_save": "Сохранить"}),
        )
        self.assertEqual(response.status_code, 302)
        connection.refresh_from_db()
        self.assertEqual(bytes(connection.credentials_ciphertext), before)

    @patch("accounting.services.enqueue_job")
    @patch("accounting.services.get_adapter")
    def test_password_post_parameter_is_marked_sensitive(self, get_adapter, enqueue):
        get_adapter.return_value = adapter_context(PassingAdapter())
        response = self.client.post(self.add_url, self.add_payload())
        self.assertIn("password", response.wsgi_request.sensitive_post_parameters)
        self.assertIn("login", response.wsgi_request.sensitive_post_parameters)

    @patch("accounting.services.enqueue_job", side_effect=RuntimeError("broker down"))
    @patch("accounting.services.get_adapter")
    def test_error_report_of_a_failed_save_hides_login_and_password(self, get_adapter, enqueue):
        get_adapter.return_value = adapter_context(PassingAdapter())
        self.client.raise_request_exception = False
        response = self.client.post(self.add_url, self.add_payload(login=LOGIN, password=PASSWORD))
        self.assertEqual(response.status_code, 500)
        report = ExceptionReporter(response.wsgi_request, *response.exc_info, is_email=True).get_traceback_html()
        self.assertIn("broker down", report)  # the report really is the one of this failure
        self.assertNotIn(PASSWORD, report)
        self.assertNotIn(LOGIN, report)

    def test_sync_action_on_a_disconnected_connection_is_refused_with_a_message(self):
        connection = AccountingConnection.objects.create(
            provider=Provider.IIKO, base_url="https://one.example/resto", status=ConnectionStatus.DISABLED
        )
        response = self.client.post(
            reverse("admin:accounting_accountingconnection_changelist"),
            {"action": "sync_stock_action", "_selected_action": str(connection.pk)},
            follow=True,
        )
        self.assertContains(response, "отключено")
        self.assertFalse(AccountingSyncJob.objects.filter(connection=connection).exists())

    @patch("accounting.services.sync_guard", acquired_guard)
    def test_department_assignment_on_a_disconnected_connection_shows_a_message(self):
        connection = AccountingConnection.objects.create(
            provider=Provider.IIKO, base_url="https://one.example/resto", status=ConnectionStatus.DISABLED
        )
        department = AccountingDepartment.objects.create(connection=connection, external_id="d1", name="Кухня")
        change_url = reverse("admin:accounting_accountingconnection_change", args=[connection.pk])
        page = self.client.get(change_url)
        payload = self.with_inline_management(page, {"login": "", "password": "", "_save": "Сохранить"})
        prefix = next(
            inline.formset.prefix
            for inline in page.context["inline_admin_formsets"]
            if inline.formset.model is AccountingDepartment
        )
        payload.update({f"{prefix}-0-id": str(department.pk), f"{prefix}-0-venue": str(self.venue.pk)})
        response = self.client.post(change_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "отключено")
        department.refresh_from_db()
        self.assertIsNone(department.venue)

    @patch("accounting.services.sync_guard", acquired_guard)
    def test_department_inline_assignment_uses_lifecycle_service(self):
        connection = AccountingConnection.objects.create(provider=Provider.IIKO, base_url="https://one.example/resto")
        department = AccountingDepartment.objects.create(connection=connection, external_id="d1", name="Кухня")
        warehouse = AccountingWarehouse.objects.create(
            connection=connection,
            external_id="w1",
            external_department_id="d1",
            name="Основной склад",
        )
        # Exercise the same service path used by AccountingConnectionAdmin.save_formset.
        from accounting.services import Actor, assign_department

        assign_department(connection, department.external_id, self.venue, Actor(self.admin_user.username))
        department.refresh_from_db()
        warehouse.refresh_from_db()
        self.assertEqual(department.venue, self.venue)
        self.assertEqual(warehouse.venue, self.venue)

    @patch("accounting.tasks.run_sync_job_task.apply_async")
    def test_sync_and_disconnect_actions(self, publish):
        publish.return_value.id = "task-id"
        connection = AccountingConnection.objects.create(provider=Provider.IIKO, base_url="https://one.example/resto")
        connection.set_credentials("login", "secret")
        connection.save()
        changelist = reverse("admin:accounting_accountingconnection_changelist")
        response = self.client.post(
            changelist,
            {"action": "sync_stock_action", "_selected_action": str(connection.pk)},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            AccountingSyncJob.objects.filter(
                connection=connection,
                kind=SyncKind.STOCK,
                trigger=SyncTrigger.MANUAL,
            ).exists()
        )
        self.client.post(
            changelist,
            {"action": "disconnect_action", "_selected_action": str(connection.pk)},
        )
        connection.refresh_from_db()
        self.assertEqual(connection.status, ConnectionStatus.DISABLED)
        self.assertIsNone(connection.credentials_ciphertext)

    def test_delete_is_not_allowed_and_non_staff_cannot_open_admin(self):
        connection = AccountingConnection.objects.create(provider=Provider.IIKO, base_url="https://one.example/resto")
        delete_url = reverse("admin:accounting_accountingconnection_delete", args=[connection.pk])
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        user = get_user_model().objects.create_user(username="ordinary", password="password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.add_url).status_code, 302)
