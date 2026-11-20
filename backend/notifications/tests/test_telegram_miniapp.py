import hashlib
import hmac
import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

from django.test import SimpleTestCase, override_settings

from notifications.telegram_miniapp import (
    TelegramMiniAppError,
    create_supplier_request_launch_token,
    read_supplier_request_launch_token,
    validate_telegram_init_data,
)


@override_settings(SECRET_KEY="test-secret", NOTIFICATION_TELEGRAM_BOT_TOKEN="123:bot-token")
class TelegramMiniAppSecurityTests(SimpleTestCase):
    def test_launch_token_is_bound_to_request_chat_and_supplier(self):
        token = create_supplier_request_launch_token(
            request_id="request-1", supplier_id="supplier-1", chat_id="-1001234567890"
        )
        payload = read_supplier_request_launch_token(token)

        self.assertEqual(
            payload,
            {
                "request_id": "request-1",
                "supplier_id": "supplier-1",
                "chat_id": "-1001234567890",
                "expires_at": payload["expires_at"],
            },
        )
        with self.assertRaises(TelegramMiniAppError):
            read_supplier_request_launch_token(token[:-1] + ("A" if token[-1] != "A" else "B"))

    def test_expired_launch_token_is_rejected(self):
        with patch("notifications.telegram_miniapp.time.time", return_value=1000):
            token = create_supplier_request_launch_token(
                request_id="request-1", supplier_id="supplier-1", chat_id="-1001234567890", ttl_seconds=1
            )
        with patch("notifications.telegram_miniapp.time.time", return_value=1002):
            with self.assertRaises(TelegramMiniAppError):
                read_supplier_request_launch_token(token)

    def test_init_data_requires_a_valid_recent_telegram_signature(self):
        auth_date = int(time.time())
        fields = {
            "auth_date": str(auth_date),
            "user": json.dumps({"id": 123456, "first_name": "Test"}, separators=(",", ":")),
        }
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", b"123:bot-token", hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

        validated = validate_telegram_init_data(urlencode(fields))

        self.assertEqual(validated["user_id"], 123456)
        self.assertEqual(validated["user"]["first_name"], "Test")

    def test_init_data_rejects_a_modified_hash(self):
        with self.assertRaises(TelegramMiniAppError):
            validate_telegram_init_data("auth_date=1&user=%7B%7D&hash=invalid")
