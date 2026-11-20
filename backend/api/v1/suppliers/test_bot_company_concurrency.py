"""Exercise real row locks with independent PostgreSQL connections."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from rest_framework.test import APIClient

from api.v1.suppliers import bot_companies
from core.models import EntityStatus, User, UserRole
from suppliers.models import BotUserBinding, BotUserState, SourceChannel, Supplier, SupplierUser, SupplierUserRole


@override_settings(SERVICE_API_TOKEN="test-token")
@skipUnlessDBFeature("has_select_for_update")
class BotCompanyConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create(
            email="concurrent-owner@example.com", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )
        self.current = self.company("Current")
        self.binding = BotUserBinding.objects.create(
            user=self.user,
            supplier=self.current,
            channel=SourceChannel.TELEGRAM,
            chat_id="concurrent-chat",
            external_user_id="concurrent-user",
            status=EntityStatus.ACTIVE,
        )
        BotUserState.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id=self.binding.chat_id,
            telegram_user_id=self.binding.external_user_id,
            payload={"draft": "preserved", "selected_company_id": str(self.current.id)},
        )
        self.identity = {
            "platform": SourceChannel.TELEGRAM,
            "chat_id": self.binding.chat_id,
            "telegram_user_id": self.binding.external_user_id,
        }

    def company(self, name):
        supplier = Supplier.objects.create(display_name=name, legal_name=name)
        SupplierUser.objects.create(
            user=self.user, supplier=supplier, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        return supplier

    def competing_requests(self, method, first_payload, second_payload):
        first_locked = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        pids = {}
        original_select = bot_companies._select_company
        calls = []

        def hold_first(binding, supplier):
            calls.append(str(supplier.id))
            if len(calls) == 1:
                first_locked.set()
                if not release_first.wait(15):
                    raise TimeoutError("Test did not release the first transaction")
            original_select(binding, supplier)

        def worker(label, payload):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '20s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[label] = cursor.fetchone()[0]
                if label == "second":
                    second_started.set()
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
                response = getattr(client, method)(
                    "/api/v1/bot-user-bindings/companies", {**self.identity, **payload}, format="json"
                )
                return response.status_code, response.data
            finally:
                connections.close_all()

        with patch.object(bot_companies, "_select_company", side_effect=hold_first):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(worker, "first", first_payload)
                try:
                    self.assertTrue(first_locked.wait(10), "First request did not reach the locked selection")
                    second = executor.submit(worker, "second", second_payload)
                    self.assertTrue(second_started.wait(10))
                    self.assertNotEqual(pids["first"], pids["second"])
                    deadline = time.monotonic() + 10
                    blocked = False
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pids["second"]]
                            )
                            row = cursor.fetchone()
                        if row and row[0] == "Lock":
                            blocked = True
                            break
                        if second.done():
                            break
                        threading.Event().wait(0.02)
                    self.assertTrue(blocked, "The competing request did not wait for the PostgreSQL row lock")
                finally:
                    release_first.set()
                return first.result(timeout=15), second.result(timeout=15)

    def test_competing_switches_keep_binding_and_state_consistent(self):
        first_company = self.company("First")
        second_company = self.company("Second")
        first, second = self.competing_requests(
            "patch", {"supplier_id": str(first_company.id)}, {"supplier_id": str(second_company.id)}
        )
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(second[0], 200, second[1])
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.supplier_id, second_company.id)
        state = BotUserState.objects.get(platform=SourceChannel.TELEGRAM, chat_id=self.binding.chat_id)
        self.assertEqual(state.payload, {"draft": "preserved", "selected_company_id": str(second_company.id)})

    def test_concurrent_duplicate_create_creates_only_one_company(self):
        payload = {"company": {"display_name": "Concurrent company", "inn": "7707083893"}}
        first, second = self.competing_requests("post", payload, payload)
        self.assertEqual(first[0], 201, first[1])
        self.assertEqual(second[0], 400, second[1])
        self.assertEqual(Supplier.objects.filter(inn="7707083893").count(), 1)
        self.binding.refresh_from_db()
        self.assertEqual(str(self.binding.supplier_id), first[1]["company"]["id"])
        state = BotUserState.objects.get(platform=SourceChannel.TELEGRAM, chat_id=self.binding.chat_id)
        self.assertEqual(state.payload["selected_company_id"], str(self.binding.supplier_id))
