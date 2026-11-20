"""Django settings for the supplier-import evaluation harness.

Everything environment-specific comes from environment variables (defaults are in brackets):
POSTGRES_HOST [127.0.0.1], POSTGRES_PORT [5432], POSTGRES_USER [postgres], POSTGRES_PASSWORD [postgres],
POSTGRES_DB [autosnab], PGSSLMODE [disable], EVAL_MEDIA_ROOT [<system temp>/autosnab-eval-media],
EVAL_LLM_MODEL [gpt-4o-mini], EVAL_OPENAI_KEY [empty; the LLM key is read ONLY from this variable].
"""

import os
import tempfile

from django.core.exceptions import ImproperlyConfigured

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
from config.settings import *  # noqa: F401,F403,E402

if os.environ.get("POSTGRES_HOST", "127.0.0.1") not in ("localhost", "127.0.0.1", "::1") and (
    os.environ.get("EVAL_ALLOW_REMOTE_DB") != "1"
):
    raise ImproperlyConfigured(
        "Харнесс создаёт в БД поставщиков и сессии импорта, поэтому по умолчанию работает только с локальной БД "
        "(POSTGRES_HOST: localhost, 127.0.0.1 или ::1). Для удалённой БД явно задайте EVAL_ALLOW_REMOTE_DB=1."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "autosnab"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "OPTIONS": {"sslmode": os.environ.get("PGSSLMODE", "disable")},
    }
}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ALLOWED_HOSTS = ["*"]
SECURE_SSL_REDIRECT = False

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MEDIA_ROOT = os.environ.get("EVAL_MEDIA_ROOT") or os.path.join(tempfile.gettempdir(), "autosnab-eval-media")

# All import flags on.
IMPORT_FILE_STORAGE_ENABLED = True
SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED = True
SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC = True
CANONICAL_SUPPLIER_PARSER_ENABLED = True
CANONICAL_IMPORT_LLM_ENABLED = True
LLM_CONNECTOR_ENABLED = True

# LLM: the key comes only from the process environment, never from a file.
LLM_PRIMARY_PROVIDER = "openai"
LLM_PRIMARY_MODEL = os.environ.get("EVAL_LLM_MODEL", "gpt-4o-mini")
LLM_PRIMARY_API_KEY = os.environ.get("EVAL_OPENAI_KEY", "")
LLM_PRIMARY_BASE_URL = ""
LLM_FALLBACK_PROVIDER = ""
LLM_FALLBACK_BASE_URL = ""
LLM_FALLBACK_API_KEY = ""
LLM_FALLBACK_MODEL = ""
OPENAI_API_KEY = LLM_PRIMARY_API_KEY
AI_NORMALIZATION_API_KEY = LLM_PRIMARY_API_KEY
