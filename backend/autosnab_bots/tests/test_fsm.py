import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import fsm


class StateManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_company_survives_scenario_state_change(self):
        response = Mock()
        response.raise_for_status = Mock()
        response.json = AsyncMock(return_value={"state": "price_processing"})
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = Mock()
        session.put = Mock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.object(fsm, "BACKEND_URL", "http://backend.invalid/api/v1"),
            patch.object(
                fsm.StateManager, "get", AsyncMock(return_value={"payload": {"selected_company_id": "company-1"}})
            ),
            patch.object(fsm.aiohttp, "ClientSession", return_value=session),
        ):
            await fsm.StateManager.set(
                "telegram",
                "100",
                "100",
                "price_processing",
                {"upload_id": "upload-1"},
            )

        body = session.put.call_args.kwargs["json"]
        self.assertEqual(json.loads(body["payload"]), {"selected_company_id": "company-1", "upload_id": "upload-1"})
        self.assertEqual(body["chat_id"], "100")
        response.raise_for_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
