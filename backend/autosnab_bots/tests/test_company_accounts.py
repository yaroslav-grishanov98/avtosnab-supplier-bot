import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import fsm
from handlers import companies
from models import UserContext
from state_backend import patch_state_backend

from api import backend
from api import company_accounts as api


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, response):
        self.request = Mock(return_value=response)
        self.get = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class CompanyAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = UserContext(chat_id="chat", telegram_user_id="user", supplier_id="old")
        fsm._user_data.clear()
        fsm._user_states.clear()

    def session(self, data, status=200):
        session = Session(Response(data, status))
        self.enterContext(patch("aiohttp.ClientSession", return_value=session))
        return session

    async def test_lists_companies_using_both_identity_fields(self):
        session = self.session({"companies": [{"id": "own"}], "selected_supplier_id": "own"})
        self.assertEqual(await backend.get_supplier_companies(self.context), [{"id": "own"}])
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/bot-user-bindings/companies"))
        self.assertEqual(kwargs["params"], {"platform": "telegram", "chat_id": "chat", "telegram_user_id": "user"})

    async def test_unlinked_error_is_not_an_empty_company_list(self):
        self.session({"code": "BOT_ACCOUNT_NOT_LINKED"}, 409)
        with self.assertRaises(api.CompanyAPIError) as caught:
            await backend.get_supplier_companies(self.context)
        self.assertEqual(caught.exception.code, "BOT_ACCOUNT_NOT_LINKED")

    async def test_create_uses_account_endpoint_and_preserves_registry_fields(self):
        session = self.session({"company": {"id": "new"}}, 201)
        result = await backend.create_supplier_company(
            {
                "inn": "7707083893",
                "company_name": "Short",
                "legal_name": "Full name",
                "address": "Legal address",
                "kpp": "123",
                "management_name": "Director",
                "registration_date": "2000-01-01",
            },
            self.context,
        )
        self.assertTrue(result["success"])
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/bot-user-bindings/companies"))
        body = kwargs["json"]
        self.assertEqual(body["telegram_user_id"], "user")
        self.assertNotIn("owner_supplier_id", body)
        self.assertEqual(body["company"]["legal_name"], "Full name")
        self.assertEqual(body["company"]["legal_address"], "Legal address")
        self.assertEqual(body["company"]["general_director"], "Director")
        self.assertEqual(body["company"]["registration_date"], "2000-01-01")

    async def test_lookup_maps_actual_backend_fields(self):
        self.session({"legal_name": "Full name", "short_name": "Short", "address": "Address", "ogrn": "123"})
        result = await backend.lookup_inn("7707083893")
        self.assertEqual(result["company_name"], "Short")
        self.assertEqual(result["legal_name"], "Full name")
        self.assertEqual(result["ogrn"], "123")

    async def test_invalid_json_and_timeout_are_reported(self):
        for error in (ValueError("html"), asyncio.TimeoutError()):
            with self.subTest(error=error):
                self.session(error)
                with self.assertRaises(api.CompanyAPIError):
                    await api.list_companies(self.context)

    async def test_denied_switch_leaves_local_selection_unchanged(self):
        self.session({"code": "BOT_COMPANY_NOT_AVAILABLE"}, 404)
        messenger = SimpleNamespace(send_message=AsyncMock())
        fsm.set_data("chat", "selected_company_id", "old")
        with patch.object(companies, "msg", messenger), patch_state_backend():
            self.assertFalse(await companies.activate_company(self.context, "foreign"))
        self.assertEqual(self.context.supplier_id, "old")
        self.assertEqual(fsm.get_data("chat")["selected_company_id"], "old")
        self.assertIn("недоступна", messenger.send_message.await_args.args[1])

    async def test_successful_switch_updates_backend_then_local_selection(self):
        session = self.session({"binding": {"supplier_id": "new"}, "company": {"id": "new", "display_name": "New"}})
        with patch_state_backend():
            self.assertTrue(await companies.activate_company(self.context, "new"))
        self.assertEqual(session.request.call_args.args[0], "PATCH")
        self.assertEqual(session.request.call_args.kwargs["json"]["supplier_id"], "new")
        self.assertEqual(self.context.supplier_id, "new")
        self.assertEqual(fsm.get_data("chat")["selected_company_id"], "new")

    async def test_company_list_error_is_shown_instead_of_fallback_supplier(self):
        self.session({"code": "BOT_ACCOUNT_NOT_LINKED"}, 409)
        messenger = SimpleNamespace(send_message=AsyncMock())
        with patch.object(companies, "msg", messenger):
            await companies.handle_my_companies(self.context)
        self.assertIn("Подключите бота заново", messenger.send_message.await_args.args[1])

    async def test_cannot_open_foreign_company_card(self):
        self.session({"companies": [{"id": "own"}]})
        messenger = SimpleNamespace(send_message=AsyncMock())
        with patch.object(companies, "msg", messenger):
            await companies.handle_company_detail(self.context, "foreign")
        self.assertIn("не найдена", messenger.send_message.await_args.args[1])
