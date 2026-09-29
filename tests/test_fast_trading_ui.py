import json
import unittest

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page


def metas() -> dict[str, AccountMeta]:
    return {
        "A": AccountMeta(id="A", num_id=1, broker="Tiger", trading_medium="API"),
        "B": AccountMeta(id="B", num_id=2, broker="Other", trading_medium="WINDOWS"),
    }


class FastTradingUiTests(unittest.TestCase):
    def test_modes_render_single_limit_account_selection_and_required_controls(self):
        html = render_control_panel_page("en", metas(), ["AAPL"])

        self.assertIn("E Buying (Upper Limit)", html)
        self.assertIn("F Selling (Lower Limit)", html)
        self.assertIn("Upper Buy Limit Price", html)
        self.assertIn("Lower Sell Limit Price", html)
        self.assertIn("data-fast-account-id='A'", html)
        self.assertIn("data-fast-account-id='B'", html)
        for prefix, choices in (
            ("fast-book-levels", range(1, 6)),
            ("fast-target-percentage", range(10, 101, 10)),
            ("fast-cycle-seconds", (5, 10, 15, 30, 60)),
        ):
            for value in choices:
                self.assertIn(f'id="{prefix}-{value}"', html)
        self.assertNotIn("fast-aggression", html)

    def test_modal_builds_new_fast_trading_payload_without_groups_or_allocations(self):
        html = render_control_panel_page("en", metas(), ["AAPL"])

        self.assertIn("price_limit: priceLimit", html)
        self.assertIn("account_ids: accountIds", html)
        self.assertIn("book_levels: bookLevels", html)
        self.assertIn("target_percentage: targetPercentage", html)
        self.assertIn("cycle_seconds: cycleSeconds", html)
        self.assertNotIn("aggression_level", html)
        self.assertNotIn("allocation_pct", html)
        self.assertNotIn("payloadGroups", html)
        self.assertIn('id="algo-generic-constraints"', html)


class FastTradingApiPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "ACCOUNT_METAS": webserver.ACCOUNT_METAS,
            "SYMBOLS": webserver.SYMBOLS,
            "COMMANDS_PRODUCER": webserver.COMMANDS_PRODUCER,
            "FAST_TRADING_MANAGER": webserver.FAST_TRADING_MANAGER,
            "SESSIONS": webserver.SESSIONS,
        }
        self.started_commands: list[dict] = []
        self.stop_calls: list[tuple[str, list[str] | None]] = []
        self.published_commands: list[tuple[str, dict]] = []

        class FakeManager:
            def __init__(inner_self, commands: list[dict], stop_calls: list) -> None:
                inner_self.commands = commands
                inner_self.stop_calls = stop_calls

            def start_strategy(inner_self, command: dict) -> str:
                inner_self.commands.append(command)
                return "E:AAPL"

            def stop_matching(inner_self, trading_mode: str, account_ids=None) -> int:
                inner_self.stop_calls.append((trading_mode, account_ids))
                return 1

        class FakeProducer:
            def __init__(inner_self, published_commands: list) -> None:
                inner_self.published_commands = published_commands

            def publish_order(inner_self, command: dict, key: str) -> None:
                inner_self.published_commands.append((key, command))

        webserver.ACCOUNT_METAS = metas()
        webserver.SYMBOLS = ["AAPL"]
        webserver.COMMANDS_PRODUCER = FakeProducer(self.published_commands)
        webserver.FAST_TRADING_MANAGER = FakeManager(self.started_commands, self.stop_calls)
        webserver.SESSIONS = {"token": "trader"}

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(webserver, name, value)

    def test_extracts_prefixed_top_level_fields(self):
        config = webserver._extract_fast_trading_config(
            {
                "fast_trading_price_limit": 125,
                "fast_trading_account_ids": ["A", "B"],
                "fast_trading_book_levels": 2,
                "fast_trading_target_percentage": 60,
                "fast_trading_cycle_seconds": 15,
                "fast_trading_test_mode": True,
            }
        )

        self.assertEqual(
            config,
            {
                "price_limit": 125,
                "account_ids": ["A", "B"],
                "book_levels": 2,
                "target_percentage": 60,
                "cycle_seconds": 15,
                "test_mode": True,
            },
        )

    def test_nested_config_wins_and_can_be_completed_by_unprefixed_aliases(self):
        config = webserver._extract_fast_trading_config(
            {
                "fast_trading_config": {"price_limit": 99, "account_ids": ["A"], "book_levels": 1},
                "price_limit": 777,
                "book_levels": 3,
                "target_percentage": 80,
                "cycle_seconds": 60,
            }
        )

        self.assertEqual(
            config,
            {"price_limit": 99, "account_ids": ["A"], "book_levels": 1,
             "target_percentage": 80, "cycle_seconds": 60},
        )

    def test_api_accepts_top_level_fast_fields_without_generic_constraints(self):
        client = TestClient(webserver.app)
        client.cookies.set("auth_token", "token")

        response = client.post(
            "/api/submit-algo",
            json={
                "trading_mode": "E",
                "symbol": "AAPL",
                "fast_trading_price_limit": 125,
                "fast_trading_account_ids": ["A", "B"],
                "fast_trading_book_levels": 2,
                "fast_trading_target_percentage": 60,
                "fast_trading_cycle_seconds": 15,
            },
        )

        self.assertEqual(response.status_code, 200)
        command = response.json()["command"]
        self.assertEqual(command["fast_trading_price_limit"], 125.0)
        self.assertEqual(command["fast_trading_account_ids"], ["A", "B"])
        self.assertEqual(command["fast_trading_book_levels"], 2)
        self.assertEqual(command["fast_trading_target_percentage"], 60)
        self.assertEqual(command["fast_trading_cycle_seconds"], 15)
        self.assertNotIn("fast_trading_aggression_level", command)
        self.assertNotIn("end_time_et", command)
        self.assertEqual(self.started_commands, [command])

    def test_form_accepts_fast_config_without_generic_fields(self):
        client = TestClient(webserver.app)
        client.cookies.set("auth_token", "token")

        response = client.post(
            "/submit-algo",
            data={
                "trading_mode": "F",
                "symbol": "AAPL",
                "fast_trading_config": json.dumps(
                    {"price_limit": 95, "account_ids": ["A"], "book_levels": 1,
                     "target_percentage": 10, "cycle_seconds": 5}
                ),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        command = self.started_commands[0]
        self.assertEqual(command["trading_mode"], "F")
        self.assertEqual(command["fast_trading_price_limit"], 95.0)
        self.assertEqual(command["fast_trading_book_levels"], 1)
        self.assertEqual(command["fast_trading_target_percentage"], 10)
        self.assertEqual(command["fast_trading_cycle_seconds"], 5)
        self.assertNotIn("price_target", command)

    def test_both_endpoints_reject_missing_or_invalid_controls_without_starting_strategy(self):
        valid = {"price_limit": 100, "account_ids": ["A"], "book_levels": 2,
                 "target_percentage": 50, "cycle_seconds": 30}
        invalid_configs = [{"price_limit": 100, "account_ids": ["A"], "aggression_level": 1}]
        for field, invalid in (("book_levels", 6), ("target_percentage", 25), ("cycle_seconds", 20)):
            invalid_configs.append({key: value for key, value in valid.items() if key != field})
            invalid_configs.append({**valid, field: invalid})
        client = TestClient(webserver.app)
        try:
            client.cookies.set("auth_token", "token")
            for mode in ("E", "F"):
                for config in invalid_configs:
                    with self.subTest(mode=mode, config=config):
                        response = client.post(
                            "/api/submit-algo",
                            json={"trading_mode": mode, "symbol": "AAPL", "fast_trading_config": config},
                        )
                        self.assertEqual(response.status_code, 400)
                        self.assertFalse(response.json()["ok"])
                        response = client.post(
                            "/submit-algo",
                            data={"trading_mode": mode, "symbol": "AAPL", "fast_trading_config": json.dumps(config)},
                            follow_redirects=False,
                        )
                        self.assertEqual(response.status_code, 303)
                        self.assertIn("?err=", response.headers["location"])
        finally:
            client.close()
        self.assertEqual(self.started_commands, [])
        self.assertEqual(self.published_commands, [])

    def test_fast_stop_preserves_and_uses_selected_account_filter(self):
        client = TestClient(webserver.app)
        client.cookies.set("auth_token", "token")

        response = client.post(
            "/submit-algo-stop",
            data={"trading_mode": "E", "account_ids": "B"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.stop_calls, [("E", ["B"])])
        self.assertEqual(self.published_commands[0][0], "E")
        self.assertEqual(self.published_commands[0][1]["account_ids"], ["B"])


if __name__ == "__main__":
    unittest.main()
