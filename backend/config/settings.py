"""Django settings for the Autosnab backend scaffold."""

import os
from pathlib import Path

from celery.schedules import crontab
from dotenv import load_dotenv
from kombu import Queue

load_dotenv(override=True)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_secret_keys(raw):
    """Parse ``"1:<b64>,2:<b64>"`` into ``{1: "<b64>", 2: "<b64>"}``.

    Unlike the lenient ``_int_env``, malformed input raises: silently dropping an
    encryption key would make stored credentials undecryptable.
    """
    keys = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        version, separator, key = part.partition(":")
        version, key = version.strip(), key.strip()
        if not separator or not key or not version.isdecimal():
            raise ValueError("ACCOUNTING_SECRET_KEYS must look like '1:<base64>,2:<base64>'.")
        if int(version) in keys:
            raise ValueError(f"ACCOUNTING_SECRET_KEYS has a duplicate key version: {int(version)}.")
        keys[int(version)] = key
    return keys


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-autosnab-local-development-only",
)

# SECURITY WARNING: don't run with debug turned on in production!
# 2026-08-15: was defaulting to "1" (debug ON) whenever DJANGO_DEBUG isn't set —
# opt-out instead of opt-in. Confirmed live on prod (avtosnab.testant.online): a
# 500 from the admin returned a full Django debug traceback page (source
# snippets, file paths, request/env dump — SECRET_KEY/tokens themselves stay
# masked by Django's own cleanse_setting, but everything else leaks) — meaning
# prod's deploy env doesn't set DJANGO_DEBUG=0 explicitly. .env.example already
# spells out DJANGO_DEBUG=1 for local dev, so flipping the code default to "0"
# doesn't change local behavior — it only changes what happens when the var is
# missing, which should never be debug-on.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if os.environ.get("DJANGO_ALLOWED_HOSTS") else []


def _to_csrf_origin(host):
    host = host.strip()
    if host.startswith("."):
        return f"https://*{host}"
    return f"https://{host}"


# Django's CSRF check compares the Referer header against this list for any
# request it considers secure — without it, POST-ing the admin login form
# over HTTPS behind the reverse proxy fails with "CSRF verification failed".
# Leading-dot ALLOWED_HOSTS entries (subdomain wildcards, e.g. ".ngrok-free.dev")
# become "https://*.ngrok-free.dev" — CSRF_TRUSTED_ORIGINS uses "*." not ".".
CSRF_TRUSTED_ORIGINS = [_to_csrf_origin(host) for host in ALLOWED_HOSTS if host.strip()]

CORS_ALLOWED_ORIGINS = (
    os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if os.environ.get("CORS_ALLOWED_ORIGINS") else []
)

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_spectacular",
    "corsheaders",
    "core",
    "suppliers",
    "catalog",
    "api",
    "notifications",
    "file_imports",
    "accounting",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "api.v1.suppliers.telemetry.LegacyUploadsTelemetryMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "autosnab"),
        "USER": os.environ.get("POSTGRES_USER", "autosnab"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "autosnab"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "OPTIONS": {
            "sslmode": os.environ.get("PGSSLMODE", "require"),
            "sslrootcert": os.environ.get("PGSSLCERT", ""),
            "sslcert": "",
            "sslkey": "",
        },
        # 2026-08-14: CONN_MAX_AGE не был задан — дефолт 0 значит новое TCP+TLS
        # соединение на каждый запрос к внешнему managed Postgres (Yandex Cloud,
        # PGSSLMODE=require — полный TLS-хендшейк каждый раз). EXPLAIN ANALYZE
        # на реальных прод-данных показал сам SQL-запрос ~10мс, но тот же запрос
        # из приложения — 1.5+ секунды: разница объясняется именно оверхедом
        # установки соединения по сети до чужого облака на каждый запрос, не
        # самим запросом. CONN_HEALTH_CHECKS проверяет живость переиспользуемого
        # соединения перед использованием (Django 4.1+) — без этого протухшее
        # соединение сломало бы первый запрос после простоя.
        "CONN_MAX_AGE": _int_env("POSTGRES_CONN_MAX_AGE", 600),
        "CONN_HEALTH_CHECKS": True,
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "ru-ru"

TIME_ZONE = "Europe/Moscow"

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Медиа-файлы (логотипы поставщиков, документы заявок на доступ и т.п.) — S3-совместимое
# object storage (Яндекс Object Storage по умолчанию), если задан AWS_STORAGE_BUCKET_NAME;
# иначе локальная ФС контейнера (dev/тесты — на этом полагается TemporaryMediaRootMixin
# в тестах, поэтому переключение обязано быть условным, не безусловной заменой).
#
# 2026-07-29: раньше был только FileSystemStorage без какого-либо persistent volume в
# деплое и без проксирования /media/ в nginx — загруженные файлы физически сохранялись
# в эфемерную ФС контейнера app и были недостижимы публично (404 на проде), см.
# docs/wiki/logs/2026-07-29-logo-url-404-no-media-storage.md. default_storage — абстракция
# Django, поэтому весь код, который уже писал через неё (persist_supplier_logo,
# persist_supplier_join_request_document, persist_supplier_price_source_file), заработал
# end-to-end сразу после переключения бэкенда, без своих правок.
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
if AWS_STORAGE_BUCKET_NAME:
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL", "https://storage.yandexcloud.net")
    AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "ru-central1")
    # Пусто по умолчанию — django-storages сам строит публичный URL из endpoint+bucket
    # (https://<bucket>.storage.yandexcloud.net/<key>). Задать, только если перед бакетом
    # стоит CDN/свой домен.
    AWS_S3_CUSTOM_DOMAIN = os.environ.get("AWS_S3_CUSTOM_DOMAIN", "") or None
    # Yandex Object Storage — публичный доступ через bucket policy, не через ACL на объект
    # (в отличие от AWS S3); отправлять ACL-заголовок не нужно и может быть отклонено.
    AWS_DEFAULT_ACL = None
    # Обычные публичные URL без подписи — logo_url/documents должны быть постоянными
    # ссылками, не истекающими presigned-ссылками.
    AWS_QUERYSTRING_AUTH = False
    AWS_S3_FILE_OVERWRITE = False
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
MEDIA_URL = os.environ.get("DJANGO_MEDIA_URL", "/media/")
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "var" / "media"))

# Исходные файлы импорта (Supplier/Master/Venue): хранение на 30 дней для повторной обработки воркером и аудита.
# Флаг выключен по умолчанию -- включается на стенде после проверки (канареечная выкатка).
IMPORT_FILE_STORAGE_ENABLED = _bool_env("IMPORT_FILE_STORAGE_ENABLED", False)
IMPORT_FILE_RETENTION_DAYS = _int_env("IMPORT_FILE_RETENTION_DAYS", 30)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SERVICE_API_TOKEN = os.environ.get("SERVICE_API_TOKEN", "")
YANDEX_MAPS_KEY = os.environ.get("YANDEX_MAPS_KEY", "")

# Public cabinet URL the account-transfer claim link points to, e.g.
# https://avtosnab.testant.online/cabinet/claim — the frontend route that
# renders the "set a password" screen and calls the claim endpoint.
ACCOUNT_TRANSFER_CLAIM_BASE_URL = os.environ.get(
    "ACCOUNT_TRANSFER_CLAIM_BASE_URL", "http://localhost:8000/cabinet/claim"
)
ACCOUNT_TRANSFER_TTL_HOURS = _int_env("ACCOUNT_TRANSFER_TTL_HOURS", 72)

