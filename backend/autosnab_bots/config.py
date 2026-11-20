import os
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().with_name(".env")
load_dotenv(ENV_FILE, override=True)

# Пытаемся взять из .env, если нет — из переменных окружения (GitLab CI)
SERVICE_TOKEN = (os.getenv("SERVICE_API_TOKEN") or "").strip()
AI_API_KEY = (os.getenv("AI_API_KEY") or os.getenv("AI_NORMALIZATION_API_KEY") or "").strip()
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
# Ссылка на кабинет, который открывается из мастера подключения уведомлений.
NOTIFICATION_CABINET_URL = (
    os.getenv("NOTIFICATION_CABINET_URL")
    or os.getenv("SUPPLIER_ORDERS_URL")
    or os.environ.get("NOTIFICATION_CABINET_URL", "https://avtosnab.testant.online")
)
FRONTEND_BASE_URL = (os.getenv("FRONTEND_BASE_URL") or "https://avtosnab.testant.online").rstrip("/")
SUPPLIER_LOGIN_URL = f"{FRONTEND_BASE_URL}/supplier/login"
SUPPLIER_COMPANIES_URL = f"{FRONTEND_BASE_URL}/supplier/cabinet/companies"
AI_BASE_URL = os.getenv("AI_BASE_URL") or os.environ.get("AI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL") or os.environ.get("AI_MODEL", "gpt-4o-mini")
# Langfuse observability
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
LANGFUSE_CAPTURE_CONTENT = os.getenv("LANGFUSE_CAPTURE_CONTENT", "false").lower() == "true"
LANGFUSE_HASH_SALT = os.getenv("LANGFUSE_HASH_SALT", "autosnab-bot")

# Заголовки авторизации
AUTH_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def validate_runtime_config() -> None:
    missing = []
    if not BACKEND_URL:
        missing.append("BACKEND_BASE_URL")
    if not SERVICE_TOKEN:
        missing.append("SERVICE_API_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if missing:
        raise RuntimeError("Не заданы обязательные настройки бота: " + ", ".join(missing))
