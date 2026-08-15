import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta, AccountSnapshot, Position


class AccountSortPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "ACCOUNT_METAS": webserver.ACCOUNT_METAS,
            "SYMBOLS": webserver.SYMBOLS,
            "APP_CONFIG": webserver.APP_CONFIG,
            "SESSIONS": webserver.SESSIONS,
        }
        webserver.ACCOUNT_METAS = {
            "LOW": AccountMeta(id="LOW", num_id=1, broker="Tiger"),
            "HIGH": AccountMeta(id="HIGH", num_id=2, broker="Tiger"),
        }
        webserver.SYMBOLS = ["TSLA", "NVDA"]
        webserver.APP_CONFIG = {
            "kafka": {"account_details_topic": "account-details"},
            "server": {"ssl_enabled": False},
        }
        webserver.SESSIONS = {"token": "trader"}
        accounts = {
            "LOW": AccountSnapshot(
                account_id="LOW",
                cash=100,
                cash_by_currency={"USD": 100},
                positions=[Position("TSLA", 5)],
            ),
            "HIGH": AccountSnapshot(
                account_id="HIGH",
                cash=50,
                cash_by_currency={"USD": 50},
                positions=[Position("TSLA", 10)],
            ),
        }
        self._snapshots_patch = patch(
            "trading_ui.webserver.get_served_snapshots",
            return_value=(accounts, "unit-test"),
        )
        self._snapshots_patch.start()

    def tearDown(self) -> None:
        self._snapshots_patch.stop()
        webserver.ACCOUNT_METAS = self._saved["ACCOUNT_METAS"]
        webserver.SYMBOLS = self._saved["SYMBOLS"]
        webserver.APP_CONFIG = self._saved["APP_CONFIG"]
        webserver.SESSIONS = self._saved["SESSIONS"]

    def test_sort_order_persists_when_refresh_drops_query_parameters(self):
        client = TestClient(webserver.app)
        client.cookies.set("auth_token", "token")

        sorted_response = client.get("/account-details?sort_by=security&security=TSLA")
        refreshed_response = client.get("/account-details")

        self.assertEqual(sorted_response.status_code, 200)
        self.assertEqual(refreshed_response.status_code, 200)
        self.assertLess(sorted_response.text.index("#2 - HIGH"), sorted_response.text.index("#1 - LOW"))
        self.assertLess(refreshed_response.text.index("#2 - HIGH"), refreshed_response.text.index("#1 - LOW"))
        self.assertIn("<option value='security' selected>", refreshed_response.text)
        self.assertIn("<option value='TSLA' selected>", refreshed_response.text)
        self.assertEqual(client.cookies.get(webserver.ACCOUNT_SORT_COOKIE), "security")
        self.assertEqual(client.cookies.get(webserver.ACCOUNT_SECURITY_COOKIE), "TSLA")


if __name__ == "__main__":
    unittest.main()
