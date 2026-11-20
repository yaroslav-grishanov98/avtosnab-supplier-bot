import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import logging_setup


class BotLoggingTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.Logger("isolated-bot-test")
        self.enterContext(patch.object(logging_setup.logging, "getLogger", return_value=self.root))
        self.enterContext(patch.object(logging_setup.logging, "captureWarnings"))
        self.enterContext(patch.dict("os.environ", {"BOT_LOG_TO_FILE": "true"}))
        self.stream = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))
        self.addCleanup(self.close_handlers)

    def close_handlers(self):
        for handler in self.root.handlers:
            handler.close()

    def test_read_only_directory_keeps_console_logging(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("read-only")):
            self.assertIsNone(logging_setup.configure_logging("/read-only"))
        self.root.info("bot remains available")
        self.assertIn("bot remains available", self.stream.getvalue())
        self.assertIn("PermissionError", self.stream.getvalue())

    def test_unwritable_existing_log_file_keeps_console_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(logging_setup, "RotatingFileHandler", side_effect=PermissionError("file owner")):
                self.assertIsNone(logging_setup.configure_logging(directory))
        self.root.info("console works")
        self.assertIn("console works", self.stream.getvalue())

    def test_container_console_mode_never_touches_filesystem(self):
        with patch.dict("os.environ", {"BOT_LOG_TO_FILE": "false"}), patch.object(Path, "mkdir") as mkdir:
            self.assertIsNone(logging_setup.configure_logging())
            self.assertIsNone(logging_setup.configure_logging())
        mkdir.assert_not_called()
        self.assertEqual(len(self.root.handlers), 1)

    def test_writable_file_receives_logs_without_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            logfile = logging_setup.configure_logging(directory)
            self.assertEqual(logging_setup.configure_logging(directory), logfile)
            self.root.info("supplier bot started")
            self.assertEqual(len(self.root.handlers), 2)
            self.assertIn("supplier bot started", logfile.read_text(encoding="utf-8"))
            self.close_handlers()


if __name__ == "__main__":
    unittest.main()
