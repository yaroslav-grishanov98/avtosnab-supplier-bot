"""Durable notification dialogs, scoped by bot, chat and responding user."""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import date
from decimal import Decimal
from functools import wraps
from pathlib import Path

PENDING_KEY = "pending_supplier_notification_action"
QUEUE_KEY = "delivery_review_queue"
STORE_KEY = "notification_state_store"
LOCKS_KEY = "notification_state_locks"
USER_LOCKS_KEY = "notification_state_user_locks"
logger = logging.getLogger(__name__)


def _encode(value):
    if isinstance(value, date):
        return {"__bot_date__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__bot_decimal__": str(value)}
    raise TypeError(f"Unsupported dialog value: {type(value).__name__}")


def _decode(value):
    if set(value) == {"__bot_date__"}:
        return date.fromisoformat(value["__bot_date__"])
    if set(value) == {"__bot_decimal__"}:
        return Decimal(value["__bot_decimal__"])
    return value


class NotificationStateStore:
    def __init__(self, path, namespace, *, ttl_seconds=7 * 86400):
        self.path = Path(path)
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS notification_dialogs ("
                "bot TEXT NOT NULL, chat TEXT NOT NULL, user TEXT NOT NULL, kind TEXT NOT NULL, "
                "payload TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY (bot, chat, user, kind))"
            )
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous = FULL")
        return db

    def load(self, chat, user):
        values = {}
        with closing(self._connect()) as db:
            db.execute("DELETE FROM notification_dialogs WHERE expires <= ?", (time.time(),))
            rows = db.execute(
                "SELECT kind, payload FROM notification_dialogs WHERE bot=? AND chat=? AND user IN (?, '')",
                (self.namespace, str(chat), str(user)),
            ).fetchall()
            db.commit()
        for kind, payload in rows:
            try:
                data = json.loads(payload, object_hook=_decode)
                if isinstance(data, dict):
                    values[kind] = data
            except (ValueError, TypeError, ArithmeticError):
                logger.warning("Stored notification dialog is invalid; restart the dialog from its message")
        return values

    def save(self, chat, user, *, pending=None, queue=None):
        values = ((PENDING_KEY, str(user), pending), (QUEUE_KEY, "", queue))
        # Serialize before opening a transaction: unsupported values never erase a valid snapshot.
        encoded = [
            (kind, owner, json.dumps(data, default=_encode, allow_nan=False) if data else None)
            for kind, owner, data in values
        ]
        with closing(self._connect()) as db, db:
            for kind, owner, payload in encoded:
                key = (self.namespace, str(chat), owner, kind)
                if payload is None:
                    db.execute("DELETE FROM notification_dialogs WHERE bot=? AND chat=? AND user=? AND kind=?", key)
                else:
                    db.execute(
                        "INSERT INTO notification_dialogs VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (bot,chat,user,kind) DO UPDATE SET payload=excluded.payload, expires=excluded.expires",
                        (*key, payload, time.time() + self.ttl_seconds),
                    )


def configure_notification_state(application, token):
    default_path = Path(tempfile.gettempdir()) / "autosnab-bot" / "notification-state.sqlite3"
    path = os.environ.get("BOT_STATE_DB_PATH") or default_path
    namespace = hashlib.sha256(token.encode()).hexdigest()[:24]
    application.bot_data[STORE_KEY] = NotificationStateStore(path, namespace)
    application.bot_data[LOCKS_KEY] = {}
    application.bot_data[USER_LOCKS_KEY] = {}


def durable_notification_state(callback):
    @wraps(callback)
    async def wrapped(update, context):
        application = getattr(context, "application", None)
        store = getattr(application, "bot_data", {}).get(STORE_KEY)
        message = getattr(update, "effective_message", None)
        user = getattr(update, "effective_user", None)
        if store is None or message is None or user is None:
            return await callback(update, context)
        chat_id, user_id = str(message.chat_id), str(user.id)
        locks = application.bot_data.setdefault(LOCKS_KEY, {})
        user_locks = application.bot_data.setdefault(USER_LOCKS_KEY, {})
        # A checklist is shared by the chat: serialize all its users, not only one responder.
        lock = locks.setdefault(chat_id, asyncio.Lock())
        user_lock = user_locks.setdefault(user_id, asyncio.Lock())
        # PTB user_data is shared by one user across chats. Always lock user, then chat.
        async with user_lock, lock:
            saved = store.load(chat_id, user_id)
            context.user_data.pop(PENDING_KEY, None)
            context.chat_data.pop(QUEUE_KEY, None)
            if PENDING_KEY in saved:
                context.user_data[PENDING_KEY] = saved[PENDING_KEY]
            if QUEUE_KEY in saved:
                context.chat_data[QUEUE_KEY] = saved[QUEUE_KEY]
            try:
                return await callback(update, context)
            finally:
                store.save(
                    chat_id,
                    user_id,
                    pending=context.user_data.get(PENDING_KEY),
                    queue=context.chat_data.get(QUEUE_KEY),
                )

    return wrapped
