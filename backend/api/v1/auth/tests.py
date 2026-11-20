import threading
import urllib.parse
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.core import mail, signing
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APITestCase

from api.v1.auth.services import (
    EmailDeliveryError,
    bootstrap_admin_user,
    confirm_email_verification,
    confirm_password_reset,
    initiate_password_reset,
    login_or_register_supplier_via_vk,
    login_or_register_supplier_via_yandex,
    login_or_register_venue_user_via_vk,
    login_or_register_venue_user_via_yandex,
    login_supplier,
    login_venue_user,
    register_supplier,
    register_venue_user,
    update_own_profile,
)
from api.v1.auth.vk_client import VKOAuthError
from api.v1.auth.yandex_client import YandexOAuthError
from core.models import (
    EmailVerificationToken,
    EmailVerificationTokenStatus,
    EntityStatus,
    PasswordResetToken,
    PasswordResetTokenStatus,
    User,
    UserRole,
    Venue,
)


class RegisterSupplierServiceTests(APITestCase):
    def test_creates_draft_user(self):
        user = register_supplier(email="new@example.com", password="password123")
        self.assertEqual(user.status, EntityStatus.DRAFT)
        self.assertEqual(user.role, UserRole.SUPPLIER)

    def test_hashes_password(self):
        user = register_supplier(email="new@example.com", password="password123")
        self.assertTrue(check_password("password123", user.password))

    def test_sends_verification_email(self):
        register_supplier(email="new@example.com", password="password123")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("new@example.com", mail.outbox[0].to)

    def test_creates_pending_verification_token(self):
        user = register_supplier(email="new@example.com", password="password123")
        self.assertTrue(
            EmailVerificationToken.objects.filter(
                user=user,
                status=EmailVerificationTokenStatus.PENDING,
            ).exists()
        )

    def test_already_active_email_raises_error(self):
        User.objects.create(
            email="taken@example.com",
            full_name="Taken",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            register_supplier(email="taken@example.com", password="password123")

    def test_reregister_before_confirming_resets_password(self):
        register_supplier(email="new@example.com", password="firstpass123")
        register_supplier(email="new@example.com", password="secondpass123")
        user = User.objects.get(email="new@example.com")
        self.assertTrue(check_password("secondpass123", user.password))
        self.assertEqual(User.objects.filter(email="new@example.com").count(), 1)

    def test_smtp_failure_raises_email_delivery_error_and_rolls_back(self):
        import smtplib

        with patch("api.v1.auth.services.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            with self.assertRaises(EmailDeliveryError):
                register_supplier(email="smtp-fail@example.com", password="password123")
        self.assertFalse(User.objects.filter(email="smtp-fail@example.com").exists())


class RegisterVenueUserServiceTests(APITestCase):
    """Самостоятельная регистрация заведения email+паролем — по аналогии с
    Ozon-покупателем, не заменяет admin-провижининг (login_venue_user всё
    равно принимает только email)."""

    def test_creates_draft_venue_user(self):
        user = register_venue_user(email="new-venue@example.com", password="password123")
        self.assertEqual(user.status, EntityStatus.DRAFT)
        self.assertEqual(user.role, UserRole.VENUE_USER)
        self.assertIsNone(user.username)

    def test_hashes_password(self):
        user = register_venue_user(email="new-venue@example.com", password="password123")
        self.assertTrue(check_password("password123", user.password))

    def test_sends_verification_email(self):
        register_venue_user(email="new-venue@example.com", password="password123")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("new-venue@example.com", mail.outbox[0].to)

    def test_already_active_email_raises_error(self):
        User.objects.create(
            email="taken-venue@example.com",
            full_name="Taken",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            register_venue_user(email="taken-venue@example.com", password="password123")

    def test_confirm_and_login_full_flow(self):
        user = register_venue_user(email="flow-venue@example.com", password="password123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)
        result = login_venue_user(email="flow-venue@example.com", password="password123")
        self.assertIn("access", result)

    def test_smtp_failure_raises_email_delivery_error_and_rolls_back(self):
        import smtplib

        with patch("api.v1.auth.services.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            with self.assertRaises(EmailDeliveryError):
                register_venue_user(email="smtp-fail-venue@example.com", password="password123")
        self.assertFalse(User.objects.filter(email="smtp-fail-venue@example.com").exists())


class DualRoleSameEmailTests(APITestCase):
    """2026-08-06: "сервисы для продавцов и покупателей абсолютно разные,
    нужна возможность одному email быть и поставщиком, и покупателем" —
    email уникален по (email, role) в core.User, не глобально. Каждая пара
    (email, role) — отдельный аккаунт со своим паролем/сессией."""

    def _confirm(self, user):
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)

    def test_same_email_can_register_as_both_supplier_and_venue_user(self):
        supplier = register_supplier(email="dual@example.com", password="supplierpass123")
        venue_user = register_venue_user(email="dual@example.com", password="venuepass123")

        self.assertNotEqual(supplier.id, venue_user.id)
        self.assertEqual(supplier.role, UserRole.SUPPLIER)
        self.assertEqual(venue_user.role, UserRole.VENUE_USER)
        self.assertEqual(User.objects.filter(email="dual@example.com").count(), 2)

    def test_registering_same_role_twice_still_blocked(self):
        register_supplier(email="dual-same-role@example.com", password="supplierpass123")
        self._confirm(User.objects.get(email="dual-same-role@example.com", role=UserRole.SUPPLIER))
        with self.assertRaises(DRFValidationError):
            register_supplier(email="dual-same-role@example.com", password="anotherpass123")

    def test_both_accounts_log_in_independently_with_own_passwords(self):
        supplier = register_supplier(email="dual-login@example.com", password="supplierpass123")
        venue_user = register_venue_user(email="dual-login@example.com", password="venuepass123")
        self._confirm(supplier)
        self._confirm(venue_user)

        supplier_result = login_supplier(email="dual-login@example.com", password="supplierpass123")
        venue_result = login_venue_user(email="dual-login@example.com", password="venuepass123")

        self.assertEqual(supplier_result["user_id"], supplier.id)
        self.assertEqual(venue_result["user_id"], venue_user.id)

    def test_supplier_password_does_not_unlock_venue_account(self):
        register_supplier(email="dual-cross@example.com", password="supplierpass123")
        venue_user = register_venue_user(email="dual-cross@example.com", password="venuepass123")
        self._confirm(User.objects.get(email="dual-cross@example.com", role=UserRole.SUPPLIER))
        self._confirm(venue_user)

        with self.assertRaises(DRFValidationError):
            login_venue_user(email="dual-cross@example.com", password="supplierpass123")

    def test_password_reset_resets_both_accounts_with_separate_emails(self):
        supplier = register_supplier(email="dual-reset@example.com", password="oldsupplierpass1")
        venue_user = register_venue_user(email="dual-reset@example.com", password="oldvenuepass1")
        self._confirm(supplier)
        self._confirm(venue_user)
        mail.outbox = []

        first_token = initiate_password_reset(email="dual-reset@example.com")
        self.assertEqual(len(mail.outbox), 2)

        all_tokens = list(PasswordResetToken.objects.filter(user__email="dual-reset@example.com"))
        self.assertEqual(len(all_tokens), 2)
        self.assertEqual({t.user_id for t in all_tokens}, {supplier.id, venue_user.id})
        # first_token — какой-то один из двух (порядок не гарантирован), но реально применим:
        confirm_password_reset(token=first_token.token, new_password="brandnewpassword1")
        first_token.user.refresh_from_db()
        self.assertTrue(check_password("brandnewpassword1", first_token.user.password))

    def test_update_own_email_to_address_used_by_other_role_succeeds(self):
        """Смена email supplier'а на адрес, уже занятый ЕГО ЖЕ (или чужим)
        venue_user-аккаунтом — не конфликт, конфликт только с другим supplier."""
        supplier = register_supplier(email="dual-change-from@example.com", password="password123")
        User.objects.create(
            email="dual-change-to@example.com", full_name="Venue", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
        )
        updated = update_own_profile(user=supplier, data={"email": "dual-change-to@example.com"})
        self.assertEqual(updated.email, "dual-change-to@example.com")

    def test_update_own_email_to_address_used_by_same_role_still_blocked(self):
        supplier = register_supplier(email="dual-change-from-2@example.com", password="password123")
        User.objects.create(
            email="dual-change-taken@example.com",
            full_name="Other Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            update_own_profile(user=supplier, data={"email": "dual-change-taken@example.com"})


class ConfirmEmailVerificationServiceTests(APITestCase):
    def setUp(self):
        self.user = register_supplier(email="confirm@example.com", password="password123")
        self.token = EmailVerificationToken.objects.get(user=self.user).token

    def test_activates_user(self):
        confirm_email_verification(token=self.token)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, EntityStatus.ACTIVE)

    def test_returns_jwt_pair(self):
        result = confirm_email_verification(token=self.token)
        self.assertIn("access", result)
        self.assertIn("refresh", result)

    def test_marks_token_confirmed(self):
        confirm_email_verification(token=self.token)
        verification = EmailVerificationToken.objects.get(token=self.token)
        self.assertEqual(verification.status, EmailVerificationTokenStatus.CONFIRMED)
        self.assertIsNotNone(verification.confirmed_at)

    def test_confirming_twice_raises_error(self):
        confirm_email_verification(token=self.token)
        with self.assertRaises(DRFValidationError):
            confirm_email_verification(token=self.token)

    def test_expired_token_raises_error_and_marks_expired(self):
        verification = EmailVerificationToken.objects.get(token=self.token)
        verification.expires_at = timezone.now() - timezone.timedelta(hours=1)
        verification.save(update_fields=["expires_at"])
        with self.assertRaises(DRFValidationError):
            confirm_email_verification(token=self.token)
        verification.refresh_from_db()
        self.assertEqual(verification.status, EmailVerificationTokenStatus.EXPIRED)


class LoginSupplierServiceTests(APITestCase):
    def setUp(self):
        self.user = register_supplier(email="login@example.com", password="password123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=self.user).token)

    def test_success_returns_jwt(self):
        result = login_supplier(email="login@example.com", password="password123")
        self.assertIn("access", result)
        self.assertIn("refresh", result)

    def test_wrong_password_raises_error(self):
        with self.assertRaises(DRFValidationError):
            login_supplier(email="login@example.com", password="wrongpassword")

    def test_unknown_email_raises_error(self):
        with self.assertRaises(DRFValidationError):
            login_supplier(email="ghost@example.com", password="password123")

    def test_unverified_user_cannot_login(self):
        register_supplier(email="unverified@example.com", password="password123")
        with self.assertRaises(DRFValidationError):
            login_supplier(email="unverified@example.com", password="password123")


class LoginVenueUserServiceTests(APITestCase):
    def setUp(self):
        from django.contrib.auth.hashers import make_password

        self.user = User.objects.create(
            email="staff@example.com",
            username="ivan.petrov",  # admin-provisioned аккаунт всё ещё может иметь username,
            full_name="Ivan Petrov",  # но login_venue_user его больше не смотрит — только email.
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
            password=make_password("issuedpass123"),
        )

    def test_success_returns_jwt(self):
        result = login_venue_user(email="staff@example.com", password="issuedpass123")
        self.assertIn("access", result)

    def test_response_includes_venue_when_assigned(self):
        venue = Venue.objects.create(display_name="Кафе У Ивана")
        self.user.venue = venue
        self.user.save(update_fields=["venue"])
        result = login_venue_user(email="staff@example.com", password="issuedpass123")
        self.assertEqual(result["venue_id"], venue.id)
        self.assertEqual(result["venue_name"], "Кафе У Ивана")

    def test_response_has_null_venue_when_unassigned(self):
        result = login_venue_user(email="staff@example.com", password="issuedpass123")
        self.assertIsNone(result["venue_id"])
        self.assertIsNone(result["venue_name"])

    def test_wrong_password_raises_error(self):
        with self.assertRaises(DRFValidationError):
            login_venue_user(email="staff@example.com", password="wrong")

    def test_self_registered_user_logs_in_by_email(self):
        user = register_venue_user(email="self-venue@example.com", password="password123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)
        result = login_venue_user(email="self-venue@example.com", password="password123")
        self.assertIn("access", result)
        self.assertEqual(result["username"], "self-venue@example.com")  # username=None → fallback на email

    def test_admin_provisioned_account_with_username_still_logs_in_by_email(self):
        # username у аккаунта задан (Django admin), но login_venue_user его игнорирует —
        # входить нужно только по email, даже если username тоже существует.
        result = login_venue_user(email="staff@example.com", password="issuedpass123")
        self.assertIn("access", result)

    def test_supplier_role_cannot_use_venue_login(self):
        from django.contrib.auth.hashers import make_password

        User.objects.create(
            email="supplier-only@example.com",
            username="supplier-login",
            full_name="Supplier",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
            password=make_password("pass12345"),
        )
        with self.assertRaises(DRFValidationError):
            login_venue_user(email="supplier-only@example.com", password="pass12345")

    def test_inactive_user_cannot_login(self):
        self.user.status = EntityStatus.INACTIVE
        self.user.save(update_fields=["status"])
        with self.assertRaises(DRFValidationError):
            login_venue_user(email="staff@example.com", password="issuedpass123")


class PasswordResetServiceTests(APITestCase):
    def setUp(self):
        self.user = register_supplier(email="forgot@example.com", password="oldpassword123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=self.user).token)
        mail.outbox = []

    def test_initiate_sends_email_for_known_active_user(self):
        initiate_password_reset(email="forgot@example.com")
        self.assertEqual(len(mail.outbox), 1)

    def test_initiate_silently_noops_for_unknown_email(self):
        result = initiate_password_reset(email="ghost@example.com")
        self.assertIsNone(result)
        self.assertEqual(len(mail.outbox), 0)

    def test_initiate_works_for_self_registered_venue_user(self):
        venue_user = register_venue_user(email="forgot-venue@example.com", password="oldpassword123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=venue_user).token)
        mail.outbox = []

        reset_token = initiate_password_reset(email="forgot-venue@example.com")
        self.assertIsNotNone(reset_token)
        self.assertEqual(len(mail.outbox), 1)

    def test_smtp_failure_does_not_raise_and_still_returns_token(self):
        # Не должно давать отличимый от "неизвестный email" результат наружу —
        # иначе сбой SMTP превратился бы в способ проверить существующие адреса.
        import smtplib

        with patch("api.v1.auth.services.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            reset_token = initiate_password_reset(email="forgot@example.com")
        self.assertIsNotNone(reset_token)

    def test_confirm_sets_new_password(self):
        reset_token = initiate_password_reset(email="forgot@example.com")
        confirm_password_reset(token=reset_token.token, new_password="newpassword123")
        self.user.refresh_from_db()
        self.assertTrue(check_password("newpassword123", self.user.password))

    def test_confirm_allows_login_with_new_password(self):
        reset_token = initiate_password_reset(email="forgot@example.com")
        confirm_password_reset(token=reset_token.token, new_password="newpassword123")
        result = login_supplier(email="forgot@example.com", password="newpassword123")
        self.assertIn("access", result)

    def test_confirm_marks_token_used(self):
        reset_token = initiate_password_reset(email="forgot@example.com")
        confirm_password_reset(token=reset_token.token, new_password="newpassword123")
        reset_token.refresh_from_db()
        self.assertEqual(reset_token.status, PasswordResetTokenStatus.USED)

    def test_confirm_twice_raises_error(self):
        reset_token = initiate_password_reset(email="forgot@example.com")
        confirm_password_reset(token=reset_token.token, new_password="newpassword123")
        with self.assertRaises(DRFValidationError):
            confirm_password_reset(token=reset_token.token, new_password="anotherpass123")

    def test_expired_token_raises_error(self):
        reset_token = initiate_password_reset(email="forgot@example.com")
        reset_token.expires_at = timezone.now() - timezone.timedelta(hours=1)
        reset_token.save(update_fields=["expires_at"])
        with self.assertRaises(DRFValidationError):
            confirm_password_reset(token=reset_token.token, new_password="newpassword123")
        reset_token.refresh_from_db()
        self.assertEqual(reset_token.status, PasswordResetTokenStatus.EXPIRED)


class AuthEndpointsHTTPTests(APITestCase):
    """Spot-check the HTTP wiring (urls/serializers/AllowAny) end to end."""

    def test_register_endpoint_is_public_and_creates_user(self):
        url = reverse("v1:auth:supplier-register")
        response = self.client.post(url, {"email": "http@example.com", "password": "Xk9#mQ2vLp7!"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email="http@example.com").exists())

    def test_register_endpoint_accepts_simple_password(self):
        """2026-08-31, прямая просьба пользователя: пароль должен быть простым —
        минимум 4 символа, буквы и/или цифры, без дополнительных требований
        сложности (реверс более раннего решения этой же сессии)."""
        url = reverse("v1:auth:supplier-register")
        response = self.client.post(url, {"email": "simple@example.com", "password": "1234"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email="simple@example.com").exists())

    def test_register_endpoint_rejects_password_shorter_than_4(self):
        url = reverse("v1:auth:supplier-register")
        response = self.client.post(url, {"email": "tooshort@example.com", "password": "123"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="tooshort@example.com").exists())

    def test_register_endpoint_returns_503_on_smtp_failure(self):
        import smtplib

        url = reverse("v1:auth:supplier-register")
        with patch("api.v1.auth.services.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            response = self.client.post(
                url, {"email": "smtp-fail-http@example.com", "password": "Xk9#mQ2vLp7!"}, format="json"
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("detail", response.data)

    def test_login_endpoint_rejects_bad_credentials(self):
        url = reverse("v1:auth:supplier-login")
        response = self.client.post(url, {"email": "nobody@example.com", "password": "x"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_full_register_confirm_login_flow_over_http(self):
        register_url = reverse("v1:auth:supplier-register")
        self.client.post(register_url, {"email": "flow@example.com", "password": "Xk9#mQ2vLp7!"}, format="json")

        token = EmailVerificationToken.objects.get(user__email="flow@example.com").token
        confirm_url = reverse("v1:auth:email-verification-confirm", args=[token])
        confirm_response = self.client.post(confirm_url, {}, format="json")
        self.assertEqual(confirm_response.status_code, 200)
        self.assertIn("access", confirm_response.data)

        login_url = reverse("v1:auth:supplier-login")
        login_response = self.client.post(
            login_url, {"email": "flow@example.com", "password": "Xk9#mQ2vLp7!"}, format="json"
        )
        self.assertEqual(login_response.status_code, 200)

    def test_forgot_password_returns_generic_message_regardless_of_email(self):
        url = reverse("v1:auth:forgot-password")
        response = self.client.post(url, {"email": "whoever@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("detail", response.data)

    def test_venue_login_endpoint_succeeds(self):
        from django.contrib.auth.hashers import make_password

        User.objects.create(
            email="venue-http@example.com",
            username="venue.http",  # admin-provisioned, но логин всё равно по email
            full_name="Venue Http",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
            password=make_password("issuedpass123"),
        )
        url = reverse("v1:auth:venue-login")
        response = self.client.post(
            url, {"email": "venue-http@example.com", "password": "issuedpass123"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_venue_login_endpoint_ignores_stray_username_field(self):
        # Реальный кейс: Swagger UI "Try it out" подставляет "string" в текстовые
        # поля примера. username больше не часть контракта — DRF молча
        # игнорирует неизвестное поле, вход по email проходит как обычно.
        from django.contrib.auth.hashers import make_password

        User.objects.create(
            email="stray-username@example.com",
            full_name="Stray Username Venue",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
            password=make_password("issuedpass123"),
        )
        url = reverse("v1:auth:venue-login")
        response = self.client.post(
            url,
            {"username": "string", "email": "stray-username@example.com", "password": "issuedpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_venue_login_endpoint_rejects_bad_credentials(self):
        url = reverse("v1:auth:venue-login")
        response = self.client.post(url, {"email": "nobody@example.com", "password": "x"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_venue_login_endpoint_rejects_missing_email(self):
        url = reverse("v1:auth:venue-login")
        response = self.client.post(url, {"password": "x"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_venue_register_endpoint_is_public_and_creates_user(self):
        url = reverse("v1:auth:venue-register")
        response = self.client.post(
            url, {"email": "venue-http-reg@example.com", "password": "Xk9#mQ2vLp7!"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email="venue-http-reg@example.com", role=UserRole.VENUE_USER).exists())

    def test_venue_register_endpoint_rejects_password_shorter_than_4(self):
        url = reverse("v1:auth:venue-register")
        response = self.client.post(url, {"email": "venue-tooshort@example.com", "password": "abc"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="venue-tooshort@example.com").exists())

    def test_venue_register_endpoint_returns_503_on_smtp_failure(self):
        import smtplib

        url = reverse("v1:auth:venue-register")
        with patch("api.v1.auth.services.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            response = self.client.post(
                url, {"email": "venue-smtp-fail-http@example.com", "password": "Xk9#mQ2vLp7!"}, format="json"
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("detail", response.data)

    def test_full_venue_register_confirm_login_flow_over_http(self):
        register_url = reverse("v1:auth:venue-register")
        self.client.post(register_url, {"email": "venue-flow@example.com", "password": "Xk9#mQ2vLp7!"}, format="json")

        token = EmailVerificationToken.objects.get(user__email="venue-flow@example.com").token
        confirm_url = reverse("v1:auth:email-verification-confirm", args=[token])
        confirm_response = self.client.post(confirm_url, {}, format="json")
        self.assertEqual(confirm_response.status_code, 200)

        login_url = reverse("v1:auth:venue-login")
        login_response = self.client.post(
            login_url, {"email": "venue-flow@example.com", "password": "Xk9#mQ2vLp7!"}, format="json"
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("access", login_response.data)

    def test_password_reset_confirm_endpoint_full_flow(self):
        user = register_supplier(email="reset-http@example.com", password="oldpassword123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)
        reset_token = initiate_password_reset(email="reset-http@example.com")

        url = reverse("v1:auth:password-reset-confirm", args=[reset_token.token])
        response = self.client.post(
            url, {"new_password": "newpassword456", "confirm_password": "newpassword456"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_password_reset_confirm_endpoint_rejects_mismatched_passwords(self):
        user = register_supplier(email="mismatch-http@example.com", password="oldpassword123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)
        reset_token = initiate_password_reset(email="mismatch-http@example.com")

        url = reverse("v1:auth:password-reset-confirm", args=[reset_token.token])
        response = self.client.post(
            url, {"new_password": "newpassword456", "confirm_password": "different789"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_password_reset_confirm_endpoint_accepts_simple_numeric_password(self):
        """2026-08-31, прямая просьба пользователя: пароль должен быть простым —
        минимум 4 символа, без требований сложности (реверс более раннего
        решения этой же сессии подключить строгую валидацию)."""
        user = register_supplier(email="numeric-reset-http@example.com", password="oldpassword123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)
        reset_token = initiate_password_reset(email="numeric-reset-http@example.com")

        url = reverse("v1:auth:password-reset-confirm", args=[reset_token.token])
        response = self.client.post(url, {"new_password": "1234", "confirm_password": "1234"}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_password_reset_confirm_endpoint_rejects_password_shorter_than_4(self):
        user = register_supplier(email="short-reset-http@example.com", password="oldpassword123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=user).token)
        reset_token = initiate_password_reset(email="short-reset-http@example.com")

        url = reverse("v1:auth:password-reset-confirm", args=[reset_token.token])
        response = self.client.post(url, {"new_password": "123", "confirm_password": "123"}, format="json")
        self.assertEqual(response.status_code, 400)


class CoreUserJWTAuthenticationTests(APITestCase):
    """get_user() unit coverage, independent of any specific protected view
    (api/v1/suppliers/views.py's SupplierCreateView etc. cover the full
    request/response path with CoreUserJWTAuthentication wired in)."""

    def setUp(self):
        self.user = User.objects.create(
            email="jwt-auth@example.com",
            full_name="JWT User",
            role=UserRole.SUPPLIER,
            status=EntityStatus.ACTIVE,
        )

    def _validated_token(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        return RefreshToken.for_user(user).access_token

    def test_get_user_returns_matching_core_user(self):
        from api.v1.authentication import CoreUserJWTAuthentication

        token = self._validated_token(self.user)
        resolved = CoreUserJWTAuthentication().get_user(token)
        self.assertEqual(resolved.id, self.user.id)

    def test_get_user_rejects_inactive_user(self):
        from rest_framework_simplejwt.exceptions import InvalidToken

        from api.v1.authentication import CoreUserJWTAuthentication

        self.user.status = EntityStatus.INACTIVE
        self.user.save(update_fields=["status"])
        token = self._validated_token(self.user)
        with self.assertRaises(InvalidToken):
            CoreUserJWTAuthentication().get_user(token)

    def test_get_user_rejects_token_for_deleted_user(self):
        from rest_framework_simplejwt.exceptions import InvalidToken

        from api.v1.authentication import CoreUserJWTAuthentication

        token = self._validated_token(self.user)
        self.user.delete()
        with self.assertRaises(InvalidToken):
            CoreUserJWTAuthentication().get_user(token)


class SupplierCabinetJWTAuthenticationTests(APITestCase):
    """get_user() unit coverage — mirrors CoreUserJWTAuthenticationTests above,
    never had direct tests of its own before (только косвенно через
    DualModelTokenRefreshHTTPTests)."""

    def setUp(self):
        from suppliers.models import Supplier

        self.supplier = Supplier.objects.create(
            legal_name="ООО Тест JWT", display_name="Тест JWT", status=EntityStatus.ACTIVE
        )

    def _validated_token(self, supplier):
        from rest_framework_simplejwt.tokens import RefreshToken

        refresh = RefreshToken()
        refresh["supplier_id"] = str(supplier.pk)
        return refresh.access_token

    def test_get_user_returns_matching_supplier(self):
        from api.v1.authentication import SupplierCabinetJWTAuthentication

        token = self._validated_token(self.supplier)
        resolved = SupplierCabinetJWTAuthentication().get_user(token)
        self.assertEqual(resolved.id, self.supplier.id)

    def test_get_user_rejects_inactive_supplier(self):
        from rest_framework_simplejwt.exceptions import InvalidToken

        from api.v1.authentication import SupplierCabinetJWTAuthentication

        self.supplier.status = EntityStatus.INACTIVE
        self.supplier.save(update_fields=["status"])
        token = self._validated_token(self.supplier)
        with self.assertRaises(InvalidToken):
            SupplierCabinetJWTAuthentication().get_user(token)

    def test_get_user_rejects_token_for_deleted_supplier(self):
        from rest_framework_simplejwt.exceptions import InvalidToken

        from api.v1.authentication import SupplierCabinetJWTAuthentication

        token = self._validated_token(self.supplier)
        self.supplier.delete()
        with self.assertRaises(InvalidToken):
            SupplierCabinetJWTAuthentication().get_user(token)

    def test_get_user_rejects_token_without_supplier_id_claim(self):
        from rest_framework_simplejwt.exceptions import InvalidToken
        from rest_framework_simplejwt.tokens import AccessToken

        from api.v1.authentication import SupplierCabinetJWTAuthentication

        bare_token = AccessToken()  # no supplier_id claim set
        with self.assertRaises(InvalidToken):
            SupplierCabinetJWTAuthentication().get_user(bare_token)


class UserAdminFormTests(APITestCase):
    """core/admin.py's password-hashing clean_password() had no test at all."""

    def test_blank_password_keeps_existing_hash(self):
        from django.contrib.auth.hashers import make_password

        from core.admin import UserAdminForm

        user = User.objects.create(
            email="admin-form@example.com",
            full_name="Admin Form",
            role=UserRole.VENUE_USER,
            password=make_password("original123"),
        )
        form = UserAdminForm(
            data={
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "status": user.status,
                "password": "",
            },
            instance=user,
        )
        form.is_valid()
        self.assertEqual(form.cleaned_data["password"], user.password)

    def test_plaintext_password_gets_hashed(self):
        from core.admin import UserAdminForm

        user = User(email="admin-form2@example.com", full_name="Admin Form 2", role=UserRole.VENUE_USER)
        form = UserAdminForm(
            data={
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "status": EntityStatus.ACTIVE,
                "password": "plaintext123",
            },
            instance=user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(check_password("plaintext123", form.cleaned_data["password"]))

    def test_already_hashed_password_is_kept_as_is(self):
        from django.contrib.auth.hashers import make_password

        from core.admin import UserAdminForm

        existing_hash = make_password("whatever123")
        user = User(email="admin-form3@example.com", full_name="Admin Form 3", role=UserRole.VENUE_USER)
        form = UserAdminForm(
            data={
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "status": EntityStatus.ACTIVE,
                "password": existing_hash,
            },
            instance=user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["password"], existing_hash)


class BootstrapAdminUserServiceTests(APITestCase):
    def test_creates_active_admin_by_default(self):
        result = bootstrap_admin_user(
            username="admin1", password="password123", full_name="Admin", email="admin1@example.com"
        )
        user = User.objects.get(username="admin1")
        self.assertEqual(user.role, UserRole.ADMIN)
        self.assertEqual(user.status, EntityStatus.ACTIVE)
        self.assertTrue(check_password("password123", user.password))
        self.assertIn("access", result)

    def test_second_call_is_rejected_once_admin_exists(self):
        bootstrap_admin_user(username="admin1", password="password123", full_name="Admin", email="admin1@example.com")
        with self.assertRaises(DRFValidationError):
            bootstrap_admin_user(
                username="admin2", password="password123", full_name="Другой", email="admin2@example.com"
            )
        self.assertEqual(User.objects.filter(role=UserRole.ADMIN).count(), 1)

    def test_rejected_when_an_admin_already_exists_from_elsewhere(self):
        User.objects.create(email="preexisting@example.com", full_name="Existing", role=UserRole.ADMIN)
        with self.assertRaises(DRFValidationError):
            bootstrap_admin_user(
                username="admin1", password="password123", full_name="Admin", email="admin1@example.com"
            )


@override_settings(SERVICE_API_TOKEN="test-token")
class BootstrapAdminUserAPITests(APITestCase):
    url = "/api/v1/auth/venue/bootstrap"

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def test_creates_account_and_returns_jwt(self):
        self.authenticate()
        response = self.client.post(
            self.url,
            {"username": "admin1", "password": "Xk9#mQ2vLp7!", "full_name": "Admin", "email": "admin1@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("access", response.data)
        self.assertEqual(User.objects.get(username="admin1").role, UserRole.ADMIN)

    def test_rejects_password_shorter_than_4(self):
        self.authenticate()
        response = self.client.post(
            self.url,
            {"username": "admin1", "password": "abc", "full_name": "Admin", "email": "admin1@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="admin1").exists())

    def test_rejects_without_service_token(self):
        response = self.client.post(
            self.url,
            {"username": "admin1", "password": "Xk9#mQ2vLp7!", "full_name": "Admin", "email": "admin1@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_second_call_returns_400(self):
        self.authenticate()
        self.client.post(
            self.url,
            {"username": "admin1", "password": "Xk9#mQ2vLp7!", "full_name": "Admin", "email": "admin1@example.com"},
            format="json",
        )
        response = self.client.post(
            self.url,
            {"username": "admin2", "password": "Xk9#mQ2vLp7!", "full_name": "Другой", "email": "admin2@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class BootstrapAdminUserConcurrencyTests(TransactionTestCase):
    """See CreateSupplierByOwnerConcurrencyTests (api/v1/suppliers/tests.py)
    for why TransactionTestCase + real threads is needed here."""

    def test_two_concurrent_calls_create_only_one_admin(self):
        results = []
        errors = []

        def worker(label):
            connections.close_all()
            try:
                result = bootstrap_admin_user(
                    username=f"conc-admin-{label}",
                    password="Password123!",
                    full_name="Concurrent Admin",
                    email=f"conc-admin-{label}@example.com",
                )
                results.append(result["user_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, f"expected exactly one admin created, got {results} errors={errors}")
        self.assertEqual(User.objects.filter(role=UserRole.ADMIN).count(), 1)


class OAuthLoginConcurrencyTests(TransactionTestCase):
    """See CreateSupplierByOwnerConcurrencyTests (api/v1/suppliers/tests.py)
    for why TransactionTestCase + real threads is needed here."""

    def test_two_concurrent_yandex_logins_for_same_new_email_create_only_one_user(self):
        results = []
        errors = []

        def worker(label):
            connections.close_all()
            try:
                result = login_or_register_venue_user_via_yandex(
                    yandex_id="conc-yandex-shared-id",
                    email="conc-oauth@example.com",
                    full_name="Concurrent OAuth",
                )
                results.append(result["user_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"expected no errors (no unhandled IntegrityError), got {errors}")
        self.assertEqual(len(set(results)), 1, f"expected both calls to resolve to the same user, got {results}")
        self.assertEqual(User.objects.filter(email="conc-oauth@example.com").count(), 1)


class OAuthLoginLostRaceTests(TestCase):
    """Deterministic form of OAuthLoginConcurrencyTests: the other callback commits the account after this one's
    lookup and before its insert. The loser must log into that account, not get "email already taken"."""

    def test_losing_callback_logs_into_the_account_created_by_the_winner(self):
        from api.v1.auth import services

        winner = User.objects.create(
            email="race@example.com",
            full_name="Race",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
            external_auth_id="yandex:race-id",
        )
        real_lookup = services._lookup_oauth_user
        calls = []

        def lookup_missing_the_winner_first(**kwargs):
            calls.append(kwargs)
            return None if len(calls) == 1 else real_lookup(**kwargs)

        with patch.object(services, "_lookup_oauth_user", side_effect=lookup_missing_the_winner_first):
            result = services.login_or_register_venue_user_via_yandex(
                yandex_id="race-id", email="race@example.com", full_name="Race"
            )

        self.assertEqual(result["user_id"], winner.id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(User.objects.filter(email="race@example.com").count(), 1)


class CoreUserJWTAuthenticationTokenClaimTests(APITestCase):
    def test_get_user_rejects_token_without_user_id_claim(self):
        from rest_framework_simplejwt.exceptions import InvalidToken
        from rest_framework_simplejwt.tokens import AccessToken

        from api.v1.authentication import CoreUserJWTAuthentication

        bare_token = AccessToken()  # no user_id claim set
        with self.assertRaises(InvalidToken):
            CoreUserJWTAuthentication().get_user(bare_token)


# ---------------------------------------------------------------------------
# Вход через Яндекс — доп. способ, не заменяет email/пароль и username/пароль
# ---------------------------------------------------------------------------


class LoginOrRegisterSupplierViaYandexServiceTests(APITestCase):
    def test_creates_new_active_supplier(self):
        result = login_or_register_supplier_via_yandex(
            yandex_id="12345", email="new-yandex@example.com", full_name="Иван Иванов"
        )
        user = User.objects.get(email="new-yandex@example.com")
        self.assertEqual(user.role, UserRole.SUPPLIER)
        self.assertEqual(user.status, EntityStatus.ACTIVE)
        self.assertEqual(user.external_auth_id, "yandex:12345")
        self.assertFalse(user.password)
        self.assertEqual(result["user_id"], user.id)
        self.assertIn("access", result)

    def test_links_existing_email_password_account(self):
        existing = register_supplier(email="existing@example.com", password="password123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=existing).token)

        login_or_register_supplier_via_yandex(yandex_id="99999", email="existing@example.com", full_name="X")

        existing.refresh_from_db()
        self.assertEqual(existing.external_auth_id, "yandex:99999")
        self.assertTrue(existing.password)  # password login still works afterwards

    def test_repeat_login_does_not_duplicate_user(self):
        first = login_or_register_supplier_via_yandex(yandex_id="1", email="a@example.com", full_name="A")
        second = login_or_register_supplier_via_yandex(yandex_id="1", email="a@example.com", full_name="A")
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertEqual(User.objects.filter(external_auth_id="yandex:1").count(), 1)

    def test_rejects_unconfirmed_email_account(self):
        register_supplier(email="draft@example.com", password="password123")  # оставлен status=draft
        with self.assertRaises(DRFValidationError):
            login_or_register_supplier_via_yandex(yandex_id="42", email="draft@example.com", full_name="X")

    def test_email_taken_by_different_role_creates_separate_account(self):
        """2026-08-06: email уникален по (email, role), не глобально — тот же
        email под ДРУГОЙ ролью больше не конфликт, это отдельный аккаунт
        (человек может быть одновременно поставщиком и покупателем)."""
        venue_user = User.objects.create(
            email="cross-role@example.com",
            username="venue.cross",
            full_name="Venue Person",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        result = login_or_register_supplier_via_yandex(yandex_id="55", email="cross-role@example.com", full_name="X")
        supplier = User.objects.get(email="cross-role@example.com", role=UserRole.SUPPLIER)
        self.assertEqual(result["user_id"], supplier.id)
        self.assertNotEqual(supplier.id, venue_user.id)


class LoginOrRegisterVenueUserViaYandexServiceTests(APITestCase):
    def test_creates_new_active_venue_user(self):
        result = login_or_register_venue_user_via_yandex(
            yandex_id="55555", email="new-venue@example.com", full_name="Пётр Петров"
        )
        user = User.objects.get(email="new-venue@example.com")
        self.assertEqual(user.role, UserRole.VENUE_USER)
        self.assertEqual(user.status, EntityStatus.ACTIVE)
        self.assertIsNone(user.username)
        self.assertIn("access", result)

    def test_links_existing_admin_created_account(self):
        existing = User.objects.create(
            email="admin-made@example.com",
            username="admin.made",
            full_name="Admin Made",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        login_or_register_venue_user_via_yandex(yandex_id="77777", email="admin-made@example.com", full_name="X")
        existing.refresh_from_db()
        self.assertEqual(existing.external_auth_id, "yandex:77777")

    def test_repeat_login_does_not_duplicate_user(self):
        first = login_or_register_venue_user_via_yandex(yandex_id="2", email="b@example.com", full_name="B")
        second = login_or_register_venue_user_via_yandex(yandex_id="2", email="b@example.com", full_name="B")
        self.assertEqual(first["user_id"], second["user_id"])

    def test_rejects_inactive_account(self):
        User.objects.create(
            email="inactive@example.com",
            username="inactive.user",
            full_name="Inactive",
            role=UserRole.VENUE_USER,
            status=EntityStatus.INACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            login_or_register_venue_user_via_yandex(yandex_id="3", email="inactive@example.com", full_name="X")

    def test_email_taken_by_different_role_creates_separate_account(self):
        supplier_user = register_supplier(email="cross-role-2@example.com", password="password123")
        result = login_or_register_venue_user_via_yandex(
            yandex_id="66", email="cross-role-2@example.com", full_name="X"
        )
        venue_user = User.objects.get(email="cross-role-2@example.com", role=UserRole.VENUE_USER)
        self.assertEqual(result["user_id"], venue_user.id)
        self.assertNotEqual(venue_user.id, supplier_user.id)


class OAuthExternalAuthIdScopedByRoleTests(APITestCase):
    """2026-08-09 (реальный баг от Вити): один и тот же Yandex-аккаунт
    (тот же yandex_id) логинится под intent=supplier, потом под
    intent=venue — external_auth_id зависит только от внешнего аккаунта,
    не от role, раньше первый lookup в _find_or_create_oauth_user искал по
    external_auth_id без учёта role и "захватывал" его для первой же
    залогинившейся роли навсегда. Проверено на реальном Postgres до фикса —
    вход с intent=venue тихо возвращал supplier-аккаунт."""

    def test_same_external_id_different_intents_creates_separate_accounts(self):
        supplier_result = login_or_register_supplier_via_yandex(
            yandex_id="shared-1", email="dual@example.com", full_name="Dual"
        )
        venue_result = login_or_register_venue_user_via_yandex(
            yandex_id="shared-1", email="dual@example.com", full_name="Dual"
        )

        self.assertNotEqual(supplier_result["user_id"], venue_result["user_id"])
        supplier_user = User.objects.get(pk=supplier_result["user_id"])
        venue_user = User.objects.get(pk=venue_result["user_id"])
        self.assertEqual(supplier_user.role, UserRole.SUPPLIER)
        self.assertEqual(venue_user.role, UserRole.VENUE_USER)
        self.assertEqual(supplier_user.external_auth_id, "yandex:shared-1")
        self.assertEqual(venue_user.external_auth_id, "yandex:shared-1")

    def test_venue_login_after_supplier_only_account_creates_new_venue_account(self):
        """Точный сценарий Вити: extimenn@yandex.ru — есть только supplier
        (уже привязан этот внешний id), вход с intent=venue должен создать
        отдельного venue-пользователя, а не тихо залогинить supplier'ом."""
        supplier_result = login_or_register_supplier_via_yandex(
            yandex_id="shared-2", email="onlysupplier@example.com", full_name="Only Supplier"
        )
        venue_result = login_or_register_venue_user_via_yandex(
            yandex_id="shared-2", email="onlysupplier@example.com", full_name="Only Supplier"
        )

        self.assertNotEqual(venue_result["user_id"], supplier_result["user_id"])
        venue_user = User.objects.get(pk=venue_result["user_id"])
        self.assertEqual(venue_user.role, UserRole.VENUE_USER)

    def test_repeat_login_same_role_still_reuses_same_account(self):
        first = login_or_register_supplier_via_yandex(yandex_id="shared-3", email="stable@example.com", full_name="S")
        second = login_or_register_supplier_via_yandex(yandex_id="shared-3", email="stable@example.com", full_name="S")

        self.assertEqual(first["user_id"], second["user_id"])


class YandexOAuthLoginViewHTTPTests(APITestCase):
    @override_settings(YANDEX_OAUTH_CLIENT_ID="test-client-id", YANDEX_OAUTH_REDIRECT_URI="http://x/callback")
    def test_redirects_to_yandex_with_nonce_as_state_and_sets_signed_cookie(self):
        url = reverse("v1:auth:yandex-login")
        response = self.client.get(url, {"intent": "supplier"})
        self.assertEqual(response.status_code, 302)
        parsed = urllib.parse.urlparse(response.url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "oauth.yandex.ru")
        self.assertEqual(query["client_id"], ["test-client-id"])
        nonce = query["state"][0]
        self.assertNotEqual(nonce, "supplier")  # state больше не голый intent

        cookie = response.cookies["yandex_oauth_state"]
        self.assertTrue(cookie["httponly"])
        payload = signing.loads(cookie.value, salt="yandex-oauth-state")
        self.assertEqual(payload, {"intent": "supplier", "nonce": nonce})

    def test_rejects_unknown_intent(self):
        url = reverse("v1:auth:yandex-login")
        response = self.client.get(url, {"intent": "bogus"})
        self.assertEqual(response.status_code, 400)


@override_settings(YANDEX_OAUTH_FRONTEND_CALLBACK_URL="http://frontend.example/auth/yandex/callback")
class YandexOAuthCallbackViewHTTPTests(APITestCase):
    def _query(self, response):
        return urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)

    def _start_login(self, intent):
        """Проходит шаг login тем же self.client — куки переносятся автоматически,
        имитируя один и тот же браузер на login и на callback."""
        login_response = self.client.get(reverse("v1:auth:yandex-login"), {"intent": intent})
        nonce = urllib.parse.parse_qs(urllib.parse.urlparse(login_response.url).query)["state"][0]
        return nonce

    def test_missing_code_redirects_with_error(self):
        nonce = self._start_login("supplier")
        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"state": nonce})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    def test_no_cookie_at_all_redirects_with_error(self):
        # Callback без предварительного /yandex/login — нет куки для сверки.
        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "abc", "state": "whatever"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    def test_tampered_cookie_redirects_with_error(self):
        self._start_login("supplier")
        self.client.cookies["yandex_oauth_state"] = "garbage-not-a-valid-signature"
        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "abc", "state": "whatever"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    def test_mismatched_state_rejects_as_csrf_protection(self):
        # Есть валидная кука (свой браузер прошёл login), но state не совпадает с
        # nonce в куке — имитация чужого code/state, подсунутого атакующим.
        self._start_login("supplier")
        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "abc", "state": "not-the-real-nonce"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    @patch("api.v1.auth.views.fetch_user_info")
    @patch("api.v1.auth.views.exchange_code_for_token")
    def test_supplier_happy_path_creates_user_and_redirects_with_tokens(self, mock_exchange, mock_userinfo):
        mock_exchange.return_value = "yandex-access-token"
        mock_userinfo.return_value = {"yandex_id": "111", "email": "seller@example.com", "full_name": "Seller"}
        nonce = self._start_login("supplier")

        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "abc", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertIn("access", query)
        self.assertIn("refresh", query)
        self.assertEqual(query["intent"], ["supplier"])
        self.assertTrue(User.objects.filter(email="seller@example.com", role=UserRole.SUPPLIER).exists())
        self.assertEqual(response.cookies["yandex_oauth_state"].value, "")  # кука одноразовая, чистится

    @patch("api.v1.auth.views.fetch_user_info")
    @patch("api.v1.auth.views.exchange_code_for_token")
    def test_venue_happy_path_creates_user_and_redirects_with_tokens(self, mock_exchange, mock_userinfo):
        mock_exchange.return_value = "yandex-access-token"
        mock_userinfo.return_value = {"yandex_id": "222", "email": "venue@example.com", "full_name": "Venue"}
        nonce = self._start_login("venue")

        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "abc", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertIn("access", query)
        self.assertEqual(query["intent"], ["venue"])
        self.assertTrue(User.objects.filter(email="venue@example.com", role=UserRole.VENUE_USER).exists())

    @patch("api.v1.auth.views.exchange_code_for_token")
    def test_yandex_error_redirects_with_error_message(self, mock_exchange):
        mock_exchange.side_effect = YandexOAuthError("Яндекс отклонил код авторизации.")
        nonce = self._start_login("supplier")

        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "bad-code", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertNotIn("access", query)
        self.assertIn("error", query)

    @patch("api.v1.auth.views.fetch_user_info")
    @patch("api.v1.auth.views.exchange_code_for_token")
    def test_inactive_account_redirects_with_validation_error_detail(self, mock_exchange, mock_userinfo):
        User.objects.create(
            email="inactive-venue@example.com",
            username="inactive.venue",
            full_name="Inactive Venue",
            role=UserRole.VENUE_USER,
            status=EntityStatus.INACTIVE,
        )
        mock_exchange.return_value = "yandex-access-token"
        mock_userinfo.return_value = {"yandex_id": "9", "email": "inactive-venue@example.com", "full_name": "X"}
        nonce = self._start_login("venue")

        url = reverse("v1:auth:yandex-callback")
        response = self.client.get(url, {"code": "abc", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertNotIn("access", query)
        self.assertEqual(query["error"], ["Доступ отключён. Обратитесь к своему менеджеру."])


class LoginOrRegisterSupplierViaVkServiceTests(APITestCase):
    def test_creates_new_active_supplier(self):
        result = login_or_register_supplier_via_vk(vk_id="12345", email="new-vk@example.com", full_name="Иван Иванов")
        user = User.objects.get(email="new-vk@example.com")
        self.assertEqual(user.role, UserRole.SUPPLIER)
        self.assertEqual(user.status, EntityStatus.ACTIVE)
        self.assertEqual(user.external_auth_id, "vk:12345")
        self.assertFalse(user.password)
        self.assertEqual(result["user_id"], user.id)
        self.assertIn("access", result)

    def test_links_existing_email_password_account(self):
        existing = register_supplier(email="existing-vk@example.com", password="password123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=existing).token)

        login_or_register_supplier_via_vk(vk_id="99999", email="existing-vk@example.com", full_name="X")

        existing.refresh_from_db()
        self.assertEqual(existing.external_auth_id, "vk:99999")
        self.assertTrue(existing.password)  # password login still works afterwards

    def test_repeat_login_does_not_duplicate_user(self):
        first = login_or_register_supplier_via_vk(vk_id="1", email="a-vk@example.com", full_name="A")
        second = login_or_register_supplier_via_vk(vk_id="1", email="a-vk@example.com", full_name="A")
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertEqual(User.objects.filter(external_auth_id="vk:1").count(), 1)

    def test_rejects_unconfirmed_email_account(self):
        register_supplier(email="draft-vk@example.com", password="password123")  # оставлен status=draft
        with self.assertRaises(DRFValidationError):
            login_or_register_supplier_via_vk(vk_id="42", email="draft-vk@example.com", full_name="X")

    def test_email_taken_by_different_role_creates_separate_account(self):
        venue_user = User.objects.create(
            email="cross-role-vk@example.com",
            username="venue.cross.vk",
            full_name="Venue Person",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        result = login_or_register_supplier_via_vk(vk_id="55", email="cross-role-vk@example.com", full_name="X")
        supplier = User.objects.get(email="cross-role-vk@example.com", role=UserRole.SUPPLIER)
        self.assertEqual(result["user_id"], supplier.id)
        self.assertNotEqual(supplier.id, venue_user.id)


class LoginOrRegisterVenueUserViaVkServiceTests(APITestCase):
    def test_creates_new_active_venue_user(self):
        result = login_or_register_venue_user_via_vk(
            vk_id="55555", email="new-venue-vk@example.com", full_name="Пётр Петров"
        )
        user = User.objects.get(email="new-venue-vk@example.com")
        self.assertEqual(user.role, UserRole.VENUE_USER)
        self.assertEqual(user.status, EntityStatus.ACTIVE)
        self.assertIsNone(user.username)
        self.assertIn("access", result)

    def test_links_existing_admin_created_account(self):
        existing = User.objects.create(
            email="admin-made-vk@example.com",
            username="admin.made.vk",
            full_name="Admin Made",
            role=UserRole.VENUE_USER,
            status=EntityStatus.ACTIVE,
        )
        login_or_register_venue_user_via_vk(vk_id="77777", email="admin-made-vk@example.com", full_name="X")
        existing.refresh_from_db()
        self.assertEqual(existing.external_auth_id, "vk:77777")

    def test_repeat_login_does_not_duplicate_user(self):
        first = login_or_register_venue_user_via_vk(vk_id="2", email="b-vk@example.com", full_name="B")
        second = login_or_register_venue_user_via_vk(vk_id="2", email="b-vk@example.com", full_name="B")
        self.assertEqual(first["user_id"], second["user_id"])

    def test_rejects_inactive_account(self):
        User.objects.create(
            email="inactive-vk@example.com",
            username="inactive.user.vk",
            full_name="Inactive",
            role=UserRole.VENUE_USER,
            status=EntityStatus.INACTIVE,
        )
        with self.assertRaises(DRFValidationError):
            login_or_register_venue_user_via_vk(vk_id="3", email="inactive-vk@example.com", full_name="X")

    def test_email_taken_by_different_role_creates_separate_account(self):
        supplier_user = register_supplier(email="cross-role-2-vk@example.com", password="password123")
        result = login_or_register_venue_user_via_vk(vk_id="66", email="cross-role-2-vk@example.com", full_name="X")
        venue_user = User.objects.get(email="cross-role-2-vk@example.com", role=UserRole.VENUE_USER)
        self.assertEqual(result["user_id"], venue_user.id)
        self.assertNotEqual(venue_user.id, supplier_user.id)


class VKOAuthLoginViewHTTPTests(APITestCase):
    @override_settings(VK_OAUTH_CLIENT_ID="test-vk-client-id", VK_OAUTH_REDIRECT_URI="http://x/vk-callback")
    def test_redirects_to_vk_with_nonce_as_state_pkce_and_sets_signed_cookie(self):
        url = reverse("v1:auth:vk-login")
        response = self.client.get(url, {"intent": "supplier"})
        self.assertEqual(response.status_code, 302)
        parsed = urllib.parse.urlparse(response.url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "id.vk.ru")
        self.assertEqual(query["client_id"], ["test-vk-client-id"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("code_challenge", query)
        nonce = query["state"][0]
        self.assertNotEqual(nonce, "supplier")  # state больше не голый intent

        cookie = response.cookies["vk_oauth_state"]
        self.assertTrue(cookie["httponly"])
        payload = signing.loads(cookie.value, salt="vk-oauth-state")
        self.assertEqual(payload["intent"], "supplier")
        self.assertEqual(payload["nonce"], nonce)
        self.assertIn("code_verifier", payload)  # PKCE verifier должен пережить редирект

    def test_rejects_unknown_intent(self):
        url = reverse("v1:auth:vk-login")
        response = self.client.get(url, {"intent": "bogus"})
        self.assertEqual(response.status_code, 400)


@override_settings(VK_OAUTH_FRONTEND_CALLBACK_URL="http://frontend.example/auth/vk/callback")
class VKOAuthCallbackViewHTTPTests(APITestCase):
    def _query(self, response):
        return urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)

    def _start_login(self, intent):
        """Проходит шаг login тем же self.client — куки переносятся автоматически,
        имитируя один и тот же браузер на login и на callback."""
        login_response = self.client.get(reverse("v1:auth:vk-login"), {"intent": intent})
        nonce = urllib.parse.parse_qs(urllib.parse.urlparse(login_response.url).query)["state"][0]
        return nonce

    def test_missing_code_redirects_with_error(self):
        nonce = self._start_login("supplier")
        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"state": nonce})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    def test_no_cookie_at_all_redirects_with_error(self):
        # Callback без предварительного /vk/login — нет куки для сверки.
        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "abc", "state": "whatever"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    def test_tampered_cookie_redirects_with_error(self):
        self._start_login("supplier")
        self.client.cookies["vk_oauth_state"] = "garbage-not-a-valid-signature"
        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "abc", "state": "whatever"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    def test_mismatched_state_rejects_as_csrf_protection(self):
        self._start_login("supplier")
        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "abc", "state": "not-the-real-nonce"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._query(response)["error"], ["invalid_request"])

    @patch("api.v1.auth.views.fetch_vk_user_info")
    @patch("api.v1.auth.views.exchange_code_for_vk_token")
    def test_supplier_happy_path_creates_user_and_redirects_with_tokens(self, mock_exchange, mock_userinfo):
        mock_exchange.return_value = {"access_token": "vk-access-token"}
        mock_userinfo.return_value = {"vk_id": "111", "email": "seller-vk@example.com", "full_name": "Seller"}
        nonce = self._start_login("supplier")

        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "abc", "state": nonce, "device_id": "dev-1"})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertIn("access", query)
        self.assertIn("refresh", query)
        self.assertEqual(query["intent"], ["supplier"])
        self.assertTrue(User.objects.filter(email="seller-vk@example.com", role=UserRole.SUPPLIER).exists())
        self.assertEqual(response.cookies["vk_oauth_state"].value, "")  # кука одноразовая, чистится

        # code_verifier из подписанной куки login-шага и device_id из callback
        # должны дойти до обмена кода — иначе PKCE/VK ID отклонят обмен.
        _, kwargs = mock_exchange.call_args
        self.assertIn("code_verifier", kwargs)
        self.assertEqual(kwargs["device_id"], "dev-1")

    @patch("api.v1.auth.views.fetch_vk_user_info")
    @patch("api.v1.auth.views.exchange_code_for_vk_token")
    def test_venue_happy_path_creates_user_and_redirects_with_tokens(self, mock_exchange, mock_userinfo):
        mock_exchange.return_value = {"access_token": "vk-access-token"}
        mock_userinfo.return_value = {"vk_id": "222", "email": "venue-vk@example.com", "full_name": "Venue"}
        nonce = self._start_login("venue")

        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "abc", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertIn("access", query)
        self.assertEqual(query["intent"], ["venue"])
        self.assertTrue(User.objects.filter(email="venue-vk@example.com", role=UserRole.VENUE_USER).exists())

    @patch("api.v1.auth.views.exchange_code_for_vk_token")
    def test_vk_error_redirects_with_error_message(self, mock_exchange):
        mock_exchange.side_effect = VKOAuthError("VK ID отклонил код авторизации.")
        nonce = self._start_login("supplier")

        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "bad-code", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertNotIn("access", query)
        self.assertIn("error", query)

    @patch("api.v1.auth.views.fetch_vk_user_info")
    @patch("api.v1.auth.views.exchange_code_for_vk_token")
    def test_inactive_account_redirects_with_validation_error_detail(self, mock_exchange, mock_userinfo):
        User.objects.create(
            email="inactive-venue-vk@example.com",
            username="inactive.venue.vk",
            full_name="Inactive Venue",
            role=UserRole.VENUE_USER,
            status=EntityStatus.INACTIVE,
        )
        mock_exchange.return_value = {"access_token": "vk-access-token"}
        mock_userinfo.return_value = {"vk_id": "9", "email": "inactive-venue-vk@example.com", "full_name": "X"}
        nonce = self._start_login("venue")

        url = reverse("v1:auth:vk-callback")
        response = self.client.get(url, {"code": "abc", "state": nonce})

        self.assertEqual(response.status_code, 302)
        query = self._query(response)
        self.assertNotIn("access", query)
        self.assertEqual(query["error"], ["Доступ отключён. Обратитесь к своему менеджеру."])


# ---------------------------------------------------------------------------
# POST /api/v1/token/refresh — стандартный TokenRefreshSerializer проверяет
# субъект токена через get_user_model() (AUTH_USER_MODEL), а не core.User /
# suppliers.Supplier — падал ValueError на любом refresh с claim'ом user_id.
# Найдено по реальному 500 на проде/dev-стенде (Swagger UI отдавал "undefined").
# ---------------------------------------------------------------------------


class DualModelTokenRefreshHTTPTests(APITestCase):
    def url(self):
        return reverse("v1:token-refresh")

    def test_refreshes_core_user_token(self):
        from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

        from api.v1.authentication import CoreUserJWTAuthentication

        user = User.objects.create(
            email="refresh-user@example.com", full_name="X", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
        )
        refresh = RefreshToken.for_user(user)
        response = self.client.post(self.url(), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)  # ROTATE_REFRESH_TOKENS=True

        # claim'ы должны реально перенестись на новый access/refresh — иначе
        # обновлённый токен молча перестанет работать с CoreUserJWTAuthentication
        new_access = AccessToken(response.data["access"])
        self.assertEqual(new_access["user_id"], str(user.id))
        resolved = CoreUserJWTAuthentication().get_user(new_access)
        self.assertEqual(resolved.id, user.id)

        new_refresh = RefreshToken(response.data["refresh"])
        self.assertEqual(new_refresh["user_id"], str(user.id))
        second_response = self.client.post(self.url(), {"refresh": str(new_refresh)}, format="json")
        self.assertEqual(second_response.status_code, 200)  # rotated refresh тоже должен работать

    def test_rejects_refresh_for_inactive_core_user(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        user = User.objects.create(
            email="inactive-refresh@example.com",
            full_name="X",
            role=UserRole.VENUE_USER,
            status=EntityStatus.INACTIVE,
        )
        refresh = RefreshToken.for_user(user)
        response = self.client.post(self.url(), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_rejects_refresh_for_deleted_core_user(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        user = User.objects.create(
            email="deleted-refresh@example.com", full_name="X", role=UserRole.VENUE_USER, status=EntityStatus.ACTIVE
        )
        refresh = RefreshToken.for_user(user)
        user.delete()
        response = self.client.post(self.url(), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_refreshes_supplier_cabinet_token(self):
        from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

        from api.v1.authentication import SupplierCabinetJWTAuthentication
        from suppliers.models import Supplier

        supplier = Supplier.objects.create(legal_name="ООО Ромашка", display_name="Ромашка", status=EntityStatus.ACTIVE)
        refresh = RefreshToken()
        refresh["supplier_id"] = str(supplier.pk)
        response = self.client.post(self.url(), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

        # claim supplier_id должен реально перенестись на новый access-токен —
        # иначе SupplierCabinetJWTAuthentication молча перестанет его узнавать
        new_access = AccessToken(response.data["access"])
        self.assertEqual(new_access["supplier_id"], str(supplier.pk))
        resolved = SupplierCabinetJWTAuthentication().get_user(new_access)
        self.assertEqual(resolved.id, supplier.id)

        new_refresh = RefreshToken(response.data["refresh"])
        self.assertEqual(new_refresh["supplier_id"], str(supplier.pk))
        second_response = self.client.post(self.url(), {"refresh": str(new_refresh)}, format="json")
        self.assertEqual(second_response.status_code, 200)  # rotated refresh тоже должен работать

    def test_rejects_refresh_for_inactive_supplier(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        from suppliers.models import Supplier

        supplier = Supplier.objects.create(
            legal_name="ООО Неактив", display_name="Неактив", status=EntityStatus.INACTIVE
        )
        refresh = RefreshToken()
        refresh["supplier_id"] = str(supplier.pk)
        response = self.client.post(self.url(), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_rejects_refresh_for_deleted_supplier(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        from suppliers.models import Supplier

        supplier = Supplier.objects.create(legal_name="ООО Удалён", display_name="Удалён", status=EntityStatus.ACTIVE)
        refresh = RefreshToken()
        refresh["supplier_id"] = str(supplier.pk)
        supplier.delete()
        response = self.client.post(self.url(), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 401)


# ---------------------------------------------------------------------------
# GET /api/v1/auth/me — 2026-07-14, найден по запросу "после авторизации по
# API необходимо передавать информацию о пользователе". Заодно вскрылось, что
# core.User вообще не был связан с Venue — venue_id/venue_name null, пока
# аккаунту не назначено заведение (сейчас только вручную через Django admin).
# ---------------------------------------------------------------------------


class MeViewHTTPTests(APITestCase):
    def url(self):
        return reverse("v1:auth:me")

    def _bearer(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        return f"Bearer {RefreshToken.for_user(user).access_token}"

    def test_rejects_unauthenticated(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)

    def test_returns_profile_for_venue_user_without_venue(self):
        user = User.objects.create(email="me-venue-novenue@example.com", full_name="No Venue", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user_id"], str(user.id))
        self.assertEqual(data["email"], "me-venue-novenue@example.com")
        self.assertEqual(data["role"], "venue_user")
        self.assertIsNone(data["venue_id"])
        self.assertIsNone(data["venue_name"])

    def test_returns_profile_for_venue_user_with_venue(self):
        venue = Venue.objects.create(display_name="Бар На Углу")
        user = User.objects.create(
            email="me-venue-withvenue@example.com", full_name="Has Venue", role=UserRole.VENUE_USER, venue=venue
        )
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()
        self.assertEqual(data["venue_id"], str(venue.id))
        self.assertEqual(data["venue_name"], "Бар На Углу")

    def test_returns_profile_for_supplier_with_null_venue(self):
        user = User.objects.create(email="me-supplier@example.com", full_name="Supplier", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()
        self.assertEqual(data["role"], "supplier")
        self.assertIsNone(data["venue_id"])
        self.assertFalse(data["has_supplier_company"])

    def test_has_supplier_company_true_when_active_company_exists(self):
        from suppliers.models import Supplier, SupplierUser

        user = User.objects.create(
            email="me-supplier-hascompany@example.com", full_name="Supplier", role=UserRole.SUPPLIER
        )
        supplier = Supplier.objects.create(
            legal_name="ООО Компания", display_name="Компания", status=EntityStatus.ACTIVE
        )
        SupplierUser.objects.create(user=user, supplier=supplier, role="owner", status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()
        self.assertTrue(data["has_supplier_company"])

    def test_has_supplier_company_false_when_membership_inactive(self):
        from suppliers.models import Supplier, SupplierUser

        user = User.objects.create(
            email="me-supplier-inactive@example.com", full_name="Supplier", role=UserRole.SUPPLIER
        )
        supplier = Supplier.objects.create(legal_name="ООО Неактив", display_name="Неактив", status=EntityStatus.ACTIVE)
        SupplierUser.objects.create(user=user, supplier=supplier, role="owner", status=EntityStatus.INACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()
        self.assertFalse(data["has_supplier_company"])

    def test_has_supplier_company_false_for_venue_user(self):
        user = User.objects.create(email="me-venue-hascompany@example.com", full_name="Venue", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()
        self.assertFalse(data["has_supplier_company"])

    def test_get_includes_new_profile_fields_null_by_default(self):
        user = User.objects.create(email="me-newfields@example.com", full_name="New Fields", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()
        self.assertIsNone(data["first_name"])
        self.assertIsNone(data["last_name"])
        self.assertIsNone(data["position"])
        self.assertIsNone(data["phone"])
        self.assertIsNone(data["timezone"])
        self.assertIsNone(data["telegram_user_id"])
        self.assertIsNone(data["telegram_chat_id"])
        self.assertFalse(data["telegram_linked"])
        self.assertIsNone(data["max_user_id"])
        self.assertIsNone(data["max_chat_id"])
        self.assertFalse(data["max_linked"])
        self.assertIsNotNone(data["created_at"])
        self.assertIsNotNone(data["updated_at"])
        self.assertIsNone(data["last_login_at"])


# ---------------------------------------------------------------------------
# PATCH /api/v1/auth/me — 2026-08-04, "в профиле пользователя должно быть
# заполняемые поля: имя/фамилия/должность/телефон/email + системное
# telegram". Изначально было открыто только role=supplier (явное решение
# пользователя) — расширено на role=venue_user 2026-08-19 (баг: "не могу
# менять профиль у сотрудника заведения, пишет доступно только под
# supplier" — ручка была искусственно закрыта для venue_user ещё до того,
# как появился кабинет заведения).
# ---------------------------------------------------------------------------


class MeUpdateHTTPTests(APITestCase):
    def url(self):
        return reverse("v1:auth:me")

    def _bearer(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        return f"Bearer {RefreshToken.for_user(user).access_token}"

    def _supplier(self, email="me-patch-supplier@example.com"):
        return User.objects.create(email=email, full_name="Original Name", role=UserRole.SUPPLIER)

    def test_rejects_unauthenticated(self):
        response = self.client.patch(self.url(), {"first_name": "Иван"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_rejects_admin_role(self):
        user = User.objects.create(email="me-patch-admin@example.com", full_name="Admin", role=UserRole.ADMIN)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"first_name": "Иван"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_updates_venue_user_profile(self):
        user = User.objects.create(email="me-patch-venue@example.com", full_name="Venue", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"first_name": "Иван", "last_name": "Петров"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["full_name"], "Иван Петров")
        user.refresh_from_db()
        self.assertEqual(user.full_name, "Иван Петров")

    def test_venue_user_email_change_does_not_conflict_with_same_email_supplier_account(self):
        """(email, role) — не глобально уникален (2026-08-06): смена email
        venue_user'а на адрес, уже занятый ЕГО ЖЕ или чужим supplier-аккаунтом,
        не конфликт."""
        User.objects.create(email="me-patch-venue-dual@example.com", full_name="Supplier", role=UserRole.SUPPLIER)
        user = User.objects.create(email="me-patch-venue-from@example.com", full_name="Venue", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"email": "me-patch-venue-dual@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.email, "me-patch-venue-dual@example.com")

    def test_updates_first_and_last_name_and_recomputes_full_name(self):
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"first_name": "Иван", "last_name": "Петров"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["first_name"], "Иван")
        self.assertEqual(response.data["last_name"], "Петров")
        self.assertEqual(response.data["full_name"], "Иван Петров")
        user.refresh_from_db()
        self.assertEqual(user.full_name, "Иван Петров")

    def test_updates_position_free_text_without_touching_full_name(self):
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"position": "Менеджер по закупкам"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["position"], "Менеджер по закупкам")
        self.assertEqual(response.data["full_name"], "Original Name")

    def test_updates_phone(self):
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"phone": "+79991234567"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["phone"], "+79991234567")

    def test_updates_timezone(self):
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"timezone": "Europe/Moscow"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["timezone"], "Europe/Moscow")

    def test_rejects_invalid_timezone(self):
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"timezone": "Not/A_Real_Zone"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_updates_email_when_unique(self):
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"email": "me-patch-new@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], "me-patch-new@example.com")
        user.refresh_from_db()
        self.assertEqual(user.email, "me-patch-new@example.com")

    def test_rejects_email_already_taken_by_another_account(self):
        User.objects.create(email="me-patch-taken@example.com", full_name="Other", role=UserRole.SUPPLIER)
        user = self._supplier()
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"email": "me-patch-taken@example.com"}, format="json")
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertNotEqual(user.email, "me-patch-taken@example.com")

    def test_partial_update_does_not_touch_omitted_fields(self):
        user = self._supplier()
        user.first_name = "Пётр"
        user.last_name = "Сидоров"
        user.save(update_fields=["first_name", "last_name"])
        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.patch(self.url(), {"position": "Директор"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["first_name"], "Пётр")
        self.assertEqual(response.data["last_name"], "Сидоров")
        self.assertEqual(response.data["position"], "Директор")


# ---------------------------------------------------------------------------
# last_login_at — 2026-08-04, "Последний раз заходил в сервис". Проставляется
# в _issue_jwt(), единой точке выдачи токена для всех путей входа.
# ---------------------------------------------------------------------------


class LastLoginAtTests(APITestCase):
    def setUp(self):
        self.user = register_supplier(email="lastlogin@example.com", password="password123")
        confirm_email_verification(token=EmailVerificationToken.objects.get(user=self.user).token)

    def test_null_before_any_jwt_issued(self):
        """confirm_email_verification() в setUp тоже выдаёт JWT (авто-вход
        сразу после подтверждения) — поэтому здесь регистрируем отдельного
        пользователя и не подтверждаем ему email, чтобы застать "ни разу не
        логинился"."""
        never_logged_in = register_supplier(email="never-logged-in@example.com", password="password123")
        self.assertIsNone(never_logged_in.last_login_at)

    def test_set_after_login(self):
        login_supplier(email="lastlogin@example.com", password="password123")
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login_at)

    def test_updated_on_second_login(self):
        login_supplier(email="lastlogin@example.com", password="password123")
        self.user.refresh_from_db()
        first_login = self.user.last_login_at

        login_supplier(email="lastlogin@example.com", password="password123")
        self.user.refresh_from_db()
        self.assertGreater(self.user.last_login_at, first_login)

    def test_login_does_not_bump_updated_at(self):
        """last_login_at != "последнее изменение профиля" — вход в систему
        не должен считаться правкой профиля."""
        self.user.refresh_from_db()
        updated_at_before = self.user.updated_at

        login_supplier(email="lastlogin@example.com", password="password123")
        self.user.refresh_from_db()
        self.assertEqual(self.user.updated_at, updated_at_before)


# ---------------------------------------------------------------------------
# Telegram/Max system fields — 2026-08-04, тот же BotUserBinding-механизм
# для обоих каналов (Max отличается только значением channel).
# ---------------------------------------------------------------------------


class MeBotChannelFieldsTests(APITestCase):
    def _bearer(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        return f"Bearer {RefreshToken.for_user(user).access_token}"

    def url(self):
        return reverse("v1:auth:me")

    def _make_binding(self, user, channel, chat_id, external_user_id):
        from suppliers.models import BotUserBinding, Supplier

        supplier = Supplier.objects.create(legal_name="Bot Fields LLC", display_name="Bot Fields")
        return BotUserBinding.objects.create(
            supplier=supplier,
            user=user,
            channel=channel,
            chat_id=chat_id,
            external_user_id=external_user_id,
            status=EntityStatus.ACTIVE,
        )

    def test_telegram_fields_populated_when_linked(self):
        from suppliers.models import SourceChannel

        user = User.objects.create(email="bot-tg@example.com", full_name="Bot TG", role=UserRole.SUPPLIER)
        self._make_binding(user, SourceChannel.TELEGRAM, chat_id="tg-chat-1", external_user_id="tg-user-1")

        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()

        self.assertEqual(data["telegram_user_id"], "tg-user-1")
        self.assertEqual(data["telegram_chat_id"], "tg-chat-1")
        self.assertTrue(data["telegram_linked"])
        self.assertIsNone(data["max_user_id"])
        self.assertFalse(data["max_linked"])

    def test_max_fields_populated_when_linked(self):
        from suppliers.models import SourceChannel

        user = User.objects.create(email="bot-max@example.com", full_name="Bot Max", role=UserRole.SUPPLIER)
        self._make_binding(user, SourceChannel.MAX, chat_id="max-chat-1", external_user_id="max-user-1")

        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()

        self.assertEqual(data["max_user_id"], "max-user-1")
        self.assertEqual(data["max_chat_id"], "max-chat-1")
        self.assertTrue(data["max_linked"])
        self.assertIsNone(data["telegram_user_id"])
        self.assertFalse(data["telegram_linked"])

    def test_inactive_binding_not_shown(self):
        from suppliers.models import SourceChannel

        user = User.objects.create(email="bot-inactive@example.com", full_name="Bot Inactive", role=UserRole.SUPPLIER)
        binding = self._make_binding(user, SourceChannel.TELEGRAM, chat_id="tg-chat-2", external_user_id="tg-user-2")
        binding.status = EntityStatus.INACTIVE
        binding.save(update_fields=["status"])

        self.client.credentials(HTTP_AUTHORIZATION=self._bearer(user))
        response = self.client.get(self.url())
        data = response.json()

        self.assertFalse(data["telegram_linked"])
