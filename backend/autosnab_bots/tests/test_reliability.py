import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from api.ocr import google_ocr
from telegram.ext import ApplicationHandlerStop


class TelegramReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_application_has_global_error_handler(self):
        with patch.object(main, "TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN"):
            app = main.build_application()
        self.assertEqual(len(app.error_handlers), 1)
        self.assertIn(-1, app.handlers)
        self.assertGreaterEqual(len(app.handlers[0]), 10)

    async def test_notification_reply_only_stops_supplier_dispatch_when_pending(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(chat_id=123))
        without_pending = SimpleNamespace(user_data={})
        with patch.object(main, "supplier_action_reply", new=AsyncMock()) as reply:
            await main.handle_notification_reply(update, without_pending)
        reply.assert_not_awaited()

        with_pending = SimpleNamespace(user_data={main.PENDING_ACTION_KEY: {"step": "date", "chat_id": "123"}})
        with patch.object(main, "supplier_action_reply", new=AsyncMock()) as reply:
            with self.assertRaises(ApplicationHandlerStop):
                await main.handle_notification_reply(update, with_pending)
        reply.assert_awaited_once_with(update, with_pending)

    async def test_pending_reply_in_another_chat_does_not_block_main_bot(self):
        context = SimpleNamespace(user_data={main.PENDING_ACTION_KEY: {"step": "date", "chat_id": "123"}})
        update = SimpleNamespace(effective_message=SimpleNamespace(chat_id=456))
        with patch.object(main, "supplier_action_reply", new=AsyncMock()) as reply:
            await main.handle_notification_reply(update, context)
        reply.assert_not_awaited()
        self.assertIn(main.PENDING_ACTION_KEY, context.user_data)

    async def test_navigation_commands_clear_pending_reply_in_current_chat(self):
        for command in ("/start", "/menu", "/stop", "/cancel", "/menu@autosnab_bot"):
            with self.subTest(command=command):
                context = SimpleNamespace(user_data={main.PENDING_ACTION_KEY: {"chat_id": "123"}})
                normalized = SimpleNamespace(chat_id="123", text=command)
                with (
                    patch.object(main, "normalize", return_value=normalized),
                    patch.object(main, "dispatch", new=AsyncMock()) as dispatch,
                ):
                    await main.handle_update(SimpleNamespace(callback_query=None), context)
                self.assertNotIn(main.PENDING_ACTION_KEY, context.user_data)
                dispatch.assert_awaited_once_with(normalized)

    async def test_navigation_in_another_chat_preserves_pending_reply(self):
        context = SimpleNamespace(user_data={main.PENDING_ACTION_KEY: {"chat_id": "123"}})
        with (
            patch.object(main, "normalize", return_value=SimpleNamespace(chat_id="456", text="/menu")),
            patch.object(main, "dispatch", new=AsyncMock()),
        ):
            await main.handle_update(SimpleNamespace(callback_query=None), context)
        self.assertIn(main.PENDING_ACTION_KEY, context.user_data)

    def test_application_rejects_missing_token(self):
        with patch.object(main, "TELEGRAM_BOT_TOKEN", ""):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
                main.build_application()

    async def test_expired_callback_does_not_stop_dispatch(self):
        answer = AsyncMock(side_effect=RuntimeError("query is too old"))
        update = SimpleNamespace(callback_query=SimpleNamespace(answer=answer))
        normalized = SimpleNamespace(chat_id="chat-1")
        dispatch = AsyncMock()

        with patch.object(main, "normalize", return_value=normalized), patch.object(main, "dispatch", new=dispatch):
            await main.handle_update(update, SimpleNamespace())

        answer.assert_awaited_once()
        dispatch.assert_awaited_once_with(normalized)

    async def test_unhandled_error_sends_safe_message(self):
        send_message = AsyncMock()
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=123))
        context = SimpleNamespace(
            error=RuntimeError("internal secret detail"),
            bot=SimpleNamespace(send_message=send_message),
        )

        await main.handle_error(update, context)

        send_message.assert_awaited_once()
        sent_text = send_message.await_args.kwargs["text"]
        self.assertIn("/menu", sent_text)
        self.assertNotIn("internal secret detail", sent_text)

    async def test_error_without_chat_is_only_logged(self):
        context = SimpleNamespace(error=RuntimeError("background failure"))
        await main.handle_error(None, context)


class OcrReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ocr_does_not_return_internal_exception_to_user(self):
        with (
            patch.object(google_ocr, "GOOGLE_DRIVE_OCR_ENABLED", True),
            patch.object(google_ocr.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("oauth secret"))),
            patch.object(google_ocr, "langfuse_track", new=AsyncMock()),
        ):
            result = await google_ocr.recognize_document(b"not-an-image", "price.jpg")

        self.assertFalse(result["success"])
        self.assertIn("Не удалось распознать документ", result["error"])
        self.assertNotIn("oauth secret", result["error"])


if __name__ == "__main__":
    unittest.main()
