"""Run bot unit tests without loading .env or sending external API requests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    source = Path(__file__).resolve().parent / "autosnab_bots"
    sys.path.insert(0, str(source))
    environment = {
        "LANGFUSE_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "",
        "SERVICE_API_TOKEN": "offline-test",
        "BACKEND_BASE_URL": "https://backend.invalid/api/v1",
        "BOT_LOG_TO_FILE": "false",
    }
    blocked = AssertionError("Offline tests attempted an external API call")
    with (
        tempfile.TemporaryDirectory(prefix="autosnab-offline-") as state_dir,
        patch.dict(os.environ, environment),
        patch("dotenv.load_dotenv", return_value=False),
        patch("aiohttp.ClientSession._request", side_effect=blocked),
        patch("httpx.AsyncClient.send", side_effect=blocked),
        patch("requests.Session.request", side_effect=blocked),
    ):
        os.environ["BOT_STATE_DB_PATH"] = str(Path(state_dir) / "notification-state.sqlite3")
        suite = unittest.defaultTestLoader.discover(str(source / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
