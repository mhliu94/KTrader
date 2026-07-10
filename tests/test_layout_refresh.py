import unittest

from trading_ui.templates import render_layout


class LayoutRefreshTests(unittest.TestCase):
    def test_account_details_keeps_page_auto_refresh(self):
        html = render_layout("en", "account-details", "<p>account</p>")

        self.assertIn('<meta http-equiv="refresh" content="30" />', html)
        self.assertIn('id="refreshCountdown"', html)

    def test_other_tabs_do_not_page_auto_refresh(self):
        for tab in (
            "control-panel",
            "market-data",
            "market-insights",
            "currency-conversion",
            "trading-status",
        ):
            with self.subTest(tab=tab):
                html = render_layout("en", tab, "<p>tab</p>")

                self.assertNotIn('<meta http-equiv="refresh" content="30" />', html)
                self.assertNotIn('id="refreshCountdown"', html)


if __name__ == "__main__":
    unittest.main()
