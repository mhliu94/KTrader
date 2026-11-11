import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from trading_ui import webserver
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.algo_sessions import AlgoSessionCoordinator
from trading_ui.services.fast_trading import FastTradingStrategyManager
from trading_ui.services.market_data import BookLevel, MarketDataStore, OrderBookSnapshot
from trading_ui.services.smart_buying import SmartBuyingEngine
from trading_ui.services.smart_buying_runtime import SmartBuyingRuntime
from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_runtime import SmartSellingRuntime


CONFLICT = "must use distinct symbols"


class AlgorithmSessionTests(unittest.TestCase):
    def setUp(self):
        self.metas = {aid: AccountMeta(aid, i, "Tiger", "API")
                      for i, aid in enumerate(("A", "B", "C"), 1)}
        now = datetime.now(timezone.utc).isoformat()
        self.snapshots = {
            aid: AccountSnapshot(aid, 10000, positions=[Position(s, 1000) for s in ("XYZ", "OTHER", "AAPL", "MSFT")], ts=now,
                                 trading_enabled=True)
            for aid in self.metas
        }
        self.producer = Mock()
        self.coordinator = AlgoSessionCoordinator()
        self.fast = FastTradingStrategyManager(
            market_data_store=MarketDataStore(),
            account_metas_provider=lambda: self.metas,
            publish_command=webserver._publish_fast_trading_command,
            session_coordinator=self.coordinator,
        )
        self.smart = SmartSellingRuntime(
            {}, account_metas_provider=lambda: self.metas,
            snapshots_provider=lambda: self.snapshots,
            publish_command=webserver._publish_trading_command,
            session_coordinator=self.coordinator,
            engine=SmartSellingEngine(":memory:"),
        )
        self.smart.feeds = {"orders": "live", "volume": "live"}
        buying_market = MarketDataStore()
        for symbol in ("XYZ", "OTHER", "AAPL", "MSFT"):
            buying_market.upsert_book(OrderBookSnapshot(
                symbol, asks=[BookLevel(10, 10000)], asof_epoch=datetime.now(timezone.utc).timestamp(),
            ))
        self.buying = SmartBuyingRuntime(
            {}, market_data_store=buying_market, account_metas_provider=lambda: self.metas,
            snapshots_provider=lambda: self.snapshots, publish_command=webserver._publish_trading_command,
            session_coordinator=self.coordinator, engine=SmartBuyingEngine(":memory:"),
        )
        self.buying.feeds = {"orders": "live", "volume": "live"}
        self.globals = patch.multiple(
            webserver, FAST_TRADING_MANAGER=self.fast, SMART_SELLING_MANAGER=self.smart,
            SMART_BUYING_MANAGER=self.buying,
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
        self.buying.close()
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

    def test_account_can_run_one_buyer_and_multiple_sellers(self):
        webserver._start_fast_strategy(self.fast_start("buy", "E", "AAPL", "A"))
        webserver._start_fast_strategy(self.fast_start("sell", "F", "MSFT", "A"))
        parent = webserver._start_smart_parent({**self.smart_settings, "account_ids": ["A"]})
        webserver._start_smart_parent({**self.smart_settings, "account_ids": ["A"], "symbol": "OTHER"})
        for start, payload in (
            (webserver._start_fast_strategy, self.fast_start("duplicate", "F", "MSFT", "C", "A")),
            (webserver._start_fast_strategy, self.fast_start("again", "E", "AAPL", "C", "A")),
            (webserver._start_smart_parent, {**self.smart_settings, "account_ids": ["C", "A"]}),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, CONFLICT):
                    start(payload)
        self.assertFalse(self.fast.owns_account("C") or self.smart.owns_account("C"))
        self.assertEqual(len(self.fast._strategies), 2)
        self.assertEqual(len(self.smart.algo_sessions()), 2)
        # Releasing a symbol permits reuse while other sessions keep running.
        self.smart.stop_parent(parent["parent_id"])
        webserver._start_smart_parent({**self.smart_settings, "account_ids": ["A"]})
        self.fast.stop_all()
        webserver._start_fast_strategy(self.fast_start("buy-again", "E", "AAPL", "A"))

    def start_mode(self, mode, symbol, *accounts):
        if mode in ("E", "F"):
            return self.fast.start_strategy(self.fast_start(
                f"{mode}-{symbol}-{'-'.join(accounts)}", mode, symbol, *accounts,
            ))
        runtime = self.smart if mode == "G" else self.buying
        return runtime.start_parent({"account_ids": list(accounts), "symbol": symbol, "deadline": None})

    def stop_modes(self):
        self.fast.stop_all()
        self.smart.stop_all()
        self.buying.stop_all()

    def test_every_mode_pair_rejects_the_same_canonical_symbol(self):
        for first in "EFGH":
            for second in "EFGH":
                with self.subTest(first=first, second=second):
                    self.start_mode(first, "XYZ", "A")
                    with self.assertRaisesRegex(ValueError, CONFLICT) as error:
                        self.start_mode(second, " us.xyz/USD ", "B", "A")
                    self.assertIn("Account(s) A:", str(error.exception))
                    self.assertIn(f"mode {first}, symbol XYZ", str(error.exception))
                    self.assertFalse(any(m.owns_account("B") for m in (self.fast, self.smart, self.buying)))
                    self.stop_modes()

    def test_every_mode_pair_on_distinct_symbols_only_rejects_two_buyers(self):
        for first in "EFGH":
            for second in "EFGH":
                with self.subTest(first=first, second=second):
                    self.start_mode(first, "XYZ", "A")
                    if first in "EH" and second in "EH":
                        with self.assertRaisesRegex(ValueError, "at most 1 buying strategy") as error:
                            self.start_mode(second, "OTHER", "B", "A")
                        self.assertIn("Account(s) A:", str(error.exception))
                        self.assertIn(f"mode {first}, symbol XYZ", str(error.exception))
                    else:
                        self.start_mode(second, "OTHER", "B", "A")
                    # The same symbol and a separate buyer are legal on another account.
                    self.start_mode(second, "XYZ", "C")
                    self.stop_modes()

    def test_concurrent_starts_on_one_account_admit_exactly_one_session(self):
        barrier = threading.Barrier(2)

        def start(callback, payload):
            barrier.wait(timeout=5)
            try:
                callback(payload)
                return True
            except ValueError:
                return False

        contested = {**self.smart_settings, "account_ids": ["A"], "symbol": "AAPL"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, webserver._start_fast_strategy, self.fast_command),
                       pool.submit(start, webserver._start_smart_parent, contested)]
            self.assertEqual(sorted(f.result(timeout=5) for f in futures), [False, True])
        self.assertNotEqual(self.fast.owns_account("A"), self.smart.owns_account("A"))

    def test_concurrent_direct_runtime_starts_are_atomic(self):
        for first, second, symbol, expected in (
            ("E", "H", "OTHER", [False, True]),
            ("H", "H", "OTHER", [False, True]),
            ("F", "G", "XYZ", [False, True]),
            ("G", "G", "XYZ", [False, True]),
            ("E", "G", "OTHER", [True, True]),
            ("H", "F", "OTHER", [True, True]),
            ("F", "G", "OTHER", [True, True]),
            ("G", "G", "OTHER", [True, True]),
        ):
            with self.subTest(first=first, second=second, symbol=symbol):
                barrier = threading.Barrier(2)

                def start(mode, ticker):
                    barrier.wait(timeout=5)
                    try:
                        self.start_mode(mode, ticker, "A")
                        return True
                    except ValueError:
                        return False

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(start, first, "XYZ"), pool.submit(start, second, symbol)]
                    self.assertEqual(sorted(f.result(timeout=5) for f in futures), expected)
                self.stop_modes()

    def test_start_endpoints_report_the_conflicting_account_mode_symbol_and_rule(self):
        self.start_mode("E", "AAPL", "A")
        for mode in "EFGH":
            with self.subTest(mode=mode):
                symbol = "OTHER" if mode in "EH" else "AAPL"
                if mode in "EF":
                    response = self.client.post("/api/submit-algo", json={
                        **self.fast_start("api", mode, symbol, "B", "A"),
                    })
                else:
                    endpoint = "/api/smart-selling" if mode == "G" else "/api/smart-buying"
                    response = self.client.post(endpoint, json={
                        "account_ids": ["B", "A"], "symbol": symbol, "deadline": None,
                    })
                self.assertEqual(response.status_code, 400, response.text)
                error = response.json()["error"]
                self.assertIn("Account(s) A:", error)
                self.assertIn("mode E, symbol AAPL", error)
                self.assertIn("at most 1 buying strategy" if mode in "EH" else CONFLICT, error)
                self.assertFalse(response.json()["ok"])
        self.assertFalse(any(m.owns_account("B") for m in (self.fast, self.smart, self.buying)))

    def test_scheduled_and_paused_buyer_keeps_its_buying_reservation(self):
        parent = self.buying.start_parent({
            "account_ids": ["A"], "symbol": "XYZ", "deadline": None,
            "start_time": datetime.now(timezone.utc).timestamp() + 3600,
        })
        self.buying.on_feed_state("orders", "disconnected")
        with self.assertRaisesRegex(ValueError, "at most 1 buying strategy"):
            self.start_mode("E", "OTHER", "A")
        self.start_mode("F", "OTHER", "A")
        self.buying.stop_parent(parent["parent_id"])
        self.start_mode("E", "XYZ", "A")

    def test_failed_start_does_not_reserve_cash_or_symbol(self):
        with self.assertRaisesRegex(ValueError, "mode must"):
            self.buying.start_parent({"account_ids": ["A"], "symbol": "XYZ", "mode": "invalid"})
        self.start_mode("E", "XYZ", "A")

    def test_selling_sessions_have_no_count_cap_and_oldest_active_remains_reserved(self):
        # Populate durable parents as on recovery, beyond the old history limit.
        for index in range(1001):
            self.smart.engine.start_parent({
                "account_id": "A", "symbol": f"STOCK{index}", "quantity": "1000", "deadline": None,
                "instrument_rules": self.smart.config["instrument_defaults"],
            })
        self.assertEqual(len(self.smart.algo_sessions()), 1001)
        self.assertEqual(len(self.smart.list_parents()), 1001)
        with self.assertRaisesRegex(ValueError, "symbol STOCK0 is already in use"):
            self.start_mode("F", "STOCK0", "A")
        self.start_mode("G", "OTHER", "A")
        self.assertEqual(self.smart.stop_all(), 1002)
        self.assertFalse(self.smart.has_active_session())
        self.start_mode("F", "STOCK0", "A")

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

    def test_fast_handoff_proceeds_while_smart_selling_shares_account_on_another_symbol(self):
        webserver._start_fast_strategy(self.fast_command)
        webserver._start_smart_parent({**self.smart_settings, "account_ids": ["A"]})
        command = {"type": "LIMIT_ORDER_FOK", "account_id": "A", "symbol": "AAPL",
                   "command_id": "child-1"}
        webserver._publish_fast_trading_command(command, "A")
        self.producer.publish_order.assert_called_once_with(command, key="A")
        # A conflicting claim on the same symbol still halts the handoff.
        self.producer.reset_mock()
        with patch.object(self.smart, "owns", return_value=True):
            with self.assertRaisesRegex(ValueError, CONFLICT):
                webserver._publish_fast_trading_command(command, "A")
        self.producer.publish_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
