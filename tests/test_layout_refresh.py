import unittest

from admin_ui.templates import render_layout as render_admin_layout
from trading_ui.templates import render_layout


class LayoutRefreshTests(unittest.TestCase):
    def test_no_trading_ui_page_has_auto_refresh(self):
        for tab in (
            "account-details",
            "control-panel",
            "market-data",
            "market-insights",
            "currency-conversion",
            "trading-status",
        ):
            with self.subTest(tab=tab):
                html = render_layout("en", tab, "<p>tab</p>")

                self.assertNotIn('http-equiv="refresh"', html.lower())
                self.assertNotIn("refreshCountdown", html)
                self.assertNotIn("Auto-refresh", html)

    def test_no_admin_ui_page_has_auto_refresh(self):
        html = render_admin_layout("en", "strategies", "<p>strategies</p>")

        self.assertNotIn('http-equiv="refresh"', html.lower())
        self.assertNotIn("refreshCountdown", html)
        self.assertNotIn("Auto-refresh", html)


if __name__ == "__main__":
    unittest.main()
