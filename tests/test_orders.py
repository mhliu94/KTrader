import unittest

from trading_ui.models import AccountMeta
from trading_ui.services.orders import validate_quick_order_inputs


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


if __name__ == "__main__":
    unittest.main()
