import unittest
from unittest.mock import patch

import fsm


class StateManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_company_survives_scenario_state_change(self):
        chat_id = "100"
        fsm._user_data[chat_id] = {"selected_company_id": "company-1"}

        with patch.object(fsm, "MOCK_MODE", True):
            result = await fsm.StateManager.set(
                "telegram",
                "100",
                chat_id,
                "price_processing",
                {"upload_id": "upload-1"},
            )

        self.assertEqual(result["payload"]["selected_company_id"], "company-1")
        self.assertEqual(result["payload"]["upload_id"], "upload-1")


if __name__ == "__main__":
    unittest.main()
