import unittest

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta, AccountSnapshot
from trading_ui.services.market_data import QuoteRow
from trading_ui.store import AccountStore
from trading_ui.templates import render_account_details_page, render_live_account_details_page


def account_metas() -> dict[str, AccountMeta]:
    return {
        "A": AccountMeta(id="A", num_id=2, broker="Tiger", broker_id="TG-A"),
        "B": AccountMeta(id="B", num_id=1, broker="Tiger", broker_id="TG-B"),
    }


class FakeProducer:
    def __init__(self) -> None:
        self.published: list[tuple[dict, str]] = []

    def publish_order(self, command: dict, key: str) -> None:
        self.published.append((command, key))


class FakeMarketDataStore:
    def __init__(self, prices: dict[str, float | None]) -> None:
        self.prices = prices

    def get_for_symbols(self, symbols: list[str]) -> dict[str, QuoteRow]:
        return {
            symbol: QuoteRow(
                symbol=symbol,
                prev_close=None,
                last=self.prices.get(symbol),
                change=None,
                change_pct=None,
                short_interest=None,
                volume=None,
                asof_epoch=None,
                error=None if self.prices.get(symbol) is not None else "unavailable",
            )
            for symbol in symbols
        }


class AccountOrderRenderingTests(unittest.TestCase):
    def test_each_account_card_has_actions_and_dialogs_are_shared(self):
        metas = account_metas()
        content = render_account_details_page(
            lang="en",
            store=AccountStore(),
            accounts={
                "A": AccountSnapshot(account_id="A", cash=100),
                "B": AccountSnapshot(account_id="B", cash=200),
            },
            source_label="unit-test",
            account_metas=metas,
            account_details_topic="account-details",
            symbols=["MSFT", "AAPL", "MSFT"],
        )
        html = render_live_account_details_page(
            content,
            lang="en",
            symbols=["MSFT", "AAPL", "MSFT"],
        )

        self.assertEqual(html.count("data-account-order='market'"), 2)
        self.assertEqual(html.count("data-account-order='limit'"), 2)
        self.assertIn("data-account-id='A'", html)
        self.assertIn("data-account-id='B'", html)
        self.assertEqual(html.count('id="account-quick-order-modal"'), 1)
        self.assertEqual(html.count('id="account-limit-order-modal"'), 1)
        self.assertGreater(
            html.index('id="account-quick-order-modal"'),
            html.index('id="trading-account-grid"'),
        )

    def test_dialogs_reuse_quick_market_and_control_panel_limit_fields(self):
        html = render_live_account_details_page(
            "<div id='trading-account-grid'></div>",
            lang="en",
            symbols=["MSFT", "AAPL", "MSFT"],
        )

        self.assertIn('action="/api/account-orders/quick-market"', html)
        self.assertIn('action="/api/account-orders/limit"', html)
        self.assertEqual(html.count('name="account_id"'), 2)
        self.assertNotIn('name="account_ids"', html)
        self.assertIn("name='symbol' value='AAPL' checked", html)
        self.assertEqual(html.count("name='symbol' value='MSFT'"), 1)
        self.assertIn('id="account-limit-symbol" name="symbol"', html)
        self.assertIn("data-account-limit-shares='3000'", html)
        self.assertIn("data-account-limit-shares='50000'", html)
        self.assertIn('name="through_market_pct" value="20"', html)
        self.assertIn('root.addEventListener("click"', html)
        self.assertIn('event.target.closest("[data-account-order]")', html)
        self.assertIn('controls.forEach(function(control) { control.disabled = true; })', html)
        self.assertIn('form.dataset.contextVersion !== contextVersion', html)
        self.assertIn('const closeButton = modal.querySelector(".modal-close")', html)


class AccountOrderEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "ACCOUNT_METAS": webserver.ACCOUNT_METAS,
            "SYMBOLS": webserver.SYMBOLS,
            "COMMANDS_PRODUCER": webserver.COMMANDS_PRODUCER,
            "MD_STORE": webserver.MD_STORE,
            "SESSIONS": webserver.SESSIONS,
        }
        webserver.ACCOUNT_METAS = account_metas()
        webserver.SYMBOLS = ["AAPL", "MSFT"]
        webserver.COMMANDS_PRODUCER = FakeProducer()
        webserver.MD_STORE = FakeMarketDataStore({"AAPL": 100.0, "MSFT": 250.0})
        webserver.SESSIONS = {"token": "trader"}
        self.client = TestClient(webserver.app)
        self.client.cookies.set("auth_token", "token")

    def tearDown(self) -> None:
        self.client.close()
        webserver.ACCOUNT_METAS = self._saved["ACCOUNT_METAS"]
        webserver.SYMBOLS = self._saved["SYMBOLS"]
        webserver.COMMANDS_PRODUCER = self._saved["COMMANDS_PRODUCER"]
        webserver.MD_STORE = self._saved["MD_STORE"]
        webserver.SESSIONS = self._saved["SESSIONS"]

    def published(self) -> list[tuple[dict, str]]:
        producer = webserver.COMMANDS_PRODUCER
        assert isinstance(producer, FakeProducer)
        return producer.published

    def test_quick_market_order_publishes_once_for_exact_account(self):
        response = self.client.post(
            "/api/account-orders/quick-market",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "BUY",
                "dollar_amount": "1050",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(len(self.published()), 1)
        command, key = self.published()[0]
        self.assertEqual(key, "A")
        self.assertEqual(command["type"], "MARKET_ORDER")
        self.assertEqual(command["account_id"], "A")
        self.assertEqual(command["qty_shares"], 10)
        self.assertNotIn("notional_usd", command)

    def test_quick_market_order_uses_ten_thousand_dollar_default(self):
        response = self.client.post(
            "/api/account-orders/quick-market",
            data={
                "account_id": "B",
                "symbol": "MSFT",
                "side": "SELL",
                "dollar_amount": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        command, key = self.published()[0]
        self.assertEqual(key, "B")
        self.assertEqual(command["account_id"], "B")
        self.assertEqual(command["side"], "SELL")
        self.assertEqual(command["qty_shares"], 40)

    def test_quick_market_normalizes_configured_symbol_case(self):
        webserver.SYMBOLS = [" aapl "]

        response = self.client.post(
            "/api/account-orders/quick-market",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "BUY",
                "dollar_amount": "1000",
            },
        )

        self.assertEqual(response.status_code, 200)
        command, _ = self.published()[0]
        self.assertEqual(command["symbol"], "AAPL")

    def test_quick_market_rejects_duplicate_or_unconfigured_accounts(self):
        duplicate = self.client.post(
            "/api/account-orders/quick-market",
            data={
                "account_id": ["A", "B"],
                "symbol": "AAPL",
                "side": "BUY",
                "dollar_amount": "1000",
            },
        )
        hidden = self.client.post(
            "/api/account-orders/quick-market",
            data={
                "account_id": "HIDDEN",
                "symbol": "AAPL",
                "side": "BUY",
                "dollar_amount": "1000",
            },
        )

        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(hidden.status_code, 400)
        self.assertEqual(self.published(), [])

    def test_limit_order_uses_manual_price_and_publishes_once(self):
        response = self.client.post(
            "/api/account-orders/limit",
            data={
                "account_id": "B",
                "symbol": "AAPL",
                "side": "BUY",
                "shares": "3000",
                "limit_price": "123.45",
                "through_market_pct": "20",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.published()), 1)
        command, key = self.published()[0]
        self.assertEqual(key, "B")
        self.assertEqual(command["type"], "LIMIT_ORDER")
        self.assertEqual(command["account_id"], "B")
        self.assertEqual(command["qty_shares"], 3000)
        self.assertEqual(command["limit_price"], 123.45)

    def test_limit_order_calculates_sell_price_through_market(self):
        response = self.client.post(
            "/api/account-orders/limit",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "SELL",
                "shares": "100",
                "limit_price": "",
                "through_market_pct": "5",
            },
        )

        self.assertEqual(response.status_code, 200)
        command, _ = self.published()[0]
        self.assertEqual(command["limit_price"], 95.0)

    def test_limit_order_rejects_duplicate_or_unconfigured_accounts(self):
        duplicate = self.client.post(
            "/api/account-orders/limit",
            data={
                "account_id": ["A", "B"],
                "symbol": "AAPL",
                "side": "BUY",
                "shares": "100",
                "limit_price": "123",
            },
        )
        hidden = self.client.post(
            "/api/account-orders/limit",
            data={
                "account_id": "HIDDEN",
                "symbol": "AAPL",
                "side": "BUY",
                "shares": "100",
                "limit_price": "123",
            },
        )

        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(hidden.status_code, 400)
        self.assertEqual(self.published(), [])

    def test_limit_order_rejects_non_finite_market_price(self):
        webserver.MD_STORE = FakeMarketDataStore({"AAPL": float("inf")})

        response = self.client.post(
            "/api/account-orders/limit",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "BUY",
                "shares": "100",
                "limit_price": "",
                "through_market_pct": "5",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.published(), [])

    def test_invalid_numeric_inputs_are_validation_errors_and_publish_nothing(self):
        quick = self.client.post(
            "/api/account-orders/quick-market",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "BUY",
                "dollar_amount": "NaN",
            },
        )
        limit = self.client.post(
            "/api/account-orders/limit",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "BUY",
                "shares": "not-an-integer",
                "limit_price": "123",
                "through_market_pct": "",
            },
        )

        self.assertEqual(quick.status_code, 400)
        self.assertEqual(limit.status_code, 400)
        self.assertEqual(self.published(), [])

    def test_order_endpoints_require_authentication(self):
        unauthenticated = TestClient(webserver.app)
        response = unauthenticated.post(
            "/api/account-orders/limit",
            data={
                "account_id": "A",
                "symbol": "AAPL",
                "side": "BUY",
                "shares": "100",
                "limit_price": "123",
            },
        )
        unauthenticated.close()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.published(), [])


if __name__ == "__main__":
    unittest.main()