# 2026-09-29: картинки-заглушки по виду товара в каталоге (прототип, см.
# api/v1/catalog/category_placeholders.py). Пусто — ссылка строится от хоста запроса
# без схемы (//host/static/...). Задать, если статика раздаётся с другого адреса.
CATALOG_PLACEHOLDER_BASE_URL = os.environ.get("CATALOG_PLACEHOLDER_BASE_URL", "")

# Supplier cabinet: invite a teammate to the same company (adds a second
# SupplierUser without touching the existing owner — unlike ACCOUNT_TRANSFER_*).
SUPPLIER_INVITE_BASE_URL = os.environ.get("SUPPLIER_INVITE_BASE_URL", "http://localhost:8000/cabinet/invite")
SUPPLIER_INVITE_TTL_HOURS = _int_env("SUPPLIER_INVITE_TTL_HOURS", 72)

# 2026-08-15: venue cabinet mirror of the ACCOUNT_TRANSFER_*/SUPPLIER_INVITE_*
# settings above — see docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md.
VENUE_ACCOUNT_TRANSFER_CLAIM_BASE_URL = os.environ.get(
    "VENUE_ACCOUNT_TRANSFER_CLAIM_BASE_URL", "http://localhost:8000/venue-cabinet/claim"
)
VENUE_ACCOUNT_TRANSFER_TTL_HOURS = _int_env("VENUE_ACCOUNT_TRANSFER_TTL_HOURS", 72)
VENUE_INVITE_BASE_URL = os.environ.get("VENUE_INVITE_BASE_URL", "http://localhost:8000/venue/invite")
VENUE_INVITE_TTL_HOURS = _int_env("VENUE_INVITE_TTL_HOURS", 72)
VENUE_INVITE_LINK_BASE_URL = os.environ.get("VENUE_INVITE_LINK_BASE_URL", "http://localhost:8000/venue/invite-link")

# "Пригласите по ссылке" (2026-08-04) — многоразовая ссылка, отдельный путь
# от SUPPLIER_INVITE_BASE_URL (тот — одноразовый, привязан к email).
SUPPLIER_INVITE_LINK_BASE_URL = os.environ.get(
    "SUPPLIER_INVITE_LINK_BASE_URL", "http://localhost:8000/cabinet/invite-link"
)

# "Присоединиться к Telegram-боту" кнопка в ЛК поставщика (2026-07-15) —
# заменила ручной ввод invite-кода в чате. Без "@" в username — так его
# ожидает deep-link https://t.me/<username>?start=<token>.
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "ddas_autosnab_price_bot")
# ═══════════════════════════════════════════════════════════════════════════════
# ДОБАВЛЕНО: Поддержка Max-бота для модального окна «Подключение к боту Поставщик»
# Раньше был только Telegram. Теперь поставщик может выбрать канал: telegram или max.
# ═══════════════════════════════════════════════════════════════════════════════

# Telegram-бот для поставщиков
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "ddas_autosnab_price_bot")

# Max-бот для поставщиков (добавлено 2026-08-03)
MAX_BOT_USERNAME = os.environ.get("MAX_BOT_USERNAME", "ddas_autosnab_price_bot")

# TTL для ссылки подключения к боту (единый для обоих каналов)
SUPPLIER_BOT_LINK_TTL_MINUTES = _int_env("SUPPLIER_BOT_LINK_TTL_MINUTES", 30)

# Токен Telegram-бота для отправки сообщений из бэкенда (прямая регистрация через Mini App)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# dadata.ru company lookup by INN (staff-facing enrichment, not blocking).
DADATA_API_KEY = os.environ.get("DADATA_API_KEY", "")
DADATA_SECRET_KEY = os.environ.get("DADATA_SECRET_KEY", "")

# "Войти через Яндекс" — доп. способ входа рядом с email/пароль и username/пароль,
# не заменяет их. Один client_id/secret на оба intent (supplier/venue) — см.
# api/v1/auth/yandex_client.py, YandexOAuthLoginView/YandexOAuthCallbackView.
YANDEX_OAUTH_CLIENT_ID = os.environ.get("YANDEX_OAUTH_CLIENT_ID", "")
YANDEX_OAUTH_CLIENT_SECRET = os.environ.get("YANDEX_OAUTH_CLIENT_SECRET", "")
YANDEX_OAUTH_REDIRECT_URI = os.environ.get(
    "YANDEX_OAUTH_REDIRECT_URI", "http://localhost:8000/api/v1/auth/yandex/callback"
)
YANDEX_OAUTH_FRONTEND_CALLBACK_URL = os.environ.get(
    "YANDEX_OAUTH_FRONTEND_CALLBACK_URL", "http://localhost:5173/auth/yandex/callback"
)

# "Войти через VK ID" — доп. способ входа, тот же паттерн, что и Яндекс (один
# client_id/secret на оба intent supplier/venue). VK ID — OAuth 2.1 + PKCE
# (id.vk.ru), не старый VK OAuth. См. api/v1/auth/vk_client.py,
# VKOAuthLoginView/VKOAuthCallbackView.
VK_OAUTH_CLIENT_ID = os.environ.get("VK_OAUTH_CLIENT_ID", "")
VK_OAUTH_CLIENT_SECRET = os.environ.get("VK_OAUTH_CLIENT_SECRET", "")  # "Защищённый ключ" в кабинете VK ID
# "Сервисный ключ доступа" из кабинета VK ID — НЕ используется в этом OAuth-флоу
# (email/имя получаем через код-обмен пользователя + users.get с его access_token),
# сохранён на случай будущих server-to-server вызовов VK API от имени приложения.
VK_OAUTH_SERVICE_KEY = os.environ.get("VK_OAUTH_SERVICE_KEY", "")
VK_OAUTH_REDIRECT_URI = os.environ.get("VK_OAUTH_REDIRECT_URI", "http://localhost:8000/api/v1/auth/vk/callback")
VK_OAUTH_FRONTEND_CALLBACK_URL = os.environ.get(
    "VK_OAUTH_FRONTEND_CALLBACK_URL", "http://localhost:5173/auth/vk/callback"
)

# Supplier self-registration: email confirmation link.
EMAIL_VERIFICATION_BASE_URL = os.environ.get(
    "EMAIL_VERIFICATION_BASE_URL", "http://localhost:8000/cabinet/verify-email"
)
EMAIL_VERIFICATION_TTL_HOURS = _int_env("EMAIL_VERIFICATION_TTL_HOURS", 24)

# "Я забыл пароль" — resetting an existing account's own password (distinct
# from ACCOUNT_TRANSFER_*, which hands the seat to a different person).
PASSWORD_RESET_BASE_URL = os.environ.get("PASSWORD_RESET_BASE_URL", "http://localhost:8000/cabinet/reset-password")
PASSWORD_RESET_TTL_HOURS = _int_env("PASSWORD_RESET_TTL_HOURS", 2)

