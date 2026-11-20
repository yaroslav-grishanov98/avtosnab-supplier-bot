import unittest
from types import SimpleNamespace

from keyboards.upload import upload_result_kb
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


if __name__ == "__main__":
    unittest.main()
