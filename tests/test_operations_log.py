import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.fast_trading import FastTradingStrategyManager
from trading_ui.services.market_data import MarketDataStore
from trading_ui.services.operations_log import OperationsLog


def snapshots(**snapshot_by_account_id):
    return {
        account_id: AccountSnapshot(
            account_id=account_id,
            cash=float(data.get("cash", 0.0)),
            account_num_id=data.get("account_num_id"),
            cash_by_currency=dict(data.get("cash_by_currency", {})),
            positions=[
                Position(symbol=str(symbol), qty=float(qty), avg_price=avg_price)
                for symbol, qty, avg_price in data.get("positions", [])
            ],
            ts=data.get("ts"),
            trading_enabled=bool(data.get("trading_enabled", False)),
        )
        for account_id, data in snapshot_by_account_id.items()
    }


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


def read_json_events(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)["events"]


class OperationsLogTests(unittest.TestCase):
    def test_records_pretty_algo_start_event_with_holdings(self):
        started_at = datetime(2026, 7, 8, 1, 2, 3, tzinfo=timezone.utc)
        account_snapshots = snapshots(
            A={
                "account_num_id": 1,
                "cash": 1000,
                "cash_by_currency": {"USD": 1000, "HKD": 50},
                "positions": [("AAPL", 10, 123.45)],
                "ts": "2026-07-08T01:02:00Z",
                "trading_enabled": True,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            operations_log = OperationsLog(
                log_dir=tmp,
                server_started_at=started_at,
                snapshots_provider=lambda: (account_snapshots, "unit-test"),
            )
            operations_log.record_algo_started(
                {
                    "command_id": "algo-start-1",
                    "trading_mode": "F",
                    "symbol": "AAPL",
                    "end_time_et": "2099-01-01T00:00:00Z",
                },
                strategy_key="F:AAPL",
            )

            self.assertEqual(operations_log.path.name, "operations_20260708T010203Z.log")
            content = operations_log.path.read_text(encoding="utf-8")
            self.assertIn('\n      "event": "algo_trading_started"', content)

            events = read_json_events(operations_log.path)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["ui_server_started_at"], "2026-07-08T01:02:03Z")
            self.assertEqual(event["session"]["command_id"], "algo-start-1")
            self.assertEqual(event["holdings"]["source"], "unit-test")
            self.assertEqual(event["holdings"]["accounts"][0]["cash_by_currency"]["USD"], 1000)
            self.assertEqual(event["holdings"]["accounts"][0]["securities"][0]["symbol"], "AAPL")

    def test_fast_trading_manager_records_start_and_end_holdings(self):
        started_at = datetime(2026, 7, 8, 1, 2, 3, tzinfo=timezone.utc)
        provider_results = [
            snapshots(A={"cash": 1000, "cash_by_currency": {"USD": 1000}, "positions": [("AAPL", 10, 100)]}),
            snapshots(A={"cash": 900, "cash_by_currency": {"USD": 900}, "positions": [("AAPL", 9, 100)]}),
        ]
        provider_calls = []

        def provider():
            idx = min(len(provider_calls), len(provider_results) - 1)
            provider_calls.append(idx)
            return provider_results[idx], "unit-test"

        with tempfile.TemporaryDirectory() as tmp:
            operations_log = OperationsLog(
                log_dir=tmp,
                server_started_at=started_at,
                snapshots_provider=provider,
            )
            manager = FastTradingStrategyManager(
                market_data_store=MarketDataStore(),
                account_metas_provider=lambda: metas("A"),
                publish_command=lambda cmd, key: None,
                operations_log=operations_log,
                fast_trading_settings={
                    "aggression_levels": {
                        1: {"book_levels": 2, "cycle_seconds": 0.1},
                        2: {"book_levels": 3, "cycle_seconds": 0.1},
                        3: {"book_levels": 5, "cycle_seconds": 0.1},
                    },
                    "minimum_cycle_seconds_by_medium": {
                        "API": 3,
                        "WEB": 30,
                        "WINDOWS": 40,
                        "EMULATOR": 40,
                    },
                },
            )
            command = {
                "command_id": "algo-start-2",
                "trading_mode": "F",
                "symbol": "AAPL",
                "fast_trading_price_limit": 100,
                "fast_trading_account_ids": ["A"],
                "fast_trading_aggression_level": 1,
            }

            try:
                manager.start_strategy(command)
                manager.stop_matching("F")
            finally:
                manager.stop_all()

            events = read_json_events(operations_log.path)
            self.assertEqual([event["event"] for event in events], ["algo_trading_started", "algo_trading_ended"])
            self.assertEqual(events[0]["session"]["fast_trading_price_limit"], 100)
            self.assertEqual(events[0]["session"]["fast_trading_account_ids"], ["A"])
            self.assertEqual(events[0]["session"]["fast_trading_aggression_level"], 1)
            self.assertEqual(events[0]["holdings"]["accounts"][0]["cash"], 1000)
            self.assertEqual(events[1]["holdings"]["accounts"][0]["cash"], 900)
            self.assertEqual(events[1]["details"]["reason"], "Manual stop")


if __name__ == "__main__":
    unittest.main()
