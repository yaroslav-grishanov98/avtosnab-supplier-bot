import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from handlers import orders, router
from keyboards.orders import request_detail_kb, request_list_kb
from models import UserContext

from api import orders as api

REQUEST_ID = str(uuid4())
SUPPLIER_ID = str(uuid4())


def row(**changes):
    return {
        "id": REQUEST_ID,
        "supplier_id": SUPPLIER_ID,
        "number": "З-101",
        "buyer_name": "Кафе",
        "status": "submitted",
        "viewed_at": None,
        "item_count": 1,
        "items": [{"position": 1, "name": "Молоко", "quantity": "2.5000", "unit": "л"}],
        **changes,
    }


def page(**changes):
    return {"results": [row()], "count": 21, "offset": 0, "limit": 10, **changes}


class OrderAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = UserContext(chat_id="11", telegram_user_id="22", supplier_id=SUPPLIER_ID)

    async def test_list_keeps_real_supplier_request_ids_and_page(self):
        with patch.object(api, "_request", new=AsyncMock(return_value=page(offset=10))) as request:
            result = await api.list_requests(self.context, 10)
        self.assertEqual(result["results"][0]["id"], REQUEST_ID)
        request.assert_awaited_once_with("", self.context, limit=10, offset=10)

    async def test_rejects_a_row_for_another_company(self):
        with patch.object(api, "_request", new=AsyncMock(return_value=page(results=[row(supplier_id=str(uuid4()))]))):
            with self.assertRaises(api.OrderAPIError):
                await api.list_requests(self.context)

    async def test_rejects_malformed_pagination_instead_of_empty_success(self):
        for body in ({}, page(count=-1), page(offset=10), page(results="invalid"), page(limit=100)):
            with self.subTest(body=body), patch.object(api, "_request", new=AsyncMock(return_value=body)):
                with self.assertRaises(api.OrderAPIError):
                    await api.list_requests(self.context)

    async def test_detail_cannot_return_another_request(self):
        with patch.object(api, "_request", new=AsyncMock(return_value=row(id=str(uuid4())))):
            with self.assertRaises(api.OrderAPIError):
                await api.get_request(self.context, REQUEST_ID)

    async def test_invalid_id_is_rejected_without_http(self):
        with patch.object(api, "_request", new=AsyncMock()) as request:
            with self.assertRaises(api.OrderAPIError):
                await api.get_request(self.context, "../another-company")
        request.assert_not_awaited()

    async def test_missing_company_is_not_replaced_by_demo_data(self):
        self.context.supplier_id = None
        with patch.object(api.aiohttp, "ClientSession") as session:
            with self.assertRaises(api.OrderAPIError):
                await api.list_requests(self.context)
        session.assert_not_called()

    async def test_transport_sends_user_chat_company_and_has_timeout(self):
        response = AsyncMock()
        response.status = 200
        response.json.return_value = page()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(api.aiohttp, "ClientSession", return_value=session) as factory:
            await api.list_requests(self.context)
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(
            (params["chat_id"], params["telegram_user_id"], params["supplier_id"]), ("11", "22", SUPPLIER_ID)
        )
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 20)

    async def test_timeout_and_non_json_are_controlled_errors(self):
        for error in (asyncio.TimeoutError(), ValueError("invalid JSON")):
            with patch.object(api.aiohttp, "ClientSession", side_effect=error):
                with self.assertRaises(api.OrderAPIError):
                    await api.list_requests(self.context)


class OrderHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = UserContext(chat_id="11", telegram_user_id="22", supplier_id=SUPPLIER_ID)

    async def test_real_list_and_detail_have_navigation_and_status(self):
        with (
            patch.object(orders, "list_requests", new=AsyncMock(return_value=page(offset=10))),
            patch.object(orders, "get_request", new=AsyncMock(return_value=row(status="partially_confirmed"))),
            patch.object(orders.msg, "send_message", new=AsyncMock()) as send,
        ):
            await orders.handle_list(self.context, 10)
            await orders.handle_detail(self.context, REQUEST_ID, 10)
        self.assertIn("Всего: 21", send.await_args_list[0].args[1])
        self.assertIn("Принята частично", send.await_args_list[1].args[1])
        self.assertIn("2.5 л", send.await_args_list[1].args[1])
        self.assertEqual(
            send.await_args_list[1].kwargs["reply_markup"].inline_keyboard[1][0].callback_data, "orders:10"
        )

    async def test_error_is_not_presented_as_empty_requests(self):
        with (
            patch.object(orders, "list_requests", new=AsyncMock(side_effect=api.OrderAPIError())),
            patch.object(orders.msg, "send_message", new=AsyncMock()) as send,
        ):
            await orders.handle_list(self.context)
        self.assertIn("Не удалось", send.await_args.args[1])

    async def test_detail_reuses_notification_actions_only_when_backend_allows(self):
        from notifications_main import _supplier_action_keyboard

        expected = [b.callback_data for line in _supplier_action_keyboard(REQUEST_ID).inline_keyboard for b in line]
        for capability in (True, False, None, "true"):
            with (
                self.subTest(capability=capability),
                patch.object(orders, "get_request", new=AsyncMock(return_value=row(can_respond=capability))),
                patch.object(orders.msg, "send_message", new=AsyncMock()) as send,
            ):
                await orders.handle_detail(self.context, REQUEST_ID, 10)
                keyboard = send.await_args.kwargs["reply_markup"]
                callbacks = [b.callback_data for line in keyboard.inline_keyboard for b in line]
                self.assertIn("orders:10", callbacks)
                for callback in expected:
                    self.assertEqual(callback in callbacks, capability is True)
                    self.assertLessEqual(len(callback.encode()), 64)

    async def test_deleted_last_page_returns_to_valid_page(self):
        with (
            patch.object(
                orders,
                "list_requests",
                new=AsyncMock(side_effect=[page(count=3, offset=20, results=[]), page(count=3)]),
            ) as get,
            patch.object(orders.msg, "send_message", new=AsyncMock()),
        ):
            await orders.handle_list(self.context, 20)
        self.assertEqual(get.await_args_list[-1].args[1], 0)

    async def test_large_detail_keeps_all_positions_within_telegram_limits(self):
        detail = row(comment="🍏" * 6000)
        with (
            patch.object(orders, "get_request", new=AsyncMock(return_value=detail)),
            patch.object(orders.msg, "send_message", new=AsyncMock()) as send,
        ):
            await orders.handle_detail(self.context, REQUEST_ID)
        self.assertGreater(send.await_count, 1)
        for call in send.await_args_list:
            self.assertLessEqual(len(call.args[1].encode("utf-16-le")) // 2, 4096)
        self.assertEqual("".join(call.args[1] for call in send.await_args_list).count("🍏"), 6000)

    def test_pagination_callbacks_fit_telegram_limit(self):
        keyboard = request_list_kb([row()], offset=10, count=21)
        callbacks = [b.callback_data for line in keyboard.inline_keyboard for b in line]
        self.assertIn("orders:0", callbacks)
        self.assertIn("orders:20", callbacks)
        self.assertIn(f"request_open:{REQUEST_ID}:10", callbacks)
        for value in callbacks:
            self.assertLessEqual(len(value.encode()), 64)
        self.assertEqual(request_detail_kb(REQUEST_ID, 10).inline_keyboard[1][0].callback_data, "orders:10")

    async def test_router_preserves_page_when_opening_detail(self):
        self.context.is_callback_query = True
        self.context.callback_data = f"request_open:{REQUEST_ID}:10"
        with (
            patch.object(router.StateManager, "get", new=AsyncMock(return_value={"state": "main_menu", "payload": {}})),
            patch.object(orders, "handle_detail", new=AsyncMock()) as detail,
        ):
            await router.dispatch(self.context)
        detail.assert_awaited_once_with(self.context, REQUEST_ID, 10)
