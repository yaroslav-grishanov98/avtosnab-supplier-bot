"""Tests for the venue cabinet mirror — covers the same invariants as the
supplier cabinet test suite (api/v1/suppliers/tests.py), scoped to the
"access settings" surface. See docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md."""

import shutil
import tempfile
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.core import mail
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from api.v1.venues.services import change_venue_user_role, issue_venue_cabinet_tokens, revoke_venue_user
from core.models import (
    AuditActorType,
    AuditLog,
    AuditLogAction,
    AuditLogEntityType,
    EntityStatus,
    User,
    UserRole,
    Venue,
    VenueAccountTransfer,
    VenueAddress,
    VenueInviteLink,
    VenueJoinRequest,
    VenueJoinRequestStatus,
    VenueUser,
    VenueUserInvite,
    VenueUserInviteStatus,
    VenueUserRole,
)


def _make_venue(name="Venue LLC", inn=None):
    return Venue.objects.create(legal_name=name, display_name=name, inn=inn)


def _make_venue_user(email=None):
    return User.objects.create(
        email=email or f"venue-user-{uuid4()}@example.com", full_name="Venue User", role=UserRole.VENUE_USER
    )


def _jwt_bearer(user):
    return f"Bearer {RefreshToken.for_user(user).access_token}"


def _venue_cabinet_bearer(venue, user=None):
    return f"Bearer {issue_venue_cabinet_tokens(venue, user=user)['access']}"


def _venue_cabinet_owner_bearer(venue):
    """Cabinet JWT with a user_id claim resolving to an active owner — needed
    for owner-gated cabinet actions (delete, bank accounts, registry fields)."""
    owner = _make_venue_user()
    VenueUser.objects.create(venue=venue, user=owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
    return _venue_cabinet_bearer(venue, user=owner)


class VenueOwnerInvariantServiceTests(APITestCase):
    """Mirrors the single-active-owner invariant tests on the supplier side."""

    def setUp(self):
        self.venue = _make_venue()
        self.owner = _make_venue_user()
        self.manager = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=self.owner, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=self.venue, user=self.manager, role=VenueUserRole.MANAGER)

    def test_revoke_last_owner_fails(self):
        with self.assertRaises(Exception):
            revoke_venue_user(
                venue_id=self.venue.id, target_user_id=self.owner.id, requested_by_user_id=self.manager.id
            )

    def test_revoke_self_fails(self):
        with self.assertRaises(Exception):
            revoke_venue_user(
                venue_id=self.venue.id, target_user_id=self.manager.id, requested_by_user_id=self.manager.id
            )

    def test_revoke_manager_when_other_owner_exists_succeeds(self):
        membership = revoke_venue_user(
            venue_id=self.venue.id, target_user_id=self.manager.id, requested_by_user_id=self.owner.id
        )
        self.assertEqual(membership.status, EntityStatus.INACTIVE)

    def test_change_role_to_owner_demotes_previous_owner(self):
        change_venue_user_role(venue_id=self.venue.id, target_user_id=self.manager.id, new_role=VenueUserRole.OWNER)
        self.owner_membership = VenueUser.objects.get(venue=self.venue, user=self.owner)
        self.assertEqual(self.owner_membership.role, VenueUserRole.MANAGER)
        self.assertEqual(self.owner_membership.status, EntityStatus.ACTIVE)

    def test_demote_last_owner_fails(self):
        with self.assertRaises(Exception):
            change_venue_user_role(venue_id=self.venue.id, target_user_id=self.owner.id, new_role=VenueUserRole.MANAGER)


