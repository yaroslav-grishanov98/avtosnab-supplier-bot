import asyncio
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from notification_state import PENDING_KEY, QUEUE_KEY, STORE_KEY, NotificationStateStore, durable_notification_state
from telegram.ext import ApplicationHandlerStop


class NotificationStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = NotificationStateStore(self.path, "bot-a")

    def test_restart_restores_date_quantities_and_queue(self):
        pending = {
            "chat_id": "1",
            "step": "confirm",
            "confirmed_delivery_date": date(2026, 10, 21),
            "item_quantities": {"item-a": Decimal("1.2500")},
        }
        queue = {"date": "2026-10-21", "index": 2, "deliveries": [{"request_id": "r"}]}
        self.store.save("1", "u", pending=pending, queue=queue)
        restarted = NotificationStateStore(self.path, "bot-a")
        self.assertEqual(restarted.load("1", "u"), {PENDING_KEY: pending, QUEUE_KEY: queue})

    def test_bot_chat_and_user_cannot_read_anothers_pending_answer(self):
        self.store.save("1", "u", pending={"chat_id": "1"})
        self.assertEqual(self.store.load("2", "u"), {})
        self.assertEqual(self.store.load("1", "another"), {})
        self.assertEqual(NotificationStateStore(self.path, "bot-b").load("1", "u"), {})

    def test_shared_checklist_does_not_expose_another_users_answer(self):
        self.store.save("1", "u", pending={"chat_id": "1"}, queue={"index": 1})
        self.assertEqual(self.store.load("1", "v"), {QUEUE_KEY: {"index": 1}})

    def test_expired_dialog_is_not_restored(self):
        with patch("notification_state.time.time", return_value=100):
            self.store.save("1", "u", pending={"chat_id": "1"})
        with patch("notification_state.time.time", return_value=100 + 8 * 86400):
            self.assertEqual(self.store.load("1", "u"), {})

    def test_completed_dialog_stays_removed_after_restart(self):
        self.store.save("1", "u", pending={"step": "date"}, queue={"index": 1})
        self.store.save("1", "u")
        self.assertEqual(NotificationStateStore(self.path, "bot-a").load("1", "u"), {})


class DurableHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = NotificationStateStore(self.path, "bot-a")
        self.app = SimpleNamespace(bot_data={STORE_KEY: self.store})
        self.update = SimpleNamespace(
            effective_message=SimpleNamespace(chat_id=1), effective_user=SimpleNamespace(id=2)
        )

    def context(self):
        return SimpleNamespace(application=self.app, user_data={}, chat_data={})

    async def test_handler_continues_saved_prompt_after_process_restart(self):
        @durable_notification_state
        async def first(update, context):
            context.user_data[PENDING_KEY] = {"chat_id": "1", "step": "delivery_date", "prompt_message_id": 55}

        await first(self.update, self.context())
        self.app = SimpleNamespace(bot_data={STORE_KEY: NotificationStateStore(self.path, "bot-a")})

        @durable_notification_state
        async def second(update, context):
            self.assertEqual(context.user_data[PENDING_KEY]["prompt_message_id"], 55)
            context.user_data[PENDING_KEY]["step"] = "confirm"

        await second(self.update, self.context())
        self.assertEqual(self.store.load("1", "2")[PENDING_KEY]["step"], "confirm")

    async def test_application_handler_stop_still_persists_completion(self):
        self.store.save("1", "2", pending={"chat_id": "1"})

        @durable_notification_state
        async def finish(update, context):
            context.user_data.pop(PENDING_KEY)
            raise ApplicationHandlerStop

        with self.assertRaises(ApplicationHandlerStop):
            await finish(self.update, self.context())
        self.assertEqual(self.store.load("1", "2"), {})

    async def test_navigation_deletes_persisted_answer_and_checklist(self):
        self.update.callback_query = None
        self.store.save("1", "2", pending={"chat_id": "1"}, queue={"index": 0})
        context = self.context()
        with (
            patch.object(main, "normalize", return_value=SimpleNamespace(chat_id="1", text="/stop")),
            patch.object(main, "dispatch", new=AsyncMock()),
        ):
            await main.handle_update(self.update, context)
        self.assertEqual(self.store.load("1", "2"), {})

    async def test_same_chat_updates_are_serialized_across_users(self):
        self.store.save("1", "2", queue={"index": 0})

        @durable_notification_state
        async def increment(update, context):
            value = context.chat_data[QUEUE_KEY]["index"]
            await asyncio.sleep(0)
            context.chat_data[QUEUE_KEY]["index"] = value + 1

        other = SimpleNamespace(effective_message=SimpleNamespace(chat_id=1), effective_user=SimpleNamespace(id=3))
        await asyncio.gather(increment(self.update, self.context()), increment(other, self.context()))
        self.assertEqual(self.store.load("1", "2")[QUEUE_KEY]["index"], 2)

    async def test_same_users_concurrent_chats_cannot_mix_persisted_answers(self):
        self.store.save("1", "2", pending={"chat_id": "1", "step": "a"})
        self.store.save("9", "2", pending={"chat_id": "9", "step": "b"})
        shared_user_data = {}

        @durable_notification_state
        async def change(update, context):
            await asyncio.sleep(0)
            context.user_data[PENDING_KEY]["step"] += "-done"

        one = SimpleNamespace(application=self.app, user_data=shared_user_data, chat_data={})
        two = SimpleNamespace(application=self.app, user_data=shared_user_data, chat_data={})
        other = SimpleNamespace(effective_message=SimpleNamespace(chat_id=9), effective_user=SimpleNamespace(id=2))
        await asyncio.gather(change(self.update, one), change(other, two))
        self.assertEqual(self.store.load("1", "2")[PENDING_KEY], {"chat_id": "1", "step": "a-done"})
        self.assertEqual(self.store.load("9", "2")[PENDING_KEY], {"chat_id": "9", "step": "b-done"})

    async def test_reply_after_restart_reaches_real_date_handler(self):
        pending = {
            "chat_id": "1",
            "step": "delivery_date",
            "prompt_message_id": 55,
            "request_id": "request-a",
            "action": "accept",
        }
        self.store.save("1", "2", pending=pending)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=2),
            effective_message=SimpleNamespace(
                chat_id=1,
                text="21.10.2026",
                reply_to_message=SimpleNamespace(message_id=55),
                reply_text=AsyncMock(return_value=SimpleNamespace(message_id=56)),
            ),
        )
        with patch("notifications_main._parse_delivery_date", return_value=date(2026, 10, 21)):
            with self.assertRaises(ApplicationHandlerStop):
                await main.handle_notification_reply(update, self.context())
        restored = NotificationStateStore(self.path, "bot-a").load("1", "2")[PENDING_KEY]
        self.assertEqual(restored["step"], "confirm")
        self.assertEqual(restored["confirmed_delivery_date"], date(2026, 10, 21))
