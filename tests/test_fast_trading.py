import unittest
import threading
import time

from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.fast_trading import (
    FastTradingStrategyManager,
    build_fast_trading_cycle_commands,
)
from trading_ui.services.market_data import BookLevel, MarketDataStore, OrderBookSnapshot


DEFAULT_MINIMUM_CYCLE_SECONDS = {
    "API": 3.0,
    "WEB": 30.0,
    "WINDOWS": 40.0,
    "EMULATOR": 40.0,
}


def fast_trading_settings(*, cycle_seconds=0.01):
    return {
        "aggression_levels": {
            1: {"book_levels": 2, "cycle_seconds": cycle_seconds},
            2: {"book_levels": 3, "cycle_seconds": cycle_seconds},
            3: {"book_levels": 5, "cycle_seconds": cycle_seconds},
        },
        "minimum_cycle_seconds_by_medium": {
            "API": 0.001,
            "WEB": 0.001,
            "WINDOWS": 0.001,
            "EMULATOR": 0.001,
        },
    }


def strategy_command(*, aggression_level=1):
    return {
        "command_id": "fast-session-test",
        "trading_mode": "E",
        "symbol": "AAPL",
        "fast_trading_price_limit": 10,
        "fast_trading_account_ids": ["A"],
        "fast_trading_aggression_level": aggression_level,
    }


class RecordingMarketDataStore(MarketDataStore):
    def __init__(self):
        super().__init__()
        self.get_book_times = []

    def get_book(self, symbol, depth_limit=20):
        self.get_book_times.append(time.monotonic())
        return super().get_book(symbol, depth_limit=depth_limit)


def metas(*account_ids):
    return {
        account_id: AccountMeta(
            id=account_id,
            num_id=idx,
            broker="Tiger",
            trading_medium="API",
        )
        for idx, account_id in enumerate(account_ids, start=1)
    }


def metas_by_medium(**medium_by_account_id):
    return {
        account_id: AccountMeta(
            id=account_id,
            num_id=idx,
            broker="Tiger",
            trading_medium=medium,
        )
        for idx, (account_id, medium) in enumerate(medium_by_account_id.items(), start=1)
    }


def snapshots(**snapshot_by_account_id):
    return {
        account_id: AccountSnapshot(
            account_id=account_id,
            cash=float(data.get("cash", 0.0)),
            cash_by_currency=dict(data.get("cash_by_currency", {})),
            available_cash_by_currency=dict(
                data.get("available_cash_by_currency", {})
            ),
            positions=[
                Position(
                    symbol=str(position[0]),
                    qty=float(position[1]),
                    available_qty=(
                        float(position[2])
                        if len(position) > 2 and position[2] is not None
                        else None
                    ),
                )
                for position in data.get("positions", [])
            ],
        )
        for account_id, data in snapshot_by_account_id.items()
    }


def build_cycle(
    *,
    mode,
    book,
    price_limit,
    account_ids,
    book_levels,
    account_metas=None,
    account_snapshots=None,
    last_actions=None,
    now=100.0,
    cycle_id="test-cycle",
):
    return build_fast_trading_cycle_commands(
        trading_mode=mode,
        symbol=book.symbol,
        book=book,
        price_limit=price_limit,
        account_ids=account_ids,
        book_levels=book_levels,
        account_metas=account_metas or metas(*account_ids),
        account_snapshots=account_snapshots,
        minimum_cycle_seconds_by_medium=DEFAULT_MINIMUM_CYCLE_SECONDS,
        last_action_monotonic_by_account=last_actions or {},
        now_monotonic=now,
        cycle_id=cycle_id,
    )


