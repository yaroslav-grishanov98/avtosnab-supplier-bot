"""Telemetry legacy-роутов /suppliers/uploads/*: кто и как часто их вызывает (нужно для решения об удалении,
спека §6). Пишет только в лог: метод, шаблон пути, код ответа, класс потребителя. Токены, тела запросов и
UUID в лог не попадают."""

import logging
import re
from hmac import compare_digest

from django.conf import settings

logger = logging.getLogger("autosnab.legacy_uploads")

_LEGACY_PREFIX = "/api/v1/suppliers/uploads"
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_USER_AGENT_LIMIT = 80


def legacy_path_template(path):
    """Путь legacy-роута с {id} вместо UUID; None, если путь не относится к /suppliers/uploads."""
    if path != _LEGACY_PREFIX and not path.startswith(_LEGACY_PREFIX + "/"):
        return None
    return _UUID_RE.sub("{id}", path)


def classify_consumer(*, has_service_token, user_agent):
    agent = (user_agent or "").lower()
    if has_service_token:
        if "aiohttp" in agent:
            return "bot"
        if "axios" in agent or "n8n" in agent:
            return "n8n"
        return "service-other"
    if "mozilla" in agent:
        return "browser"
    return "other"


def _has_service_token(request):
    expected = getattr(settings, "SERVICE_API_TOKEN", "")
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    provided = token.strip() if scheme.lower() == "bearer" else request.headers.get("X-Service-Token", "").strip()
    return bool(expected and provided and compare_digest(provided, expected))


class LegacyUploadsTelemetryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not getattr(settings, "LEGACY_UPLOADS_TELEMETRY_ENABLED", True):
            return response
        try:
            template = legacy_path_template(request.path)
            if template is not None:
                user_agent = request.headers.get("User-Agent", "")
                consumer = classify_consumer(has_service_token=_has_service_token(request), user_agent=user_agent)
                logger.info(
                    'legacy_uploads.call method=%s path=%s status=%s consumer=%s ua="%s"',
                    request.method,
                    template,
                    response.status_code,
                    consumer,
                    user_agent[:_USER_AGENT_LIMIT].replace('"', "'"),
                )
        except Exception:  # noqa: BLE001 -- telemetry не должна ломать ответ
            pass
        return response
