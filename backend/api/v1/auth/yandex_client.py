import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger("autosnab.auth.yandex_client")

_TOKEN_URL = "https://oauth.yandex.ru/token"
_USER_INFO_URL = "https://login.yandex.ru/info"


class YandexOAuthError(Exception):
    """Raised when Yandex OAuth isn't configured, rejects the code, or fails."""


def exchange_code_for_token(code):
    client_id = str(getattr(settings, "YANDEX_OAUTH_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(settings, "YANDEX_OAUTH_CLIENT_SECRET", "") or "").strip()
    if not client_id or not client_secret:
        raise YandexOAuthError("Вход через Яндекс не настроен: нет client_id/client_secret.")

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("yandex_oauth.token_http_error status=%s body=%s", error.code, body_text)
        raise YandexOAuthError("Яндекс отклонил код авторизации.") from error
    except urllib.error.URLError as error:
        logger.warning("yandex_oauth.token_network_error error=%s", error)
        raise YandexOAuthError("Яндекс OAuth недоступен (сетевая ошибка).") from error

    access_token = payload.get("access_token")
    if not access_token:
        raise YandexOAuthError("Яндекс не вернул access_token.")
    return access_token


def fetch_user_info(access_token):
    request = urllib.request.Request(
        f"{_USER_INFO_URL}?format=json",
        headers={"Authorization": f"OAuth {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("yandex_oauth.userinfo_http_error status=%s body=%s", error.code, body_text)
        raise YandexOAuthError("Не удалось получить данные пользователя от Яндекса.") from error
    except urllib.error.URLError as error:
        logger.warning("yandex_oauth.userinfo_network_error error=%s", error)
        raise YandexOAuthError("Яндекс OAuth недоступен (сетевая ошибка).") from error

    yandex_id = payload.get("psuid") or payload.get("id")
    email = payload.get("default_email")
    if not yandex_id or not email:
        raise YandexOAuthError("Яндекс не вернул email — разрешите доступ к почте при входе.")

    full_name = " ".join(filter(None, [payload.get("first_name"), payload.get("last_name")])).strip()
    return {
        "yandex_id": yandex_id,
        "email": email,
        "full_name": full_name or email,
    }