class FastTradingCycleTests(unittest.TestCase):
    def test_mode_e_sums_only_top_n_asks(self):
        book = OrderBookSnapshot(
            symbol="MSFT",
            bids=[],
            asks=[
                BookLevel(price=100, quantity=200),
                BookLevel(price=110, quantity=300),
                BookLevel(price=120, quantity=700),
            ],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=130,
            account_ids=["A"],
            book_levels=2,
        )

        self.assertEqual(result.total_resting_qty, 500)
        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0]["qty_shares"], 500)
        self.assertEqual(result.limit_price, 110)

    def test_nonpositive_book_rows_do_not_consume_top_n_slots(self):
        result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[
                    BookLevel(price=1, quantity=0),
                    BookLevel(price=2, quantity=100),
                    BookLevel(price=3, quantity=100),
                ],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
        )

        self.assertEqual(result.total_resting_qty, 200)
        self.assertEqual(result.limit_price, 3)

    def test_mode_e_caps_quantity_and_limit_at_upper_price(self):
        book = OrderBookSnapshot(
            symbol="MSFT",
            bids=[],
            asks=[
                BookLevel(price=100, quantity=200),
                BookLevel(price=110, quantity=300),
                BookLevel(price=120, quantity=700),
            ],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=105,
            account_ids=["A"],
            book_levels=2,
        )

        self.assertEqual(result.total_resting_qty, 200)
        self.assertEqual(result.limit_price, 105)
        self.assertEqual(result.commands[0]["limit_price"], 105)
        self.assertEqual(result.commands[0]["side"], "BUY")
        self.assertEqual(result.commands[0]["type"], "LIMIT_ORDER_FOK")
        self.assertEqual(result.commands[0]["time_in_force"], "FOK")
        self.assertTrue(result.commands[0]["cancel_unfilled"])

    def test_mode_f_sums_only_top_n_bids(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[
                BookLevel(price=120, quantity=200),
                BookLevel(price=110, quantity=300),
                BookLevel(price=100, quantity=700),
            ],
            asks=[],
        )

        result = build_cycle(
            mode="F",
            book=book,
            price_limit=90,
            account_ids=["A"],
            book_levels=2,
        )

        self.assertEqual(result.total_resting_qty, 500)
        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0]["qty_shares"], 500)
        self.assertEqual(result.limit_price, 110)

    def test_mode_f_floors_quantity_and_limit_at_lower_price(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[
                BookLevel(price=100, quantity=200),
                BookLevel(price=90, quantity=300),
                BookLevel(price=80, quantity=700),
            ],
            asks=[],
        )

        result = build_cycle(
            mode="F",
            book=book,
            price_limit=95,
            account_ids=["A"],
            book_levels=2,
            account_snapshots=snapshots(A={"positions": [("AAPL", 1000)]}),
        )

        self.assertEqual(result.total_resting_qty, 200)
        self.assertEqual(result.limit_price, 95)
        self.assertEqual(result.commands[0]["limit_price"], 95)
        self.assertEqual(result.commands[0]["side"], "SELL")
        self.assertEqual(result.commands[0]["type"], "LIMIT_ORDER_FOK")
        self.assertEqual(result.commands[0]["time_in_force"], "FOK")
        self.assertTrue(result.commands[0]["cancel_unfilled"])

    def test_even_allocation_discards_indivisible_remainder(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=1001)],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=["A", "B", "C"],
            book_levels=2,
        )

        self.assertEqual([cmd["account_id"] for cmd in result.commands], ["A", "B", "C"])
        self.assertEqual([cmd["qty_shares"] for cmd in result.commands], [333, 333, 333])
        self.assertEqual(result.allocated_quantity, 999)

    def test_buy_capacity_exclusion_is_iterative_and_redistributes_evenly(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=900)],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=["A", "B", "C"],
            book_levels=2,
            account_snapshots=snapshots(
                A={"cash_by_currency": {"USD": 2500}},
                B={"cash_by_currency": {"USD": 4000}},
                C={"cash_by_currency": {"USD": 9000}},
            ),
        )

        self.assertEqual([cmd["account_id"] for cmd in result.commands], ["C"])
        self.assertEqual([cmd["qty_shares"] for cmd in result.commands], [900])

    def test_sell_capacity_exclusion_is_iterative_and_redistributes_evenly(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[BookLevel(price=10, quantity=900)],
            asks=[],
        )

        result = build_cycle(
            mode="F",
            book=book,
            price_limit=10,
            account_ids=["A", "B", "C"],
            book_levels=2,
            account_snapshots=snapshots(
                A={"positions": [("AAPL", 250)]},
                B={"positions": [("AAPL", 400)]},
                C={"positions": [("AAPL", 900)]},
            ),
        )

        self.assertEqual([cmd["account_id"] for cmd in result.commands], ["C"])
        self.assertEqual([cmd["qty_shares"] for cmd in result.commands], [900])

    def test_buy_capacity_prefers_available_cash_over_balance(self):
        result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            account_snapshots=snapshots(
                A={
                    "cash_by_currency": {"USD": 2_000},
                    "available_cash_by_currency": {"USD": 1_990},
                }
            ),
        )

        self.assertEqual(result.commands, [])
        self.assertEqual(result.reason, "no_account_capacity")
        self.assertTrue(result.capacity_noop)

    def test_sell_capacity_prefers_available_quantity_over_position_quantity(self):
        result = build_cycle(
            mode="F",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[BookLevel(price=10, quantity=200)],
                asks=[],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            account_snapshots=snapshots(A={"positions": [("AAPL", 200, 199)]}),
        )

        self.assertEqual(result.commands, [])
        self.assertEqual(result.reason, "no_account_capacity")
        self.assertTrue(result.capacity_noop)

    def test_cycle_is_skipped_when_equal_order_is_under_one_hundred_shares(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=399)],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=["A", "B", "C", "D"],
            book_levels=2,
        )

        self.assertEqual(result.total_resting_qty, 399)
        self.assertEqual(result.commands, [])
        self.assertEqual(result.reason, "per_account_quantity_below_minimum")
        self.assertFalse(result.capacity_noop)

    def test_underfunded_account_can_be_removed_before_minimum_share_check(self):
        result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=150)],
            ),
            price_limit=10,
            account_ids=["A", "B"],
            book_levels=2,
            account_snapshots=snapshots(
                A={"cash_by_currency": {"USD": 0}},
                B={"cash_by_currency": {"USD": 1500}},
            ),
        )

        self.assertEqual([cmd["account_id"] for cmd in result.commands], ["B"])
        self.assertEqual(result.commands[0]["qty_shares"], 150)

    def test_accounts_without_a_previous_action_are_immediately_eligible(self):
        account_metas = metas_by_medium(
            API_ACCOUNT="API",
            WEB_ACCOUNT="WEB",
            WINDOWS_ACCOUNT="WINDOWS",
            EMULATOR_ACCOUNT="EMULATOR",
        )
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=800)],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=list(account_metas),
            book_levels=2,
            account_metas=account_metas,
            last_actions={},
        )

        self.assertEqual(
            result.cooldown_eligible_account_ids,
            ["API_ACCOUNT", "WEB_ACCOUNT", "WINDOWS_ACCOUNT", "EMULATOR_ACCOUNT"],
        )
        self.assertEqual([cmd["qty_shares"] for cmd in result.commands], [200, 200, 200, 200])

    def test_cooldown_is_filtered_per_account_and_medium(self):
        account_metas = metas_by_medium(
            API_ACCOUNT="API",
            WEB_ACCOUNT="WEB",
            WINDOWS_ACCOUNT="WINDOWS",
            EMULATOR_ACCOUNT="EMULATOR",
        )
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=800)],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=list(account_metas),
            book_levels=2,
            account_metas=account_metas,
            now=100.0,
            last_actions={
                "API_ACCOUNT": 97.001,
                "WEB_ACCOUNT": 70.001,
                "WINDOWS_ACCOUNT": 60.001,
                "EMULATOR_ACCOUNT": 60.0,
            },
        )

        self.assertEqual(result.cooldown_eligible_account_ids, ["EMULATOR_ACCOUNT"])
        self.assertEqual([cmd["account_id"] for cmd in result.commands], ["EMULATOR_ACCOUNT"])
        self.assertEqual(result.commands[0]["qty_shares"], 800)

    def test_exact_medium_cooldown_boundary_is_eligible(self):
        account_metas = metas_by_medium(
            API_ACCOUNT="API",
            WEB_ACCOUNT="WEB",
            WINDOWS_ACCOUNT="WINDOWS",
            EMULATOR_ACCOUNT="EMULATOR",
        )
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=800)],
        )

        result = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=list(account_metas),
            book_levels=2,
            account_metas=account_metas,
            now=100.0,
            last_actions={
                "API_ACCOUNT": 97.0,
                "WEB_ACCOUNT": 70.0,
                "WINDOWS_ACCOUNT": 60.0,
                "EMULATOR_ACCOUNT": 60.0,
            },
        )

        self.assertEqual(
            result.cooldown_eligible_account_ids,
            ["API_ACCOUNT", "WEB_ACCOUNT", "WINDOWS_ACCOUNT", "EMULATOR_ACCOUNT"],
        )

    def test_only_capacity_exhaustion_is_a_capacity_noop(self):
        capacity_result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            account_snapshots=snapshots(A={"cash_by_currency": {"USD": 1990}}),
        )
        sell_capacity_result = build_cycle(
            mode="F",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[BookLevel(price=10, quantity=200)],
                asks=[],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            account_snapshots=snapshots(A={"positions": [("AAPL", 199)]}),
        )
        cooldown_result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            last_actions={"A": 100.0},
        )
        empty_book_result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(symbol="AAPL", bids=[], asks=[]),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
        )
        out_of_limit_result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=11, quantity=1000)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
        )
        minimum_result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=99)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            account_snapshots=snapshots(A={"cash_by_currency": {"USD": 0}}),
        )

        self.assertEqual(capacity_result.reason, "no_account_capacity")
        self.assertTrue(capacity_result.capacity_noop)
        self.assertEqual(sell_capacity_result.reason, "no_account_capacity")
        self.assertTrue(sell_capacity_result.capacity_noop)
        self.assertEqual(cooldown_result.reason, "no_accounts_off_cooldown")
        self.assertFalse(cooldown_result.capacity_noop)
        self.assertEqual(empty_book_result.reason, "no_resting_orders")
        self.assertFalse(empty_book_result.capacity_noop)
        self.assertEqual(out_of_limit_result.reason, "no_resting_orders_within_price_limit")
        self.assertFalse(out_of_limit_result.capacity_noop)
        self.assertEqual(minimum_result.reason, "per_account_quantity_below_minimum")
        self.assertFalse(minimum_result.capacity_noop)

    def test_missing_snapshot_data_is_not_a_capacity_noop(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[BookLevel(price=10, quantity=200)],
        )

        unavailable = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=["A"],
            book_levels=2,
            account_snapshots={},
        )
        incomplete = build_cycle(
            mode="E",
            book=book,
            price_limit=10,
            account_ids=["A", "B"],
            book_levels=2,
            account_snapshots=snapshots(A={"cash_by_currency": {"USD": 0}}),
        )

        self.assertEqual(unavailable.reason, "account_snapshots_unavailable")
        self.assertFalse(unavailable.capacity_noop)
        self.assertEqual(incomplete.reason, "account_snapshots_incomplete")
        self.assertFalse(incomplete.capacity_noop)