AI_NORMALIZATION_URL = os.environ.get("AI_NORMALIZATION_URL", "https://api.artemox.com/v1/chat/completions")
AI_NORMALIZATION_MODEL = os.environ.get("AI_NORMALIZATION_MODEL", "gpt-4o-mini")
AI_NORMALIZATION_API_KEY = os.environ.get("AI_NORMALIZATION_API_KEY", "")
AI_NORMALIZATION_AUTHORIZATION = os.environ.get("AI_NORMALIZATION_AUTHORIZATION", "")
AI_NORMALIZATION_PROVIDER = os.environ.get("AI_NORMALIZATION_PROVIDER", "openai").strip().lower()
AI_NORMALIZATION_ANTHROPIC_VERSION = os.environ.get("AI_NORMALIZATION_ANTHROPIC_VERSION", "2023-06-01")
AI_NORMALIZATION_ENABLED = _bool_env("AI_NORMALIZATION_ENABLED", True)
AI_NORMALIZATION_DEFAULT_MODE = os.environ.get("AI_NORMALIZATION_DEFAULT_MODE", "ambiguous")
AI_NORMALIZATION_FULL_MAX_ROWS = _int_env("AI_NORMALIZATION_FULL_MAX_ROWS", 200)
AI_NORMALIZATION_LOCAL_MAX_ROWS = _int_env("AI_NORMALIZATION_LOCAL_MAX_ROWS", 200)
AI_NORMALIZATION_STOP_ON_SYSTEM_ERROR = _bool_env("AI_NORMALIZATION_STOP_ON_SYSTEM_ERROR", True)
AI_NORMALIZATION_CHUNK_SIZE = _int_env("AI_NORMALIZATION_CHUNK_SIZE", 20)
AI_NORMALIZATION_TIMEOUT_SECONDS = _int_env("AI_NORMALIZATION_TIMEOUT_SECONDS", 110)
AI_NORMALIZATION_MAX_RETRIES = _int_env("AI_NORMALIZATION_MAX_RETRIES", 0)
AI_NORMALIZATION_USE_INSTRUCTOR = _bool_env("AI_NORMALIZATION_USE_INSTRUCTOR", False)

# TASK-75 (partial): LLM unit-of-measurement suggestion for Master Catalog
# import rows with unit_unrecognized=True. Separate flag from
# AI_NORMALIZATION_ENABLED (which gates supplier price normalization, a
# different product) so either can be toggled independently. Reuses the
# same AI_NORMALIZATION_* provider/credentials — one AI gateway per project.
MASTER_CATALOG_UNIT_LLM_ENABLED = _bool_env("MASTER_CATALOG_UNIT_LLM_ENABLED", True)
MASTER_CATALOG_UNIT_LLM_TIMEOUT_SECONDS = _int_env("MASTER_CATALOG_UNIT_LLM_TIMEOUT_SECONDS", 20)
# ТЗ §48 "Производительность": send unrecognized-unit rows to the LLM in
# batches instead of one call per whole file — independent of
# AI_NORMALIZATION_CHUNK_SIZE (supplier price), this call's per-row payload
# is much smaller so a larger default chunk is fine.
MASTER_CATALOG_UNIT_LLM_CHUNK_SIZE = _int_env("MASTER_CATALOG_UNIT_LLM_CHUNK_SIZE", 30)

# TASK-75 second half (§40-42): LLM re-ranking of find_candidate_master_products'
# own candidate list, only in the ambiguous score zone (see
# MASTER_PRODUCT_MATCH_CONFIDENT_SCORE next to MASTER_PRODUCT_MATCH_MIN_SCORE).
# Independent flag/timeout/chunk-size from MASTER_CATALOG_UNIT_LLM_* — different
# feature, different failure risk, independently toggleable.
MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED = _bool_env("MASTER_PRODUCT_CANDIDATE_RANKING_ENABLED", False)
MASTER_PRODUCT_CANDIDATE_RANKING_TIMEOUT_SECONDS = _int_env("MASTER_PRODUCT_CANDIDATE_RANKING_TIMEOUT_SECONDS", 20)
MASTER_PRODUCT_CANDIDATE_RANKING_CHUNK_SIZE = _int_env("MASTER_PRODUCT_CANDIDATE_RANKING_CHUNK_SIZE", 30)

