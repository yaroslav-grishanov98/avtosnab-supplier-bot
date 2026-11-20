import os
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().with_name(".env")
load_dotenv(ENV_FILE, override=True)

# Пытаемся взять из .env, если нет — из переменных окружения (GitLab CI)
SERVICE_TOKEN = os.getenv("SERVICE_API_TOKEN") or os.environ.get("SERVICE_API_TOKEN", "mock")
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
AI_PROVIDER = (os.getenv("AI_PROVIDER") or ("groq" if GROQ_API_KEY else "openai")).strip().lower()
AI_API_KEY = (
    (GROQ_API_KEY if AI_PROVIDER == "groq" else "")
    or os.getenv("AI_API_KEY")
    or os.environ.get("AI_NORMALIZATION_API_KEY", "mock")
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
# MAX-бот уведомлений использует тот же backend-сервисный токен и отдельный токен MAX.
MAX_BOT_TOKEN = (
    os.getenv("MAX_BOT_TOKEN") or os.getenv("NOTIFICATION_MAX_BOT_TOKEN") or os.environ.get("MAX_BOT_TOKEN", "")
)

# Legacy handlers still import BOT_TOKEN. Keep the alias until those handlers
# are fully migrated to the shared Messenger abstraction.
BOT_TOKEN = TELEGRAM_BOT_TOKEN

# Дополнительные переменные с дефолтными значениями
BACKEND_URL = os.getenv("BACKEND_BASE_URL") or os.environ.get("BACKEND_BASE_URL", "")
AI_BASE_URL = os.getenv("AI_BASE_URL") or (
    "https://api.groq.com/openai/v1" if AI_PROVIDER == "groq" else "https://api.openai.com/v1"
)
AI_MODEL = os.getenv("AI_MODEL") or (
    os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile") if AI_PROVIDER == "groq" else "gpt-4o-mini"
)
AI_TRANSCRIPTION_MODEL = os.getenv("AI_TRANSCRIPTION_MODEL") or (
    "whisper-large-v3-turbo" if AI_PROVIDER == "groq" else "whisper-1"
)
# Langfuse observability
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
LANGFUSE_CAPTURE_CONTENT = os.getenv("LANGFUSE_CAPTURE_CONTENT", "false").lower() == "true"
LANGFUSE_HASH_SALT = os.getenv("LANGFUSE_HASH_SALT", "autosnab-bot")

# Заголовки авторизации
AUTH_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}

# Полный mock подменяет все backend-запросы. Demo-auth подменяет только
# узнавание Telegram-пользователя, оставляя товары и загрузки настоящими.
MOCK_MODE = SERVICE_TOKEN == "mock"
DEMO_AUTH_MODE = os.getenv("DEMO_AUTH_MODE", "false").lower() == "true"
DEMO_SUPPLIER_ID = os.getenv("DEMO_SUPPLIER_ID", "").strip()
DEMO_SUPPLIER_NAME = os.getenv("DEMO_SUPPLIER_NAME", "Тестовая компания").strip()

# Привязка к конкретному пользователю в мок-режиме
MOCK_USER_BOUND = os.getenv("MOCK_USER_BOUND", "true").lower() == "true"
