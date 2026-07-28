import unittest

from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.store import AccountStore
from trading_ui.templates import _aggregate_portfolio, render_account_details_page


def snapshot(
    account_id,
    *,
    cash=0.0,
    cash_by_currency=None,
    positions=None,
):
    return AccountSnapshot(
        account_id=account_id,
        cash=float(cash),
        cash_by_currency=dict(cash_by_currency or {}),
        positions=list(positions or []),
    )


def metas(*account_ids):
    return {
        account_id: AccountMeta(
            id=account_id,
            num_id=index,
            broker="Tiger",
            trading_medium="API",
        )
        for index, account_id in enumerate(account_ids, start=1)
    }


def render(accounts, account_metas):
    return render_account_details_page(
        lang="en",
        store=AccountStore(),
        accounts=accounts,
        source_label="unit-test",
        account_metas=account_metas,
        account_details_topic="account-details",
    )


def portfolio_fragment(html):
    start = html.index("<section id='portfolio-summary'")
    end = html.index("</section>", start) + len("</section>")
    return html[start:end]


class AccountPortfolioTests(unittest.TestCase):
    def test_aggregates_cash_per_currency_and_same_symbol_positions(self):
        account_metas = metas("A", "B")
        accounts = {
            "A": snapshot(
                "A",
                cash=9999,
                cash_by_currency={"USD": 1000, "HKD": 150},
                positions=[Position(symbol="AAPL", qty=10, avg_price=100)],
            ),
            "B": snapshot(
                "B",
                cash=9999,
                cash_by_currency={"USD": 250, "HKD": -25, "EUR": 3.5},
                positions=[
                    Position(symbol="AAPL", qty=30, avg_price=120),
                    Position(symbol="AAPL", qty=10, avg_price=140),
                    Position(symbol="MSFT", qty=4, avg_price=None),
                    Position(symbol="ZERO", qty=0, avg_price=999),
                ],
            ),
        }

        portfolio = _aggregate_portfolio(accounts, account_metas)

        self.assertEqual(portfolio["cash"], {"EUR": 3.5, "HKD": 125, "USD": 1250})
        self.assertEqual(portfolio["included_accounts"], 2)
        self.assertEqual(portfolio["total_accounts"], 2)
        self.assertEqual(
            [position["symbol"] for position in portfolio["positions"]],
            ["AAPL", "MSFT"],
        )
        aapl, msft = portfolio["positions"]
        self.assertEqual(aapl["qty"], 50)
        self.assertAlmostEqual(aapl["avg_price"], 120.0)
        self.assertEqual(aapl["account_count"], 2)
        self.assertEqual(msft["qty"], 4)
        self.assertIsNone(msft["avg_price"])
        self.assertEqual(msft["account_count"], 1)

        summary = portfolio_fragment(render(accounts, account_metas))
        for expected in ("EUR", "3.50", "HKD", "125.00", "USD", "1,250.00"):
            self.assertIn(expected, summary)
        self.assertIn("50.00", summary)
        self.assertIn("120.0000", summary)

    def test_missing_snapshots_are_reported_and_unknown_snapshots_are_excluded(self):
        account_metas = metas("A", "WAITING")
        accounts = {
            "A": snapshot(
                "A",
                cash=75,
                cash_by_currency={"HKD": 10},
                positions=[Position(symbol="AAPL", qty=2, avg_price=90)],
            ),
            "NOT_CONFIGURED": snapshot(
                "NOT_CONFIGURED",
                cash_by_currency={"USD": 1_000_000},
                positions=[Position(symbol="ZZZZ", qty=1_000_000, avg_price=1)],
            ),
        }

        portfolio = _aggregate_portfolio(accounts, account_metas)

        self.assertEqual(portfolio["cash"], {"HKD": 10, "USD": 75})
        self.assertEqual(portfolio["included_accounts"], 1)
        self.assertEqual(portfolio["total_accounts"], 2)
        self.assertEqual([position["symbol"] for position in portfolio["positions"]], ["AAPL"])

        summary = portfolio_fragment(render(accounts, account_metas))
        self.assertIn("Latest snapshots: <b>1 / 2</b>", summary)
        self.assertIn("totals include available accounts only", summary)
        self.assertNotIn("1,000,000", summary)
        self.assertNotIn("ZZZZ", summary)

    def test_portfolio_symbols_and_currency_labels_are_html_escaped(self):
        unsafe_currency = "<usd&"
        unsafe_symbol = "<img src=x onerror=alert(1)>"
        account_metas = metas("A")
        accounts = {
            "A": snapshot(
                "A",
                cash_by_currency={unsafe_currency: 12},
                positions=[Position(symbol=unsafe_symbol, qty=1, avg_price=2)],
            )
        }

        summary = portfolio_fragment(render(accounts, account_metas))

        self.assertNotIn(unsafe_currency.upper(), summary)
        self.assertNotIn(unsafe_symbol.upper(), summary)
        self.assertIn("&lt;USD&amp;", summary)
        self.assertIn("&lt;IMG SRC=X ONERROR=ALERT(1)&gt;", summary)


if __name__ == "__main__":
    unittest.main()
