"""Единая настройка локального журнала Telegram-бота."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"


def configure_logging(log_dir: str | Path | None = None) -> Path | None:
    """Всегда писать в консоль; недоступный файловый журнал не блокирует запуск."""
    root = logging.getLogger()
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    handlers = [handler for handler in root.handlers if getattr(handler, "_autosnab_handler", False)]
    if not handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console._autosnab_handler = True

        root.addHandler(console)
        logging.captureWarnings(True)
        if os.getenv("BOT_LOG_TO_FILE", "true").lower() in {"1", "true", "yes"}:
            target_dir = Path(log_dir or os.getenv("BOT_LOG_DIR") or DEFAULT_LOG_DIR)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    target_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
            except OSError as exc:
                root.warning("Файловый журнал недоступен (%s); журнал продолжается в консоли", type(exc).__name__)
            else:
                file_handler.setFormatter(formatter)
                file_handler._autosnab_handler = True
                root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)
    for handler in root.handlers:
        if getattr(handler, "_autosnab_handler", False) and isinstance(handler, logging.FileHandler):
            return Path(handler.baseFilename)
    return None