class FastTradingManagerTests(unittest.TestCase):
    def test_manager_records_order_action_time_for_future_cooldown_checks(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            )
        )
        settings = fast_trading_settings()
        settings["minimum_cycle_seconds_by_medium"]["API"] = 10.0
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=lambda command, key: None,
            fast_trading_settings=settings,
        )

        first_result = manager.run_cycle(strategy_command())
        second_result = manager.run_cycle(strategy_command())

        self.assertEqual(len(first_result.commands), 1)
        self.assertEqual(second_result.commands, [])
        self.assertEqual(second_result.reason, "no_accounts_off_cooldown")
        self.assertFalse(second_result.capacity_noop)

    def test_failed_publish_does_not_start_account_cooldown(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            )
        )
        publish_attempts = 0

        def publish(command, key):
            nonlocal publish_attempts
            publish_attempts += 1
            if publish_attempts == 1:
                raise RuntimeError("publisher unavailable")

        settings = fast_trading_settings()
        settings["minimum_cycle_seconds_by_medium"]["API"] = 10.0
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=publish,
            fast_trading_settings=settings,
        )

        with self.assertRaisesRegex(RuntimeError, "publisher unavailable"):
            manager.run_cycle(strategy_command())
        retry_result = manager.run_cycle(strategy_command())

        self.assertEqual(len(retry_result.commands), 1)
        self.assertEqual(publish_attempts, 2)

    def test_account_is_reserved_while_publish_is_in_flight(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            )
        )
        publish_started = threading.Event()
        release_publish = threading.Event()
        first_result = []

        def publish(command, key):
            publish_started.set()
            if not release_publish.wait(1.0):
                raise RuntimeError("test publisher timed out")

        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=publish,
            fast_trading_settings=fast_trading_settings(),
        )
        worker = threading.Thread(
            target=lambda: first_result.append(manager.run_cycle(strategy_command())),
        )

        worker.start()
        self.assertTrue(publish_started.wait(1.0))
        concurrent_result = manager.run_cycle(strategy_command())
        release_publish.set()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(first_result[0].commands), 1)
        self.assertEqual(concurrent_result.commands, [])
        self.assertEqual(concurrent_result.reason, "no_accounts_off_cooldown")

    def test_account_cooldown_is_shared_across_buy_and_sell_sessions(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[BookLevel(price=10, quantity=200)],
                asks=[BookLevel(price=10, quantity=200)],
            )
        )
        settings = fast_trading_settings()
        settings["minimum_cycle_seconds_by_medium"]["API"] = 10.0
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            account_snapshots_provider=lambda: snapshots(
                A={
                    "cash_by_currency": {"USD": 2000},
                    "positions": [("AAPL", 200)],
                }
            ),
            publish_command=lambda command, key: None,
            fast_trading_settings=settings,
        )

        buy_result = manager.run_cycle(strategy_command())
        sell_command = {
            **strategy_command(),
            "trading_mode": "F",
        }
        sell_result = manager.run_cycle(sell_command)

        self.assertEqual(len(buy_result.commands), 1)
        self.assertEqual(sell_result.reason, "no_accounts_off_cooldown")
        self.assertEqual(sell_result.commands, [])

    def test_manager_uses_configured_book_depth_for_each_aggression_level(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[],
            asks=[
                BookLevel(price=6, quantity=100),
                BookLevel(price=7, quantity=100),
                BookLevel(price=8, quantity=100),
                BookLevel(price=9, quantity=100),
                BookLevel(price=10, quantity=100),
                BookLevel(price=11, quantity=1000),
            ],
        )

        for aggression_level, expected_quantity in ((1, 200), (2, 300), (3, 500)):
            with self.subTest(aggression_level=aggression_level):
                store = MarketDataStore()
                store.upsert_book(book)
                manager = FastTradingStrategyManager(
                    market_data_store=store,
                    account_metas_provider=lambda: metas("A"),
                    publish_command=lambda command, key: None,
                    fast_trading_settings=fast_trading_settings(),
                )

                result = manager.run_cycle(strategy_command(aggression_level=aggression_level))

                self.assertEqual(result.total_resting_qty, expected_quantity)

    def test_strategy_cycles_at_configured_aggression_interval_even_for_skips(self):
        store = RecordingMarketDataStore()
        settings = fast_trading_settings(cycle_seconds=0.04)
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=lambda command, key: None,
            fast_trading_settings=settings,
        )

        try:
            key = manager.start_strategy(strategy_command(aggression_level=2))
            deadline = time.monotonic() + 1.0
            while len(store.get_book_times) < 3 and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertGreaterEqual(len(store.get_book_times), 3)
            self.assertIn(key, manager._strategies)
            gaps = [
                later - earlier
                for earlier, later in zip(store.get_book_times[:2], store.get_book_times[1:3])
            ]
            self.assertTrue(all(gap >= 0.025 for gap in gaps), gaps)
        finally:
            manager.stop_all()

    def test_ten_capacity_noops_auto_stop_and_commands_reset_the_counter(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            )
        )
        provider_calls = 0

        def provide_snapshots():
            nonlocal provider_calls
            provider_calls += 1
            cash = 2000 if provider_calls == 10 else 1990
            return snapshots(A={"cash_by_currency": {"USD": cash}})

        published = []
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            account_snapshots_provider=provide_snapshots,
            publish_command=lambda command, key: published.append((key, command)),
            fast_trading_settings=fast_trading_settings(cycle_seconds=0.01),
        )

        try:
            key = manager.start_strategy(strategy_command())
            deadline = time.monotonic() + 2.0
            while key in manager._strategies and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertNotIn(key, manager._strategies)
            self.assertGreaterEqual(provider_calls, 20)
            self.assertEqual(len(published), 1)
        finally:
            manager.stop_all()

    def test_non_capacity_skips_do_not_auto_stop(self):
        store = RecordingMarketDataStore()
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=lambda command, key: None,
            fast_trading_settings=fast_trading_settings(cycle_seconds=0.01),
        )

        try:
            key = manager.start_strategy(strategy_command())
            deadline = time.monotonic() + 1.0
            while len(store.get_book_times) < 12 and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertGreaterEqual(len(store.get_book_times), 12)
            self.assertIn(key, manager._strategies)
        finally:
            manager.stop_all()

    def test_missing_snapshots_do_not_auto_stop(self):
        store = RecordingMarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[],
                asks=[BookLevel(price=10, quantity=200)],
            )
        )
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            account_snapshots_provider=lambda: {},
            publish_command=lambda command, key: None,
            fast_trading_settings=fast_trading_settings(cycle_seconds=0.01),
        )

        try:
            key = manager.start_strategy(strategy_command())
            deadline = time.monotonic() + 1.0
            while len(store.get_book_times) < 12 and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertGreaterEqual(len(store.get_book_times), 12)
            self.assertIn(key, manager._strategies)
        finally:
            manager.stop_all()


if __name__ == "__main__":
    unittest.main()
