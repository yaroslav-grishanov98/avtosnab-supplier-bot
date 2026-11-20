from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import EntityStatus, User, UserRole
from suppliers.models import BotUserBinding, BotUserState, SourceChannel, Supplier, SupplierUser, SupplierUserRole


def _supplier(name):
    return Supplier.objects.create(legal_name=name, display_name=name)


def _supplier_user(email="owner@example.com"):
    return User.objects.create(
        email=email,
        full_name="Owner",
        role=UserRole.SUPPLIER,
        status=EntityStatus.ACTIVE,
    )


def _jwt_bearer(user):
    return f"Bearer {RefreshToken.for_user(user).access_token}"


@override_settings(SERVICE_API_TOKEN="test-token")
class BotUserCompanyAPITests(APITestCase):
    url = "/api/v1/bot-user-bindings/companies"

    def setUp(self):
        self.user = _supplier_user()
        self.current_supplier = _supplier("Current company")
        SupplierUser.objects.create(
            supplier=self.current_supplier,
            user=self.user,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        self.binding = BotUserBinding.objects.create(
            supplier=self.current_supplier,
            user=self.user,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            external_user_id="user-1",
            status=EntityStatus.ACTIVE,
        )

    def authenticate_bot(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def identity(self):
        return {
            "platform": SourceChannel.TELEGRAM,
            "telegram_user_id": "user-1",
            "chat_id": "chat-1",
        }

    def test_requires_service_token(self):
        response = self.client.get(self.url, self.identity())

        self.assertEqual(response.status_code, 403)

    def test_lists_only_linked_accounts_companies(self):
        second = _supplier("Second company")
        SupplierUser.objects.create(
            supplier=second,
            user=self.user,
            role=SupplierUserRole.MANAGER,
            status=EntityStatus.ACTIVE,
        )
        unrelated = _supplier("Unrelated company")
        other_user = _supplier_user("other@example.com")
        SupplierUser.objects.create(
            supplier=unrelated,
            user=other_user,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        self.authenticate_bot()

        response = self.client.get(self.url, self.identity())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["id"] for item in response.data["companies"]},
            {str(self.current_supplier.id), str(second.id)},
        )
        self.assertEqual(response.data["selected_supplier_id"], str(self.current_supplier.id))

    def test_rejects_binding_without_cabinet_account(self):
        self.binding.user = None
        self.binding.save(update_fields=["user"])
        self.authenticate_bot()

        response = self.client.post(
            self.url,
            {**self.identity(), "company": {"display_name": "New company", "inn": "7707083893"}},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "BOT_ACCOUNT_NOT_LINKED")

    def test_creates_company_for_linked_user_and_selects_it(self):
        self.authenticate_bot()

        response = self.client.post(
            self.url,
            {
                **self.identity(),
                "company": {
                    "display_name": "ПАО Сбербанк",
                    "legal_name": "ПАО Сбербанк",
                    "short_name": "Сбербанк",
                    "inn": "7707083893",
                    "legal_address": "г. Москва, ул. Вавилова, д. 19",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        created_id = response.data["company"]["id"]
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier_id=created_id,
                user=self.user,
                role=SupplierUserRole.OWNER,
                status=EntityStatus.ACTIVE,
            ).exists()
        )
        self.binding.refresh_from_db()
        self.assertEqual(str(self.binding.supplier_id), created_id)
        state = BotUserState.objects.get(platform=SourceChannel.TELEGRAM, chat_id="chat-1")
        self.assertEqual(state.payload["selected_company_id"], created_id)

        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        mine = self.client.get("/api/v1/suppliers/mine")
        self.assertEqual(mine.status_code, 200)
        self.assertIn(created_id, {item["id"] for item in mine.data})

    def test_creates_first_company_for_account_only_binding(self):
        self.binding.supplier = None
        self.binding.save(update_fields=["supplier"])
        SupplierUser.objects.filter(user=self.user).delete()
        self.authenticate_bot()

        listed = self.client.get(self.url, self.identity())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["companies"], [])
        self.assertIsNone(listed.data["selected_supplier_id"])

        response = self.client.post(
            self.url,
            {
                **self.identity(),
                "company": {
                    "display_name": "Первая компания",
                    "legal_name": "ООО Первая компания",
                    "inn": "7707083893",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.binding.refresh_from_db()
        self.assertEqual(str(self.binding.supplier_id), response.data["company"]["id"])
        self.assertTrue(
            SupplierUser.objects.filter(
                supplier_id=self.binding.supplier_id,
                user=self.user,
                role=SupplierUserRole.OWNER,
                status=EntityStatus.ACTIVE,
            ).exists()
        )

    def test_selects_existing_company_owned_by_linked_user(self):
        second = _supplier("Second company")
        SupplierUser.objects.create(
            supplier=second,
            user=self.user,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        self.authenticate_bot()

        response = self.client.patch(
            self.url,
            {**self.identity(), "supplier_id": str(second.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["binding"]["supplier_id"], str(second.id))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.supplier_id, second.id)

    def test_cannot_select_unrelated_company(self):
        unrelated = _supplier("Unrelated company")
        self.authenticate_bot()

        response = self.client.patch(
            self.url,
            {**self.identity(), "supplier_id": str(unrelated.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "BOT_COMPANY_NOT_AVAILABLE")

    def test_identity_must_match_both_chat_and_user(self):
        self.authenticate_bot()
        for identity in (
            {**self.identity(), "telegram_user_id": "different-user"},
            {**self.identity(), "chat_id": "different-chat"},
            {**self.identity(), "platform": SourceChannel.MAX},
        ):
            with self.subTest(identity=identity):
                response = self.client.get(self.url, identity)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data["code"], "BOT_USER_BINDING_NOT_FOUND")

    def test_missing_identity_is_rejected(self):
        self.authenticate_bot()
        for field in self.identity():
            identity = self.identity()
            identity.pop(field)
            self.assertEqual(self.client.get(self.url, identity).status_code, 400)

    def test_inactive_binding_and_account_are_rejected(self):
        self.authenticate_bot()
        self.binding.status = EntityStatus.INACTIVE
        self.binding.save()
        self.assertEqual(self.client.get(self.url, self.identity()).status_code, 404)
        self.binding.status = EntityStatus.ACTIVE
        self.binding.save()
        self.user.status = EntityStatus.INACTIVE
        self.user.save()
        self.assertEqual(self.client.get(self.url, self.identity()).status_code, 403)

    def test_unlinked_account_is_rejected_for_list_and_select(self):
        self.authenticate_bot()
        self.binding.user = None
        self.binding.save()
        for response in (
            self.client.get(self.url, self.identity()),
            self.client.patch(
                self.url, {**self.identity(), "supplier_id": str(self.current_supplier.id)}, format="json"
            ),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], "BOT_ACCOUNT_NOT_LINKED")

    def test_inactive_membership_or_company_cannot_be_listed_or_selected(self):
        self.authenticate_bot()
        membership = SupplierUser.objects.get(user=self.user, supplier=self.current_supplier)
        for record in (membership, self.current_supplier):
            with self.subTest(record=type(record).__name__):
                record.status = EntityStatus.INACTIVE
                record.save()
                response = self.client.get(self.url, self.identity())
                self.assertEqual(response.data["companies"], [])
                self.assertIsNone(response.data["selected_supplier_id"])
                response = self.client.patch(
                    self.url, {**self.identity(), "supplier_id": str(self.current_supplier.id)}, format="json"
                )
                self.assertEqual(response.status_code, 404)
                record.status = EntityStatus.ACTIVE
                record.save()

    def test_switch_preserves_scenario_payload_and_is_visible_to_resolve(self):
        self.authenticate_bot()
        second = _supplier("Manager company")
        SupplierUser.objects.create(
            supplier=second, user=self.user, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        state = BotUserState.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            state="awaiting_price_file",
            payload={"selected_company_id": str(self.current_supplier.id), "draft": "keep"},
        )
        response = self.client.patch(self.url, {**self.identity(), "supplier_id": str(second.id)}, format="json")
        self.assertEqual(response.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.state, "awaiting_price_file")
        self.assertEqual(state.payload, {"selected_company_id": str(second.id), "draft": "keep"})
        resolved = self.client.get("/api/v1/bot-user-bindings/resolve", self.identity())
        self.assertEqual(resolved.data["binding"]["supplier_id"], str(second.id))

    def test_state_failure_rolls_back_company_creation_and_binding(self):
        self.authenticate_bot()
        count = Supplier.objects.count()
        with patch.object(BotUserState, "save", side_effect=RuntimeError("state failed")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, {**self.identity(), "company": {"display_name": "Rollback"}}, format="json")
        self.assertEqual(Supplier.objects.count(), count)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.supplier_id, self.current_supplier.id)

    def test_invalid_inn_does_not_create_company(self):
        self.authenticate_bot()
        count = Supplier.objects.count()
        response = self.client.post(
            self.url, {**self.identity(), "company": {"display_name": "Invalid", "inn": "123"}}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Supplier.objects.count(), count)

    def test_cabinet_user_token_cannot_replace_service_token(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        self.assertEqual(self.client.get(self.url, self.identity()).status_code, 403)

    def test_company_writes_require_service_token(self):
        for credentials in ({}, {"HTTP_AUTHORIZATION": "Bearer wrong"}):
            self.client.credentials(**credentials)
            response = self.client.post(
                self.url, {**self.identity(), "company": {"display_name": "Denied"}}, format="json"
            )
            self.assertEqual(response.status_code, 403)
            response = self.client.patch(
                self.url, {**self.identity(), "supplier_id": str(self.current_supplier.id)}, format="json"
            )
            self.assertEqual(response.status_code, 403)

    def test_non_supplier_account_cannot_create_company(self):
        self.user.role = UserRole.VENUE_USER
        self.user.save()
        self.authenticate_bot()
        response = self.client.post(self.url, {**self.identity(), "company": {"display_name": "Denied"}}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_select_handles_legacy_json_string_payload(self):
        self.authenticate_bot()
        state = BotUserState.objects.create(
            platform=SourceChannel.TELEGRAM, chat_id="chat-1", payload='{"draft": "keep"}'
        )
        response = self.client.patch(
            self.url, {**self.identity(), "supplier_id": str(self.current_supplier.id)}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.payload["draft"], "keep")
        self.assertEqual(state.payload["selected_company_id"], str(self.current_supplier.id))

    def test_max_identity_is_supported(self):
        self.binding.channel = SourceChannel.MAX
        self.binding.save()
        self.authenticate_bot()
        response = self.client.patch(
            self.url,
            {**self.identity(), "platform": SourceChannel.MAX, "supplier_id": str(self.current_supplier.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(BotUserState.objects.filter(platform=SourceChannel.MAX, chat_id="chat-1").exists())


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierInnLookupServiceTokenTests(APITestCase):
    @patch("api.v1.suppliers.views.registration.lookup_company_by_inn")
    def test_invalid_token_never_calls_lookup(self, lookup_mock):
        for token in ("", "Bearer invalid-service-token"):
            self.client.credentials(HTTP_AUTHORIZATION=token)
            response = self.client.get("/api/v1/suppliers/inn-lookup", {"inn": "7707083893"})
            self.assertIn(response.status_code, (401, 403))
        lookup_mock.assert_not_called()

    @patch("api.v1.suppliers.views.registration.lookup_company_by_inn")
    def test_service_token_can_lookup_company_for_bot(self, lookup_mock):
        lookup_mock.return_value = {
            "inn": "7707083893",
            "legal_name": "ПАО Сбербанк",
            "short_name": "Сбербанк",
            "address": "г. Москва",
        }
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

        response = self.client.get("/api/v1/suppliers/inn-lookup", {"inn": "7707083893"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["legal_name"], "ПАО Сбербанк")


@override_settings(TELEGRAM_BOT_TOKEN="")
class SupplierCabinetBotLoginAccountLinkTests(APITestCase):
    def test_legacy_login_does_not_retain_previous_account(self):
        from api.v1.suppliers.services import issue_supplier_cabinet_tokens

        user = _supplier_user()
        supplier = _supplier("Legacy company")
        binding = BotUserBinding.objects.create(
            supplier=supplier,
            user=user,
            channel=SourceChannel.TELEGRAM,
            chat_id="chat-1",
            external_user_id="previous-user",
            status=EntityStatus.ACTIVE,
        )
        token = issue_supplier_cabinet_tokens(supplier)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.post(
            "/api/v1/suppliers/cabinet/bot-link/confirm-login",
            {"platform": SourceChannel.TELEGRAM, "telegram_user_id": "user-1", "chat_id": "chat-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        binding.refresh_from_db()
        self.assertIsNone(binding.user_id)

    def test_confirm_login_rejects_inactive_or_unrelated_account(self):
        from api.v1.suppliers.services import issue_supplier_cabinet_tokens

        for inactive in (False, True):
            user = _supplier_user(f"denied-{inactive}@example.com")
            supplier = _supplier(f"Denied company {inactive}")
            if inactive:
                SupplierUser.objects.create(
                    supplier=supplier, user=user, role=SupplierUserRole.OWNER, status=EntityStatus.INACTIVE
                )
            token = issue_supplier_cabinet_tokens(supplier, user=user)["access"]
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            response = self.client.post(
                "/api/v1/suppliers/cabinet/bot-link/confirm-login",
                {"platform": SourceChannel.TELEGRAM, "telegram_user_id": "user-1", "chat_id": "chat-1"},
                format="json",
            )
            self.assertEqual(response.status_code, 403)
            self.assertFalse(BotUserBinding.objects.exists())

    def test_confirm_login_persists_core_user_on_binding(self):
        user = _supplier_user()
        supplier = _supplier("Current company")
        SupplierUser.objects.create(
            supplier=supplier,
            user=user,
            role=SupplierUserRole.OWNER,
            status=EntityStatus.ACTIVE,
        )
        from api.v1.suppliers.services import issue_supplier_cabinet_tokens

        token = issue_supplier_cabinet_tokens(supplier, user=user)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.client.post(
            "/api/v1/suppliers/cabinet/bot-link/confirm-login",
            {"platform": SourceChannel.TELEGRAM, "telegram_user_id": "user-1", "chat_id": "chat-1"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="chat-1")
        self.assertEqual(binding.user, user)