class VenueMineAPITests(APITestCase):
    url = "/api/v1/venues/mine"

    def test_rejects_unauthenticated(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_returns_empty_list_when_no_venues(self):
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_lists_venues_with_my_role(self):
        user = _make_venue_user()
        venue = _make_venue()
        VenueUser.objects.create(venue=venue, user=user, role=VenueUserRole.OWNER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["my_role"], "owner")

    def test_post_creates_venue_and_becomes_owner(self):
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {"display_name": "Кафе Ромашка", "inn": "7707083893"}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["display_name"], "Кафе Ромашка")
        self.assertEqual(body["my_role"], "owner")
        self.assertTrue(VenueUser.objects.filter(venue_id=body["id"], user=user, role=VenueUserRole.OWNER).exists())

    def test_post_rejects_non_venue_role(self):
        user = User.objects.create(email="admin@example.com", full_name="Admin", role=UserRole.ADMIN)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {"display_name": "Не моё"}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_post_rejects_duplicate_inn(self):
        _make_venue(inn="7707083893")
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {"display_name": "Другое заведение", "inn": "7707083893"}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_can_own_multiple_venues(self):
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        self.client.post(self.url, {"display_name": "Кафе 1"}, format="json")
        self.client.post(self.url, {"display_name": "Кафе 2"}, format="json")

        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)


