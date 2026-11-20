import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import notifications_main as notifications
from handlers import orders
from keyboards.orders import request_detail_kb
from models import UserContext
from telegram.error import TimedOut
from test_orders import REQUEST_ID, SUPPLIER_ID, row
from texts.orders import format_request_detail


class SupplierRequestUXTests(unittest.IsolatedAsyncioTestCase):
    def test_actions_precede_navigation_and_connection_has_a_real_next_step(self):
        keyboard = request_detail_kb(REQUEST_ID, 10, can_respond=True)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, f"supplier_action:{REQUEST_ID}:accept")
        disconnected = request_detail_kb(REQUEST_ID, response_unavailable_reason="chat_not_connected")
        callbacks = [button.callback_data for line in disconnected.inline_keyboard for button in line]
        self.assertIn("onboarding:menu", callbacks)
        self.assertFalse(any(value.startswith("supplier_action:") for value in callbacks))

    def test_card_shows_supplier_context_and_connection_reason(self):
        text = format_request_detail(
            row(supplier_name="Наш поставщик", response_unavailable_reason="chat_not_connected")
        )
        self.assertIn("Покупатель: Кафе", text)
        self.assertIn("Поставщик: Наш поставщик", text)
        self.assertIn("подключите этот чат", text)

    async def test_large_request_has_one_final_status_and_keeps_all_data(self):
        context = UserContext(chat_id="11", telegram_user_id="22", supplier_id=SUPPLIER_ID)
        with (
            patch.object(orders, "get_request", new=AsyncMock(return_value=row(comment="🍏" * 6000, can_respond=True))),
            patch.object(orders.msg, "send_message", new=AsyncMock()) as send,
        ):
            await orders.handle_detail(context, REQUEST_ID, 10)
        texts = [call.args[1] for call in send.await_args_list]
        self.assertEqual("".join(texts).count("Статус:"), 1)
        self.assertIn("Статус:", texts[-1])
        self.assertEqual("".join(texts).count("🍏"), 6000)
        self.assertEqual(send.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text, "Принять")

    def test_origin_card_state_can_be_saved_and_navigation_restored(self):
        message = SimpleNamespace(
            text="Статус: Ожидает ответа", reply_markup=request_detail_kb(REQUEST_ID, 10, can_respond=True)
        )
        self.assertEqual(
            notifications._request_card_state(message, REQUEST_ID),
            {
                "card_text": message.text,
                "card_offset": 10,
            },
        )

    async def test_long_company_details_are_preserved_without_oversized_messages(self):
        context = UserContext(chat_id="11", telegram_user_id="22", supplier_id=SUPPLIER_ID)
        detail = row(supplier_name="🍎" * 6000, can_respond=True)
        with (
            patch.object(orders, "get_request", new=AsyncMock(return_value=detail)),
            patch.object(orders.msg, "send_message", new=AsyncMock()) as send,
        ):
            await orders.handle_detail(context, REQUEST_ID)
        texts = [call.args[1] for call in send.await_args_list]
        for text in texts:
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertGreaterEqual("".join(texts).count("🍎"), 6000)
        self.assertEqual("".join(texts).count("Статус:"), 1)

    async def test_success_updates_card_from_backend_status_and_keeps_page(self):
        context = SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()))
        state = {
            "request_id": REQUEST_ID,
            "chat_id": "11",
            "action": "accept",
            "origin_message_id": 123,
            "card_text": "📄 Заявка\nСтатус: Ожидает ответа",
            "card_offset": 10,
        }
        with patch.object(
            notifications,
            "respond_to_supplier_request",
            new=AsyncMock(return_value={"success": True, "status": "confirmed"}),
        ):
            result = await notifications._submit_supplier_action(SimpleNamespace(effective_user=None), context, state)
        self.assertTrue(result["card_refreshed"])
        args = context.bot.edit_message_text.await_args.kwargs
        self.assertIn("Статус: Подтверждена", args["text"])
        self.assertEqual(args["message_id"], 123)
        self.assertIn(
            "orders:10", [button.callback_data for line in args["reply_markup"].inline_keyboard for button in line]
        )

    async def test_telegram_edit_failure_keeps_saved_reply_and_explains_next_step(self):
        context = SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock(side_effect=TimedOut())))
        state = {
            "request_id": REQUEST_ID,
            "chat_id": "11",
            "action": "accept",
            "origin_message_id": 123,
            "card_text": "Статус: Ожидает ответа",
            "card_offset": 0,
        }
        with patch.object(
            notifications,
            "respond_to_supplier_request",
            new=AsyncMock(return_value={"success": True, "status": "confirmed"}),
        ):
            result = await notifications._submit_supplier_action(SimpleNamespace(effective_user=None), context, state)
        self.assertTrue(result["success"])
        self.assertFalse(result["card_refreshed"])
        self.assertIn("/menu", notifications._action_success_text(result))

    async def test_cancel_restores_both_actions_and_navigation(self):
        context = SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))
        state = {"request_id": REQUEST_ID, "chat_id": "11", "origin_message_id": 123, "card_offset": 10}
        await notifications._restore_supplier_actions(context, state)
        keyboard = context.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for line in keyboard.inline_keyboard for button in line]
        self.assertIn("orders:10", callbacks)
        self.assertIn(f"supplier_action:{REQUEST_ID}:accept", callbacks)
