import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fsm import clear_data, get_data, get_state, set_data
from handlers import router
from models import UserState


class OfflineCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_does_not_wait_for_backend(self):
        context = SimpleNamespace(chat_id="offline-help", text="/help", callback_data="")
        with (
            patch.object(router, "resolve_supplier_binding", new=AsyncMock(side_effect=TimeoutError)) as lookup,
            patch.object(router.msg, "send_message", new=AsyncMock()) as send,
        ):
            await router.dispatch(context)
        lookup.assert_not_awaited()
        send.assert_awaited_once()

    async def test_stop_clears_scenario_without_backend(self):
        context = SimpleNamespace(chat_id="offline-stop", text="/stop", callback_data="")
        self.addCleanup(clear_data, context.chat_id)
        set_data(context.chat_id, "selected_company_id", "company-1")
        with (
            patch.object(router, "resolve_supplier_binding", new=AsyncMock(side_effect=TimeoutError)) as lookup,
            patch.object(router.msg, "send_message", new=AsyncMock()) as send,
        ):
            await router.dispatch(context)
        lookup.assert_not_awaited()
        self.assertEqual(get_data(context.chat_id), {})
        self.assertEqual(get_state(context.chat_id), UserState.START)
        send.assert_awaited_once()
