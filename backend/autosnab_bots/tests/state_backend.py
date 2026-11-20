"""Explicit state-service doubles for handler tests; never loaded by the bot."""

from contextlib import contextmanager
from unittest.mock import patch

import fsm


@contextmanager
def patch_state_backend():
    async def get(platform, telegram_user_id, chat_id):
        return {"state": fsm.get_state(chat_id), "payload": fsm.get_data(chat_id)}

    async def set_state(platform, telegram_user_id, chat_id, state, payload=None):
        values = dict(payload or {})
        selected = fsm.get_data(chat_id).get("selected_company_id")
        if selected and "selected_company_id" not in values:
            values["selected_company_id"] = selected
        fsm.set_state(chat_id, state)
        fsm._user_data[chat_id] = values
        return {"state": state, "payload": values}

    with (
        patch.object(fsm.StateManager, "get", side_effect=get),
        patch.object(fsm.StateManager, "set", side_effect=set_state),
    ):
        yield
