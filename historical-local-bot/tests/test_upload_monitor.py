import unittest
from unittest.mock import AsyncMock, patch

from handlers import upload_monitor
from models import UserContext


class UploadMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        upload_monitor._tasks.clear()
        upload_monitor._notified.clear()
        self.context = UserContext(
            chat_id="100",
            telegram_user_id="100",
            supplier_id="supplier-1",
            file_name="price.xlsx",
            upload_id="upload-1",
        )

    async def test_completed_upload_notifies_once(self):
        complete = AsyncMock()
        failed = AsyncMock()
        statuses = [
            {"status": "processing"},
            {"status": "completed", "products_created": 3},
        ]

        with patch.object(upload_monitor.asyncio, "sleep", new=AsyncMock()), patch.object(
            upload_monitor, "get_upload_stats", new=AsyncMock(side_effect=statuses)
        ):
            await upload_monitor._monitor_upload(
                self.context, "upload-1", complete, failed
            )

        complete.assert_awaited_once()
        failed.assert_not_awaited()

    async def test_failed_upload_uses_failure_callback(self):
        complete = AsyncMock()
        failed = AsyncMock()

        with patch.object(upload_monitor.asyncio, "sleep", new=AsyncMock()), patch.object(
            upload_monitor,
            "get_upload_stats",
            new=AsyncMock(return_value={"status": "failed"}),
        ):
            await upload_monitor._monitor_upload(
                self.context, "upload-2", complete, failed
            )

        failed.assert_awaited_once()
        complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
