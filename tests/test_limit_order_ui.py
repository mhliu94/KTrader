import unittest

from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page


def metas() -> dict[str, AccountMeta]:
    return {
        "A": AccountMeta(id="A", num_id=1, broker="Tiger", trading_medium="API"),
        "B": AccountMeta(id="B", num_id=2, broker="Tiger", trading_medium="API"),
    }


class LimitOrderUiTests(unittest.TestCase):
    def test_limit_order_renders_quick_share_buttons(self):
        html = render_control_panel_page("en", metas(), ["MSFT", "AAPL"])

        for shares in (3000, 5000, 10000, 20000, 30000, 50000):
            self.assertIn(f"data-limit-shares='{shares}'", html)
            self.assertIn(f">{shares}</button>", html)

    def test_limit_order_persists_last_selected_symbol_in_local_storage(self):
        html = render_control_panel_page("en", metas(), ["MSFT", "AAPL"])

        self.assertIn('const symbolStorageKey = "trading_ui.limit_order.symbol";', html)
        self.assertIn("restoreLimitSymbol();", html)
        self.assertIn("persistLimitSymbol();", html)
        self.assertIn('id="limit_symbol"', html)
        self.assertIn("<option value='AAPL'>AAPL</option>", html)

    def test_limit_order_persists_last_selected_accounts_in_local_storage(self):
        html = render_control_panel_page("en", metas(), ["MSFT", "AAPL"])

        self.assertIn('const accountStorageKey = "trading_ui.limit_order.account_ids";', html)
        self.assertIn("restoreLimitAccounts();", html)
        self.assertIn("persistLimitAccounts();", html)
        self.assertIn("JSON.stringify(selectedAccounts)", html)
        self.assertIn("id='limit-acct-1'", html)
        self.assertIn("id='limit-acct-2'", html)

    def test_limit_order_persists_last_selected_side_in_local_storage(self):
        html = render_control_panel_page("en", metas(), ["MSFT", "AAPL"])

        self.assertIn('const sideStorageKey = "trading_ui.limit_order.side";', html)
        self.assertIn("restoreLimitSide();", html)
        self.assertIn("persistLimitSide();", html)
        self.assertIn('savedSide !== "BUY" && savedSide !== "SELL"', html)
        self.assertIn("id='limit-side-buy'", html)
        self.assertIn("id='limit-side-sell'", html)


if __name__ == "__main__":
    unittest.main()
