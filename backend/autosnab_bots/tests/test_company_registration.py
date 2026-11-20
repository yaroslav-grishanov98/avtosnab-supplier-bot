import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fsm
from handlers import company_registration
from models import UserContext, UserState
from state_backend import patch_state_backend


class CompanyRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fsm._user_data.clear()
        fsm._user_states.clear()

    def context(self, **values):
        base = {
            "chat_id": "100",
            "telegram_user_id": "100",
            "text": "",
            "message_type": "text",
            "supplier_id": "owner-1",
        }
        base.update(values)
        return UserContext(**base)

    def test_inn_checksum(self):
        self.assertTrue(company_registration.is_valid_inn("7707083893"))
        self.assertTrue(company_registration.is_valid_inn("500100732259"))
        self.assertFalse(company_registration.is_valid_inn("7707083894"))
        self.assertFalse(company_registration.is_valid_inn("123"))

    async def test_found_company_waits_for_confirmation(self):
        context = self.context(text="7707083893")
        messenger = SimpleNamespace(send_message=AsyncMock())
        with (
            patch.object(company_registration, "msg", messenger),
            patch.object(
                company_registration,
                "lookup_inn",
                AsyncMock(return_value={"found": True, "company_name": "ООО Тест", "address": "Москва"}),
            ),
            patch_state_backend(),
        ):
            await company_registration.handle_inn(context)

        self.assertEqual(fsm.get_state("100"), UserState.COMPANY_CONFIRMATION)
        self.assertEqual(fsm.get_data("100")["company_candidate"]["inn"], "7707083893")
        keyboard = messenger.send_message.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for button in keyboard.inline_keyboard[0]]
        self.assertEqual(callbacks, ["company_inn_confirm", "company_inn_cancel"])

    async def test_cancel_returns_to_inn_input(self):
        context = self.context()
        messenger = SimpleNamespace(send_message=AsyncMock())
        with patch.object(company_registration, "msg", messenger), patch_state_backend():
            await company_registration.cancel_company(context)
        self.assertEqual(fsm.get_state("100"), UserState.AWAITING_INN)

    async def test_lookup_error_answers_instead_of_hanging(self):
        context = self.context(text="7707083893")
        messenger = SimpleNamespace(send_message=AsyncMock())
        with (
            patch.object(company_registration, "msg", messenger),
            patch.object(
                company_registration,
                "lookup_inn",
                AsyncMock(return_value={"found": False, "error": {"code": "backend_unavailable"}}),
            ),
        ):
            await company_registration.handle_inn(context)
        self.assertIn("не ответил", messenger.send_message.await_args.args[1])

    async def test_confirm_adds_company(self):
        context = self.context(
            bot_user_state={
                "payload": {
                    "company_candidate": {
                        "inn": "7707083893",
                        "company_name": "ООО Тест",
                        "address": "Москва",
                    }
                }
            }
        )
        messenger = SimpleNamespace(send_message=AsyncMock())
        created = {
            "success": True,
            "company": {"id": "company-1", "display_name": "ООО Тест", "inn": "7707083893"},
        }
        with (
            patch.object(company_registration, "msg", messenger),
            patch.object(company_registration, "create_supplier_company", AsyncMock(return_value=created)),
            patch_state_backend(),
        ):
            await company_registration.confirm_company(context)

        self.assertEqual(fsm.get_state("100"), UserState.MAIN_MENU)
        self.assertEqual(fsm.get_data("100")["selected_company_id"], "company-1")
        self.assertEqual(context.supplier_id, "company-1")


if __name__ == "__main__":
    unittest.main()
