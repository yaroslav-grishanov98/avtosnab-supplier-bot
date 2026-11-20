"""Background monitoring for asynchronous supplier uploads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from api.backend import get_upload_stats
from models import UserContext
from utils import langfuse_track

TERMINAL_STATUSES = {"applied", "completed", "completed_with_errors", "needs_review", "failed", "rejected"}
_tasks: dict[str, asyncio.Task] = {}
_notified: set[str] = set()


def start_upload_monitor(
    context: UserContext,
    upload_id: str,
    on_complete: Callable[[UserContext, dict], Awaitable[None]],
    on_failed: Callable[[UserContext, dict], Awaitable[None]],
) -> asyncio.Task:
    """Start one monitor per upload and return immediately."""
    existing = _tasks.get(upload_id)
    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        _monitor_upload(context, upload_id, on_complete, on_failed),
        name=f"upload-monitor:{upload_id}",
    )
    _tasks[upload_id] = task
    task.add_done_callback(lambda _: _tasks.pop(upload_id, None))
    return task


async def _monitor_upload(
    context: UserContext,
    upload_id: str,
    on_complete: Callable[[UserContext, dict], Awaitable[None]],
    on_failed: Callable[[UserContext, dict], Awaitable[None]],
) -> None:
    """Poll for up to 24 hours and send exactly one terminal notification."""
    delay = 5
    deadline = asyncio.get_running_loop().time() + 24 * 60 * 60
    errors = 0

    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(delay)
        try:
            stats = await get_upload_stats(upload_id)
            errors = 0
        except Exception as exc:
            errors += 1
            delay = min(60, 5 * (2 ** min(errors, 3)))
            await langfuse_track(
                "upload_monitor_retry",
                {"upload_id": upload_id, "attempt": errors, "error": str(exc)},
                level="WARNING",
            )
            continue

        status = str(stats.get("status") or "processing").lower()
        if status not in TERMINAL_STATUSES:
            delay = 15
            continue

        if upload_id in _notified:
            return
        _notified.add(upload_id)

        if status in {"failed", "rejected"}:
            await on_failed(context, stats)
        else:
            await on_complete(context, stats)
        await langfuse_track(
            "upload_terminal_notification_sent",
            {"upload_id": upload_id, "status": status, "chat_id": context.chat_id},
        )
        return

    await langfuse_track(
        "upload_monitor_expired",
        {"upload_id": upload_id, "chat_id": context.chat_id},
        level="WARNING",
    )


def is_upload_monitored(upload_id: str) -> bool:
    task = _tasks.get(upload_id)
    return bool(task and not task.done())
