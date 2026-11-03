import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from trading_ui import webserver
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.fast_trading import FastTradingStrategyManager
from trading_ui.services.market_data import MarketDataStore
from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_runtime import SmartSellingRuntime


CONFLICT = "already belong to an algorithm session"


class AlgorithmSessionTests(unittest.TestCase):
    def setUp(self):
        self.metas = {aid: AccountMeta(aid, i, "Tiger", "API")
                      for i, aid in enumerate(("A", "B", "C"), 1)}
        now = datetime.now(timezone.utc).isoformat()
        self.snapshots = {
            aid: AccountSnapshot(aid, 10000, positions=[Position("XYZ", 1000)], ts=now,
                                 trading_enabled=True)
            for aid in self.metas
        }
        self.producer = Mock()
        # Each manager asks the other about account ownership, as the server wires them.
        self.fast = FastTradingStrategyManager(
            market_data_store=MarketDataStore(),
            account_metas_provider=lambda: self.metas,
            publish_command=webserver._publish_fast_trading_command,
            smart_owns_account=lambda account: self.smart.owns_account(account),
        )
        self.smart = SmartSellingRuntime(
            {}, account_metas_provider=lambda: self.metas,
            snapshots_provider=lambda: self.snapshots,
            publish_command=webserver._publish_trading_command,
            fast_owns_account=self.fast.owns_account,
            engine=SmartSellingEngine(":memory:"),
        )
        self.smart.feeds = {"orders": "live", "volume": "live"}
        self.globals = patch.multiple(
            webserver, FAST_TRADING_MANAGER=self.fast, SMART_SELLING_MANAGER=self.smart,
            COMMANDS_PRODUCER=self.producer, ACCOUNT_METAS=self.metas,
            SESSIONS={"token": "trader"},
        )
        self.globals.start()
        self.client = TestClient(webserver.app)
        self.client.cookies.set("auth_token", "token")
        self.fast_command = {
            "command_id": "fast-session", "trading_mode": "E", "symbol": "AAPL",
            "fast_trading_price_limit": 10, "fast_trading_account_ids": ["A"],
            "fast_trading_book_levels": 1, "fast_trading_target_percentage": 100,
            "fast_trading_cycle_seconds": 5,
        }
        self.smart_settings = {"account_ids": ["B"], "symbol": "XYZ", "deadline": None}

    def tearDown(self):
        self.fast.stop_all()
        self.smart.close()
        self.client.close()
        self.globals.stop()

    def fast_start(self, command_id, mode, symbol, *accounts):
        return {**self.fast_command, "command_id": command_id, "trading_mode": mode,
                "symbol": symbol, "fast_trading_account_ids": list(accounts)}

    def test_sessions_in_every_mode_run_at_once_when_their_accounts_are_disjoint(self):
        webserver._start_fast_strategy(self.fast_start("buy", "E", "AAPL", "A"))
        webserver._start_fast_strategy(self.fast_start("sell", "F", "MSFT", "C"))
        parent = webserver._start_smart_parent(self.smart_settings)
        self.assertEqual(len(self.fast._strategies), 2)
        self.assertTrue(self.smart.has_active_session())
        for account, owner in (("A", self.fast), ("C", self.fast), ("B", self.smart)):
            self.assertTrue(owner.owns_account(account))
        self.smart.stop_parent(parent["parent_id"])
        self.assertEqual(self.fast.stop_all(), 2)

    def test_account_in_one_session_cannot_join_another_in_any_mode(self):
        webserver._start_fast_strategy(self.fast_start("buy", "E", "AAPL", "A"))
        parent = webserver._start_smart_parent(self.smart_settings)
        for start, payload in (
            (webserver._start_fast_strategy, self.fast_start("sell", "F", "MSFT", "C", "A")),
            (webserver._start_fast_strategy, self.fast_start("again", "E", "AAPL", "B")),
            (webserver._start_smart_parent, {**self.smart_settings, "account_ids": ["C", "A"]}),
            (webserver._start_smart_parent, {**self.smart_settings, "symbol": "OTHER"}),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, CONFLICT):
                    start(payload)
        self.assertFalse(self.fast.owns_account("C") or self.smart.owns_account("C"))
        # A released account is available to every mode again.
        self.fast.stop_all()
        webserver._start_smart_parent({**self.smart_settings, "account_ids": ["A"]})
        self.smart.stop_parent(parent["parent_id"])
        webserver._start_fast_strategy(self.fast_start("buy-b", "E", "AAPL", "B"))

    def test_concurrent_starts_on_one_account_admit_exactly_one_session(self):
        barrier = threading.Barrier(2)

        def start(callback, payload):
            barrier.wait(timeout=5)
            try:
                callback(payload)
                return True
            except ValueError:
                return False

        contested = {**self.smart_settings, "account_ids": ["A"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, webserver._start_fast_strategy, self.fast_command),
                       pool.submit(start, webserver._start_smart_parent, contested)]
            self.assertEqual(sorted(f.result(timeout=5) for f in futures), [False, True])
        self.assertNotEqual(self.fast.owns_account("A"), self.smart.owns_account("A"))

    def test_manual_orders_and_cancellations_are_blocked_for_entire_owned_accounts(self):
        webserver._start_fast_strategy(self.fast_command)
        webserver._start_smart_parent(self.smart_settings)
        for account in ("A", "B"):
            for kind in ("MARKET_ORDER", "DELAYED_MARKET_ORDER", "LIMIT_ORDER",
                         "LIMIT_ORDER_FOK", "CANCEL_OPEN_ORDERS", "CURRENCY_CONVERSION"):
                for symbol in ("XYZ", "OTHER", None):
                    with self.subTest(account=account, kind=kind, symbol=symbol):
                        with self.assertRaisesRegex(ValueError, "owns this account"):
                            webserver._publish_control_command(
                                {"type": kind, "account_id": account, "symbol": symbol}, account
                            )
        self.producer.publish_order.assert_not_called()

    def test_manual_cancellations_on_unselected_accounts_remain_available(self):
        webserver._start_smart_parent(self.smart_settings)
        response = self.client.post("/submit-cancel-open-orders", data={"account_ids": "A"},
                                    follow_redirects=False)
        self.assertIn("ok=", response.headers["location"])
        self.assertEqual(self.producer.publish_order.call_args.args[0]["account_id"], "A")
        self.producer.reset_mock()
        response = self.client.post("/submit-cancel-open-orders", data={"account_ids": "B"},
                                    follow_redirects=False)
        self.assertIn("err=", response.headers["location"])
        self.producer.publish_order.assert_not_called()

    def test_stop_all_stops_smart_even_if_notification_transport_fails(self):
        webserver._start_smart_parent(self.smart_settings)
        self.producer.publish_order.side_effect = RuntimeError("transport unavailable")
        response = self.client.post("/submit-algo-stop", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertFalse(self.smart.has_active_session())
        self.assertIn("err=", response.headers["location"])
        self.assertIn("ok=", response.headers["location"])

    def test_stop_all_ends_every_running_session(self):
        webserver._start_fast_strategy(self.fast_start("buy", "E", "AAPL", "A"))
        webserver._start_fast_strategy(self.fast_start("sell", "F", "MSFT", "C"))
        webserver._start_smart_parent(self.smart_settings)
        response = self.client.post("/submit-algo-stop", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("ok=", response.headers["location"])
        self.assertFalse(self.fast.has_active_session())
        self.assertFalse(self.smart.has_active_session())
        self.assertEqual(self.producer.publish_order.call_args.args[0]["type"], "STOP_ALGO_TRADING")

    def test_new_start_waits_until_previous_global_stop_notification_is_enqueued(self):
        webserver._start_smart_parent(self.smart_settings)
        building, release, starting = threading.Event(), threading.Event(), threading.Event()
        original_builder = webserver.build_algo_stop_command

        def build_notification(**kwargs):
            building.set()
            self.assertTrue(release.wait(5))
            return original_builder(**kwargs)

        def start_next():
            starting.set()
            webserver._start_fast_strategy(self.fast_command)
            self.producer.publish_order.assert_called_once()
            self.assertEqual(self.producer.publish_order.call_args.args[0]["type"], "STOP_ALGO_TRADING")

        with patch.object(webserver, "build_algo_stop_command", side_effect=build_notification):
            with ThreadPoolExecutor(max_workers=2) as pool:
                stop = pool.submit(self.client.post, "/submit-algo-stop", follow_redirects=False)
                try:
                    self.assertTrue(building.wait(5))
                    start = pool.submit(start_next)
                    self.assertTrue(starting.wait(5))
                    with self.assertRaises(TimeoutError):
                        start.result(timeout=0.05)
                finally:
                    release.set()
                self.assertEqual(stop.result(timeout=5).status_code, 303)
                start.result(timeout=5)

    def test_algorithm_order_handoff_bypasses_manual_guard_but_not_stop(self):
        webserver._start_fast_strategy(self.fast_command)
        command = {"type": "LIMIT_ORDER_FOK", "account_id": "A", "symbol": "AAPL",
                   "command_id": "child-1"}
        webserver._publish_fast_trading_command(command, "A")
        self.producer.publish_order.assert_called_once_with(command, key="A")
        self.fast.stop_all()
        self.producer.reset_mock()
        webserver._publish_fast_trading_command(command, "A")
        self.producer.publish_order.assert_not_called()

    def test_fast_handoff_proceeds_while_smart_selling_owns_other_accounts(self):
        webserver._start_fast_strategy(self.fast_command)
        webserver._start_smart_parent(self.smart_settings)
        command = {"type": "LIMIT_ORDER_FOK", "account_id": "A", "symbol": "AAPL",
                   "command_id": "child-1"}
        webserver._publish_fast_trading_command(command, "A")
        self.producer.publish_order.assert_called_once_with(command, key="A")
        # Ownership is exclusive: a Smart selling claim on the same account halts the handoff.
        self.producer.reset_mock()
        with patch.object(self.smart, "owns_account", return_value=True):
            with self.assertRaisesRegex(ValueError, CONFLICT):
                webserver._publish_fast_trading_command(command, "A")
        self.producer.publish_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
