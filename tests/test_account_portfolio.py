import unittest
from datetime import datetime, timedelta, timezone

from trading_ui.config import load_account_metas
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.store import AccountStore
from trading_ui.templates import (
    _aggregate_portfolio,
    _is_snapshot_stale,
    render_account_details_page,
)


def snapshot(
    account_id,
    *,
    cash=0.0,
    cash_by_currency=None,
    positions=None,
    ts=None,
    trading_enabled=False,
):
    return AccountSnapshot(
        account_id=account_id,
        cash=float(cash),
        cash_by_currency=dict(cash_by_currency or {}),
        positions=list(positions or []),
        ts=ts,
        trading_enabled=trading_enabled,
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
    def test_monitor_defaults_to_unavailable_when_loading_account_metadata(self):
        account_metas = load_account_metas(
            {
                "accounts": [
                    {"string_id": "ENABLED", "numeric_id": 1, "broker": "Tiger", "monitor": True},
                    {"string_id": "DISABLED", "numeric_id": 2, "broker": "Tiger", "monitor": False},
                    {"string_id": "DEFAULT", "numeric_id": 3, "broker": "Tiger"},
                ]
            }
        )

        self.assertTrue(account_metas["ENABLED"].monitor)
        self.assertFalse(account_metas["DISABLED"].monitor)
        self.assertFalse(account_metas["DEFAULT"].monitor)

    def test_monitor_ip_is_linked_and_unavailable_monitor_ip_is_plain_text(self):
        account_metas = {
            "MONITORED": AccountMeta(
                id="MONITORED",
                num_id=1,
                broker="Tiger",
                ip_address="10.0.0.1",
                monitor=True,
            ),
            "UNAVAILABLE": AccountMeta(
                id="UNAVAILABLE",
                num_id=2,
                broker="Tiger",
                ip_address="10.0.0.2",
            ),
        }

        html = render({}, account_metas)
        monitored_card = html[html.index("#1 - MONITORED"):html.index("#2 - UNAVAILABLE")]
        unavailable_card = html[html.index("#2 - UNAVAILABLE"):]

        self.assertIn("href='https://10.0.0.1'", monitored_card)
        self.assertIn(">10.0.0.1</a>", monitored_card)
        self.assertIn("10.0.0.2", unavailable_card)
        self.assertNotIn("href=", unavailable_card)

    def test_snapshot_is_stale_only_after_five_minutes(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

        self.assertFalse(_is_snapshot_stale("2026-07-30T11:55:00Z", now))
        self.assertTrue(_is_snapshot_stale("2026-07-30T11:54:59.999999+00:00", now))
        self.assertFalse(_is_snapshot_stale("not-a-timestamp", now))
        self.assertFalse(_is_snapshot_stale(None, now))

    def test_stale_badge_is_shown_next_to_trading_status(self):
        account_metas = metas("STALE", "CURRENT", "WAITING")
        accounts = {
            "STALE": snapshot(
                "STALE",
                ts=(datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
                trading_enabled=True,
            ),
            "CURRENT": snapshot(
                "CURRENT",
                ts=(datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),
            ),
        }

        html = render(accounts, account_metas)
        stale_card = html[html.index("#1 - STALE"):html.index("#2 - CURRENT")]
        current_card = html[html.index("#2 - CURRENT"):html.index("#3 - WAITING")]
        waiting_card = html[html.index("#3 - WAITING"):]

        self.assertIn("status-on", stale_card)
        self.assertIn("status-stale", stale_card)
        self.assertIn(">Stale</span>", stale_card)
        self.assertNotIn("status-stale", current_card)
        self.assertNotIn("status-stale", waiting_card)

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
