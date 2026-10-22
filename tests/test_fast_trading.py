import unittest
import threading
import time
from unittest.mock import Mock

from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.fast_trading import (
    FastTradingStrategyManager,
    _FastStrategy,
    build_fast_trading_cycle_commands,
)
from trading_ui.services.market_data import BookLevel, MarketDataStore, OrderBookSnapshot


DEFAULT_MINIMUM_CYCLE_SECONDS = {
    "API": 3.0,
    "WEB": 30.0,
    "WINDOWS": 40.0,
    "EMULATOR": 40.0,
}


def fast_trading_settings():
    return {
        "aggression_levels": {
            1: {"book_levels": 2, "cycle_seconds": 30},
            2: {"book_levels": 3, "cycle_seconds": 20},
            3: {"book_levels": 5, "cycle_seconds": 10},
        },
        "minimum_cycle_seconds_by_medium": {
            "API": 0.001,
            "WEB": 0.001,
            "WINDOWS": 0.001,
            "EMULATOR": 0.001,
        },
    }


def strategy_command(*, book_levels=2, target_percentage=100, cycle_seconds=5):
    return {
        "command_id": "fast-session-test",
        "trading_mode": "E",
        "symbol": "AAPL",
        "fast_trading_price_limit": 10,
        "fast_trading_account_ids": ["A"],
        "fast_trading_book_levels": book_levels,
        "fast_trading_target_percentage": target_percentage,
        "fast_trading_cycle_seconds": cycle_seconds,
    }


class RecordingMarketDataStore(MarketDataStore):
    def __init__(self):
        super().__init__()
        self.get_book_times = []

    def get_book(self, symbol, depth_limit=20):
        self.get_book_times.append(time.monotonic())
        return super().get_book(symbol, depth_limit=depth_limit)


class SimulatedCycleClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def run_strategy(self, manager, command, *, max_cycles=25):
        def wait(seconds):
            self.waits.append(seconds)
            self.now += seconds
            return len(self.waits) >= max_cycles

        stop_event = Mock(spec=threading.Event)
        stop_event.is_set.return_value = False
        stop_event.wait.side_effect = wait
        key = manager._strategy_key(command["trading_mode"], command["symbol"])
        strategy = _FastStrategy(key=key, command=command, stop_event=stop_event)
        manager._strategies[key] = strategy
        manager._run_strategy(strategy)
        return strategy


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
    target_percentage=100,
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
        target_percentage=target_percentage,
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

    def test_percentage_targets_only_selected_levels_within_price_limit(self):
        for mode, prices in (("E", (8, 9, 11, 12)), ("F", (12, 11, 9, 8))):
            with self.subTest(mode=mode):
                levels = [
                    BookLevel(price=price, quantity=quantity)
                    for price, quantity in zip(prices, (501, 508, 1000, 2000))
                ]
                result = build_cycle(
                    mode=mode,
                    book=OrderBookSnapshot(
                        symbol="AAPL",
                        bids=levels if mode == "F" else [],
                        asks=levels if mode == "E" else [],
                    ),
                    price_limit=10,
                    account_ids=["A", "B", "C"],
                    book_levels=3,
                    target_percentage=30,
                )

                self.assertEqual(result.total_resting_qty, 1009)
                self.assertEqual(result.target_quantity, 302)
                self.assertEqual(result.limit_price, 10)
                self.assertEqual(result.allocated_quantity, 300)
                self.assertEqual([cmd["qty_shares"] for cmd in result.commands], [100] * 3)
                for command in result.commands:
                    self.assertEqual(command["fast_trading_target_percentage"], 30)
                    self.assertEqual(command["fast_trading_target_qty"], 302)
                    self.assertEqual(command["type"], "LIMIT_ORDER_FOK")

    def test_target_percentage_is_applied_before_rounding_whole_shares(self):
        result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL", bids=[], asks=[BookLevel(price=10, quantity=333.9)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=1,
            target_percentage=30,
        )

        self.assertEqual(result.target_quantity, 100)
        self.assertEqual(result.commands[0]["qty_shares"], 100)

    def test_percentage_target_keeps_minimum_share_rule(self):
        result = build_cycle(
            mode="E",
            book=OrderBookSnapshot(
                symbol="AAPL", bids=[], asks=[BookLevel(price=10, quantity=999)],
            ),
            price_limit=10,
            account_ids=["A"],
            book_levels=1,
            target_percentage=10,
        )

        self.assertEqual(result.target_quantity, 99)
        self.assertEqual(result.commands, [])
        self.assertEqual(result.reason, "per_account_quantity_below_minimum")
        self.assertFalse(result.capacity_noop)

    def test_capacity_is_evaluated_against_percentage_target(self):
        for mode in ("E", "F"):
            with self.subTest(mode=mode):
                levels = [BookLevel(price=10, quantity=1000)]
                result = build_cycle(
                    mode=mode,
                    book=OrderBookSnapshot(
                        symbol="AAPL",
                        bids=levels if mode == "F" else [],
                        asks=levels if mode == "E" else [],
                    ),
                    price_limit=10,
                    account_ids=["A"],
                    book_levels=1,
                    target_percentage=20,
                    account_snapshots=snapshots(
                        A={"cash_by_currency": {"USD": 2000}, "positions": [("AAPL", 200)]},
                    ),
                )

                self.assertEqual(result.target_quantity, 200)
                self.assertEqual(result.commands[0]["qty_shares"], 200)
                self.assertFalse(result.capacity_noop)

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
    def test_stop_all_stops_both_modes_all_symbols_and_accounts_outside_current_config(self):
        publish = Mock()
        manager = FastTradingStrategyManager(
            market_data_store=MarketDataStore(),
            account_metas_provider=lambda: metas("A"),
            publish_command=publish,
        )
        self.addCleanup(manager.stop_all)
        # Keep worker cycles inert while exercising real worker shutdown.
        # Include accounts outside the current configuration to check scope.
        manager.run_cycle = Mock(return_value=Mock(commands=[], capacity_noop=False, reason="test cycle"))
        strategies = []
        for mode, symbol, account in (("E", "AAPL", "A"), ("F", "AAPL", "B"), ("E", "MSFT", "REMOVED")):
            command = {**strategy_command(), "trading_mode": mode, "symbol": symbol,
                       "fast_trading_account_ids": [account]}
            key = manager.start_strategy(command)
            strategies.append(manager._strategies[key])
        self.assertEqual(manager.stop_all(), 3)
        self.assertEqual(manager._strategies, {})
        self.assertTrue(all(strategy.stop_event.is_set() for strategy in strategies))
        self.assertTrue(all(not strategy.thread.is_alive() for strategy in strategies))
        self.assertEqual(manager.stop_all(), 0)
        publish.assert_not_called()

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

    def test_manager_uses_each_selected_book_depth_and_target_percentage(self):
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

        for book_levels in range(1, 6):
            with self.subTest(book_levels=book_levels):
                store = MarketDataStore()
                store.upsert_book(book)
                manager = FastTradingStrategyManager(
                    market_data_store=store,
                    account_metas_provider=lambda: metas("A"),
                    publish_command=lambda command, key: None,
                    fast_trading_settings=fast_trading_settings(),
                )

                result = manager.run_cycle(strategy_command(book_levels=book_levels))

                self.assertEqual(result.total_resting_qty, book_levels * 100)
                self.assertEqual(result.commands[0]["fast_trading_book_levels"], book_levels)
                self.assertNotIn("fast_trading_aggression_level", result.commands[0])

        for percentage in range(10, 101, 10):
            with self.subTest(percentage=percentage):
                store = MarketDataStore()
                store.upsert_book(OrderBookSnapshot(
                    symbol="AAPL", bids=[], asks=[BookLevel(price=10, quantity=1000)],
                ))
                manager = FastTradingStrategyManager(
                    market_data_store=store,
                    account_metas_provider=lambda: metas("A"),
                    publish_command=lambda command, key: None,
                )

                result = manager.run_cycle(strategy_command(target_percentage=percentage))

                self.assertEqual(result.commands[0]["qty_shares"], percentage * 10)
                self.assertEqual(result.commands[0]["fast_trading_target_percentage"], percentage)

    def test_manager_rejects_missing_or_invalid_controls_before_starting_or_publishing(self):
        publish = Mock()
        manager = FastTradingStrategyManager(
            market_data_store=MarketDataStore(),
            account_metas_provider=lambda: metas("A"),
            publish_command=publish,
        )
        invalid_values = {
            "fast_trading_book_levels": [None, "", 0, 6, 1.5, True],
            "fast_trading_target_percentage": [None, "", 0, 15, 110, True],
            "fast_trading_cycle_seconds": [None, "", 0, 1, 20, 120, True],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    command = strategy_command()
                    if value is None:
                        command.pop(field)
                        command["fast_trading_aggression_level"] = 1
                    else:
                        command[field] = value
                    with self.assertRaises(ValueError):
                        manager.start_strategy(command)
                    with self.assertRaises(ValueError):
                        manager.run_cycle(command)
                    self.assertEqual(manager._strategies, {})
        publish.assert_not_called()

    def test_test_mode_uses_selected_target_and_keeps_cooldown_without_publishing(self):
        store = MarketDataStore()
        store.upsert_book(OrderBookSnapshot(
            symbol="AAPL", bids=[], asks=[BookLevel(price=10, quantity=1000)],
        ))
        publish = Mock()
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=publish,
        )
        command = {**strategy_command(target_percentage=20, cycle_seconds=15), "fast_trading_test_mode": True}

        result = manager.run_cycle(command)
        next_result = manager.run_cycle(command)

        self.assertEqual(result.commands[0]["qty_shares"], 200)
        self.assertEqual(result.commands[0]["fast_trading_cycle_seconds"], 15)
        self.assertTrue(result.commands[0]["fast_trading_test_mode"])
        self.assertEqual(next_result.reason, "no_accounts_off_cooldown")
        publish.assert_not_called()

    def test_strategy_observes_each_minimum_interval_and_longer_processing_even_for_skips(self):
        for interval in (5, 10, 15, 30, 60):
            for processing_seconds in (2, 65):
                with self.subTest(interval=interval, processing_seconds=processing_seconds):
                    clock = SimulatedCycleClock()
                    store = RecordingMarketDataStore()
                    manager = FastTradingStrategyManager(
                        market_data_store=store,
                        account_metas_provider=lambda: metas("A"),
                        publish_command=lambda command, key: None,
                        fast_trading_settings=fast_trading_settings(),
                        monotonic=clock.monotonic,
                    )
                    cycle_starts = []
                    original_run_cycle = manager.run_cycle

                    def run_cycle(command):
                        cycle_starts.append(clock.now)
                        result = original_run_cycle(command)
                        clock.now += processing_seconds
                        return result

                    manager.run_cycle = run_cycle
                    strategy = clock.run_strategy(
                        manager, strategy_command(cycle_seconds=interval), max_cycles=3,
                    )

                    self.assertEqual(strategy.cycles, 3)
                    self.assertEqual(strategy.last_reason, "no data yet")
                    self.assertEqual(clock.waits, [max(0, interval - processing_seconds)] * 3)
                    self.assertEqual(cycle_starts, [0, max(interval, processing_seconds), max(interval, processing_seconds) * 2])

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
        clock = SimulatedCycleClock()
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            account_snapshots_provider=provide_snapshots,
            publish_command=lambda command, key: published.append((key, command)),
            fast_trading_settings=fast_trading_settings(),
            monotonic=clock.monotonic,
        )

        strategy = clock.run_strategy(manager, strategy_command())

        self.assertNotIn(strategy.key, manager._strategies)
        self.assertEqual(provider_calls, 20)
        self.assertEqual(len(published), 1)
        self.assertEqual(strategy.last_reason, "10 consecutive purchasing-power no-ops")

    def test_non_capacity_skips_do_not_auto_stop(self):
        store = RecordingMarketDataStore()
        clock = SimulatedCycleClock()
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=lambda command, key: None,
            fast_trading_settings=fast_trading_settings(),
            monotonic=clock.monotonic,
        )

        strategy = clock.run_strategy(manager, strategy_command(), max_cycles=12)

        self.assertEqual(len(store.get_book_times), 12)
        self.assertEqual(strategy.consecutive_capacity_noops, 0)
        self.assertEqual(strategy.last_reason, "no data yet")

    def test_missing_snapshots_do_not_auto_stop(self):
        store = RecordingMarketDataStore()
        clock = SimulatedCycleClock()
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
            fast_trading_settings=fast_trading_settings(),
            monotonic=clock.monotonic,
        )

        strategy = clock.run_strategy(manager, strategy_command(), max_cycles=12)

        self.assertEqual(len(store.get_book_times), 12)
        self.assertEqual(strategy.consecutive_capacity_noops, 0)
        self.assertEqual(strategy.last_reason, "account_snapshots_unavailable")


if __name__ == "__main__":
    unittest.main()
