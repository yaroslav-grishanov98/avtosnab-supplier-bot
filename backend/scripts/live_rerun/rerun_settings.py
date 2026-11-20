# ruff: noqa
"""Live re-run settings (LLM off): local Postgres 5435, Redis db 12/13."""

import os
from pathlib import Path

ROOT = Path("/home/itmedia_pc/projects/auto-snab-import-quality")
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.update(
    DJANGO_DEBUG="1",
    DJANGO_SECRET_KEY="local-live-rerun-only",
    POSTGRES_HOST="127.0.0.1",
    POSTGRES_PORT="5435",
    POSTGRES_DB="autosnab_live_rerun",
    POSTGRES_USER="autosnab",
    POSTGRES_PASSWORD="autosnab_local_test",
    PGSSLMODE="disable",
)
from config.settings import *

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
SECURE_SSL_REDIRECT = False
DATABASES["default"]["OPTIONS"] = {"sslmode": "disable"}
CELERY_BROKER_URL = "redis://127.0.0.1:6379/12"
CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/13"
CELERY_TASK_ALWAYS_EAGER = False
IMPORT_FILE_STORAGE_ENABLED = True
SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED = True
SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC = True
CANONICAL_SUPPLIER_PARSER_ENABLED = True
CANONICAL_IMPORT_LLM_ENABLED = False
LLM_CONNECTOR_ENABLED = False
SUPPLIER_PRODUCT_IMPORT_ENRICHMENT_ENABLED = False
AI_NORMALIZATION_ENABLED = False
CATALOG_QDRANT_AUTO_SYNC_ENABLED = False
NOTIFICATION_DELIVERY_DRY_RUN = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MEDIA_ROOT = str(ROOT / "var/live-rerun/media")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
