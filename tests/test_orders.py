import unittest

from trading_ui.models import AccountMeta
from trading_ui.services.orders import (
    parse_fast_trading_settings,
    parse_fast_trading_test_mode,
    validate_algo_start_inputs,
    validate_limit_order_inputs,
    validate_order_inputs,
    validate_quick_order_inputs,
)


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


class QuickOrderValidationTests(unittest.TestCase):
    def test_floors_dollar_amount_to_whole_shares(self):
        cmd, err = validate_quick_order_inputs(
            account_id="A",
            symbol="AAPL",
            side="BUY",
            dollars_raw="10000",
            market_last=123.45,
            account_metas=metas("A"),
            symbols=["AAPL"],
            invalid_account="invalid account",
            invalid_symbol="invalid symbol",
            invalid_side="invalid side",
            dollars_positive="dollars positive",
            no_last_price="no last price",
            dollars_too_low="too low",
        )

        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["qty_shares"], 81)
        self.assertNotIn("notional_usd", cmd)

    def test_rejects_amount_below_one_share(self):
        cmd, err = validate_quick_order_inputs(
            account_id="A",
            symbol="AAPL",
            side="BUY",
            dollars_raw="10",
            market_last=123.45,
            account_metas=metas("A"),
            symbols=["AAPL"],
            invalid_account="invalid account",
            invalid_symbol="invalid symbol",
            invalid_side="invalid side",
            dollars_positive="dollars positive",
            no_last_price="no last price",
            dollars_too_low="too low",
        )

        self.assertIsNone(cmd)
        self.assertEqual(err, "too low")

    def test_rejects_non_finite_market_price(self):
        cmd, err = validate_quick_order_inputs(
            account_id="A",
            symbol="AAPL",
            side="BUY",
            dollars_raw="10000",
            market_last=float("nan"),
            account_metas=metas("A"),
            symbols=["AAPL"],
            invalid_account="invalid account",
            invalid_symbol="invalid symbol",
            invalid_side="invalid side",
            dollars_positive="dollars positive",
            no_last_price="no last price",
            dollars_too_low="too low",
        )

        self.assertIsNone(cmd)
        self.assertEqual(err, "no last price")

    def test_regular_market_order_does_not_ignore_malformed_shares(self):
        cmd, err = validate_order_inputs(
            account_id="A",
            symbol="AAPL",
            side="BUY",
            shares_raw="not-shares",
            dollars_raw="1000",
            account_metas=metas("A"),
            symbols=["AAPL"],
            invalid_account="invalid account",
            invalid_symbol="invalid symbol",
            invalid_side="invalid side",
            both_shares_and_dollars="both",
            neither_shares_nor_dollars="neither",
            shares_positive="shares positive",
            dollars_positive="dollars positive",
        )

        self.assertIsNone(cmd)
        self.assertEqual(err, "shares positive")

    def test_limit_order_rejects_non_finite_market_and_calculated_prices(self):
        common = {
            "account_id": "A",
            "symbol": "AAPL",
            "side": "BUY",
            "shares_raw": "100",
            "limit_price_raw": "",
            "through_market_pct_raw": "5",
            "account_metas": metas("A"),
            "symbols": ["AAPL"],
            "invalid_account": "invalid account",
            "invalid_symbol": "invalid symbol",
            "invalid_side": "invalid side",
            "shares_positive": "shares positive",
            "price_positive": "price positive",
            "no_last_price": "no last price",
        }

        non_finite_market_cmd, non_finite_market_err = validate_limit_order_inputs(
            market_last=float("inf"),
            **common,
        )
        overflow_cmd, overflow_err = validate_limit_order_inputs(
            market_last=1e308,
            **{**common, "through_market_pct_raw": "1e308"},
        )

        self.assertIsNone(non_finite_market_cmd)
        self.assertEqual(non_finite_market_err, "no last price")
        self.assertIsNone(overflow_cmd)
        self.assertEqual(overflow_err, "price positive")

    def test_parses_fast_trading_test_mode_from_config_payload(self):
        enabled, err = parse_fast_trading_test_mode(
            {"groups": [], "test_mode": "true"},
            fast_config_required="required",
        )

        self.assertIsNone(err)
        self.assertTrue(enabled)

    def test_missing_fast_trading_test_mode_defaults_false(self):
        enabled, err = parse_fast_trading_test_mode(
            {"groups": []},
            fast_config_required="required",
        )

        self.assertIsNone(err)
        self.assertFalse(enabled)