class VenueCabinetSessionFromLoginAPITests(APITestCase):
    url = "/api/v1/venues/cabinet/session-from-login"

    def test_returns_404_without_any_venue(self):
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_defaults_to_latest_created_venue(self):
        user = _make_venue_user()
        first = _make_venue("First")
        second = _make_venue("Second")
        VenueUser.objects.create(venue=first, user=user, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=second, user=user, role=VenueUserRole.OWNER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["venue_id"], str(second.id))

    def test_explicit_venue_id_selects_that_venue(self):
        user = _make_venue_user()
        first = _make_venue("First")
        second = _make_venue("Second")
        VenueUser.objects.create(venue=first, user=user, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=second, user=user, role=VenueUserRole.OWNER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {"venue_id": str(first.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["venue_id"], str(first.id))

    def test_venue_not_belonging_to_user_returns_404(self):
        user = _make_venue_user()
        other_venue = _make_venue("Not mine")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.post(self.url, {"venue_id": str(other_venue.id)}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_issued_token_works_as_cabinet_jwt(self):
        user = _make_venue_user()
        venue = _make_venue()
        VenueUser.objects.create(venue=venue, user=user, role=VenueUserRole.OWNER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        session_response = self.client.post(self.url, {}, format="json")
        access = session_response.json()["access"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        cabinet_response = self.client.get("/api/v1/venues/cabinet/me")

        self.assertEqual(cabinet_response.status_code, 200)
        self.assertEqual(cabinet_response.json()["id"], str(venue.id))


class VenueCabinetAPITests(APITestCase):
    url = "/api/v1/venues/cabinet/me"

    def setUp(self):
        self.venue = _make_venue()

    def test_get_returns_venue(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(self.venue.id))

    def test_patch_updates_general_fields_without_identified_member(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.patch(self.url, {"description": "Уютное кафе в центре"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["description"], "Уютное кафе в центре")

    def test_patch_registry_field_without_identified_member_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.patch(self.url, {"inn": "7707083893"}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_patch_registry_field_with_identified_member_succeeds(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_owner_bearer(self.venue))

        response = self.client.patch(self.url, {"inn": "7707083893"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["inn"], "7707083893")

    def test_delete_archives_venue(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_owner_bearer(self.venue))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 204)
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.status, EntityStatus.ARCHIVED)

    def test_delete_without_identified_member_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 403)

    def test_archived_venue_cabinet_jwt_rejected(self):
        self.venue.status = EntityStatus.ARCHIVED
        self.venue.save(update_fields=["status"])
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_restore_reactivates_venue(self):
        owner = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=owner, role=VenueUserRole.OWNER)
        self.venue.status = EntityStatus.ARCHIVED
        self.venue.save(update_fields=["status"])
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/restore")

        self.assertEqual(response.status_code, 200)
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.status, EntityStatus.ACTIVE)

    def test_restore_non_archived_fails(self):
        owner = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=owner, role=VenueUserRole.OWNER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/restore")

        self.assertEqual(response.status_code, 400)


class VenueCabinetAddressCRUDTests(APITestCase):
    def setUp(self):
        self.venue = _make_venue()
        self.other = _make_venue("Other")

    def test_create_list_update_delete_cycle(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        create_response = self.client.post(
            "/api/v1/venues/cabinet/addresses",
            {"address_type": "legal", "country": "Россия", "locality": "Москва"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        address_id = create_response.data["id"]

        list_response = self.client.get("/api/v1/venues/cabinet/addresses")
        self.assertEqual(len(list_response.data), 1)

        patch_response = self.client.patch(
            f"/api/v1/venues/cabinet/addresses/{address_id}", {"locality": "Санкт-Петербург"}, format="json"
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["locality"], "Санкт-Петербург")

        delete_response = self.client.delete(f"/api/v1/venues/cabinet/addresses/{address_id}")
        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(VenueAddress.objects.filter(venue=self.venue).count(), 0)

    def test_cannot_touch_another_venues_address(self):
        address = VenueAddress.objects.create(venue=self.other, address_type="legal", locality="Казань")
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.patch(
            f"/api/v1/venues/cabinet/addresses/{address.id}", {"locality": "Пермь"}, format="json"
        )

        self.assertEqual(response.status_code, 404)


class _TemporaryMediaRootMixin:
    """Mirrors api.v1.suppliers.tests.TemporaryMediaRootMixin — small enough
    that duplicating it keeps this test module self-contained rather than
    reaching into another app's test module."""

    @classmethod
    def setUpClass(cls):
        cls._temporary_media_root = tempfile.mkdtemp(prefix="autosnab-test-media-")
        cls._temporary_media_override = override_settings(MEDIA_ROOT=cls._temporary_media_root)
        cls._temporary_media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._temporary_media_override.disable()
        shutil.rmtree(cls._temporary_media_root, ignore_errors=True)


class VenueCabinetLogoUploadTests(_TemporaryMediaRootMixin, APITestCase):
    """2026-09-13: логотип заведения расширен до галереи из нескольких фото
    (по прямой просьбе пользователя — "везде, включая оба лого"). Mirrors
    api.v1.suppliers.tests.SupplierCabinetLogoUploadTests."""

    def setUp(self):
        self.venue = _make_venue()

    def _auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

    def test_upload_sets_logo_url_and_persists_file(self):
        self._auth()
        image = SimpleUploadedFile("logo.png", b"fake-png-bytes", content_type="image/png")
        response = self.client.post("/api/v1/venues/cabinet/logo", {"logo": image}, format="multipart")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["image_url"])

        self.venue.refresh_from_db()
        self.assertEqual(self.venue.logo_url, response.data["image_url"])
        self.assertTrue(default_storage.exists(response.data["image_url"].removeprefix(settings.MEDIA_URL)))

    def test_second_upload_adds_photo_without_replacing_logo_url(self):
        self._auth()
        first = self.client.post(
            "/api/v1/venues/cabinet/logo",
            {"logo": SimpleUploadedFile("logo1.png", b"first", content_type="image/png")},
            format="multipart",
        ).data
        second = self.client.post(
            "/api/v1/venues/cabinet/logo",
            {"logo": SimpleUploadedFile("logo2.png", b"second", content_type="image/png")},
            format="multipart",
        ).data
        self.assertNotEqual(first["id"], second["id"])

        self.venue.refresh_from_db()
        self.assertEqual(self.venue.logo_url, first["image_url"])

        list_response = self.client.get("/api/v1/venues/cabinet/logo")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual({row["id"] for row in list_response.data}, {first["id"], second["id"]})

    def test_delete_photo_that_is_current_logo_falls_back_to_next_oldest(self):
        self._auth()
        first = self.client.post(
            "/api/v1/venues/cabinet/logo",
            {"logo": SimpleUploadedFile("logo1.png", b"first", content_type="image/png")},
            format="multipart",
        ).data
        second = self.client.post(
            "/api/v1/venues/cabinet/logo",
            {"logo": SimpleUploadedFile("logo2.png", b"second", content_type="image/png")},
            format="multipart",
        ).data

        response = self.client.delete(f"/api/v1/venues/cabinet/logo/{first['id']}")
        self.assertEqual(response.status_code, 204)

        self.venue.refresh_from_db()
        self.assertEqual(self.venue.logo_url, second["image_url"])

    def test_delete_last_photo_clears_logo_url(self):
        self._auth()
        only = self.client.post(
            "/api/v1/venues/cabinet/logo",
            {"logo": SimpleUploadedFile("logo.png", b"only", content_type="image/png")},
            format="multipart",
        ).data
        self.client.delete(f"/api/v1/venues/cabinet/logo/{only['id']}")
        self.venue.refresh_from_db()
        self.assertIsNone(self.venue.logo_url)

    def test_rejects_disallowed_content_type(self):
        self._auth()
        bad_file = SimpleUploadedFile("logo.exe", b"binary", content_type="application/octet-stream")
        response = self.client.post("/api/v1/venues/cabinet/logo", {"logo": bad_file}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_requires_cabinet_authentication(self):
        image = SimpleUploadedFile("logo.png", b"fake-png-bytes", content_type="image/png")
        response = self.client.post("/api/v1/venues/cabinet/logo", {"logo": image}, format="multipart")
        self.assertEqual(response.status_code, 401)


class VenueCabinetContactCRUDTests(APITestCase):
    def test_create_list_delete_cycle(self):
        venue = _make_venue()
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(venue))

        create_response = self.client.post(
            "/api/v1/venues/cabinet/contacts", {"contact_type": "phone", "value": "+79991234567"}, format="json"
        )
        self.assertEqual(create_response.status_code, 201)

        list_response = self.client.get("/api/v1/venues/cabinet/contacts")
        self.assertEqual(len(list_response.data), 1)

        delete_response = self.client.delete(f"/api/v1/venues/cabinet/contacts/{create_response.data['id']}")
        self.assertEqual(delete_response.status_code, 204)


class VenueCabinetBankAccountCRUDTests(APITestCase):
    def setUp(self):
        self.venue = _make_venue()

    def test_create_without_identified_member_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(self.venue))

        response = self.client.post(
            "/api/v1/venues/cabinet/bank-accounts",
            {"bank_name": "Тест-Банк", "account_number": "40702810000000000001", "bik": "044525225"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_create_with_identified_member_succeeds(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_owner_bearer(self.venue))

        response = self.client.post(
            "/api/v1/venues/cabinet/bank-accounts",
            {"bank_name": "Тест-Банк", "account_number": "40702810000000000001", "bik": "044525225"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)

    def test_second_primary_account_unsets_first(self):
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_owner_bearer(self.venue))
        first = self.client.post(
            "/api/v1/venues/cabinet/bank-accounts",
            {"bank_name": "Банк 1", "account_number": "1", "bik": "044525225", "is_primary": True},
            format="json",
        )
        self.client.post(
            "/api/v1/venues/cabinet/bank-accounts",
            {"bank_name": "Банк 2", "account_number": "2", "bik": "044525225", "is_primary": True},
            format="json",
        )

        list_response = self.client.get("/api/v1/venues/cabinet/bank-accounts")
        first_account = next(a for a in list_response.data if a["id"] == first.data["id"])
        self.assertFalse(first_account["is_primary"])


class VenueUserListAndRevokeAPITests(APITestCase):
    def setUp(self):
        self.venue = _make_venue()
        self.owner = _make_venue_user()
        self.manager = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=self.owner, role=VenueUserRole.OWNER)
        VenueUser.objects.create(venue=self.venue, user=self.manager, role=VenueUserRole.MANAGER)

    def test_list_team(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.get(f"/api/v1/venues/{self.venue.id}/users")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)

    def test_revoke_manager(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/users/{self.manager.id}/revoke")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            VenueUser.objects.filter(venue=self.venue, user=self.manager, status=EntityStatus.ACTIVE).exists()
        )

    def test_manager_cannot_grant_owner_role(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.manager))

        response = self.client.patch(
            f"/api/v1/venues/{self.venue.id}/users/{self.manager.id}", {"role": "owner"}, format="json"
        )

        self.assertEqual(response.status_code, 403)

    def test_owner_can_grant_owner_role(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.patch(
            f"/api/v1/venues/{self.venue.id}/users/{self.manager.id}", {"role": "owner"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.owner_membership_refreshed = VenueUser.objects.get(venue=self.venue, user=self.owner)
        self.assertEqual(self.owner_membership_refreshed.role, VenueUserRole.MANAGER)


class VenueUserInviteAPITests(APITestCase):
    def setUp(self):
        self.venue = _make_venue()
        self.owner = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=self.owner, role=VenueUserRole.OWNER)

    def test_create_invite_sends_email(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.post(
            f"/api/v1/venues/{self.venue.id}/invites", {"target_email": "new@example.com"}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])

    def test_preview_invite(self):
        invite = VenueUserInvite.objects.create(
            venue=self.venue,
            target_email="preview@example.com",
            token="preview-token",
            expires_at=timezone.now() + timedelta(hours=1),
        )

        response = self.client.get(f"/api/v1/venues/invites/{invite.token}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["target_email"], "preview@example.com")

    def test_accept_invite_requires_venue_role(self):
        non_venue_user = User.objects.create(email="wrong@example.com", full_name="Wrong", role=UserRole.SUPPLIER)
        invite = VenueUserInvite.objects.create(
            venue=self.venue,
            target_email="wrong@example.com",
            token="accept-token",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(non_venue_user))

        response = self.client.post(f"/api/v1/venues/invites/{invite.token}/accept")

        self.assertEqual(response.status_code, 403)

    def test_accept_invite_email_mismatch_fails(self):
        invitee = _make_venue_user(email="actual@example.com")
        invite = VenueUserInvite.objects.create(
            venue=self.venue,
            target_email="expected@example.com",
            token="mismatch-token",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(invitee))

        response = self.client.post(f"/api/v1/venues/invites/{invite.token}/accept")

        self.assertEqual(response.status_code, 400)

    def test_accept_invite_succeeds(self):
        invitee = _make_venue_user(email="accept@example.com")
        invite = VenueUserInvite.objects.create(
            venue=self.venue,
            target_email="accept@example.com",
            token="ok-token",
            role=VenueUserRole.MANAGER,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(invitee))

        response = self.client.post(f"/api/v1/venues/invites/{invite.token}/accept")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            VenueUser.objects.filter(
                venue=self.venue, user=invitee, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
            ).exists()
        )
        invite.refresh_from_db()
        self.assertEqual(invite.status, VenueUserInviteStatus.CLAIMED)


class VenueInviteLinkAPITests(APITestCase):
    def setUp(self):
        self.venue = _make_venue()
        self.owner = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=self.owner, role=VenueUserRole.OWNER)

    def test_get_creates_link_on_first_call(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.get(f"/api/v1/venues/{self.venue.id}/invite-link")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(VenueInviteLink.objects.filter(venue=self.venue).exists())

    def test_regenerate_changes_token(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        first = self.client.get(f"/api/v1/venues/{self.venue.id}/invite-link").data["token"]

        second = self.client.post(f"/api/v1/venues/{self.venue.id}/invite-link/regenerate").data["token"]

        self.assertNotEqual(first, second)

    def test_join_via_link_requires_venue_role(self):
        link = VenueInviteLink.objects.create(venue=self.venue, token="join-token")
        wrong_role_user = User.objects.create(email="wrong2@example.com", full_name="Wrong", role=UserRole.SUPPLIER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(wrong_role_user))

        response = self.client.post(f"/api/v1/venues/invite-link/{link.token}/join")

        self.assertEqual(response.status_code, 403)

    def test_join_via_link_succeeds(self):
        link = VenueInviteLink.objects.create(venue=self.venue, token="join-token-2", role=VenueUserRole.MANAGER)
        joiner = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(joiner))

        response = self.client.post(f"/api/v1/venues/invite-link/{link.token}/join")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(VenueUser.objects.filter(venue=self.venue, user=joiner, status=EntityStatus.ACTIVE).exists())

    def test_join_via_disabled_link_fails(self):
        link = VenueInviteLink.objects.create(venue=self.venue, token="disabled-token", status=EntityStatus.INACTIVE)
        joiner = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(joiner))

        response = self.client.post(f"/api/v1/venues/invite-link/{link.token}/join")

        self.assertEqual(response.status_code, 400)


class VenueSearchByInnAPITests(APITestCase):
    url = "/api/v1/venues/search-by-inn"

    def test_finds_active_venue_by_inn(self):
        venue = _make_venue(inn="7707083893")
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.get(self.url, {"inn": "7707083893"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(venue.id))

    def test_returns_404_when_not_found(self):
        user = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

        response = self.client.get(self.url, {"inn": "7707083893"})

        self.assertEqual(response.status_code, 404)


class VenueJoinRequestAPITests(APITestCase):
    def setUp(self):
        self.venue = _make_venue(inn="7707083893")
        self.owner = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=self.owner, role=VenueUserRole.OWNER)

    @override_settings(VENUE_JOIN_REQUEST_NOTIFICATION_EMAIL="staff@example.com")
    def test_create_join_request_notifies_fixed_address(self):
        requester = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(requester))

        response = self.client.post(
            f"/api/v1/venues/{self.venue.id}/join-requests", {"message": "Хочу доступ"}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(mail.outbox[0].to, ["staff@example.com"])

    def test_duplicate_pending_request_rejected(self):
        requester = _make_venue_user()
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(requester))
        self.client.post(f"/api/v1/venues/{self.venue.id}/join-requests", {}, format="json")

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/join-requests", {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_existing_member_cannot_request_join(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/join-requests", {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_approve_adds_member(self):
        requester = _make_venue_user()
        join_request = VenueJoinRequest.objects.create(venue=self.venue, requested_by=requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/join-requests/{join_request.id}/approve")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(VenueUser.objects.filter(venue=self.venue, user=requester, status=EntityStatus.ACTIVE).exists())

    def test_reject_does_not_add_member(self):
        requester = _make_venue_user()
        join_request = VenueJoinRequest.objects.create(venue=self.venue, requested_by=requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.post(f"/api/v1/venues/{self.venue.id}/join-requests/{join_request.id}/reject")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], VenueJoinRequestStatus.REJECTED)
        self.assertFalse(VenueUser.objects.filter(venue=self.venue, user=requester).exists())

    def test_mine_lists_requesters_own_requests(self):
        requester = _make_venue_user()
        VenueJoinRequest.objects.create(venue=self.venue, requested_by=requester)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(requester))

        response = self.client.get("/api/v1/venues/join-requests/mine")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)


class VenueAccountTransferAPITests(APITestCase):
    def setUp(self):
        self.venue = _make_venue()
        self.owner = _make_venue_user()
        VenueUser.objects.create(venue=self.venue, user=self.owner, role=VenueUserRole.OWNER)

    def initiate_url(self):
        return f"/api/v1/venues/{self.venue.id}/account-transfer"

    def claim_url(self, token):
        return f"/api/v1/venues/account-transfer/{token}/claim"

    def test_initiate_sends_email(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

        response = self.client.post(self.initiate_url(), {"target_email": "newowner@example.com"}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(mail.outbox[0].to, ["newowner@example.com"])

    def test_claim_rejects_mismatched_passwords(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        create_response = self.client.post(self.initiate_url(), {"target_email": "mismatch@example.com"}, format="json")
        token = VenueAccountTransfer.objects.get(id=create_response.data["id"]).token

        response = self.client.post(
            self.claim_url(token), {"new_password": "password123", "confirm_password": "different456"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_claim_transfers_ownership(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        create_response = self.client.post(self.initiate_url(), {"target_email": "claimed@example.com"}, format="json")
        token = VenueAccountTransfer.objects.get(id=create_response.data["id"]).token
        self.client.credentials()

        response = self.client.post(
            self.claim_url(token), {"new_password": "password123", "confirm_password": "password123"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        new_owner = User.objects.get(email="claimed@example.com")
        self.assertTrue(
            VenueUser.objects.filter(
                venue=self.venue, user=new_owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
            ).exists()
        )
        self.assertFalse(
            VenueUser.objects.filter(venue=self.venue, user=self.owner, status=EntityStatus.ACTIVE).exists()
        )

    def test_claim_resets_master_product_links_on_transferred_venue(self):
        from api.v1.catalog.services import create_master_product, link_master_product_to_venue

        master_product = create_master_product(owner=self.owner, canonical_name="Мука")
        matrix_item = link_master_product_to_venue(master_product=master_product, venue=self.venue)
        self.assertEqual(matrix_item.master_product_id, master_product.id)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))
        create_response = self.client.post(self.initiate_url(), {"target_email": "newowner@example.com"}, format="json")
        token = VenueAccountTransfer.objects.get(id=create_response.data["id"]).token
        self.client.credentials()

        response = self.client.post(
            self.claim_url(token), {"new_password": "password123", "confirm_password": "password123"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        matrix_item.refresh_from_db()
        self.assertIsNone(matrix_item.master_product_id)


# ---------------------------------------------------------------------------
# GET /api/v1/venues/inn-lookup, /api/v1/venues/address-lookup — 2026-08-19,
# Vitya asked for the venue side of the dadata-backed autofill suppliers
# already had (api/v1/suppliers/tests.py's DadataLookupByINNTests /
# DadataAddressLookupHTTPTests mirrored here; the dadata_client.py functions
# themselves moved to api/v1/dadata_client.py and are tested once, from the
# supplier side — no need to duplicate the client-level tests here too).
# ---------------------------------------------------------------------------


class VenueInnLookupHTTPTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create(email="venue-inn-admin@example.com", full_name="Admin", role=UserRole.ADMIN)

    def url(self):
        return "/api/v1/venues/inn-lookup"

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 401)

    def test_rejects_bad_checksum_inn(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.admin))
        response = self.client.get(self.url(), {"inn": "1234567890"})
        self.assertEqual(response.status_code, 400)

    def test_rejects_supplier_role(self):
        supplier_user = User.objects.create(
            email="venue-inn-notallowed@example.com", full_name="X", role=UserRole.SUPPLIER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_user))
        response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 403)

    @override_settings(DADATA_API_KEY="", DADATA_SECRET_KEY="")
    def test_returns_503_when_not_configured(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.admin))
        response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 503)

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_returns_data_when_dadata_finds_company(self):
        from unittest.mock import patch

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.admin))
        with patch("api.v1.venues.views.lookup_company_by_inn") as mock_lookup:
            mock_lookup.return_value = {
                "inn": "7707083893",
                "kpp": "770701001",
                "ogrn": "1027700132195",
                "legal_name": "ООО РОМАШКА",
                "short_name": "ООО «Ромашка»",
                "entity_type": "organization",
                "management_name": "Иванов И.И.",
                "management_post": "Директор",
                "address": "660049, Красноярский край, г Красноярск, ул Новосибирская, стр 4, оф 32",
                "address_type": "legal",
                "region": "Красноярский край",
                "district": "Емельяновский район",
                "city_type": "город",
                "city": "Красноярск",
                "street": "ул Новосибирская, стр 4",
                "apartment_office": "оф 32",
                "postal_code": "660049",
                "status": "ACTIVE",
                "okved": "46.90",
                "okpo": "12345678",
                "registration_date": "2015-03-12",
            }
            response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["legal_name"], "ООО РОМАШКА")
        self.assertEqual(response.data["entity_type"], "organization")
        self.assertEqual(response.data["okved"], "46.90")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_returns_404_when_dadata_finds_nothing(self):
        from unittest.mock import patch

        from api.v1.dadata_client import INNLookupError

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.admin))
        with patch("api.v1.venues.views.lookup_company_by_inn") as mock_lookup:
            mock_lookup.side_effect = INNLookupError("Компания с таким ИНН не найдена в dadata.")
            response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 404)

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_allows_venue_user_role(self):
        from unittest.mock import patch

        venue_user = _make_venue_user(email="venue-inn-allowed@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))
        with patch("api.v1.venues.views.lookup_company_by_inn") as mock_lookup:
            mock_lookup.return_value = {
                "inn": "7707083893",
                "kpp": None,
                "ogrn": None,
                "legal_name": "ООО РОМАШКА",
                "short_name": None,
                "entity_type": None,
                "management_name": None,
                "management_post": None,
                "address": None,
                "address_type": None,
                "region": None,
                "district": None,
                "city_type": None,
                "city": None,
                "street": None,
                "apartment_office": None,
                "postal_code": None,
                "status": "ACTIVE",
                "okved": None,
                "okpo": None,
                "registration_date": None,
            }
            response = self.client.get(self.url(), {"inn": "7707083893"})
        self.assertEqual(response.status_code, 200)


class VenueAddressLookupHTTPTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create(email="venue-addr-admin@example.com", full_name="Admin", role=UserRole.ADMIN)

    def url(self):
        return "/api/v1/venues/address-lookup"

    def test_rejects_request_without_auth(self):
        response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 401)

    def test_rejects_supplier_role(self):
        supplier_user = User.objects.create(
            email="venue-addr-notallowed@example.com", full_name="X", role=UserRole.SUPPLIER
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(supplier_user))
        response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 403)

    @override_settings(DADATA_API_KEY="", DADATA_SECRET_KEY="")
    def test_returns_503_when_not_configured(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.admin))
        response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 503)

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_returns_suggestions(self):
        from unittest.mock import patch

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.admin))
        with patch("api.v1.venues.views.suggest_addresses") as mock_suggest:
            mock_suggest.return_value = [
                {
                    "value": "660049, Красноярский край, г Красноярск, ул Новосибирская, д 4",
                    "country": "Россия",
                    "region": "Красноярский край",
                    "district": None,
                    "locality": "г Красноярск",
                    "street": "ул Новосибирская, д 4",
                    "apartment_office": "оф 32",
                    "postal_code": "660049",
                }
            ]
            response = self.client.get(self.url(), {"query": "Новосибирская"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["locality"], "г Красноярск")

    @override_settings(DADATA_API_KEY="test-key", DADATA_SECRET_KEY="test-secret")
    def test_empty_query_result_is_not_an_error(self):
        from unittest.mock import patch

        venue_user = _make_venue_user(email="venue-addr-allowed@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(venue_user))
        with patch("api.v1.venues.views.suggest_addresses") as mock_suggest:
            mock_suggest.return_value = []
            response = self.client.get(self.url(), {"query": "ыыы"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])


class VenueCabinetDeleteAuditLogTests(APITestCase):
    def test_delete_cabinet_records_audit_log_with_venue_actor(self):
        venue = Venue.objects.create(display_name="Кафе Тест", status=EntityStatus.ACTIVE)
        venue_user = VenueUser.objects.create(
            venue=venue,
            user=User.objects.create(email="cabinet-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER),
            role=VenueUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(venue, user=venue_user.user))
        response = self.client.delete("/api/v1/venues/cabinet/me")
        self.assertEqual(response.status_code, 204)
        log = AuditLog.objects.get(entity_type=AuditLogEntityType.VENUE, entity_id=venue.id)
        self.assertEqual(log.action, AuditLogAction.ARCHIVED)
        self.assertEqual(log.summary, "Кафе Тест")
        self.assertEqual(log.actor_type, AuditActorType.VENUE)
        self.assertEqual(log.actor_id, venue.id)


@override_settings(SERVICE_API_TOKEN="test-token")
class VenueAuditLogViewTests(APITestCase):
    def test_member_reads_archive_history(self):
        venue = Venue.objects.create(display_name="Кафе История", status=EntityStatus.ACTIVE)
        owner = User.objects.create(email="va-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        VenueUser.objects.create(venue=venue, user=owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.client.credentials(HTTP_AUTHORIZATION=_venue_cabinet_bearer(venue, user=owner))
        self.client.delete("/api/v1/venues/cabinet/me")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(owner))
        response = self.client.get(f"/api/v1/venues/{venue.id}/audit-log")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["action"], "archived")

    def test_non_member_is_denied(self):
        venue = Venue.objects.create(display_name="Кафе Чужое", status=EntityStatus.ACTIVE)
        stranger = User.objects.create(email="va-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER)
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(stranger))
        response = self.client.get(f"/api/v1/venues/{venue.id}/audit-log")
        self.assertEqual(response.status_code, 403)
