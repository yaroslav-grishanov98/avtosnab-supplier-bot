import unittest
from unittest.mock import patch

from api.backend import delete_supplier_product, get_supplier_products, patch_product_price


class _Response:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}
        self.raised = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        self.raised = True

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, response, **kwargs):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.response

    def patch(self, url, **kwargs):
        self.calls.append(("patch", url, kwargs))
        return self.response

    def delete(self, url, **kwargs):
        self.calls.append(("delete", url, kwargs))
        return self.response


class ProductApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_passes_company_and_query(self):
        response = _Response(200, {"results": [{"id": "p1"}]})
        session = _Session(response)
        with patch("api.backend.aiohttp.ClientSession", return_value=session):
            result = await get_supplier_products("supplier-1", query="сыр", limit=10)
        self.assertEqual(result["results"][0]["id"], "p1")
        self.assertEqual(session.calls[0][2]["params"]["supplier_id"], "supplier-1")
        self.assertEqual(session.calls[0][2]["params"]["q"], "сыр")
        self.assertTrue(response.raised)

    async def test_price_update_uses_shared_product_endpoint(self):
        response = _Response(200, {"id": "p1"})
        session = _Session(response)
        with patch("api.backend.aiohttp.ClientSession", return_value=session):
            await patch_product_price("p1", "590.00")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "patch")
        self.assertTrue(url.endswith("/suppliers/products/p1"))
        self.assertEqual(kwargs["json"]["price"], "590.00")

    async def test_delete_accepts_empty_204_response(self):
        response = _Response(204)
        session = _Session(response)
        with patch("api.backend.aiohttp.ClientSession", return_value=session):
            result = await delete_supplier_product("p1")
        self.assertEqual(result, {"deleted": True})
        self.assertEqual(session.calls[0][0], "delete")
        self.assertTrue(response.raised)


if __name__ == "__main__":
    unittest.main()