class AlgoOrderValidationTests(unittest.TestCase):
    def _validate(self, mode="E", fast_config=None, **overrides):
        values = {
            "trading_mode": mode,
            "symbol": "AAPL",
            "max_volume_raw": None,
            "market_volume_target_raw": "not-used",
            "end_time_et_raw": None,
            "abs_pos_change_limit_raw": None,
            "price_target_raw": None,
            "single_order_notional_limit_raw": None,
            "order_rate_limit_per_minute_raw": None,
            "fast_trading_config_raw": fast_config,
            "symbols": ["AAPL"],
            "account_metas": metas("A", "B"),
            "invalid_mode": "invalid mode",
            "invalid_symbol": "invalid symbol",
            "invalid_account": "invalid account",
            "number_required": "number required",
            "end_time_required": "end time required",
            "fast_config_required": "fast config required",
            "fast_price_limit_positive": "price limit positive",
            "fast_accounts_required": "accounts required",
            "fast_account_duplicate": "duplicate {account}",
            "fast_book_levels_invalid": "invalid levels",
            "fast_target_percentage_invalid": "invalid percentage",
            "fast_cycle_seconds_invalid": "invalid cycle",
        }
        values.update(overrides)
        return validate_algo_start_inputs(**values)

    def test_fast_mode_uses_new_configuration_and_ignores_generic_constraints(self):
        cmd, err = self._validate(
            fast_config={
                "price_limit": "123.45",
                "account_ids": ["A", "B"],
                "book_levels": "2",
                "target_percentage": "70",
                "cycle_seconds": "15",
                "test_mode": "true",
            }
        )

        self.assertIsNone(err)
        assert cmd is not None
        self.assertEqual(cmd["fast_trading_price_limit"], 123.45)
        self.assertEqual(cmd["fast_trading_account_ids"], ["A", "B"])
        self.assertEqual(cmd["fast_trading_book_levels"], 2)
        self.assertEqual(cmd["fast_trading_target_percentage"], 70)
        self.assertEqual(cmd["fast_trading_cycle_seconds"], 15)
        self.assertNotIn("fast_trading_aggression_level", cmd)
        self.assertTrue(cmd["fast_trading_test_mode"])
        self.assertNotIn("end_time_et", cmd)
        self.assertNotIn("max_volume", cmd)
        self.assertNotIn("price_target", cmd)

    def test_fast_sell_mode_accepts_json_configuration(self):
        cmd, err = self._validate(
            mode="F",
            fast_config='{"price_limit": 98.5, "account_ids": ["B"], "book_levels": 5, "target_percentage": 10, "cycle_seconds": 60}',
        )

        self.assertIsNone(err)
        assert cmd is not None
        self.assertEqual(cmd["trading_mode"], "F")
        self.assertEqual(cmd["fast_trading_price_limit"], 98.5)
        self.assertEqual(cmd["fast_trading_book_levels"], 5)
        self.assertEqual(cmd["fast_trading_target_percentage"], 10)
        self.assertEqual(cmd["fast_trading_cycle_seconds"], 60)
        self.assertFalse(cmd["fast_trading_test_mode"])

    def test_fast_settings_reject_invalid_price_accounts_and_controls(self):
        common = {
            "account_metas": metas("A"),
            "invalid_account": "invalid account",
            "fast_config_required": "required",
            "fast_price_limit_positive": "bad price",
            "fast_accounts_required": "accounts required",
            "fast_account_duplicate": "duplicate {account}",
            "fast_book_levels_invalid": "bad levels",
            "fast_target_percentage_invalid": "bad percentage",
            "fast_cycle_seconds_invalid": "bad cycle",
        }
        valid = {"price_limit": 10, "account_ids": ["A"], "book_levels": 2,
                 "target_percentage": 50, "cycle_seconds": 15}
        cases = [
            ({**valid, "price_limit": 0}, "bad price"),
            ({**valid, "account_ids": []}, "accounts required"),
            ({**valid, "account_ids": ["A", "A"]}, "duplicate A"),
            ({"price_limit": 10, "account_ids": ["A"], "aggression_level": 1}, "bad levels"),
        ]
        for field, invalid_values, error in (
            ("book_levels", (0, 6, 1.5), "bad levels"),
            ("target_percentage", (0, 5, 25, 110, 10.5), "bad percentage"),
            ("cycle_seconds", (0, 1, 20, 90, 5.5), "bad cycle"),
        ):
            cases.append(({key: value for key, value in valid.items() if key != field}, error))
            for value in (*invalid_values, None, "", "bad", True, False, [], {}, "NaN", "Infinity"):
                cases.append(({**valid, field: value}, error))

        for payload, expected_error in cases:
            with self.subTest(payload=payload):
                settings, err = parse_fast_trading_settings(payload, **common)
                self.assertIsNone(settings)
                self.assertEqual(err, expected_error)

    def test_fast_modes_accept_every_offered_control_value(self):
        valid = {"price_limit": 10, "account_ids": ["A"], "book_levels": 2,
                 "target_percentage": 100, "cycle_seconds": 30}
        for field, choices in (
            ("book_levels", range(1, 6)),
            ("target_percentage", range(10, 101, 10)),
            ("cycle_seconds", (5, 10, 15, 30, 60)),
        ):
            for mode in ("E", "F"):
                for value in choices:
                    with self.subTest(mode=mode, field=field, value=value):
                        command, error = self._validate(mode=mode, fast_config={**valid, field: value})
                        self.assertIsNone(error)
                        self.assertEqual(command[f"fast_trading_{field}"], value)

    def test_modes_a_through_d_accept_generic_inputs_without_fast_configuration(self):
        for mode in ("A", "B", "C", "D"):
            with self.subTest(mode=mode):
                command, error = self._validate(
                    mode=mode,
                    max_volume_raw="-1",
                    market_volume_target_raw="-1",
                    abs_pos_change_limit_raw="-1",
                    price_target_raw="100",
                    single_order_notional_limit_raw="-1",
                    order_rate_limit_per_minute_raw="-1",
                    end_time_et_raw="2099-12-31T00:00",
                )
                self.assertIsNone(error)
                self.assertEqual(command["trading_mode"], mode)
                self.assertEqual(command["price_target"], 100)
                self.assertFalse(any(key.startswith("fast_trading_") for key in command))

    def test_modes_a_through_d_still_require_generic_inputs(self):
        cmd, err = self._validate(mode="A", fast_config=None)

        self.assertIsNone(cmd)
        self.assertEqual(err, "number required")


if __name__ == "__main__":
    unittest.main()
