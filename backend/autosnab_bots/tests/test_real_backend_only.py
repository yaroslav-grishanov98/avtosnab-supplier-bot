"""Legacy environment switches must never bypass backend authorization."""

import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from models import UserContext

from api import backend, company_accounts


class RealBackendOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "BOT_MOCK_MODE": "true",
                    "DEMO_AUTH_MODE": "true",
                    "DEMO_SUPPLIER_ID": "fake-company",
                    "DEMO_SUPPLIER_NAME": "Fake supplier",
                    "MOCK_USER_BOUND": "true",
                },
            )
        )

    def test_missing_credentials_still_prevent_startup(self):
        with patch.dict(os.environ, {"BACKEND_BASE_URL": "", "SERVICE_API_TOKEN": "", "TELEGRAM_BOT_TOKEN": ""}):
            config = runpy.run_path(str(Path(backend.__file__).parents[1] / "config.py"))
        with self.assertRaisesRegex(RuntimeError, "BACKEND_BASE_URL.*SERVICE_API_TOKEN.*TELEGRAM_BOT_TOKEN"):
            config["validate_runtime_config"]()

    async def test_legacy_switches_cannot_fabricate_bound_account(self):
        with patch.object(backend.aiohttp, "ClientSession", side_effect=AssertionError("backend required")):
            with self.assertRaisesRegex(AssertionError, "backend required"):
                await backend.resolve_supplier_binding("telegram", "user", "chat")

    async def test_legacy_switches_cannot_fabricate_successful_write(self):
        with patch.object(backend.aiohttp, "ClientSession", side_effect=AssertionError("backend required")):
            with self.assertRaisesRegex(AssertionError, "backend required"):
                await backend.patch_product_price("product", "123.45")

    async def test_legacy_switches_cannot_fabricate_companies(self):
        context = UserContext(chat_id="chat", telegram_user_id="user")
        with patch.object(company_accounts.aiohttp, "ClientSession", side_effect=AssertionError("backend required")):
            with self.assertRaisesRegex(AssertionError, "backend required"):
                await company_accounts.list_companies(context)
