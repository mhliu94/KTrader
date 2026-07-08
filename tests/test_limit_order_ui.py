import unittest

from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page


def metas() -> dict[str, AccountMeta]:
    return {
        "A": AccountMeta(id="A", num_id=1, broker="Tiger", trading_medium="API"),
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


if __name__ == "__main__":
    unittest.main()
