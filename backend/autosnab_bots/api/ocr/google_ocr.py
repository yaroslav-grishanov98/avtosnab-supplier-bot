"""Google Drive OCR for images and PDF price lists."""

import asyncio
import logging
import mimetypes
import os
import tempfile
import time
from pathlib import Path

from utils import langfuse_track

from api.ocr.config import (
    GOOGLE_DRIVE_OCR_DELETE_TEMP_FILES,
    GOOGLE_DRIVE_OCR_ENABLED,
    GOOGLE_DRIVE_OCR_EXPORT_RETRY_ATTEMPTS,
    GOOGLE_DRIVE_OCR_EXPORT_RETRY_DELAY_SECONDS,
    GOOGLE_DRIVE_OCR_LANGUAGE,
    GOOGLE_DRIVE_OCR_MIN_TEXT_LENGTH,
    GOOGLE_OAUTH_ACCESS_TOKEN,
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    GOOGLE_OAUTH_REFRESH_TOKEN,
    GOOGLE_OCR_PROVIDER,
)

SUPPORTED_OCR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff", ".bmp", ".gif"}
logger = logging.getLogger(__name__)


def is_ocr_supported(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in SUPPORTED_OCR_EXTENSIONS


async def recognize_document(file_bytes: bytes, file_name: str) -> dict:
    """Extract text without blocking Telegram's event loop."""
    if not GOOGLE_DRIVE_OCR_ENABLED:
        return {"success": False, "error": "OCR отключён в настройках."}
    if not is_ocr_supported(file_name):
        return {"success": False, "error": f"Формат не поддерживается: {file_name}"}

    await langfuse_track(
        "ocr_started",
        {"file_name": file_name, "file_size": len(file_bytes), "provider": GOOGLE_OCR_PROVIDER},
    )
    paths_to_remove: set[str] = set()
    try:
        suffix = Path(file_name).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            original_path = tmp.name
        paths_to_remove.add(original_path)

        prepared_path = original_path
        if suffix != ".pdf":
            prepared_path = await asyncio.to_thread(_prepare_image, original_path)
            paths_to_remove.add(prepared_path)

        raw_text = await asyncio.to_thread(_google_ocr, prepared_path)
        if not raw_text or len(raw_text.strip()) < GOOGLE_DRIVE_OCR_MIN_TEXT_LENGTH:
            await langfuse_track(
                "ocr_failed",
                {"file_name": file_name, "reason": "text_too_short"},
                level="ERROR",
            )
            return {"success": False, "error": "Не удалось распознать текст. Проверьте качество файла."}

        await langfuse_track(
            "ocr_completed",
            {"file_name": file_name, "text_length": len(raw_text)},
        )
        return {"success": True, "raw_text": raw_text, "error": None}
    except Exception as exc:
        logger.exception("Ошибка OCR file_name=%s", file_name)
        await langfuse_track(
            "ocr_failed",
            {"file_name": file_name, "error": str(exc)},
            level="ERROR",
        )
        return {
            "success": False,
            "error": "Не удалось распознать документ. Попробуйте другой файл или фотографию лучшего качества.",
        }
    finally:
        for path in paths_to_remove:
            try:
                if path and os.path.exists(path):
                    os.unlink(path)
            except OSError:
                logger.warning("Не удалось удалить временный OCR-файл path=%s", path, exc_info=True)


def _prepare_image(file_path: str) -> str:
    try:
        from PIL import Image, ImageOps

        with Image.open(file_path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.autocontrast(img, cutoff=1)
            improved_path = file_path + ".improved.jpg"
            img.save(improved_path, format="JPEG", quality=95, optimize=True)
            return improved_path
    except Exception:
        logger.warning("Не удалось улучшить изображение перед OCR path=%s", file_path, exc_info=True)
        return file_path


def _google_ocr(file_path: str) -> str:
    if GOOGLE_OCR_PROVIDER != "google_drive_ocr":
        raise ValueError(f"Неизвестный GOOGLE_OCR_PROVIDER: {GOOGLE_OCR_PROVIDER}")
    return _google_drive_ocr(file_path)


def _google_drive_ocr(file_path: str) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    required = {
        "GOOGLE_OAUTH_CLIENT_ID": GOOGLE_OAUTH_CLIENT_ID,
        "GOOGLE_OAUTH_CLIENT_SECRET": GOOGLE_OAUTH_CLIENT_SECRET,
        "GOOGLE_OAUTH_REFRESH_TOKEN": GOOGLE_OAUTH_REFRESH_TOKEN,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Не заданы настройки Google OAuth: " + ", ".join(missing))

    credentials = Credentials(
        token=GOOGLE_OAUTH_ACCESS_TOKEN or None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    path = Path(file_path)
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type:
        mime_type = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"

    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
    document = (
        drive_service.files()
        .create(
            body={"name": f"ocr_{path.stem}", "mimeType": "application/vnd.google-apps.document"},
            media_body=media,
            fields="id",
            ocrLanguage=GOOGLE_DRIVE_OCR_LANGUAGE,
            supportsAllDrives=True,
        )
        .execute()
    )

    document_id = document["id"]
    try:
        raw_text = ""
        for _ in range(GOOGLE_DRIVE_OCR_EXPORT_RETRY_ATTEMPTS):
            time.sleep(GOOGLE_DRIVE_OCR_EXPORT_RETRY_DELAY_SECONDS)
            exported = (
                drive_service.files()
                .export(
                    fileId=document_id,
                    mimeType="text/plain",
                )
                .execute()
            )
            raw_text = (
                exported.decode("utf-8", errors="replace") if isinstance(exported, bytes) else str(exported)
            ).lstrip("﻿")
            if len(raw_text.strip()) >= GOOGLE_DRIVE_OCR_MIN_TEXT_LENGTH:
                break
        return raw_text
    finally:
        if GOOGLE_DRIVE_OCR_DELETE_TEMP_FILES:
            try:
                drive_service.files().delete(
                    fileId=document_id,
                    supportsAllDrives=True,
                ).execute()
            except Exception:
                logger.warning(
                    "Не удалось удалить временный Google Drive OCR-документ id=%s",
                    document_id,
                    exc_info=True,
                )