# TASK-87: shared provider-neutral LLM connector (api/llm) — safe migration flag,
# disabled by default until price-import and product-card acceptance tests pass
# together. Primary/fallback route (e.g. local Bonsai primary + OpenAI fallback);
# empty LLM_PRIMARY_* falls back to the AI_NORMALIZATION_*/OPENAI_* settings above.
# Local primary accepts an empty key only for localhost/127.0.0.1.
LLM_CONNECTOR_ENABLED = _bool_env("LLM_CONNECTOR_ENABLED", False)
LLM_PRIMARY_PROVIDER = os.environ.get("LLM_PRIMARY_PROVIDER", "")
LLM_PRIMARY_BASE_URL = os.environ.get("LLM_PRIMARY_BASE_URL", "")
LLM_PRIMARY_API_KEY = os.environ.get("LLM_PRIMARY_API_KEY", "")
LLM_PRIMARY_MODEL = os.environ.get("LLM_PRIMARY_MODEL", "")
LLM_PRIMARY_TIMEOUT_SECONDS = _int_env("LLM_PRIMARY_TIMEOUT_SECONDS", 60)
LLM_FALLBACK_PROVIDER = os.environ.get("LLM_FALLBACK_PROVIDER", "")
LLM_FALLBACK_BASE_URL = os.environ.get("LLM_FALLBACK_BASE_URL", "")
LLM_FALLBACK_API_KEY = os.environ.get("LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "")
LLM_FALLBACK_TIMEOUT_SECONDS = _int_env("LLM_FALLBACK_TIMEOUT_SECONDS", 60)

# TASK-87/TASK-69 (задача 6, реанимирована 2026-09-25): автозаполнение карточки
# товара поставщика (product_card_llm.suggest_product_card) — рекомендация,
# ничего не сохраняет. При недоступности LLM или выключенном флаге вызывающая
# сторона (suggest_supplier_product_card) отдаёт пустую рекомендацию, а не 5xx.
SUPPLIER_PRODUCT_CARD_LLM_ENABLED = _bool_env("SUPPLIER_PRODUCT_CARD_LLM_ENABLED", True)

AI_CLEANUP_QUEUE = os.environ.get("AI_CLEANUP_QUEUE", "ai_normalization")
NOTIFICATION_QUEUE = os.environ.get("NOTIFICATION_QUEUE", "notifications")
NOTIFICATION_WORKER_CONCURRENCY = _int_env("NOTIFICATION_WORKER_CONCURRENCY", 2)
AI_CLEANUP_MAX_IN_FLIGHT = _int_env("AI_CLEANUP_MAX_IN_FLIGHT", 2)
AI_CLEANUP_RATE_LIMIT = os.environ.get("AI_CLEANUP_RATE_LIMIT", "6/m")

# Accounting (iiko) integration.
# Versioned symmetric keys for stored iiko credentials: base64 of 32 bytes each, "1:<b64>,2:<b64>".
# New data is encrypted with the active version; older versions stay for decryption during rotation.
ACCOUNTING_SECRET_KEYS = parse_secret_keys(os.environ.get("ACCOUNTING_SECRET_KEYS", ""))
ACCOUNTING_SECRET_ACTIVE_KEY_VERSION = _int_env(
    "ACCOUNTING_SECRET_ACTIVE_KEY_VERSION", max(ACCOUNTING_SECRET_KEYS, default=1)
)
# Risky behaviour: periodic iiko sync stays off until explicitly enabled.
ACCOUNTING_SCHEDULED_SYNC_ENABLED = _bool_env("ACCOUNTING_SCHEDULED_SYNC_ENABLED", False)
ACCOUNTING_STOCK_INTERVAL_MINUTES = _int_env("ACCOUNTING_STOCK_INTERVAL_MINUTES", 60)
ACCOUNTING_CATALOG_INTERVAL_HOURS = _int_env("ACCOUNTING_CATALOG_INTERVAL_HOURS", 24)
ACCOUNTING_STOCK_STALE_AFTER_MINUTES = _int_env("ACCOUNTING_STOCK_STALE_AFTER_MINUTES", 180)
ACCOUNTING_MANUAL_SYNC_THROTTLE_SECONDS = _int_env("ACCOUNTING_MANUAL_SYNC_THROTTLE_SECONDS", 60)
ACCOUNTING_HOST_SLOTS = _int_env("ACCOUNTING_HOST_SLOTS", 1)
ACCOUNTING_WORKER_CONCURRENCY = _int_env("ACCOUNTING_WORKER_CONCURRENCY", 12)
ACCOUNTING_BREAKER_THRESHOLD = _int_env("ACCOUNTING_BREAKER_THRESHOLD", 5)
ACCOUNTING_BREAKER_COOLDOWN_MINUTES = _int_env("ACCOUNTING_BREAKER_COOLDOWN_MINUTES", 60)
ACCOUNTING_IIKO_REQUEST_DELAY = _float_env("ACCOUNTING_IIKO_REQUEST_DELAY", 0.5)
ACCOUNTING_IIKO_TIMEOUT = _int_env("ACCOUNTING_IIKO_TIMEOUT", 15)
ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION = _int_env("ACCOUNTING_IIKO_MAX_REQUESTS_PER_SESSION", 60)
ACCOUNTING_STORES_PER_REQUEST = _int_env("ACCOUNTING_STORES_PER_REQUEST", 20)
ACCOUNTING_OLAP_POSTS_PER_SESSION = _int_env("ACCOUNTING_OLAP_POSTS_PER_SESSION", 8)
ACCOUNTING_JOB_STALL_SECONDS = _int_env("ACCOUNTING_JOB_STALL_SECONDS", 900)
# Transaction types that are not consumption per the spec (ТЗ).
ACCOUNTING_CONSUMPTION_EXCLUDED_TYPES = ("INVOICE", "SALES_REVENUE", "DISCOUNT")
ACCOUNTING_CONSUMPTION_WINDOW_DAYS = _int_env("ACCOUNTING_CONSUMPTION_WINDOW_DAYS", 7)  # complete past days read
ACCOUNTING_CONSUMPTION_INTERVAL_HOURS = _int_env("ACCOUNTING_CONSUMPTION_INTERVAL_HOURS", 24)
# A day is read again if it was first read earlier than this many hours after its end (late shift closings).
ACCOUNTING_CONSUMPTION_SETTLE_HOURS = _int_env("ACCOUNTING_CONSUMPTION_SETTLE_HOURS", 6)
ACCOUNTING_QUEUE_SYNC = os.environ.get("ACCOUNTING_QUEUE_SYNC", "iiko_sync")
ACCOUNTING_QUEUE_MANUAL = os.environ.get("ACCOUNTING_QUEUE_MANUAL", "iiko_manual")
# Cheap scheduler tasks (claim due connections, recover stalled jobs). They must not wait behind long syncs.
ACCOUNTING_QUEUE_CONTROL = os.environ.get("ACCOUNTING_QUEUE_CONTROL", "iiko_control")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", AI_NORMALIZATION_API_KEY)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_NORMALIZATION_MODEL = os.environ.get("OPENAI_NORMALIZATION_MODEL", AI_NORMALIZATION_MODEL)
OPENAI_NORMALIZATION_TIMEOUT_SECONDS = _int_env(
    "OPENAI_NORMALIZATION_TIMEOUT_SECONDS",
    AI_NORMALIZATION_TIMEOUT_SECONDS,
)
SUPPLIER_PRICE_PDF_EXTRACTOR = os.environ.get("SUPPLIER_PRICE_PDF_EXTRACTOR", "auto")

# Catalog "search by photo": recognizes a product on an uploaded photo via the
# same AI_NORMALIZATION_* vision-capable model, then runs the result through
# the existing text search — see api/v1/catalog/vision_client.py.
CATALOG_PHOTO_SEARCH_MAX_BYTES = _int_env("CATALOG_PHOTO_SEARCH_MAX_BYTES", 8 * 1024 * 1024)

# Catalog hybrid search: MiniLM produces normalized embeddings, Qdrant Cloud
# stores/searches vectors, and api.v1.catalog.search combines lexical and vector
# ranks through weighted reciprocal rank fusion (RRF).
CATALOG_VECTOR_SEARCH_ENABLED = _bool_env("CATALOG_VECTOR_SEARCH_ENABLED", True)
CATALOG_QDRANT_AUTO_SYNC_ENABLED = _bool_env("CATALOG_QDRANT_AUTO_SYNC_ENABLED", True)
CATALOG_QDRANT_SYNC_BATCH_SIZE = _int_env("CATALOG_QDRANT_SYNC_BATCH_SIZE", 128)
CATALOG_EMBEDDING_MODEL = os.environ.get(
    "CATALOG_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
CATALOG_VECTOR_SIZE = _int_env("CATALOG_VECTOR_SIZE", 384)
# Сколько секунд после неудачной загрузки embedding-модели повторные вызовы падают сразу, не обращаясь к сети.
CATALOG_EMBEDDING_FAILURE_TTL_SECONDS = _int_env("CATALOG_EMBEDDING_FAILURE_TTL_SECONDS", 60)
CATALOG_VECTOR_SEARCH_TOP_K = _int_env("CATALOG_VECTOR_SEARCH_TOP_K", 100)
CATALOG_VECTOR_SEARCH_TIMEOUT_SECONDS = float(os.environ.get("CATALOG_VECTOR_SEARCH_TIMEOUT_SECONDS", "1"))
CATALOG_VECTOR_MIN_SCORE = float(os.environ.get("CATALOG_VECTOR_MIN_SCORE", "0.2"))
CATALOG_SEARCH_CANDIDATE_LIMIT = _int_env("CATALOG_SEARCH_CANDIDATE_LIMIT", 200)
CATALOG_VECTOR_FALLBACK_MIN_LEXICAL = _int_env("CATALOG_VECTOR_FALLBACK_MIN_LEXICAL", 20)
CATALOG_VECTOR_SKIP_MIN_LEXICAL_SCORE = float(os.environ.get("CATALOG_VECTOR_SKIP_MIN_LEXICAL_SCORE", "35"))

# Master-catalog product matching (spec §3.2, find_candidate_master_products
# contract, master-catalog-import-stage-2 plan): MasterProduct.canonical_name
# embeddings, scoped per owner, compared with pgvector cosine similarity
# directly in Postgres — not Qdrant like the platform-wide catalog.Product
# search above: a single owner's MasterProduct list is small (dozens/hundreds,
# not the whole catalog), so an extra network hop and per-owner filtering in
# an external vector store isn't warranted here.
MASTER_PRODUCT_MATCH_MIN_SCORE = float(os.environ.get("MASTER_PRODUCT_MATCH_MIN_SCORE", "0.5"))
MASTER_PRODUCT_MATCH_TOP_K = _int_env("MASTER_PRODUCT_MATCH_TOP_K", 5)
# Vector score at or above which we trust the search as-is and skip LLM
# ranking (§40-42) — only scores in [MASTER_PRODUCT_MATCH_MIN_SCORE, this)
# are "ambiguous enough" to spend an LLM call on.
MASTER_PRODUCT_MATCH_CONFIDENT_SCORE = float(os.environ.get("MASTER_PRODUCT_MATCH_CONFIDENT_SCORE", "0.8"))
# TASK-112 (ТЗ §48): master catalog import batching — a file over this many
# rows is processed async (Celery, see api/v1/catalog/tasks.py) instead of
# synchronously in the preview request; MASTER_PRODUCT_IMPORT_BATCH_SIZE is
# the outer-batch size the task resolves rows in, so progress (rows_done)
# updates incrementally rather than only at the very end.
MASTER_PRODUCT_IMPORT_ASYNC_THRESHOLD_ROWS = _int_env("MASTER_PRODUCT_IMPORT_ASYNC_THRESHOLD_ROWS", 500)
MASTER_PRODUCT_IMPORT_BATCH_SIZE = _int_env("MASTER_PRODUCT_IMPORT_BATCH_SIZE", 500)
MASTER_PRODUCT_IMPORT_MAX_ROWS = _int_env("MASTER_PRODUCT_IMPORT_MAX_ROWS", 12000)
MASTER_PRODUCT_IMPORT_TASK_TIME_LIMIT = _int_env("MASTER_PRODUCT_IMPORT_TASK_TIME_LIMIT", 3600)
MASTER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT = _int_env("MASTER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT", 3300)
# Файловый preview Справочника заведения для больших файлов идёт в фоне (как у Мастер-справочника и импорта
# поставщика): сессия pending -> processing -> ready, строки создаются пачками. Требует IMPORT_FILE_STORAGE_ENABLED.
VENUE_IMPORT_ASYNC_ENABLED = _bool_env("VENUE_IMPORT_ASYNC_ENABLED", False)
VENUE_IMPORT_ASYNC_THRESHOLD_ROWS = _int_env("VENUE_IMPORT_ASYNC_THRESHOLD_ROWS", 500)
VENUE_IMPORT_MAX_ROWS = _int_env("VENUE_IMPORT_MAX_ROWS", 12000)
VENUE_IMPORT_BATCH_SIZE = _int_env("VENUE_IMPORT_BATCH_SIZE", 500)
# Мастер-справочник и Справочник заведения: файловый preview любого размера идёт в воркере и всегда отвечает 202
# (как analyze импорта товаров поставщика). Требует IMPORT_FILE_STORAGE_ENABLED (воркер читает сохранённый файл);
# для заведения ещё и VENUE_IMPORT_ASYNC_ENABLED. Выключено -- прежнее поведение: до порога строк в запросе, ответ 200.
CATALOG_IMPORT_ALWAYS_ASYNC = _bool_env("CATALOG_IMPORT_ALWAYS_ASYNC", False)
VENUE_IMPORT_TASK_TIME_LIMIT = _int_env("VENUE_IMPORT_TASK_TIME_LIMIT", 3600)
VENUE_IMPORT_TASK_SOFT_TIME_LIMIT = _int_env("VENUE_IMPORT_TASK_SOFT_TIME_LIMIT", 3300)
# TASK-78 (api/v1/catalog/selectors.py:_venue_import_candidates): keep this
# at or above 0.3, pg_trgm's own default `similarity_threshold` — the query
# also prefilters with the `%` operator (to use the GIN trigram index),
# which uses that default GUC, not this setting. Below 0.3 the prefilter can
# silently exclude candidates this setting would otherwise allow.
VENUE_IMPORT_MATCH_MIN_SIMILARITY = float(os.environ.get("VENUE_IMPORT_MATCH_MIN_SIMILARITY", "0.3"))
# TASK-122 (raw ТЗ §1 «Проверка перед импортом» requires identical logic for
# Мастер-справочник AND Справочник компании; TASK-75 only ever built the
# Мастер-справочник half). Same ambiguous-zone LLM re-ranking as
# MASTER_PRODUCT_CANDIDATE_RANKING_* below, now for _venue_import_candidates'
# pg_trgm score instead of the vector score — independent flag/timeout/
# chunk-size, off by default until measured on the real stand (same rollout
# pattern project-wide for new LLM-touching features).
VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY = float(os.environ.get("VENUE_IMPORT_MATCH_CONFIDENT_SIMILARITY", "0.8"))
VENUE_IMPORT_CANDIDATE_RANKING_ENABLED = _bool_env("VENUE_IMPORT_CANDIDATE_RANKING_ENABLED", False)
VENUE_IMPORT_CANDIDATE_RANKING_TIMEOUT_SECONDS = _int_env("VENUE_IMPORT_CANDIDATE_RANKING_TIMEOUT_SECONDS", 20)
VENUE_IMPORT_CANDIDATE_RANKING_CHUNK_SIZE = _int_env("VENUE_IMPORT_CANDIDATE_RANKING_CHUNK_SIZE", 30)
# TASK-122 п.1 (raw ТЗ §1/§35-39): same unit-of-measurement LLM suggestion as
# MASTER_CATALOG_UNIT_LLM_* above, now for Справочник компании
# (requested_unit_label unresolved against core.Unit) — independent
# flag/timeout/chunk-size, default True (unlike candidate ranking above,
# this only ever fires for the rare row whose unit text didn't match the
# closed list directly, not a full pass over every row).
VENUE_IMPORT_UNIT_LLM_ENABLED = _bool_env("VENUE_IMPORT_UNIT_LLM_ENABLED", True)
VENUE_IMPORT_UNIT_LLM_TIMEOUT_SECONDS = _int_env("VENUE_IMPORT_UNIT_LLM_TIMEOUT_SECONDS", 20)
VENUE_IMPORT_UNIT_LLM_CHUNK_SIZE = _int_env("VENUE_IMPORT_UNIT_LLM_CHUNK_SIZE", 30)
# Same pg_trgm threshold pattern as VENUE_IMPORT_MATCH_MIN_SIMILARITY above —
# «Импорт товаров поставщика» duplicate search (api/v1/suppliers/product_import_matching.py).
SUPPLIER_PRODUCT_IMPORT_MATCH_MIN_SIMILARITY = float(
    os.environ.get("SUPPLIER_PRODUCT_IMPORT_MATCH_MIN_SIMILARITY", "0.3")
)
# Off by default — ship the code, turn on after a latency/quality check on the real stand
# (same rollout pattern used project-wide for new LLM-touching features).
SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED = (
    os.environ.get("SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED", "false").lower() == "true"
)
# Чанк 10 строк и 16 потоков вместо 30 и 8: замер 2026-10-01 на файле Очаково (2099 строк) -- LLM 274 с -> 160 с, время
# вызова определяется длиной ответа модели. Значения переопределяются окружением (лимиты провайдера по токенам в минуту).
SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE = _int_env("SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_SIZE", 10)
SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_TIMEOUT_SECONDS = _int_env("SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_TIMEOUT_SECONDS", 40)
# Параллельных запросов к модели на один импорт (чанки независимы; замер 2026-09-30: 5 чанков параллельно = время одного).
SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS = _int_env("SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_MAX_WORKERS", 16)
SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_RETRIES = _int_env("SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_CHUNK_RETRIES", 1)
# TASK-132: дозаполнение и сопоставление импорта товаров поставщика идут в Celery для файла любого размера
# (analyze отвечает 202 сразу). BATCH_SIZE -- сколько строк обрабатывается за шаг (между шагами обновляется
# rows_done и в GET сессии появляются готовые строки). Не задан (0/пусто) = ENRICHMENT_CHUNK_SIZE x
# ENRICHMENT_MAX_WORKERS: пачка занимает все потоки ровно одной «волной». Явное значение в окружении главнее.
SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE = _int_env("SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE", 0) or None
# Лимиты импорта товаров: строк в файле и время одной задачи обработки. 12 000 строк при последовательной обработке
# (~160-275 с на 2099 строк, замер 2026-10-01) укладываются в ~16-26 мин -- выше общего CELERY_TASK_TIME_LIMIT (1800 с).
SUPPLIER_PRODUCT_IMPORT_MAX_ROWS = _int_env("SUPPLIER_PRODUCT_IMPORT_MAX_ROWS", 12000)
# Страница строк в GET /suppliers/products/import/{id}: по умолчанию и максимум (limit).
SUPPLIER_PRODUCT_IMPORT_ITEMS_PAGE_SIZE = _int_env("SUPPLIER_PRODUCT_IMPORT_ITEMS_PAGE_SIZE", 500)
SUPPLIER_PRODUCT_IMPORT_ITEMS_MAX_PAGE_SIZE = _int_env("SUPPLIER_PRODUCT_IMPORT_ITEMS_MAX_PAGE_SIZE", 1000)
SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT = _int_env("SUPPLIER_PRODUCT_IMPORT_TASK_TIME_LIMIT", 3600)
SUPPLIER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT = _int_env("SUPPLIER_PRODUCT_IMPORT_TASK_SOFT_TIME_LIMIT", 3300)
# POST products/import/<id>/commit: true -- 202 и применение в Celery-воркере (статус в commit_status сессии, итог в
# commit_result, сессия не удаляется сразу); false (по умолчанию) -- прежний синхронный ответ 200 с итогом.
SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC = _bool_env("SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC", False)
# Сколько часов хранится завершённая фоновым commit сессия (нужна, чтобы клиент успел прочитать commit_result).
SUPPLIER_PRODUCT_IMPORT_COMMITTED_TTL_HOURS = _int_env("SUPPLIER_PRODUCT_IMPORT_COMMITTED_TTL_HOURS", 24)
# Сессии с упавшей обработкой удаляются фоновой очисткой через столько часов.
SUPPLIER_PRODUCT_IMPORT_FAILED_TTL_HOURS = _int_env("SUPPLIER_PRODUCT_IMPORT_FAILED_TTL_HOURS", 24)
# Сессия, которая «обрабатывается» без движения дольше этого (воркер упал, задача потерялась), тоже удаляется:
# дольше TASK_TIME_LIMIT одна задача не живёт. Удалить её руками нельзя (идёт обработка).
SUPPLIER_PRODUCT_IMPORT_STUCK_TTL_HOURS = _int_env("SUPPLIER_PRODUCT_IMPORT_STUCK_TTL_HOURS", 6)
# Потолок слоя чтения файлов (file_imports): выше любого продуктового лимита, чтобы данные не обрезались молча.
FILE_IMPORT_MAX_EXTRACTED_ROWS = _int_env("FILE_IMPORT_MAX_EXTRACTED_ROWS", 60000)
# PDF/DOCX/DOC/TXT/image при импорте товаров поставщика извлекаются в воркере из сохранённого файла (требует
# IMPORT_FILE_STORAGE_ENABLED). Выключено -- прежнее поведение (разбор внутри запроса analyze).
SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED = _bool_env("SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED", False)
# Доля строк с ценой, ниже которой сессия импорта получает предупреждение low_price_share (file_imports.quality).
SUPPLIER_IMPORT_MIN_PRICE_SHARE = float(os.environ.get("SUPPLIER_IMPORT_MIN_PRICE_SHARE", "0.5"))
CATALOG_FUZZY_MIN_SIMILARITY = float(os.environ.get("CATALOG_FUZZY_MIN_SIMILARITY", "0.2"))
CATALOG_FUZZY_CANDIDATE_LIMIT = _int_env("CATALOG_FUZZY_CANDIDATE_LIMIT", 200)
CATALOG_FUZZY_SCAN_LIMIT = _int_env("CATALOG_FUZZY_SCAN_LIMIT", 20_000)
DIRECTORY_SEARCH_TRIGRAM_THRESHOLD = float(os.environ.get("DIRECTORY_SEARCH_TRIGRAM_THRESHOLD", "0.3"))
CATALOG_RRF_K = _int_env("CATALOG_RRF_K", 60)
CATALOG_RRF_LEXICAL_WEIGHT = float(os.environ.get("CATALOG_RRF_LEXICAL_WEIGHT", "1.35"))
CATALOG_RRF_VECTOR_WEIGHT = float(os.environ.get("CATALOG_RRF_VECTOR_WEIGHT", "1.0"))
QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "autosnab_backend_products")
QDRANT_TIMEOUT_SECONDS = float(os.environ.get("QDRANT_TIMEOUT_SECONDS", "5"))

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
# A dead broker must fail a web request quickly. By default kombu retries it forever: the request then hangs until
# gunicorn kills the worker (a bare 500 page and a half-created import session). Bounded retries turn that into an
# error the views already answer with 503.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "max_retries": _int_env("CELERY_BROKER_MAX_RETRIES", 2),
    "interval_start": 0,
    "interval_step": 0.5,
    "interval_max": 1,
    "socket_connect_timeout": _int_env("CELERY_BROKER_CONNECT_TIMEOUT", 3),
}
CELERY_IMPORTS = (
    "api.v1.suppliers.tasks",
    "notifications.tasks",
    "api.v1.catalog.tasks",
    "file_imports.tasks",
    "accounting.tasks",
)
CELERY_TASK_ALWAYS_EAGER = _bool_env("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = _bool_env("CELERY_TASK_EAGER_PROPAGATES", True)
CELERY_TASK_TIME_LIMIT = _int_env("CELERY_TASK_TIME_LIMIT", 1800)
CELERY_TASK_SOFT_TIME_LIMIT = _int_env("CELERY_TASK_SOFT_TIME_LIMIT", 1500)
CELERY_TASK_ROUTES = {
    "accounting.enqueue_due_syncs": {"queue": ACCOUNTING_QUEUE_CONTROL},
    "accounting.run_sync_job": {"queue": ACCOUNTING_QUEUE_SYNC},
    "accounting.recover_stalled_jobs": {"queue": ACCOUNTING_QUEUE_CONTROL},
    "suppliers.normalize_supplier_price_upload": {"queue": AI_CLEANUP_QUEUE},
    "suppliers.ai_cleanup_supplier_price_upload": {"queue": AI_CLEANUP_QUEUE},
    "suppliers.sync_supplier_catalog_to_qdrant": {"queue": AI_CLEANUP_QUEUE},
    "notifications.dispatch_delivery": {"queue": NOTIFICATION_QUEUE},
    "notifications.enqueue_daily_delivery_checklists": {"queue": NOTIFICATION_QUEUE},
    "notifications.enqueue_pending_deliveries": {"queue": NOTIFICATION_QUEUE},
    "notifications.remove_supplier_action_buttons": {"queue": NOTIFICATION_QUEUE},
    "notifications.update_cancelled_supplier_messages": {"queue": NOTIFICATION_QUEUE},
    "catalog.process_master_product_import_upload": {"queue": AI_CLEANUP_QUEUE},
    # Without a route the task lands in the default "celery" queue, which no worker consumes
    # (entrypoint.sh starts the worker with -Q ai_normalization only) -> session stuck in processing.
    "suppliers.process_supplier_product_import_session": {"queue": AI_CLEANUP_QUEUE},
    "suppliers.extract_supplier_product_import_session": {"queue": AI_CLEANUP_QUEUE},
    "suppliers.commit_supplier_product_import_session": {"queue": AI_CLEANUP_QUEUE},
    "suppliers.purge_committed_product_import_sessions": {"queue": AI_CLEANUP_QUEUE},
    "catalog.process_venue_import_session": {"queue": AI_CLEANUP_QUEUE},
    "file_imports.purge_expired_import_files": {"queue": AI_CLEANUP_QUEUE},
    "file_imports.recover_stalled_imports": {"queue": AI_CLEANUP_QUEUE},
}
CELERY_TASK_QUEUES = (
    Queue(ACCOUNTING_QUEUE_SYNC),
    Queue(ACCOUNTING_QUEUE_MANUAL),
    Queue(ACCOUNTING_QUEUE_CONTROL),
    Queue(AI_CLEANUP_QUEUE),
    Queue(NOTIFICATION_QUEUE),
)
CELERY_TASK_ANNOTATIONS = {
    "suppliers.ai_cleanup_supplier_price_upload": {"rate_limit": AI_CLEANUP_RATE_LIMIT},
    "notifications.dispatch_delivery": {"acks_late": True, "reject_on_worker_lost": True, "ignore_result": True},
    "notifications.enqueue_daily_delivery_checklists": {
        "acks_late": True,
        "reject_on_worker_lost": True,
        "ignore_result": True,
    },
    "notifications.enqueue_pending_deliveries": {
        "acks_late": True,
        "reject_on_worker_lost": True,
        "ignore_result": True,
    },
    "notifications.remove_supplier_action_buttons": {
        "acks_late": True,
        "reject_on_worker_lost": True,
        "ignore_result": True,
    },
    "notifications.update_cancelled_supplier_messages": {
        "acks_late": True,
        "reject_on_worker_lost": True,
        "ignore_result": True,
    },
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "autosnab-accounting-enqueue": {
        "task": "accounting.enqueue_due_syncs",
        "schedule": crontab(minute="*"),
    },
    "autosnab-accounting-recover": {
        "task": "accounting.recover_stalled_jobs",
        "schedule": crontab(minute="*/5"),
    },
    "autosnab-retry-pending-notifications": {
        "task": "notifications.enqueue_pending_deliveries",
        "schedule": crontab(minute="*"),
    },
    "autosnab-daily-delivery-notifications": {
        "task": "notifications.enqueue_daily_delivery_checklists",
        "schedule": crontab(minute="*"),
    },
    "autosnab-recover-stalled-imports": {
        "task": "file_imports.recover_stalled_imports",
        "schedule": crontab(minute="*/5"),
    },
    "autosnab-purge-committed-product-import-sessions": {
        "task": "suppliers.purge_committed_product_import_sessions",
        "schedule": crontab(minute=20),
    },
    "autosnab-purge-expired-import-files": {
        "task": "file_imports.purge_expired_import_files",
        "schedule": crontab(hour=3, minute=15),
    },
}
NOTIFICATION_LINK_TTL_MINUTES = _int_env("NOTIFICATION_LINK_TTL_MINUTES", 60)
NOTIFICATION_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get(
    "NOTIFICATION_TELEGRAM_BOT_TOKEN", ""
)
NOTIFICATION_MAX_BOT_TOKEN = os.environ.get("NOTIFICATION_MAX_BOT_TOKEN") or os.environ.get("MAX_BOT_TOKEN", "")
NOTIFICATION_MAX_API_BASE = os.environ.get("NOTIFICATION_MAX_API_BASE", "https://platform-api2.max.ru")
NOTIFICATION_HTTP_TIMEOUT_SECONDS = float(os.environ.get("NOTIFICATION_HTTP_TIMEOUT_SECONDS", "10"))
SUPPLIER_ORDERS_URL = os.environ.get(
    "SUPPLIER_ORDERS_URL", "https://avtosnab.testant.online/supplier/cabinet/companies"
)
NOTIFICATION_TELEGRAM_MINI_APP_LINK = os.environ.get("NOTIFICATION_TELEGRAM_MINI_APP_LINK", "").strip()
NOTIFICATION_TELEGRAM_MINI_APP_BOT_USERNAME = (
    os.environ.get("NOTIFICATION_TELEGRAM_MINI_APP_BOT_USERNAME", "TEST_AUTOSNAB_BOT").strip().lstrip("@")
)
NOTIFICATION_DELIVERY_DRY_RUN = _bool_env("NOTIFICATION_DELIVERY_DRY_RUN", False)

DJANGO_LOG_LEVEL = os.environ.get("DJANGO_LOG_LEVEL", "WARNING")

# Telemetry вызовов legacy /suppliers/uploads/* (лог autosnab.legacy_uploads): нужна, чтобы по фактам решить,
# можно ли удалять legacy (спека §6). Только запись в лог.
LEGACY_UPLOADS_TELEMETRY_ENABLED = _bool_env("LEGACY_UPLOADS_TELEMETRY_ENABLED", True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
        },
    },
    "loggers": {
        "autosnab": {
            "handlers": ["console"],
            "level": DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        # Вызовы legacy /suppliers/uploads/* пишутся на INFO независимо от DJANGO_LOG_LEVEL.
        "autosnab.legacy_uploads": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # Расход токенов и длительность каждого вызова LLM (строка llm.finish) нужны на стенде всегда, независимо от
        # DJANGO_LOG_LEVEL: по ним считается стоимость импорта.
        # Запись/чтение/удаление исходных файлов импорта (import_file.stored / reused / read / purge): по ним на
        # стенде видно, что файл реально лёг в S3, независимо от DJANGO_LOG_LEVEL.
        "autosnab.file_imports": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # Этапы импорта товаров поставщика (import.analyze/extract/process.*): время парсинга, дозаполнения LLM,
        # поиска дублей по пачкам -- нужны на стенде всегда, независимо от DJANGO_LOG_LEVEL.
        "autosnab.supplier_import": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "autosnab.llm.connector": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

REST_FRAMEWORK = {
    # Left empty on purpose: HasServiceToken (below) covers every current
    # endpoint via its own manual Authorization-header check, and the new
    # JWT cabinet endpoints (api/v1/auth) issue tokens without needing to
    # *consume* them yet — no view currently reads request.user. Wiring
    # CoreUserJWTAuthentication in here globally breaks every existing
    # SERVICE_API_TOKEN request, because JWTAuthentication also inspects
    # the "Authorization: Bearer ..." header and raises on a non-JWT value
    # before HasServiceToken ever runs. Apply it per-view (authentication_classes
    # on a specific APIView) once a protected cabinet endpoint needs request.user.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [
        "api.v1.permissions.HasServiceToken",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# JWT for cabinet end-user auth (suppliers, venue staff) — separate from
# SERVICE_API_TOKEN, which only covers service-to-service calls (bot/n8n).
# Tokens carry core.User.id, not django.contrib.auth's User (AUTH_USER_MODEL
# is intentionally left as Django's default so /admin/ keeps working
# unchanged); see api/v1/authentication.py for the custom user lookup.
from datetime import timedelta as _timedelta  # noqa: E402

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": _timedelta(minutes=_int_env("JWT_ACCESS_TOKEN_MINUTES", 30)),
    "REFRESH_TOKEN_LIFETIME": _timedelta(days=_int_env("JWT_REFRESH_TOKEN_DAYS", 30)),
    "ROTATE_REFRESH_TOKENS": True,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# Email
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = _int_env("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
# 587 = STARTTLS (EMAIL_USE_TLS), 465 = implicit SSL (EMAIL_USE_SSL) — взаимоисключающие,
# при порте 465 (например, reg.ru) нужно явно выставить EMAIL_USE_TLS=0 EMAIL_USE_SSL=1.
EMAIL_USE_TLS = _bool_env("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = _bool_env("EMAIL_USE_SSL", False)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@autosnab.ru")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
ADMINS = [("АвтоСнаб Admin", ADMIN_EMAIL)] if ADMIN_EMAIL else []
# 2026-08-13: заявки на присоединение к существующей компании (SupplierJoinRequest)
# раньше шли всем активным owner/manager этой компании — по прямой просьбе теперь
# идут только на этот фиксированный адрес, никому больше.
SUPPLIER_JOIN_REQUEST_NOTIFICATION_EMAIL = os.environ.get("SUPPLIER_JOIN_REQUEST_NOTIFICATION_EMAIL", "s@avtosnab.pro")
VENUE_JOIN_REQUEST_NOTIFICATION_EMAIL = os.environ.get("VENUE_JOIN_REQUEST_NOTIFICATION_EMAIL", "s@avtosnab.pro")

SPECTACULAR_SETTINGS = {
    "TITLE": "АвтоСнаб API",
    "DESCRIPTION": (
        "Backend API для Мастер-справочника, справочников товаров компаний, товаров поставщиков и заказов.\n\n"
        "**Авторизация зависит от раздела:** публичный каталог доступен без входа; справочники компаний и "
        "Мастер-справочник требуют JWT пользователя; кабинет поставщика использует JWT поставщика; "
        "служебные интеграции могут использовать `X-Service-Token`."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Preserve URLConf order in Swagger: multi-step flows are declared in the order
    # in which clients should call them instead of being sorted alphabetically by path.
    "SORT_OPERATIONS": False,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]",
    "SECURITY": [{"BearerAuth": []}],
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "CoreUserJWT": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT access token активного пользователя АвтоСнаб.",
            },
            "ServiceApiToken": {
                "type": "http",
                "scheme": "bearer",
                "description": "Служебный SERVICE_API_TOKEN для вызовов бота и backend-интеграций.",
            },
            "ServiceApiTokenHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Service-Token",
                "description": "Альтернативный способ передать SERVICE_API_TOKEN.",
            },
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Service API Token. Пример: `Authorization: Bearer your-token`",
            },
        }
    },
    "TAGS": [
        {"name": "Служебное", "description": "Healthcheck и утилиты"},
        {"name": "Каталог", "description": "Каталог товаров поставщиков для заведений"},
        {"name": "Избранное", "description": "Избранные товары заведения"},
        {"name": "Вишлисты", "description": "Именованные списки товаров пользователя (JWT)"},
        {"name": "Заказы", "description": "Заявки заведений поставщикам"},
        {
            "name": "Справочник товаров компании",
            "description": (
                "Товары конкретной компании/заведения. Внутреннее имя URL — product-matrix. Каждый товар "
                "может быть связан с одним мастер-товаром и одним товаром поставщика; эти связи действуют "
                "только в данной компании."
            ),
        },
        {
            "name": "Импорт справочника товаров компании",
            "description": (
                "Основной сценарий фронтенда: `POST .../import/file/preview` → проверка пользователем → "
                "`POST .../import/apply`. Первая операция разбирает файл без записи в БД, вторая применяет "
                "подтверждённые решения вместе с единицей измерения и примечанием новых товаров."
            ),
        },
        {
            "name": "Импорт справочника компании — дополнительные операции",
            "description": (
                "Необязательные сценарии: preview уже распарсенного JSON и ручная проверка привязок "
                "к мастер-каталогу. Для обычной загрузки файла эти операции не нужны."
            ),
        },
        {
            "name": "Мастер-справочник",
            "description": "Общие товары владельца, объединяющие товары его компаний без глобального поставщика/цены.",
        },
        {
            "name": "Импорт Мастер-справочника",
            "description": "Основной сценарий: `POST .../import/preview` с файлом → `POST .../import/apply`.",
        },
        {
            "name": "Мастер-справочник — дополнительные операции",
            "description": (
                "Совместимые низкоуровневые операции. Для модалки компаний основной сценарий — "
                "venues → venues/batch/preview → venues/batch/apply."
            ),
        },
        {"name": "Товары поставщика", "description": "Справочник товаров и цен конкретного поставщика."},
        {
            "name": "Импорт прайса поставщика",
            "description": (
                "Фронтовый сценарий: загрузить файл → опрашивать статус → открыть preview → при необходимости "
                "исправить строки → publish. Normalize/AI/column-mapping — дополнительные операции обработки."
            ),
        },
        {
            "name": "Импорт прайса поставщика — дополнительные операции",
            "description": (
                "Ручной запуск нормализации/AI и техническая диагностика. Обычный фронтовый сценарий следует "
                "next_action и available_actions из ответа загрузки."
            ),
        },
        {"name": "Бот", "description": "Telegram-бот: привязка, регистрация, состояния"},
        {"name": "Google Sheets", "description": "Экспорт и логи синхронизации с Google Sheets"},
        {"name": "Авторизация", "description": "Вход/регистрация ЛК: поставщик, сотрудник заведения/закупщик"},
        {
            "name": "Кабинет поставщика",
            "description": "Личный кабинет поставщика: своя компания, приглашения, привязка бота, передача владения",
        },
    ],
    "ENUM_NAME_OVERRIDES": {
        "SourceChannelEnum": "suppliers.models.SourceChannel",
        "EntityStatusEnum": "core.models.EntityStatus",
        "UploadStatusEnum": "suppliers.models.UploadStatus",
        "UploadItemStatusEnum": "suppliers.models.UploadItemStatus",
        "PriceStatusEnum": "suppliers.models.PriceStatus",
        "OwnershipRequestStatusEnum": "suppliers.models.OwnershipRequestStatus",
        "AiModeEnum": ["full", "ambiguous", "off"],
        "AiCleanupScopeEnum": ["all", "ambiguous", "needs_review"],
    },
}

