"""
Настройки OCR-модуля.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

# Провайдер OCR: google_drive_ocr или google_cloud_vision
GOOGLE_OCR_PROVIDER = os.getenv("GOOGLE_OCR_PROVIDER", "google_drive_ocr")
GOOGLE_DRIVE_OCR_ENABLED = os.getenv("GOOGLE_DRIVE_OCR_ENABLED", "true").lower() == "true"
GOOGLE_DRIVE_OCR_LANGUAGE = os.getenv("GOOGLE_DRIVE_OCR_LANGUAGE", "ru")
GOOGLE_DRIVE_OCR_DELETE_TEMP_FILES = os.getenv("GOOGLE_DRIVE_OCR_DELETE_TEMP_FILES", "true").lower() == "true"
GOOGLE_DRIVE_OCR_EXPORT_RETRY_ATTEMPTS = int(os.getenv("GOOGLE_DRIVE_OCR_EXPORT_RETRY_ATTEMPTS", "6"))
GOOGLE_DRIVE_OCR_EXPORT_RETRY_DELAY_SECONDS = float(os.getenv("GOOGLE_DRIVE_OCR_EXPORT_RETRY_DELAY_SECONDS", "4.0"))
GOOGLE_DRIVE_OCR_MIN_TEXT_LENGTH = int(os.getenv("GOOGLE_DRIVE_OCR_MIN_TEXT_LENGTH", "20"))

# Google OAuth (для Drive OCR)
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_ACCESS_TOKEN = os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")

# Сервисный аккаунт (для Cloud Vision — будущее)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
