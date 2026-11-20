import unittest

from handlers.checklist import OnboardingProgress, format_checklist, progress_from_uploads
from keyboards.onboarding import welcome_kb


class OnboardingChecklistTests(unittest.TestCase):
    def test_new_user_is_sent_to_authorization(self):
        progress = OnboardingProgress(False, False, False)
        self.assertEqual(progress.next_step, "authorize")
        self.assertIn("⬜ Войти", format_checklist(progress))

    def test_welcome_keyboard_allows_authorization_retry(self):
        callbacks = [
            button.callback_data
            for row in welcome_kb().inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("check_authorization", callbacks)

    def test_bound_user_without_company_is_sent_to_company_creation(self):
        progress = OnboardingProgress(True, False, False)
        self.assertEqual(progress.next_step, "create_company")
        self.assertIn("✅ Войти", format_checklist(progress))

    def test_processing_upload_is_not_marked_complete(self):
        progress = progress_from_uploads(
            authorized=True,
            company_created=True,
            uploads=[{"status": "processing"}],
        )
        self.assertEqual(progress.next_step, "wait_for_price")
        self.assertIn("⏳ Загрузить", format_checklist(progress))

    def test_completed_upload_finishes_checklist(self):
        progress = progress_from_uploads(
            authorized=True,
            company_created=True,
            uploads=[{"status": "applied"}],
        )
        self.assertTrue(progress.complete)
        self.assertEqual(progress.next_step, "complete")
        self.assertIn("Всё готово", format_checklist(progress))


if __name__ == "__main__":
    unittest.main()
