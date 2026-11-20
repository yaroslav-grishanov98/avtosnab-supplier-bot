import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from api.orders import LocalOrderRepository
from handlers import orders
from keyboards.menu import main_menu_kb
from keyboards.orders import request_detail_kb, request_list_kb
from models import UserContext
from texts.orders import format_request_detail, format_request_list


class LocalOrderRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = LocalOrderRepository(Path(self.temp_dir.name) / "orders.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_demo_request_is_created_with_items(self):
        requests = self.repository.list_requests("supplier-1")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "new")
        self.assertEqual(requests[0]["item_count"], 3)
        request = self.repository.get_request(requests[0]["id"], "supplier-1")
        self.assertIsNotNone(request)
        self.assertEqual(len(request["items"]), 3)
        self.assertEqual(request["items"][0]["sku"], "BP-123")

    def test_opening_request_marks_it_viewed(self):
        request_id = self.repository.list_requests("supplier-1")[0]["id"]
        self.assertTrue(self.repository.mark_viewed(request_id, "supplier-1"))
        self.assertEqual(self.repository.get_request(request_id, "supplier-1")["status"], "viewed")
        self.assertFalse(self.repository.mark_viewed(request_id, "supplier-1"))


class OrderUxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        repository = LocalOrderRepository(Path(self.temp_dir.name) / "orders.sqlite3")
        self.requests = repository.list_requests("supplier-1")
        self.request = repository.get_request(self.requests[0]["id"], "supplier-1")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_main_menu_opens_requests(self):
        button = main_menu_kb().inline_keyboard[0][0]
        self.assertEqual(button.text, "📥 Входящие заявки")
        self.assertEqual(button.callback_data, "orders")

    def test_request_list_and_detail_have_navigation(self):
        list_keyboard = request_list_kb(self.requests)
        self.assertTrue(list_keyboard.inline_keyboard[0][0].callback_data.startswith("request_open:"))
        detail_keyboard = request_detail_kb(self.request["id"])
        self.assertEqual(detail_keyboard.inline_keyboard[1][0].callback_data, "orders")

    def test_request_text_contains_buyer_and_positions(self):
        self.assertIn("новых: 1", format_request_list(self.requests))
        detail_text = format_request_detail(self.request)
        self.assertIn("ООО «АвтоТранс»", detail_text)
        self.assertIn("BP-123", detail_text)
        self.assertIn("12 компл.", detail_text)


class OrderHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = LocalOrderRepository(Path(self.temp_dir.name) / "orders.sqlite3")
        self.context = UserContext(
            chat_id="chat-1",
            telegram_user_id="user-1",
            supplier_id="supplier-1",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_list_and_detail_are_sent_without_network(self):
        send_message = AsyncMock()
        request_id = self.repository.list_requests("supplier-1")[0]["id"]
        with (
            patch.object(orders, "get_order_repository", return_value=self.repository),
            patch.object(orders.msg, "send_message", new=send_message),
        ):
            await orders.handle_list(self.context)
            await orders.handle_detail(self.context, request_id)

        self.assertEqual(send_message.await_count, 2)
        self.assertIn("Входящие заявки", send_message.await_args_list[0].args[1])
        self.assertIn("Заявка З-2026-001", send_message.await_args_list[1].args[1])
        self.assertEqual(self.repository.get_request(request_id, "supplier-1")["status"], "viewed")


if __name__ == "__main__":
    unittest.main()
