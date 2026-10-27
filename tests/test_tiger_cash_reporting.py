"""Native currency balances must not include converted account totals."""

import json
import math
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from trading_server.tiger import AccountReporter, TigerBroker
from trading_ui.kafka.consumer_account_details import parse_account_message
from trading_ui.models import AccountMeta
from trading_ui.templates import _aggregate_portfolio


def broker_with_assets(native_balances, *, global_assets=None):
    broker = TigerBroker.__new__(TigerBroker)
    broker.cash_currencies = ["USD", "HKD", "AUD", "NZD"]
    broker.account_num_map = {"TIGER-A": 1}
    broker._trading_state_lock = threading.RLock()
    broker._trading_enabled_by_account = {"TIGER-A": False}
    broker._trading_enabled_default = False
    broker._trade_client = Mock()
    broker._trade_client.get_positions.return_value = []

    def prime_assets(*, account, base_currency):
        # Tiger's segment totals express the same holdings in different units.
        converted_total = {"USD": 1000, "HKD": 7800, "AUD": 1500, "NZD": 1700}[base_currency]
        return [{"segments": {"S": {
            "currency": base_currency,
            "cash_balance": converted_total,
            "cash_available_for_trade": converted_total * 2,
            "currency_assets": native_balances,
        }}}]

    broker._trade_client.get_prime_assets.side_effect = prime_assets
    broker._trade_client.get_assets.return_value = [global_assets or {
        "summary": {"cash": 1000, "available_funds": 2000},
        "market_values": {},
    }]
    return broker


class TigerCashReportingTests(unittest.TestCase):
    def test_published_snapshot_does_not_add_aud_nzd_equivalents_to_ui_totals(self):
        broker = broker_with_assets({
            "USD": {"cash_balance": 1000, "cash_available_for_trade": 625},
            "HKD": {"cash_balance": 0},
        })
        reporter = AccountReporter.__new__(AccountReporter)
        reporter._broker = broker
        reporter._producer = Mock()
        reporter._targets = [("TIGER-A", "BROKER-A")]
        reporter._publish_lock = threading.RLock()
        reporter._topic = "account-details"

        reporter.publish_all_now("test")

        reporter._producer.produce.assert_called_once()
        published = reporter._producer.produce.call_args.kwargs
        self.assertEqual(published["key"], b"TIGER-A")
        payload = json.loads(published["value"])
        self.assertEqual(payload["cash"], 1000)
        self.assertEqual(payload["cash_by_currency"], {"HKD": 0, "USD": 1000})
        self.assertEqual(payload["available_cash_by_currency"], {"HKD": 0, "USD": 625})
        snapshot = parse_account_message(published["value"])
        self.assertIsNotNone(snapshot)
        portfolio = _aggregate_portfolio(
            {"TIGER-A": snapshot},
            {"TIGER-A": AccountMeta(id="TIGER-A", num_id=1, broker="Tiger")},
        )
        self.assertEqual(portfolio["cash"], {"HKD": 0, "USD": 1000})

    def test_real_aud_nzd_balances_and_negative_cash_are_preserved(self):
        broker = broker_with_assets({
            "USD": {"cash_balance": 1000, "cash_available_for_trade": 625},
            "HKD": {"cash_balance": 0},
            "AUD": {"cash_balance": 40, "cash_available_for_trade": 25},
            "NZD": {"cash_balance": -15, "cash_available_for_trade": -15},
            "SGD": {"cash_balance": 12},
        })

        snapshot = broker.account_snapshot("TIGER-A", "BROKER-A")

        self.assertEqual(snapshot["cash_by_currency"],
                         {"USD": 1000, "HKD": 0, "AUD": 40, "NZD": -15, "SGD": 12})
        self.assertEqual(snapshot["available_cash_by_currency"]["AUD"], 25)
        self.assertEqual(snapshot["available_cash_by_currency"]["USD"], 625)
        broker._trade_client.get_prime_assets.assert_called_once_with(
            account="BROKER-A", base_currency="USD")

    def test_missing_native_usd_does_not_become_converted_aud_total(self):
        broker = broker_with_assets({"AUD": {"cash_balance": 40}})

        snapshot = broker.account_snapshot("TIGER-A", "BROKER-A")

        self.assertEqual(snapshot["cash"], 0)
        self.assertEqual(snapshot["cash_by_currency"], {"AUD": 40})
        self.assertEqual(snapshot["available_cash_by_currency"], {"AUD": 40})

    def test_prime_helper_uses_native_sdk_objects_only(self):
        broker = broker_with_assets({})
        portfolio = SimpleNamespace(segments={"S": SimpleNamespace(
            currency="AUD", cash_balance=1500, cash_available_for_trade=3000,
            currency_assets={"USD": SimpleNamespace(cash_balance=1000,
                                                     cash_available_for_trade=625)},
        )})

        self.assertEqual(broker._cash_pair_from_prime_assets(portfolio, "usd"), (1000, 625))
        self.assertEqual(broker._cash_pair_from_prime_assets(portfolio, "AUD"), (None, None))

    def test_invalid_native_balance_is_not_replaced_by_segment_total(self):
        for invalid in (None, math.inf, math.nan):
            with self.subTest(invalid=invalid):
                broker = broker_with_assets({"USD": {"cash_balance": 1000},
                                             "AUD": {"cash_balance": invalid}})
                self.assertEqual(broker._query_cash_by_currency("BROKER-A"), {"USD": 1000})

    def test_global_fallback_uses_explicit_currency_cash(self):
        broker = broker_with_assets({}, global_assets={
            "summary": {"cash": 9000, "available_funds": 18000},
            "market_values": {"USD": {"cash_balance": 1000},
                              "AUD": {"cash_balance": 40},
                              "NZD": {"cash_balance": 0}},
        })
        broker._trade_client.get_prime_assets.side_effect = RuntimeError("Not a prime account")

        snapshot = broker.account_snapshot("TIGER-A", "BROKER-A")

        self.assertEqual(snapshot["cash_by_currency"], {"USD": 1000, "AUD": 40, "NZD": 0})
        self.assertEqual(snapshot["available_cash_by_currency"], snapshot["cash_by_currency"])

    def test_global_summary_and_margin_funds_are_not_native_cash(self):
        broker = broker_with_assets({})
        for summary in ({"cash": 1000}, {"cash_balance": 1000}, {"available_funds": 2000}):
            with self.subTest(summary=summary):
                portfolio = {"summary": summary, "market_values": {"AUD": {"cash_balance": math.inf}}}
                self.assertIsNone(broker._cash_from_global_assets(portfolio, "AUD"))
                self.assertIsNone(broker._cash_from_global_assets(portfolio, "USD"))

    def test_missing_native_balances_prevents_publishing_a_fabricated_snapshot(self):
        broker = broker_with_assets({})

        with self.assertRaisesRegex(RuntimeError, "Unable to find cash balances"):
            broker.account_snapshot("TIGER-A", "BROKER-A")

    def test_native_available_cash_survives_global_balance_fallback(self):
        broker = broker_with_assets(
            {"USD": {"cash_available_for_trade": 625}},
            global_assets={"market_values": {"USD": {"cash_balance": 1000}}},
        )

        snapshot = broker.account_snapshot("TIGER-A", "BROKER-A")

        self.assertEqual(snapshot["cash_by_currency"], {"USD": 1000})
        self.assertEqual(snapshot["available_cash_by_currency"], {"USD": 625})


if __name__ == "__main__":
    unittest.main()
