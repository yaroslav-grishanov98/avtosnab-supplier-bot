import asyncio
import logging
import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import notifications_main
from telegram.error import TimedOut

from api import notifications as notifications_api


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response, *args, **kwargs):
        self.response = response
        self.post_args = None
        self.get_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def post(self, *args, **kwargs):
        self.post_args = (args, kwargs)
        return self.response

    def get(self, *args, **kwargs):
        self.get_args = (args, kwargs)
        return self.response


class NotificationConnectApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_requests_current_chat_connection(self):
        """Проверить GET-запрос состояния чата и его параметры."""
        session = FakeSession(FakeResponse(200, {"connected": True, "status": "connected"}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.get_notification_status(chat_id="-100123", channel="telegram")

        self.assertTrue(result["success"])
        args, kwargs = session.get_args
        self.assertEqual(args[0], "http://web:8000/api/v1/notifications/bot/status")
        self.assertEqual(kwargs["params"], {"channel": "telegram", "chat_id": "-100123"})

    async def test_status_backend_failure_is_safe(self):
        """Проверить безопасный ответ при недоступности backend."""
        session = FakeSession(FakeResponse(503, {"detail": "private backend detail"}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.get_notification_status(chat_id="123")

        self.assertEqual(result, {"success": False, "code": "backend_unavailable"})

    async def test_connect_posts_real_chat_id_and_service_auth(self):
        session = FakeSession(FakeResponse(201, {"id": "destination-id"}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1/"),
            patch.object(
                notifications_api,
                "AUTH_HEADERS",
                {"Authorization": "Bearer local-test"},
            ),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.connect_notification_chat(
                chat_id="-100123",
                token="one-time-token",
                destination_type="group",
                title="Тестовая группа",
            )

        self.assertEqual(result, {"success": True})
        args, kwargs = session.post_args
        self.assertEqual(args[0], "http://web:8000/api/v1/notifications/bot/connect")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer local-test"})
        self.assertEqual(
            kwargs["json"],
            {
                "channel": "telegram",
                "destination_type": "group",
                "chat_id": "-100123",
                "token": "one-time-token",
                "title": "Тестовая группа",
            },
        )

    async def test_expired_or_used_link_is_reported_without_exposing_response_body(
        self,
    ):
        session = FakeSession(FakeResponse(400, {"detail": "private backend detail"}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.connect_notification_chat(
                chat_id="123",
                token="one-time-token",
                destination_type="personal",
            )

        self.assertEqual(result, {"success": False, "code": "link_invalid"})
        self.assertNotIn("private backend detail", str(result))

    async def test_timeout_is_a_safe_backend_unavailable_result(self):
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(
                notifications_api.aiohttp,
                "ClientSession",
                side_effect=asyncio.TimeoutError,
            ),
        ):
            result = await notifications_api.connect_notification_chat(
                chat_id="123",
                token="one-time-token",
                destination_type="personal",
            )

        self.assertEqual(result, {"success": False, "code": "backend_unavailable"})

    async def test_supplier_action_posts_chat_binding_and_action_data(self):
        session = FakeSession(FakeResponse(200, {"ok": True, "request_id": "12345678-1234-1234-1234-123456789abc"}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(notifications_api, "AUTH_HEADERS", {"Authorization": "Bearer local-test"}),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.respond_to_supplier_request(
                request_id="12345678-1234-1234-1234-123456789abc",
                chat_id="-100123",
                action="accept",
                confirmed_delivery_date=date(2026, 9, 26),
            )

        self.assertEqual(result, {"success": True})
        args, kwargs = session.post_args
        self.assertEqual(
            args[0],
            "http://web:8000/api/v1/notifications/bot/supplier-requests/12345678-1234-1234-1234-123456789abc/respond",
        )
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer local-test"})
        self.assertEqual(
            kwargs["json"],
            {
                "channel": "telegram",
                "chat_id": "-100123",
                "action": "accept",
                "confirmed_delivery_date": "2026-09-26",
            },
        )

    async def test_partial_action_sends_item_quantities(self):
        session = FakeSession(FakeResponse(200, {"ok": True}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.respond_to_supplier_request(
                request_id="12345678-1234-1234-1234-123456789abc",
                chat_id="-100123",
                action="partial",
                confirmed_delivery_date=date(2026, 9, 26),
                item_quantities={"87654321-1234-1234-1234-123456789abc": Decimal("10")},
            )

        self.assertEqual(result, {"success": True})
        self.assertEqual(
            session.post_args[1]["json"]["item_quantities"],
            {"87654321-1234-1234-1234-123456789abc": "10"},
        )

    async def test_partial_lookup_is_scoped_to_linked_chat(self):
        session = FakeSession(FakeResponse(200, {"id": "request-id", "items": []}))
        with (
            patch.object(notifications_api, "BACKEND_URL", "http://web:8000/api/v1"),
            patch.object(notifications_api.aiohttp, "ClientSession", return_value=session),
        ):
            result = await notifications_api.get_supplier_request_for_chat(
                request_id="12345678-1234-1234-1234-123456789abc", chat_id="-100123"
            )

        self.assertTrue(result["success"])
        args, kwargs = session.get_args
        self.assertEqual(
            args[0],
            "http://web:8000/api/v1/notifications/bot/supplier-requests/12345678-1234-1234-1234-123456789abc",
        )
        self.assertEqual(kwargs["params"], {"channel": "telegram", "chat_id": "-100123"})


class NotificationBotHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = self.enterContext(patch.object(notifications_main, "datetime", wraps=datetime))
        clock.now.return_value = datetime(2026, 9, 25, 12, tzinfo=notifications_main.MINSK_TZ)

    def make_update(self, *, chat_id=123, chat_type="private", title="", full_name="Test User"):
        message = SimpleNamespace(reply_text=AsyncMock())
        chat = SimpleNamespace(id=chat_id, type=chat_type, title=title)
        user = SimpleNamespace(full_name=full_name)
        return SimpleNamespace(effective_message=message, effective_chat=chat, effective_user=user)

    async def test_start_without_token_does_not_call_backend(self):
        """Показать меню онбординга при запуске без одноразовой ссылки."""
        update = self.make_update()
        with patch.object(notifications_main, "connect_notification_chat", new=AsyncMock()) as connect:
            await notifications_main.start_command(update, SimpleNamespace(args=[]))

        connect.assert_not_awaited()
        args, kwargs = update.effective_message.reply_text.await_args
        self.assertEqual(args[0], notifications_main.WELCOME_TEXT)
        self.assertIsNotNone(kwargs["reply_markup"])

    async def test_private_chat_uses_personal_destination_type(self):
        """Передать backend тип личного чата и данные пользователя."""
        update = self.make_update(chat_id=123, chat_type="private", full_name="Buyer Test")
        connect = AsyncMock(return_value={"success": True})
        with patch.object(notifications_main, "connect_notification_chat", new=connect):
            await notifications_main.start_command(update, SimpleNamespace(args=["short-token"]))

        connect.assert_awaited_once_with(
            chat_id="123",
            token="short-token",
            destination_type="personal",
            title="",
            user_id="",
            username="",
            display_name="Buyer Test",
        )
        args, kwargs = update.effective_message.reply_text.await_args
        self.assertEqual(args[0], notifications_main.CONNECTED_TEXT)
        self.assertIsNotNone(kwargs["reply_markup"])

    async def test_group_chat_uses_group_destination_type_and_title(self):
        update = self.make_update(chat_id=-100123, chat_type="supergroup", title="Kitchen Test")
        connect = AsyncMock(return_value={"success": True})
        with patch.object(notifications_main, "connect_notification_chat", new=connect):
            await notifications_main.start_command(update, SimpleNamespace(args=["short-token"]))

        self.assertEqual(connect.await_args.kwargs["chat_id"], "-100123")
        self.assertEqual(connect.await_args.kwargs["destination_type"], "group")
        self.assertEqual(connect.await_args.kwargs["title"], "Kitchen Test")

    async def test_invalid_link_gets_plain_language_error(self):
        update = self.make_update()
        with patch.object(
            notifications_main,
            "connect_notification_chat",
            new=AsyncMock(return_value={"success": False, "code": "link_invalid"}),
        ):
            await notifications_main.start_command(update, SimpleNamespace(args=["expired-token"]))

        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("использована или устарела", reply)
        self.assertNotIn("expired-token", reply)

    async def test_accept_button_prompts_for_delivery_date(self):
        message = SimpleNamespace(
            chat_id=-100123,
            message_id=800,
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        )
        query = SimpleNamespace(
            data="supplier_action:12345678-1234-1234-1234-123456789abc:accept",
            message=message,
            answer=AsyncMock(),
        )
        context = SimpleNamespace(user_data={})

        await notifications_main.supplier_action_callback(SimpleNamespace(callback_query=query), context)

        query.answer.assert_awaited_once()
        self.assertEqual(context.user_data[notifications_main.PENDING_ACTION_KEY]["step"], "delivery_date")
        self.assertEqual(context.user_data[notifications_main.PENDING_ACTION_KEY]["chat_id"], "-100123")
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        prompt_text = message.reply_text.await_args.args[0]
        self.assertIn("Выберите дату", prompt_text)
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("supplier_custom:12345678-1234-1234-1234-123456789abc:a", callbacks)
        self.assertIn("supplier_cancel:12345678-1234-1234-1234-123456789abc", callbacks)
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in callbacks))

    async def test_callback_ack_timeout_does_not_abort_action_prompt(self):
        message = SimpleNamespace(
            chat_id=-100123,
            message_id=800,
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        )
        query = SimpleNamespace(
            data="supplier_action:12345678-1234-1234-1234-123456789abc:accept",
            message=message,
            answer=AsyncMock(side_effect=TimedOut("temporary timeout")),
        )
        context = SimpleNamespace(user_data={})

        await notifications_main.supplier_action_callback(SimpleNamespace(callback_query=query), context)

        query.answer.assert_awaited_once()
        message.reply_text.assert_awaited_once()
        self.assertEqual(context.user_data[notifications_main.PENDING_ACTION_KEY]["step"], "delivery_date")

    async def test_date_button_shows_an_explicit_confirmation_before_accepting(self):
        message = SimpleNamespace(
            chat_id=-100123,
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="supplier_date:12345678-1234-1234-1234-123456789abc:a:2026-09-26",
            message=message,
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            user_data={
                notifications_main.PENDING_ACTION_KEY: {
                    "request_id": "12345678-1234-1234-1234-123456789abc",
                    "chat_id": "-100123",
                    "action": "accept",
                }
            }
        )
        await notifications_main.supplier_date_callback(SimpleNamespace(callback_query=query), context)

        self.assertIn("26.09.2026", message.reply_text.await_args.args[0])
        state = context.user_data[notifications_main.PENDING_ACTION_KEY]
        self.assertEqual(state["step"], "confirm")
        self.assertEqual(state["confirmed_delivery_date"], date(2026, 9, 26))
        self.assertIn(
            "supplier_confirm:12345678-1234-1234-1234-123456789abc:accept",
            [
                button.callback_data
                for row in message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                for button in row
            ],
        )

    async def test_partial_date_button_requests_item_quantities(self):
        message = SimpleNamespace(
            chat_id=-100123,
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        )
        query = SimpleNamespace(
            data="supplier_date:12345678-1234-1234-1234-123456789abc:p:2026-09-26",
            message=message,
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            user_data={
                notifications_main.PENDING_ACTION_KEY: {
                    "request_id": "12345678-1234-1234-1234-123456789abc",
                    "chat_id": "-100123",
                    "action": "partial",
                    "items": [{"id": "item-1", "name": "Мука", "quantity": "25", "unit": "кг"}],
                }
            }
        )

        await notifications_main.supplier_date_callback(SimpleNamespace(callback_query=query), context)

        state = context.user_data[notifications_main.PENDING_ACTION_KEY]
        self.assertEqual(state["step"], "quantities")
        self.assertEqual(state["confirmed_delivery_date"], date(2026, 9, 26))
        self.assertIn("Ответьте на это сообщение", message.reply_text.await_args.args[0])

    async def test_custom_date_prompt_explains_to_reply_to_message(self):
        message = SimpleNamespace(
            chat_id=-100123,
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        )
        query = SimpleNamespace(
            data="supplier_custom:12345678-1234-1234-1234-123456789abc:a",
            message=message,
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            user_data={
                notifications_main.PENDING_ACTION_KEY: {
                    "request_id": "12345678-1234-1234-1234-123456789abc",
                    "chat_id": "-100123",
                    "action": "accept",
                    "step": "delivery_date",
                }
            }
        )

        await notifications_main.supplier_custom_date_callback(SimpleNamespace(callback_query=query), context)

        prompt_text = message.reply_text.await_args.args[0]
        self.assertIn("Ответьте на это сообщение", prompt_text)
        self.assertIn("25.09.2026", prompt_text)
        self.assertEqual(context.user_data[notifications_main.PENDING_ACTION_KEY]["prompt_message_id"], 901)

    def test_delivery_date_rejects_past_and_next_year(self):
        self.assertIsNone(notifications_main._parse_delivery_date("24.09.2026"))
        self.assertIsNone(notifications_main._parse_delivery_date("01.01.2027"))
        self.assertEqual(notifications_main._parse_delivery_date("25.09.2026"), date(2026, 9, 25))

    def test_delivery_date_handles_year_boundary(self):
        notifications_main.datetime.now.return_value = datetime(2026, 12, 31, 12, tzinfo=notifications_main.MINSK_TZ)
        self.assertEqual(notifications_main._parse_delivery_date("31.12.2026"), date(2026, 12, 31))
        self.assertIsNone(notifications_main._parse_delivery_date("01.01.2027"))
        notifications_main.datetime.now.return_value = datetime(2027, 1, 1, 12, tzinfo=notifications_main.MINSK_TZ)
        self.assertEqual(notifications_main._parse_delivery_date("01.01.2027"), date(2027, 1, 1))

    async def test_custom_accept_date_requires_confirmation(self):
        message = SimpleNamespace(
            chat_id=-100123,
            text="26.09.2026",
            reply_to_message=SimpleNamespace(message_id=900),
            reply_text=AsyncMock(),
        )
        context = SimpleNamespace(
            user_data={
                notifications_main.PENDING_ACTION_KEY: {
                    "request_id": "12345678-1234-1234-1234-123456789abc",
                    "chat_id": "-100123",
                    "action": "accept",
                    "step": "delivery_date",
                    "prompt_message_id": 900,
                }
            }
        )
        await notifications_main.supplier_action_reply(SimpleNamespace(effective_message=message), context)

        state = context.user_data[notifications_main.PENDING_ACTION_KEY]
        self.assertEqual(state["confirmed_delivery_date"], date(2026, 9, 26))
        self.assertEqual(state["step"], "confirm")
        self.assertIn("Подтвердить поставку", message.reply_text.await_args.args[0])

    async def test_confirm_button_sends_action_and_records_responding_user(self):
        message = SimpleNamespace(
            chat_id=-100123,
            edit_reply_markup=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="supplier_confirm:12345678-1234-1234-1234-123456789abc:accept",
            message=message,
            answer=AsyncMock(),
        )
        user = SimpleNamespace(first_name="Иван", last_name="Петров", username="ivan")
        update = SimpleNamespace(callback_query=query, effective_user=user)
        context = SimpleNamespace(
            user_data={
                notifications_main.PENDING_ACTION_KEY: {
                    "request_id": "12345678-1234-1234-1234-123456789abc",
                    "chat_id": "-100123",
                    "action": "accept",
                    "step": "confirm",
                    "confirmed_delivery_date": date(2026, 9, 26),
                    "origin_message_id": 800,
                }
            }
        )
        respond = AsyncMock(return_value={"success": True})
        with patch.object(notifications_main, "respond_to_supplier_request", new=respond):
            await notifications_main.supplier_confirm_callback(update, context)

        self.assertEqual(respond.await_args.kwargs["responded_by"], "Иван Петров")
        self.assertEqual(respond.await_args.kwargs["confirmed_delivery_date"], date(2026, 9, 26))
        self.assertNotIn(notifications_main.PENDING_ACTION_KEY, context.user_data)

    def test_partial_quantity_parser_maps_number_to_item_id(self):
        quantities = notifications_main._parse_partial_quantities("1: 10", [{"id": "item-1", "quantity": "25.0000"}])

        self.assertEqual(quantities, {"item-1": Decimal("10")})

    def test_application_requires_shared_telegram_bot_token(self):
        with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
            notifications_main.build_application("")

    def test_application_registers_start_action_and_reply_handlers(self):
        application = notifications_main.build_application("123456:TEST_TOKEN")
        self.assertEqual(len(application.handlers[0]), 18)
        self.assertEqual(len(application.error_handlers), 1)

    def test_logging_suppresses_httpx_request_urls_that_contain_bot_tokens(self):
        httpx_logger = Mock()
        with (
            patch.object(notifications_main.logging, "basicConfig"),
            patch.object(
                notifications_main.logging,
                "getLogger",
                return_value=httpx_logger,
            ),
        ):
            notifications_main.configure_logging()
        httpx_logger.setLevel.assert_called_once_with(logging.WARNING)


if __name__ == "__main__":
    unittest.main()
