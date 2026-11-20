import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import company_registration, router
from keyboards.upload import upload_result_kb
from models import UserContext, UserState
from normalizer import normalize


class ScenarioUxTests(unittest.TestCase):
    def test_upload_result_opens_company_catalog(self):
        keyboard = upload_result_kb(True)
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "📦 Открыть загруженные товары")
        self.assertIn("/supplier/cabinet/companies", button.web_app.url)

    def test_cancel_callback_has_own_route(self):
        user = SimpleNamespace(id=1, username="tester", first_name="Test", last_name="")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_id=10,
            text=None,
            caption=None,
            from_user=user,
            document=None,
            photo=None,
            voice=None,
            audio=None,
            video_note=None,
            video=None,
            sticker=None,
            animation=None,
            contact=None,
            location=None,
        )
        callback = SimpleNamespace(message=message, from_user=user, data="cancel_current_action", id="cb-1")
        context = normalize(SimpleNamespace(callback_query=callback, message=None))
        self.assertEqual(context.route, "cancel_current_action")


class CompanyRegistrationRoutingTests(unittest.IsolatedAsyncioTestCase):
    def context(self, **values):
        base = {
            "chat_id": "100",
            "telegram_user_id": "100",
            "supplier_id": "supplier-1",
            "message_type": "text",
        }
        base.update(values)
        return UserContext(**base)

    async def test_add_company_callback_starts_native_inn_flow(self):
        context = self.context(is_callback_query=True, callback_data="add_company_by_inn")
        with (
            patch.object(
                router.StateManager,
                "get",
                AsyncMock(return_value={"state": UserState.MAIN_MENU, "payload": {}}),
            ),
            patch.object(company_registration, "ask_inn", AsyncMock()) as ask_inn,
        ):
            await router.dispatch(context)

        ask_inn.assert_awaited_once_with(context)

    async def test_confirmation_callbacks_reach_company_handler(self):
        context = self.context(is_callback_query=True, callback_data="company_inn_confirm")
        with (
            patch.object(
                router.StateManager,
                "get",
                AsyncMock(
                    return_value={
                        "state": UserState.COMPANY_CONFIRMATION,
                        "payload": {"company_candidate": {"inn": "7707083893"}},
                    }
                ),
            ),
            patch.object(company_registration, "confirm_company", AsyncMock()) as confirm_company,
        ):
            await router.dispatch(context)

        confirm_company.assert_awaited_once_with(context)

    async def test_inn_text_reaches_company_handler(self):
        context = self.context(text="7707083893")
        with (
            patch.object(
                router.StateManager,
                "get",
                AsyncMock(return_value={"state": UserState.AWAITING_INN, "payload": {}}),
            ),
            patch.object(company_registration, "handle_inn", AsyncMock()) as handle_inn,
        ):
            await router.dispatch(context)

        handle_inn.assert_awaited_once_with(context)


if __name__ == "__main__":
    unittest.main()
