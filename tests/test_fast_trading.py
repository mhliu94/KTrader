import unittest
from datetime import datetime, timezone

from trading_ui.models import AccountMeta
from trading_ui.services.fast_trading import (
    FastTradingStrategyManager,
    build_fast_trading_cycle_commands,
    normalize_fast_trading_end_time,
)
from trading_ui.services.market_data import BookLevel, MarketDataStore, OrderBookSnapshot


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


class FastTradingCycleTests(unittest.TestCase):
    def test_mode_f_combines_bid_intervals_and_uses_least_aggressive_group(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[
                BookLevel(price=105, quantity=300),
                BookLevel(price=95, quantity=200),
                BookLevel(price=85, quantity=400),
            ],
            asks=[],
        )
        groups = [
            {
                "group_id": 1,
                "price_limit": 80,
                "accounts": [
                    {"account_id": "G", "allocation_pct": 20},
                    {"account_id": "H", "allocation_pct": 30},
                    {"account_id": "I", "allocation_pct": 40},
                ],
            },
            {"group_id": 2, "price_limit": 90, "accounts": [{"account_id": "J", "allocation_pct": 100}]},
            {"group_id": 3, "price_limit": 100, "accounts": [{"account_id": "K", "allocation_pct": 100}]},
        ]

        result = build_fast_trading_cycle_commands(
            trading_mode="F",
            symbol="AAPL",
            book=book,
            fast_trading_groups=groups,
            account_metas=metas("G", "H", "I", "J", "K"),
            cycle_id="cycle-1",
        )

        self.assertEqual(result.total_resting_qty, 900)
        self.assertEqual(result.execution_group_id, 1)
        self.assertEqual(result.limit_price, 80)
        self.assertEqual([cmd["account_id"] for cmd in result.commands], ["G", "H", "I"])
        self.assertEqual([cmd["qty_shares"] for cmd in result.commands], [180, 270, 360])
        self.assertTrue(all(cmd["type"] == "LIMIT_ORDER_FOK" for cmd in result.commands))
        self.assertTrue(all(cmd["side"] == "SELL" for cmd in result.commands))

    def test_mode_e_combines_ask_intervals_and_buys_at_upper_bound(self):
        book = OrderBookSnapshot(
            symbol="MSFT",
            bids=[],
            asks=[
                BookLevel(price=101, quantity=600),
                BookLevel(price=108, quantity=500),
            ],
        )
        groups = [
            {"group_id": 1, "price_limit": 105, "accounts": [{"account_id": "A", "allocation_pct": 100}]},
            {"group_id": 2, "price_limit": 110, "accounts": [{"account_id": "B", "allocation_pct": 50}]},
        ]

        result = build_fast_trading_cycle_commands(
            trading_mode="E",
            symbol="MSFT",
            book=book,
            fast_trading_groups=groups,
            account_metas=metas("A", "B"),
            cycle_id="cycle-2",
        )

        self.assertEqual(result.total_resting_qty, 1100)
        self.assertEqual(result.execution_group_id, 2)
        self.assertEqual(result.limit_price, 110)
        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0]["account_id"], "B")
        self.assertEqual(result.commands[0]["side"], "BUY")
        self.assertEqual(result.commands[0]["qty_shares"], 550)

    def test_skips_when_combined_quantity_is_below_minimum(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[BookLevel(price=105, quantity=99)],
            asks=[],
        )
        groups = [
            {"group_id": 1, "price_limit": 100, "accounts": [{"account_id": "A", "allocation_pct": 100}]},
        ]

        result = build_fast_trading_cycle_commands(
            trading_mode="F",
            symbol="AAPL",
            book=book,
            fast_trading_groups=groups,
            account_metas=metas("A"),
            cycle_id="cycle-3",
        )

        self.assertEqual(result.total_resting_qty, 99)
        self.assertEqual(result.commands, [])
        self.assertEqual(result.reason, "total_below_minimum")

    def test_floors_allocated_share_quantities(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[BookLevel(price=105, quantity=101)],
            asks=[],
        )
        groups = [
            {"group_id": 1, "price_limit": 100, "accounts": [{"account_id": "A", "allocation_pct": 33.3}]},
        ]

        result = build_fast_trading_cycle_commands(
            trading_mode="F",
            symbol="AAPL",
            book=book,
            fast_trading_groups=groups,
            account_metas=metas("A"),
            cycle_id="cycle-4",
        )

        self.assertEqual(result.commands[0]["qty_shares"], 33)

    def test_wait_uses_slowest_trading_medium_in_execution_group(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[BookLevel(price=105, quantity=1000)],
            asks=[],
        )
        groups = [
            {
                "group_id": 1,
                "price_limit": 100,
                "accounts": [
                    {"account_id": "API_ACCOUNT", "allocation_pct": 10},
                    {"account_id": "WEB_ACCOUNT", "allocation_pct": 10},
                    {"account_id": "WINDOWS_ACCOUNT", "allocation_pct": 10},
                    {"account_id": "EMULATOR_ACCOUNT", "allocation_pct": 10},
                ],
            },
        ]

        result = build_fast_trading_cycle_commands(
            trading_mode="F",
            symbol="AAPL",
            book=book,
            fast_trading_groups=groups,
            account_metas=metas_by_medium(
                API_ACCOUNT="API",
                WEB_ACCOUNT="WEB",
                WINDOWS_ACCOUNT="WINDOWS",
                EMULATOR_ACCOUNT="EMULATOR",
            ),
            cycle_id="cycle-5",
        )

        self.assertEqual(result.wait_seconds, 35.0)
        self.assertTrue(all(cmd["fast_trading_wait_seconds"] == 35.0 for cmd in result.commands))

    def test_wait_for_api_only_execution_group_is_five_seconds(self):
        book = OrderBookSnapshot(
            symbol="AAPL",
            bids=[BookLevel(price=105, quantity=1000)],
            asks=[],
        )
        groups = [
            {"group_id": 1, "price_limit": 100, "accounts": [{"account_id": "A", "allocation_pct": 100}]},
        ]

        result = build_fast_trading_cycle_commands(
            trading_mode="F",
            symbol="AAPL",
            book=book,
            fast_trading_groups=groups,
            account_metas=metas("A"),
            cycle_id="cycle-6",
        )

        self.assertEqual(result.wait_seconds, 5.0)

    def test_past_stop_time_becomes_manual_termination_time(self):
        normalized = normalize_fast_trading_end_time(
            "2024-01-01T00:00:00Z",
            now=datetime(2026, 7, 6, tzinfo=timezone.utc),
        )

        self.assertEqual(normalized, "2099-01-01T00:00:00Z")

    def test_test_mode_logs_without_publishing(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[BookLevel(price=105, quantity=1000)],
                asks=[],
            )
        )
        published = []
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=lambda cmd, key: published.append((key, cmd)),
        )

        command = {
            "trading_mode": "F",
            "symbol": "AAPL",
            "fast_trading_test_mode": True,
            "fast_trading_groups": [
                {"group_id": 1, "price_limit": 100, "accounts": [{"account_id": "A", "allocation_pct": 100}]},
            ],
        }

        with self.assertLogs("trading-ui.fast-trading", level="INFO") as logs:
            result = manager.run_cycle(command)

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(published, [])
        self.assertTrue(result.commands[0]["fast_trading_test_mode"])
        self.assertIn("TEST MODE", "\n".join(logs.output))
        self.assertIn("without Kafka publish", "\n".join(logs.output))

    def test_normal_mode_logs_and_publishes(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[BookLevel(price=105, quantity=1000)],
                asks=[],
            )
        )
        published = []
        manager = FastTradingStrategyManager(
            market_data_store=store,
            account_metas_provider=lambda: metas("A"),
            publish_command=lambda cmd, key: published.append((key, cmd)),
        )

        command = {
            "trading_mode": "F",
            "symbol": "AAPL",
            "fast_trading_groups": [
                {"group_id": 1, "price_limit": 100, "accounts": [{"account_id": "A", "allocation_pct": 100}]},
            ],
        }

        with self.assertLogs("trading-ui.fast-trading", level="INFO") as logs:
            result = manager.run_cycle(command)

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][0], "A")
        self.assertNotIn("fast_trading_test_mode", published[0][1])
        self.assertIn("Publishing fast trading command", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
