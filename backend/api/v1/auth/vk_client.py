import base64
import hashlib
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger("autosnab.auth.vk_client")

# VK ID (id.vk.ru) — OAuth 2.1 + PKCE, не путать со старым VK OAuth (oauth.vk.com).
_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
_USER_INFO_URL = "https://id.vk.ru/oauth2/user_info"


class VKOAuthError(Exception):
    """Raised when VK ID OAuth isn't configured, rejects the code, or fails."""


def generate_pkce_pair():
    """RFC 7636 code_verifier/code_challenge (S256) — VK ID requires PKCE even
    for this confidential server-side flow (OAuth 2.1)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def exchange_code_for_token(code, *, code_verifier, device_id=None):
    client_id = str(getattr(settings, "VK_OAUTH_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(settings, "VK_OAUTH_CLIENT_SECRET", "") or "").strip()
    if not client_id or not client_secret:
        raise VKOAuthError("Вход через VK ID не настроен: нет client_id/client_secret.")

    body_params = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": settings.VK_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    # device_id приходит от VK ID вместе с code на callback и обязателен для
    # части приложений при обмене кода — пробрасываем, если он был.
    if device_id:
        body_params["device_id"] = device_id

    body = urllib.parse.urlencode(body_params).encode("ascii")
    request = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("vk_oauth.token_http_error status=%s body=%s", error.code, body_text)
        raise VKOAuthError("VK ID отклонил код авторизации.") from error
    except urllib.error.URLError as error:
        logger.warning("vk_oauth.token_network_error error=%s", error)
        raise VKOAuthError("VK ID недоступен (сетевая ошибка).") from error

    if not payload.get("access_token"):
        raise VKOAuthError("VK ID не вернул access_token.")
    return payload


def fetch_user_info(token_response):
    # VK ID (id.vk.ru) — новый протокол, email/телефон отдаёт ТОЛЬКО через
    # выделенный oauth2/user_info (POST client_id+access_token, ответ вложен
    # в {"user": {...}}). Раньше пробовали декодировать id_token (JWT) —
    # неверно: VK ID не гарантирует email-claim в id_token без scope=openid,
    # у нас scope=email. Подтверждено внешним рабочим примером (github.com/
    # C0nstantin/omniauth-vk_id) — реальный баг, из-за которого "Email
    # разрешён" в консенте, но бэкенд получал VKOAuthError "VK не вернул email".
    access_token = token_response.get("access_token")
    if not access_token:
        raise VKOAuthError("VK ID не вернул access_token.")

    client_id = str(getattr(settings, "VK_OAUTH_CLIENT_ID", "") or "").strip()
    body = urllib.parse.urlencode({"client_id": client_id, "access_token": access_token}).encode("ascii")
    request = urllib.request.Request(
        _USER_INFO_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("vk_oauth.userinfo_http_error status=%s body=%s", error.code, body_text)
        raise VKOAuthError("Не удалось получить данные пользователя от VK.") from error
    except urllib.error.URLError as error:
        logger.warning("vk_oauth.userinfo_network_error error=%s", error)
        raise VKOAuthError("VK ID недоступен (сетевая ошибка).") from error

    profile = payload.get("user") or {}
    vk_id = profile.get("user_id")
    if not vk_id:
        raise VKOAuthError("Не удалось получить данные пользователя от VK.")

    email = profile.get("email")
    if not email:
        raise VKOAuthError("VK не вернул email — разрешите доступ к почте при входе.")

    full_name = " ".join(filter(None, [profile.get("first_name"), profile.get("last_name")])).strip()
    return {
        "vk_id": str(vk_id),
        "email": email,
        "full_name": full_name or email,
    }
