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


class AlgorithmSessionTests(unittest.TestCase):
    def setUp(self):
        self.metas = {aid: AccountMeta(aid, i, "Tiger", "API")
                      for i, aid in enumerate(("A", "B"), 1)}
        now = datetime.now(timezone.utc).isoformat()
        self.snapshots = {
            aid: AccountSnapshot(aid, 10000, positions=[Position("XYZ", 1000)], ts=now,
                                 trading_enabled=True)
            for aid in self.metas
        }
        self.producer = Mock()
        self.fast = FastTradingStrategyManager(
            market_data_store=MarketDataStore(),
            account_metas_provider=lambda: self.metas,
            publish_command=webserver._publish_fast_trading_command,
        )
        self.smart = SmartSellingRuntime(
            {}, account_metas_provider=lambda: self.metas,
            snapshots_provider=lambda: self.snapshots,
            publish_command=webserver._publish_trading_command,
            fast_active=self.fast.has_active_session,
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

    def test_modes_exclude_each_other_even_for_disjoint_accounts_and_symbols(self):
        webserver._start_fast_strategy(self.fast_command)
        with self.assertRaisesRegex(ValueError, "Stop the existing"):
            webserver._start_smart_parent(self.smart_settings)
        self.fast.stop_all()
        parent = webserver._start_smart_parent(self.smart_settings)
        with self.assertRaisesRegex(ValueError, "Stop the existing"):
            webserver._start_fast_strategy(self.fast_command)
        self.smart.stop_parent(parent["parent_id"])
        webserver._start_fast_strategy(self.fast_command)

    def test_concurrent_smart_and_fast_starts_allow_exactly_one_session(self):
        barrier = threading.Barrier(2)

        def start(callback, payload):
            barrier.wait(timeout=5)
            try:
                callback(payload)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, webserver._start_fast_strategy, self.fast_command),
                       pool.submit(start, webserver._start_smart_parent, self.smart_settings)]
            self.assertEqual(sorted(f.result(timeout=5) for f in futures), [False, True])
        self.assertNotEqual(self.fast.has_active_session(), self.smart.has_active_session())

    def test_manual_orders_and_cancellations_are_blocked_for_entire_owned_accounts(self):
        for mode in ("fast", "smart"):
            with self.subTest(mode=mode):
                if mode == "fast":
                    webserver._start_fast_strategy(self.fast_command)
                    account = "A"
                else:
                    webserver._start_smart_parent(self.smart_settings)
                    account = "B"
                for kind in ("MARKET_ORDER", "DELAYED_MARKET_ORDER", "LIMIT_ORDER",
                             "LIMIT_ORDER_FOK", "CANCEL_OPEN_ORDERS", "CURRENCY_CONVERSION"):
                    for symbol in ("XYZ", "OTHER", None):
                        with self.subTest(kind=kind, symbol=symbol):
                            with self.assertRaisesRegex(ValueError, "owns this account"):
                                webserver._publish_control_command(
                                    {"type": kind, "account_id": account, "symbol": symbol}, account
                                )
                self.producer.publish_order.assert_not_called()
                self.fast.stop_all()
                self.smart.stop_all()

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


if __name__ == "__main__":
    unittest.main()
