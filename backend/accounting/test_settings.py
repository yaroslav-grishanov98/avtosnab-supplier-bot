from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase

from config.settings import parse_secret_keys


class AccountingSettingsTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertIn("accounting", settings.INSTALLED_APPS)
        self.assertEqual(apps.get_app_config("accounting").name, "accounting")

    def test_defaults(self):
        self.assertIs(settings.ACCOUNTING_SCHEDULED_SYNC_ENABLED, False)
        self.assertEqual(settings.ACCOUNTING_STOCK_INTERVAL_MINUTES, 60)
        self.assertEqual(settings.ACCOUNTING_CATALOG_INTERVAL_HOURS, 24)
        self.assertEqual(settings.ACCOUNTING_STOCK_STALE_AFTER_MINUTES, 180)
        self.assertEqual(settings.ACCOUNTING_MANUAL_SYNC_THROTTLE_SECONDS, 60)
        self.assertEqual(settings.ACCOUNTING_HOST_SLOTS, 1)
        self.assertEqual(settings.ACCOUNTING_WORKER_CONCURRENCY, 12)
        self.assertEqual(settings.ACCOUNTING_BREAKER_THRESHOLD, 5)
        self.assertEqual(settings.ACCOUNTING_BREAKER_COOLDOWN_MINUTES, 60)
        self.assertEqual(settings.ACCOUNTING_IIKO_REQUEST_DELAY, 0.5)
        self.assertEqual(settings.ACCOUNTING_IIKO_TIMEOUT, 15)
        self.assertEqual(settings.ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION, 60)
        self.assertEqual(settings.ACCOUNTING_STORES_PER_REQUEST, 20)
        self.assertEqual(settings.ACCOUNTING_OLAP_POSTS_PER_SESSION, 8)
        self.assertEqual(settings.ACCOUNTING_JOB_STALL_SECONDS, 900)
        self.assertEqual(settings.ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES, ("INVOICE", "SALES_REVENUE", "DISCOUNT"))
        self.assertEqual(settings.ACCOUNTING_CONSUMPTION_WINDOW_DAYS, 7)
        self.assertEqual(settings.ACCOUNTING_CONSUMPTION_INTERVAL_HOURS, 24)
        self.assertEqual(settings.ACCOUNTING_CONSUMPTION_SETTLE_HOURS, 6)
        self.assertEqual(settings.ACCOUNTING_QUEUE_SYNC, "iiko_sync")
        self.assertEqual(settings.ACCOUNTING_QUEUE_MANUAL, "iiko_manual")
        self.assertEqual(settings.ACCOUNTING_QUEUE_CONTROL, "iiko_control")

    def test_secret_key_settings_have_expected_types(self):
        self.assertIsInstance(settings.ACCOUNTING_SECRET_KEYS, dict)
        self.assertIsInstance(settings.ACCOUNTING_SECRET_ACTIVE_KEY_VERSION, int)

    def test_secret_keys_parse_env(self):
        self.assertEqual(parse_secret_keys("1:abc,2:def"), {1: "abc", 2: "def"})
        self.assertEqual(parse_secret_keys(""), {})

    def test_secret_keys_parse_tolerates_whitespace_and_padding(self):
        self.assertEqual(parse_secret_keys(" 1:abc= , 2:de=f "), {1: "abc=", 2: "de=f"})
        self.assertEqual(parse_secret_keys(" , "), {})

    def test_secret_keys_parse_rejects_malformed_entries(self):
        for raw in ("abc", "x:abc", "1:", "1:abc,1:def"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_secret_keys(raw)
