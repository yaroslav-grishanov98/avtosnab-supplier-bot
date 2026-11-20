"""Backend-derived onboarding checklist for suppliers."""

import logging
from dataclasses import dataclass

from api.backend import get_upload_history
from keyboards.onboarding import onboarding_checklist_kb
from models import UserContext

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger
logger = logging.getLogger(__name__)


TERMINAL_UPLOAD_STATUSES = {
    "applied",
    "completed",
    "completed_with_errors",
    "needs_review",
    "processed",
    "confirmed",
}
PROCESSING_UPLOAD_STATUSES = {"pending", "processing", "normalizing", "price_processing"}


@dataclass(frozen=True)
class OnboardingProgress:
    authorized: bool
    company_created: bool
    price_uploaded: bool
    price_processing: bool = False
    status_available: bool = True

    @property
    def complete(self) -> bool:
        return self.authorized and self.company_created and self.price_uploaded

    @property
    def next_step(self) -> str:
        if not self.authorized:
            return "authorize"
        if not self.company_created:
            return "create_company"
        if self.price_processing:
            return "wait_for_price"
        if not self.price_uploaded:
            return "upload_price"
        return "complete"


def progress_from_uploads(*, authorized: bool, company_created: bool, uploads: list[dict]) -> OnboardingProgress:
    statuses = {str(item.get("status") or "").lower() for item in uploads}
    return OnboardingProgress(
        authorized=authorized,
        company_created=company_created,
        price_uploaded=bool(statuses & TERMINAL_UPLOAD_STATUSES),
        price_processing=bool(statuses & PROCESSING_UPLOAD_STATUSES),
    )


def format_checklist(progress: OnboardingProgress) -> str:
    def mark(done: bool) -> str:
        return "✅" if done else "⬜"

    price_mark = "⏳" if progress.price_processing and not progress.price_uploaded else mark(progress.price_uploaded)
    lines = [
        "Ваш старт в АвтоСнаб:",
        "",
        f"{mark(progress.authorized)} Войти в АвтоСнаб",
        f"{mark(progress.company_created)} Создать компанию",
        f"{price_mark} Загрузить первый прайс",
        "",
    ]

    if not progress.status_available:
        lines.append("Не удалось обновить прогресс. Попробуйте ещё раз немного позже.")
    elif progress.complete:
        lines.append("Всё готово — можно работать с товарами и ценами.")
    elif progress.next_step == "authorize":
        lines.append("Сначала войдите или зарегистрируйтесь — это займёт пару минут.")
    elif progress.next_step == "create_company":
        lines.append("Следующий шаг — создайте первую компанию.")
    elif progress.next_step == "wait_for_price":
        lines.append("Прайс обрабатывается. Я сообщу, когда всё будет готово.")
    else:
        lines.append("Остался последний шаг — отправьте прайс файлом, фото или текстом.")
    return "\n".join(lines)


async def load_progress(context: UserContext, *, company_created: bool = True) -> OnboardingProgress:
    if not context.supplier_id:
        return OnboardingProgress(bool(context.binding), False, False)

    try:
        uploads = await get_upload_history(context.supplier_id, limit=10)
        return progress_from_uploads(
            authorized=True,
            company_created=company_created,
            uploads=uploads,
        )
    except Exception:
        logger.exception(
            "Не удалось загрузить прогресс онбординга supplier_id=%s",
            context.supplier_id,
        )
        return OnboardingProgress(
            authorized=True,
            company_created=company_created,
            price_uploaded=False,
            status_available=False,
        )


async def show_checklist(context: UserContext, *, company_created: bool = True) -> OnboardingProgress:
    progress = await load_progress(context, company_created=company_created)
    await msg.send_message(
        context.chat_id,
        format_checklist(progress),
        reply_markup=onboarding_checklist_kb(progress.next_step),
    )
    return progress