# Enable after the frontend handles open sessions returned by partial commit.
CATALOG_IMPORT_PARTIAL_COMMIT_ENABLED = _bool_env("CATALOG_IMPORT_PARTIAL_COMMIT_ENABLED", False)

CATALOG_IMPORT_ASYNC_EXTRACTION_ENABLED = _bool_env("CATALOG_IMPORT_ASYNC_EXTRACTION_ENABLED", False)

IMPORT_RECOVERY_ENABLED = _bool_env("IMPORT_RECOVERY_ENABLED", False)

VENUE_IMPORT_PREPARED_MATCHING_ENABLED = _bool_env("VENUE_IMPORT_PREPARED_MATCHING_ENABLED", False)

# Canonical parsing rollout: each workflow can be activated independently.
CANONICAL_SUPPLIER_PARSER_ENABLED = os.environ.get("CANONICAL_SUPPLIER_PARSER_ENABLED", "false").lower() == "true"
CANONICAL_MASTER_PARSER_ENABLED = os.environ.get("CANONICAL_MASTER_PARSER_ENABLED", "false").lower() == "true"
CANONICAL_VENUE_PARSER_ENABLED = os.environ.get("CANONICAL_VENUE_PARSER_ENABLED", "false").lower() == "true"
CANONICAL_LEGACY_PARSER_ENABLED = os.environ.get("CANONICAL_LEGACY_PARSER_ENABLED", "false").lower() == "true"
CANONICAL_IMPORT_LLM_ENABLED = os.environ.get("CANONICAL_IMPORT_LLM_ENABLED", "false").lower() == "true"
# D12: a row with a price but no unit stays without a unit and goes to a manual choice (import spec, section 52).
# True brings back the earlier rule "no unit, a price -> шт (defaulted)".
CANONICAL_DEFAULT_PIECE_UNIT = _bool_env("CANONICAL_DEFAULT_PIECE_UNIT", False)
# Advisory country hints of the canonical parser: a country from a section heading ("section") and from a barcode's
# GS1 prefix ("gs1_prefix"). Keep off until the per-source precision gate of the country-of-origin spec is measured
# (section >= 95%) and the frontend renders field_sources values. A country in the row, a country column and the
# barcode field itself work regardless.
CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED = _bool_env("CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED", False)
# Tabular files parsed inside the HTTP request (async extraction off) can run up to MAX_REQUESTS
# LLM requests, so keep async extraction on when the LLM flag is on.
CANONICAL_IMPORT_LLM_MAX_REQUESTS = _int_env("CANONICAL_IMPORT_LLM_MAX_REQUESTS", 40)
CANONICAL_IMPORT_LLM_BATCH_SIZE = _int_env("CANONICAL_IMPORT_LLM_BATCH_SIZE", 12)
# The batched requests run in parallel, at most this many at a time.
CANONICAL_IMPORT_LLM_MAX_WORKERS = _int_env("CANONICAL_IMPORT_LLM_MAX_WORKERS", 8)

# Preserve existing enum processing and existing required response fields.
SPECTACULAR_SETTINGS["POSTPROCESSING_HOOKS"] = SPECTACULAR_SETTINGS.get(
    "POSTPROCESSING_HOOKS", ["drf_spectacular.hooks.postprocess_schema_enums"]
) + ["file_imports.schema.optional_import_diagnostics"]
