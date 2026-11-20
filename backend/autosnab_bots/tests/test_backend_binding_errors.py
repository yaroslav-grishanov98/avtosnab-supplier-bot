import asyncio
import unittest
from unittest.mock import patch

import aiohttp
from api.backend import resolve_supplier_binding


class _FakeResponse:
    def __init__(self, status, payload=None, json_error=None):
        self.status = status
        self.payload = payload
        self.json_error = json_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class _FakeSession:
    def __init__(self, response=None, enter_error=None, **kwargs):
        self.response = response
        self.enter_error = enter_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        if self.enter_error:
            return _RaisingContext(self.enter_error)
        return self.response


class _RaisingContext:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ResolveSupplierBindingTests(unittest.IsolatedAsyncioTestCase):
    async def _resolve_with(self, *, status=200, payload=None, json_error=None, enter_error=None):
        response = _FakeResponse(status, payload=payload, json_error=json_error)
        with (
            patch("api.backend.aiohttp.ClientSession", return_value=_FakeSession(response, enter_error=enter_error)),
        ):
            return await resolve_supplier_binding("telegram", "user-1", "chat-1")

    async def test_200_preserves_missing_binding(self):
        self.assertEqual(await self._resolve_with(payload={"exists": False}), {"exists": False})

    async def test_200_preserves_existing_binding(self):
        result = await self._resolve_with(payload={"exists": True, "binding": {"supplier_id": "s1"}})
        self.assertTrue(result["exists"])

    async def test_404_binding_not_found_is_missing_binding(self):
        result = await self._resolve_with(
            status=404,
            payload={
                "exists": False,
                "code": "BOT_USER_BINDING_NOT_FOUND",
                "message": "No BotUserBinding matches the given query.",
            },
        )
        self.assertEqual(result, {"exists": False})

    async def test_unexpected_404_is_backend_error(self):
        result = await self._resolve_with(status=404, payload={"detail": "Not found."})
        self.assertEqual(result["error"], {"code": "backend_request_error", "status": 404})

    async def test_401_and_403_are_auth_errors(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result = await self._resolve_with(status=status)
                self.assertEqual(result["error"], {"code": "backend_auth_error", "status": status})

    async def test_5xx_is_backend_unavailable(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                result = await self._resolve_with(status=status)
                self.assertEqual(result["error"], {"code": "backend_unavailable", "status": status})

    async def test_timeout_is_not_missing_binding(self):
        result = await self._resolve_with(enter_error=asyncio.TimeoutError())
        self.assertEqual(result["error"]["code"], "backend_timeout")

    async def test_connection_error_is_not_missing_binding(self):
        result = await self._resolve_with(enter_error=aiohttp.ClientConnectionError())
        self.assertEqual(result["error"]["code"], "backend_unavailable")

    async def test_invalid_json_is_not_missing_binding(self):
        result = await self._resolve_with(payload=None, json_error=ValueError("bad json"))
        self.assertEqual(result["error"], {"code": "invalid_backend_response", "status": 200})


if __name__ == "__main__":
    unittest.main()
