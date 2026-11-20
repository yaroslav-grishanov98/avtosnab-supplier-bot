import unittest
from unittest.mock import patch

from handlers import file_upload
from models import UserContext


class UploadRecoveryTests(unittest.TestCase):
    def test_resume_uses_backend_fsm_payload(self):
        context = UserContext(chat_id="100", telegram_user_id="100")
        payload = {
            "upload_id": "upload-1",
            "supplier_id": "supplier-1",
            "file_name": "price.xlsx",
        }

        with patch.object(file_upload, "start_upload_monitor") as start:
            resumed = file_upload.resume_upload_monitor(context, payload)

        self.assertTrue(resumed)
        self.assertEqual(context.upload_id, "upload-1")
        self.assertEqual(context.supplier_id, "supplier-1")
        self.assertEqual(context.file_name, "price.xlsx")
        start.assert_called_once()

    def test_resume_ignores_payload_without_upload(self):
        context = UserContext(chat_id="100", telegram_user_id="100")

        with patch.object(file_upload, "start_upload_monitor") as start:
            resumed = file_upload.resume_upload_monitor(context, {})

        self.assertFalse(resumed)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
