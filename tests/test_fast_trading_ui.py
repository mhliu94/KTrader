import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

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
            ("fast-target-percentage", (*range(10, 101, 10), 120, 150)),
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
        self.assertNotIn('id="algo-generic-constraints"', html)
        for mode in "abcd":
            self.assertNotIn(f'id="algo-mode-{mode}"', html)
        self.assertNotIn('id="stop-mode-', html)
        self.assertNotIn("id='stop-acct-", html)


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
        self.stop_calls: list[str] = []
        self.published_commands: list[tuple[str, dict]] = []

        class FakeManager:
            def __init__(inner_self, commands: list[dict], stop_calls: list) -> None:
                inner_self.commands = commands
                inner_self.stop_calls = stop_calls

            def has_active_session(inner_self) -> bool:
                return False

            def start_strategy(inner_self, command: dict) -> str:
                inner_self.commands.append(command)
                return "E:AAPL"

            def stop_all(inner_self) -> int:
                inner_self.stop_calls.append("ALL")
                return 3

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

    def test_both_endpoints_accept_oversized_targets_for_both_fast_modes(self):
        client = TestClient(webserver.app)
        try:
            client.cookies.set("auth_token", "token")
            for mode in ("E", "F"):
                for percentage in (120, 150):
                    with self.subTest(mode=mode, percentage=percentage):
                        config = {"price_limit": 10, "account_ids": ["A"], "book_levels": 2,
                                  "target_percentage": str(percentage), "cycle_seconds": 5}
                        response = client.post(
                            "/api/submit-algo",
                            json={"trading_mode": mode, "symbol": "AAPL", "fast_trading_config": config},
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["command"]["fast_trading_target_percentage"], percentage)
                        self.assertEqual(self.started_commands[-1]["trading_mode"], mode)
                        response = client.post(
                            "/submit-algo",
                            data={"trading_mode": mode, "symbol": "AAPL", "fast_trading_config": json.dumps(config)},
                            follow_redirects=False,
                        )
                        self.assertEqual(response.status_code, 303)
                        self.assertEqual(self.started_commands[-1]["trading_mode"], mode)
                        self.assertEqual(self.started_commands[-1]["fast_trading_target_percentage"], percentage)
        finally:
            client.close()
        self.assertEqual(len(self.started_commands), 8)

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

    def test_stop_without_inputs_stops_every_strategy_and_publishes_global_scope(self):
        client = TestClient(webserver.app)
        self.addCleanup(client.close)
        client.cookies.set("auth_token", "token")

        response = client.post(
            "/submit-algo-stop",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.stop_calls, ["ALL"])
        key, command = self.published_commands[0]
        self.assertEqual(key, "ALGO_STOP")
        self.assertEqual(command["scope"], "ALL")
        self.assertNotIn("trading_mode", command)
        self.assertNotIn("account_ids", command)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertIn("Stopped 3 algorithm strategies", query["ok"][0])

    def test_stop_ignores_stale_or_forged_mode_and_account_filters(self):
        client = TestClient(webserver.app)
        self.addCleanup(client.close)
        client.cookies.set("auth_token", "token")
        for mode in ("E", "F", "A", "bogus"):
            with self.subTest(mode=mode):
                response = client.post("/submit-algo-stop?lang=zh", data={
                    "trading_mode": mode, "account_ids": ["B", "UNKNOWN"],
                    "symbol": "AAPL", "reason": "  risk limit  ",
                }, follow_redirects=False)
                query = parse_qs(urlsplit(response.headers["location"]).query)
                self.assertIn("所有模式和账户", query["ok"][0])
                command = self.published_commands[-1][1]
                self.assertEqual(command["scope"], "ALL")
                self.assertEqual(command["reason"], "risk limit")
                self.assertNotIn("trading_mode", command)
                self.assertNotIn("account_ids", command)
                self.assertNotIn("symbol", command)
        self.assertEqual(self.stop_calls, ["ALL"] * 4)

    def test_stop_remains_effective_when_kafka_notification_fails(self):
        client = TestClient(webserver.app)
        self.addCleanup(client.close)
        client.cookies.set("auth_token", "token")
        broken = Mock()
        broken.publish_order.side_effect = RuntimeError("Kafka unavailable")
        for producer in (None, broken):
            with self.subTest(producer=producer):
                webserver.COMMANDS_PRODUCER = producer
                response = client.post("/submit-algo-stop", follow_redirects=False)
                query = parse_qs(urlsplit(response.headers["location"]).query)
                self.assertIn("Stopped 3", query["ok"][0])
                self.assertIn("notification could not be published", query["err"][0])
        self.assertEqual(self.stop_calls, ["ALL", "ALL"])

    def test_stop_requires_authentication(self):
        client = TestClient(webserver.app)
        self.addCleanup(client.close)
        response = client.post("/submit-algo-stop", follow_redirects=False)
        self.assertIn("/login", response.headers["location"])
        self.assertEqual(self.stop_calls, [])
        self.assertEqual(self.published_commands, [])

    def test_stop_does_not_report_success_when_manager_is_unavailable(self):
        client = TestClient(webserver.app)
        self.addCleanup(client.close)
        client.cookies.set("auth_token", "token")
        webserver.FAST_TRADING_MANAGER = None
        response = client.post("/submit-algo-stop", follow_redirects=False)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertNotIn("ok", query)
        self.assertIn("could not be confirmed", query["err"][0])
        self.assertEqual(self.published_commands, [])

    def test_retired_modes_cannot_be_started_from_either_endpoint(self):
        client = TestClient(webserver.app)
        self.addCleanup(client.close)
        client.cookies.set("auth_token", "token")
        for mode in "ABCD":
            with self.subTest(mode=mode):
                payload = {"trading_mode": mode, "symbol": "AAPL"}
                response = client.post("/api/submit-algo", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("E or F", response.json()["error"])
                response = client.post("/submit-algo", data=payload, follow_redirects=False)
                self.assertIn("?err=", response.headers["location"])
        self.assertEqual(self.started_commands, [])
        self.assertEqual(self.published_commands, [])


if __name__ == "__main__":
    unittest.main()
