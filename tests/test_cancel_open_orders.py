import unittest

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta
from trading_ui.services.orders import validate_cancel_open_orders_inputs
from trading_ui.templates import render_control_panel_page


def metas() -> dict[str, AccountMeta]:
    return {
        "A": AccountMeta(
            id="A",
            num_id=2,
            broker="Tiger",
            trading_medium="API",
            broker_id="TG-A",
            machine_alias="Linode-A",
            ip_address="10.0.0.2",
        ),
        "B": AccountMeta(
            id="B",
            num_id=1,
            broker="Tiger",
            trading_medium="API",
            broker_id="TG-B",
            machine_alias="Linode-B",
            ip_address="10.0.0.1",
        ),
    }


class FakeProducer:
    def __init__(self) -> None:
        self.published: list[tuple[dict, str]] = []

    def publish_order(self, command: dict, key: str) -> None:
        self.published.append((command, key))


class CancelOpenOrdersValidationTests(unittest.TestCase):
    def test_builds_symbol_scoped_cancel_command(self):
        cmd, err = validate_cancel_open_orders_inputs(
            account_id="A",
            symbol="aapl",
            account_metas=metas(),
            symbols=["AAPL", "MSFT"],
            invalid_account="invalid account",
            invalid_symbol="invalid symbol",
        )

        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["type"], "CANCEL_OPEN_ORDERS")
        self.assertEqual(cmd["account_id"], "A")
        self.assertEqual(cmd["symbol"], "AAPL")
        self.assertEqual(cmd["broker"], "Tiger")
        self.assertEqual(cmd["broker_id"], "TG-A")

    def test_builds_all_symbol_cancel_command_when_symbol_is_blank(self):
        cmd, err = validate_cancel_open_orders_inputs(
            account_id="A",
            symbol="",
            account_metas=metas(),
            symbols=["AAPL", "MSFT"],
            invalid_account="invalid account",
            invalid_symbol="invalid symbol",
        )

        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["type"], "CANCEL_OPEN_ORDERS")
        self.assertNotIn("symbol", cmd)


class CancelOpenOrdersUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "ACCOUNT_METAS": webserver.ACCOUNT_METAS,
            "SYMBOLS": webserver.SYMBOLS,
            "COMMANDS_PRODUCER": webserver.COMMANDS_PRODUCER,
            "SESSIONS": webserver.SESSIONS,
        }
        webserver.ACCOUNT_METAS = metas()
        webserver.SYMBOLS = ["AAPL", "MSFT"]
        webserver.COMMANDS_PRODUCER = FakeProducer()
        webserver.SESSIONS = {"token": "trader"}

    def tearDown(self) -> None:
        webserver.ACCOUNT_METAS = self._saved["ACCOUNT_METAS"]
        webserver.SYMBOLS = self._saved["SYMBOLS"]
        webserver.COMMANDS_PRODUCER = self._saved["COMMANDS_PRODUCER"]
        webserver.SESSIONS = self._saved["SESSIONS"]

    def test_control_panel_renders_cancel_servers_as_multi_select_choices(self):
        html = render_control_panel_page("en", metas(), ["AAPL", "MSFT"])

        self.assertIn("Servers (multi-select)", html)
        self.assertIn("type='checkbox' id='cancel-acct-all'", html)
        self.assertIn("type='checkbox' id='cancel-acct-1'", html)
        self.assertNotIn("type='radio' id='cancel-acct-1'", html)
        self.assertIn("Linode-B", html)
        self.assertIn("Linode-A", html)

    def test_submit_publishes_symbol_scoped_cancel_commands_for_multiple_servers(self):
        client = TestClient(webserver.app)
        client.cookies.set("auth_token", "token")

        response = client.post(
            "/submit-cancel-open-orders",
            data={"account_ids": ["A", "B"], "symbol": "aapl"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        producer = webserver.COMMANDS_PRODUCER
        assert isinstance(producer, FakeProducer)
        self.assertEqual(len(producer.published), 2)
        self.assertEqual([key for _, key in producer.published], ["A", "B"])
        self.assertEqual([cmd["account_id"] for cmd, _ in producer.published], ["A", "B"])
        self.assertTrue(all(cmd["type"] == "CANCEL_OPEN_ORDERS" for cmd, _ in producer.published))
        self.assertTrue(all(cmd["symbol"] == "AAPL" for cmd, _ in producer.published))

    def test_submit_expands_all_servers_and_all_symbols(self):
        client = TestClient(webserver.app)
        client.cookies.set("auth_token", "token")

        response = client.post(
            "/submit-cancel-open-orders",
            data={"account_ids": ["__ALL__"], "symbol": ""},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        producer = webserver.COMMANDS_PRODUCER
        assert isinstance(producer, FakeProducer)
        self.assertEqual(len(producer.published), 2)
        self.assertEqual([cmd["account_id"] for cmd, _ in producer.published], ["B", "A"])
        self.assertTrue(all("symbol" not in cmd for cmd, _ in producer.published))


if __name__ == "__main__":
    unittest.main()
