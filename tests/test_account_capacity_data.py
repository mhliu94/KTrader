import math
import threading
import unittest

from trading_server.tiger import TigerBroker
from trading_ui.models import AccountSnapshot, Position
from trading_ui.services.fallback import parse_snapshot_obj
from trading_ui.services.operations_log import OperationsLog


class AccountCapacityModelTests(unittest.TestCase):
    def test_snapshot_to_dict_includes_available_cash_and_position_quantity(self):
        snapshot = AccountSnapshot(
            account_id="A",
            cash=1_000,
            cash_by_currency={"USD": 1_000},
            available_cash_by_currency={"USD": 625},
            positions=[
                Position(
                    symbol="AAPL",
                    qty=150,
                    avg_price=123.45,
                    available_qty=120,
                )
            ],
        )

        payload = snapshot.to_dict()

        self.assertEqual(payload["cash_by_currency"], {"USD": 1_000})
        self.assertEqual(payload["available_cash_by_currency"], {"USD": 625})
        self.assertEqual(payload["positions"][0]["qty"], 150)
        self.assertEqual(payload["positions"][0]["available_qty"], 120)

    def test_fallback_parser_reads_capacity_fields_and_preserves_missing_values(self):
        snapshot = parse_snapshot_obj(
            {
                "account_id": "A",
                "cash": 1_000,
                "cash_by_currency": {"USD": 1_000},
                "available_cash_by_currency": {"usd": 625, "hkd": math.inf},
                "positions": [
                    {"symbol": "AAPL", "qty": 150, "available_qty": 120},
                    {"symbol": "MSFT", "qty": 50, "salable_qty": 40},
                    {"symbol": "NVDA", "qty": 25, "available_qty": math.nan},
                ],
            }
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.cash_by_currency, {"USD": 1_000})
        self.assertEqual(snapshot.available_cash_by_currency, {"USD": 625})
        self.assertEqual(
            [(position.symbol, position.qty, position.available_qty) for position in snapshot.positions],
            [("AAPL", 150, 120), ("MSFT", 50, 40), ("NVDA", 25, None)],
        )

        legacy = parse_snapshot_obj({"account_id": "LEGACY", "cash": 100, "positions": []})
        self.assertIsNotNone(legacy)
        assert legacy is not None
        self.assertEqual(legacy.available_cash_by_currency, {})

    def test_operations_log_holdings_include_balance_and_available_capacity(self):
        holdings = OperationsLog._account_holdings(
            AccountSnapshot(
                account_id="A",
                cash=1_000,
                cash_by_currency={"USD": 1_000},
                available_cash_by_currency={"USD": 625},
                positions=[Position("AAPL", 150, 123.45, 120)],
            )
        )

        self.assertEqual(holdings["cash_by_currency"], {"USD": 1_000})
        self.assertEqual(holdings["available_cash_by_currency"], {"USD": 625})
        self.assertEqual(holdings["securities"][0]["qty"], 150)
        self.assertEqual(holdings["securities"][0]["available_qty"], 120)


class FakeTigerSnapshotClient:
    def __init__(self):
        self.prime_calls = []
        self.portfolio = {
            "segments": {
                "S": {
                    "currency_assets": {
                        "USD": {
                            "cash_balance": 1_000,
                            "cash_available_for_trade": 625,
                        },
                        "HKD": {"cash_balance": 250},
                        "EUR": {
                            "cash_balance": math.inf,
                            "cash_available_for_trade": math.nan,
                        },
                    }
                }
            }
        }
        self.positions = [
            {
                "contract": {"symbol": "aapl"},
                "position_qty": 150,
                "salable_qty": 120,
                "average_cost": 123.45,
            },
            {
                "contract": {"symbol": "msft"},
                "position_qty": 50,
                "saleable": math.inf,
                "average_cost": math.nan,
            },
        ]

    def get_prime_assets(self, **kwargs):
        self.prime_calls.append(kwargs)
        return [self.portfolio]

    def get_positions(self, **kwargs):
        return list(self.positions)


class TigerAccountCapacitySnapshotTests(unittest.TestCase):
    def test_snapshot_uses_available_fields_with_finite_balance_fallbacks(self):
        client = FakeTigerSnapshotClient()
        broker = TigerBroker.__new__(TigerBroker)
        broker._trade_client = client
        broker.cash_currencies = ["USD", "HKD"]
        broker.account_num_map = {"UI-A": 7}
        broker._trading_state_lock = threading.RLock()
        broker._trading_enabled_by_account = {"UI-A": True}
        broker._trading_enabled_default = False

        snapshot = broker.account_snapshot("UI-A", "TIGER-123")

        self.assertEqual(
            client.prime_calls,
            [{"account": "TIGER-123", "base_currency": "USD"}],
        )
        self.assertEqual(snapshot["cash"], 1_000)
        self.assertEqual(snapshot["cash_by_currency"], {"HKD": 250, "USD": 1_000})
        self.assertEqual(
            snapshot["available_cash_by_currency"],
            {"HKD": 250, "USD": 625},
        )
        self.assertEqual(
            snapshot["positions"],
            [
                {
                    "symbol": "AAPL",
                    "qty": 150,
                    "avg_price": 123.45,
                    "available_qty": 120,
                },
                {
                    "symbol": "MSFT",
                    "qty": 50,
                    "avg_price": None,
                    "available_qty": 50,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
