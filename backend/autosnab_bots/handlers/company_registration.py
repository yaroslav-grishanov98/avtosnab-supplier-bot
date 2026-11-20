import re

from api.backend import create_supplier_company, lookup_inn
from api.company_accounts import error_text
from fsm import StateManager, get_data, set_state
from keyboards.company_registration import company_confirmation_kb, inn_retry_kb
from keyboards.onboarding import after_company_added_kb
from models import UserContext, UserState

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


def normalize_inn(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def is_valid_inn(value: str) -> bool:
    inn = normalize_inn(value)
    if len(inn) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        return sum(int(digit) * weight for digit, weight in zip(inn[:9], weights)) % 11 % 10 == int(inn[9])
    if len(inn) == 12:
        weights_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        weights_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        digit_11 = sum(int(digit) * weight for digit, weight in zip(inn[:10], weights_11)) % 11 % 10
        digit_12 = sum(int(digit) * weight for digit, weight in zip(inn[:11], weights_12)) % 11 % 10
        return digit_11 == int(inn[10]) and digit_12 == int(inn[11])
    return False


async def ask_inn(context: UserContext, *, retry: bool = False) -> None:
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        UserState.AWAITING_INN,
        {"source": "add_company_by_inn"},
    )
    set_state(context.chat_id, UserState.AWAITING_INN)
    text = "Введите ИНН ещё раз." if retry else "Отправьте ИНН компании — 10 или 12 цифр."
    await msg.send_message(context.chat_id, text, reply_markup=inn_retry_kb())


async def handle_inn(context: UserContext) -> None:
    inn = normalize_inn(context.text)
    if not is_valid_inn(inn):
        await msg.send_message(
            context.chat_id,
            "ИНН не прошёл проверку. Проверьте цифры и отправьте корректный ИНН из 10 или 12 цифр.",
            reply_markup=inn_retry_kb(),
        )
        return

    result = await lookup_inn(inn)
    if result.get("error"):
        await msg.send_message(
            context.chat_id,
            "Сервис поиска компаний сейчас не ответил. ИНН сохранён — отправьте его ещё раз немного позже.",
            reply_markup=inn_retry_kb(),
        )
        return
    if not result.get("found"):
        await msg.send_message(
            context.chat_id,
            "Компания с таким ИНН не найдена. Проверьте ИНН и отправьте его ещё раз.",
            reply_markup=inn_retry_kb(),
        )
        return

    candidate = {
        **{
            key: result.get(key)
            for key in (
                "legal_name",
                "short_name",
                "kpp",
                "ogrn",
                "entity_type",
                "management_name",
                "okved",
                "okpo",
                "registration_date",
            )
        },
        "inn": inn,
        "company_name": result.get("company_name") or "Без названия",
        "address": result.get("address") or "",
    }
    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        UserState.COMPANY_CONFIRMATION,
        {"company_candidate": candidate},
    )
    set_state(context.chat_id, UserState.COMPANY_CONFIRMATION)
    await msg.send_message(
        context.chat_id,
        "Проверьте компанию:\n\n"
        f"Название: {candidate['company_name']}\n"
        f"ИНН: {candidate['inn']}\n"
        f"Адрес: {candidate['address']}\n\n"
        "Добавить эту компанию?",
        reply_markup=company_confirmation_kb(),
    )


async def confirm_company(context: UserContext) -> None:
    persisted_payload = (context.bot_user_state or {}).get("payload") or {}
    candidate = persisted_payload.get("company_candidate") or get_data(context.chat_id).get("company_candidate")
    if not candidate:
        await msg.send_message(context.chat_id, "Данные компании устарели. Отправьте ИНН ещё раз.")
        await ask_inn(context, retry=True)
        return

    result = await create_supplier_company(candidate, context)
    if not result.get("success"):
        await msg.send_message(
            context.chat_id,
            error_text((result.get("error") or {}).get("code")),
            reply_markup=company_confirmation_kb(),
        )
        return

    company = result.get("company") or {}
    company_id = str(company.get("id") or "")
    existing = list(get_data(context.chat_id).get("companies") or [])
    if company_id and not any(str(item.get("id")) == company_id for item in existing):
        existing.append(company)
    payload = {"companies": existing}
    if company_id:
        payload["selected_company_id"] = company_id
        context.supplier_id = company_id
        context.selected_company_id = company_id

    await StateManager.set(
        "telegram",
        context.telegram_user_id,
        context.chat_id,
        UserState.MAIN_MENU,
        payload,
    )
    set_state(context.chat_id, UserState.MAIN_MENU)
    await msg.send_message(
        context.chat_id,
        f"✅ Компания «{candidate['company_name']}» добавлена.",
        reply_markup=after_company_added_kb(),
    )


async def cancel_company(context: UserContext) -> None:
    await ask_inn(context, retry=True)
