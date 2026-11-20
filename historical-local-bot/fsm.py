"""Local onboarding state plus the backend-backed legacy state manager."""

import json
import logging

import aiohttp

from config import AUTH_HEADERS, BACKEND_URL, DEMO_AUTH_MODE, MOCK_MODE
from models import UserState

logger = logging.getLogger(__name__)

_user_states: dict[str, str] = {}
_user_data: dict[str, dict] = {}


def get_state(chat_id: str) -> str:
    return _user_states.get(chat_id, UserState.START)


def set_state(chat_id: str, state: str) -> None:
    _user_states[chat_id] = state


def get_data(chat_id: str) -> dict:
    return _user_data.get(chat_id, {})


def set_data(chat_id: str, key: str, value) -> None:
    if chat_id not in _user_data:
        _user_data[chat_id] = {}
    _user_data[chat_id][key] = value


def clear_data(chat_id: str) -> None:
    _user_data.pop(chat_id, None)
    _user_states.pop(chat_id, None)

class StateManager:
    """Compatibility layer for handlers that persist state in the backend."""

    @staticmethod
    async def get(platform: str, telegram_user_id: str, chat_id: str) -> dict:
        if MOCK_MODE or DEMO_AUTH_MODE or not BACKEND_URL:
            return {"state": get_state(chat_id), "payload": get_data(chat_id)}

        url = f"{BACKEND_URL}/bot-user-states/current"
        params = {"platform": platform, "telegram_user_id": telegram_user_id, "chat_id": chat_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=AUTH_HEADERS) as resp:
                resp.raise_for_status()
                data = await resp.json()
                payload = data.get("payload", {})
                if isinstance(payload, str):
                    try:
                        data["payload"] = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("Backend вернул некорректный JSON payload состояния")
                        data["payload"] = {}
                return data

    @staticmethod
    async def set(
        platform: str,
        telegram_user_id: str,
        chat_id: str,
        state: str,
        payload: dict | None = None,
    ) -> dict:
        new_payload = dict(payload or {})

        if MOCK_MODE or DEMO_AUTH_MODE or not BACKEND_URL:
            selected_company_id = get_data(chat_id).get("selected_company_id")
            if selected_company_id and "selected_company_id" not in new_payload:
                new_payload["selected_company_id"] = selected_company_id
            set_state(chat_id, state)
            _user_data[chat_id] = new_payload
            return {"state": state, "payload": new_payload}

        # Company choice is profile-like state: preserve it while scenario
        # payload changes from onboarding to upload processing and back.
        try:
            current = await StateManager.get(platform, telegram_user_id, chat_id)
            selected_company_id = (current.get("payload") or {}).get("selected_company_id")
            if selected_company_id and "selected_company_id" not in new_payload:
                new_payload["selected_company_id"] = selected_company_id
        except Exception:
            logger.exception(
                "Не удалось сохранить выбранную компанию при смене состояния chat_id=%s",
                chat_id,
            )

        url = f"{BACKEND_URL}/bot-user-states/current"
        body = {
            "platform": platform,
            "telegram_user_id": telegram_user_id,
            "chat_id": chat_id,
            "state": state,
            "payload": json.dumps(new_payload),
        }
        async with aiohttp.ClientSession() as session:
            async with session.put(url, json=body, headers=AUTH_HEADERS) as resp:
                resp.raise_for_status()
                return await resp.json()
