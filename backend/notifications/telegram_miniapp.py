"""Проверка Telegram Mini App и подписанного контекста заявки поставщика."""

import base64
import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen

from django.conf import settings


class TelegramMiniAppError(ValueError):
    """Ошибка проверки данных или токена Telegram Mini App."""


def _b64encode(value: bytes) -> str:
    """Закодировать байты в URL-безопасную строку без заполнителей."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    """Декодировать URL-безопасную строку с восстановлением заполнителей."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_supplier_request_launch_token(*, request_id, supplier_id, chat_id, ttl_seconds=86400) -> str:
    """Создать подписанный временный токен для открытия заявки."""
    payload = {
        "request_id": str(request_id),
        "supplier_id": str(supplier_id),
        "chat_id": str(chat_id),
        "expires_at": int(time.time()) + ttl_seconds,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(settings.SECRET_KEY.encode(), encoded.encode(), hashlib.sha256).digest())
    # Telegram startapp parameters only allow URL-safe alphanumerics, '_' and '-'.
    # Keep the fixed-length SHA-256 signature separated with '_' so the token
    # can be passed as start_param without Telegram rejecting the deep link.
    return f"{encoded}_{signature}"


def read_supplier_request_launch_token(token: str) -> dict:
    """Проверить подпись и срок действия токена заявки."""
    signature_length = 43  # unpadded URL-safe base64 SHA-256 digest
    separator_index = len(token) - signature_length - 1
    if separator_index <= 0 or token[separator_index] != "_":
        raise TelegramMiniAppError("Invalid launch token")
    encoded = token[:separator_index]
    signature = token[separator_index + 1 :]
    expected = _b64encode(hmac.new(settings.SECRET_KEY.encode(), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise TelegramMiniAppError("Invalid launch token")
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TelegramMiniAppError("Invalid launch token") from exc
    if not isinstance(payload, dict) or int(payload.get("expires_at", 0)) < int(time.time()):
        raise TelegramMiniAppError("Expired launch token")
    if not all(payload.get(field) for field in ("request_id", "supplier_id", "chat_id")):
        raise TelegramMiniAppError("Invalid launch token")
    return payload


def validate_telegram_init_data(init_data: str, *, max_age_seconds=600, bot_token: str | None = None) -> dict:
    """Проверить подпись Telegram и вернуть данные пользователя."""
    if not init_data or len(init_data) > 8192:
        raise TelegramMiniAppError("Missing Telegram authorization data")
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise TelegramMiniAppError("Invalid Telegram authorization data") from exc
    received_hash = fields.pop("hash", "")
    bot_token = bot_token or getattr(settings, "NOTIFICATION_TELEGRAM_BOT_TOKEN", "")
    if not received_hash or not bot_token:
        raise TelegramMiniAppError("Invalid Telegram authorization data")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise TelegramMiniAppError("Invalid Telegram authorization data")
    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        telegram_user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramMiniAppError("Incomplete Telegram authorization data") from exc
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > max_age_seconds:
        raise TelegramMiniAppError("Expired Telegram authorization data")
    return {"user": user, "user_id": telegram_user_id}


def telegram_user_is_chat_member(*, chat_id: str, user_id: int) -> bool:
    """Проверить через Telegram, состоит ли пользователь в связанном чате."""
    token = getattr(settings, "NOTIFICATION_TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    query = urlencode({"chat_id": chat_id, "user_id": user_id})
    request = Request(f"https://api.telegram.org/bot{token}/getChatMember?{query}")
    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return False
    if not result.get("ok"):
        return False
    member = result.get("result", {})
    status = member.get("status")
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and member.get("is_member") is True
    )
