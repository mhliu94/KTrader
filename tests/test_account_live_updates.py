import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.market_data import MarketDataStore, StockQuote
from trading_ui.store import AccountStore
from trading_ui.templates import ACCOUNT_SORT_NUMERIC_ID, render_live_account_details_page


def event_payload(event: str) -> dict:
    data = next(line[6:] for line in event.splitlines() if line.startswith("data: "))
    return json.loads(data)


class AccountLiveUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "store": webserver.store,
            "ACCOUNT_METAS": webserver.ACCOUNT_METAS,
            "SYMBOLS": webserver.SYMBOLS,
            "APP_CONFIG": webserver.APP_CONFIG,
            "SESSIONS": webserver.SESSIONS,
            "MD_STORE": webserver.MD_STORE,
        }
        webserver.store = AccountStore()
        webserver.MD_STORE = MarketDataStore()
        webserver.ACCOUNT_METAS = {
            "A": AccountMeta(id="A", num_id=1, broker="Tiger"),
        }
        webserver.SYMBOLS = ["TSLA"]
        webserver.APP_CONFIG = {
            "kafka": {"account_details_topic": "account-details"},
        }
        webserver.SESSIONS = {"token": "trader"}

    def tearDown(self) -> None:
        webserver.store = self._saved["store"]
        webserver.ACCOUNT_METAS = self._saved["ACCOUNT_METAS"]
        webserver.SYMBOLS = self._saved["SYMBOLS"]
        webserver.APP_CONFIG = self._saved["APP_CONFIG"]
        webserver.SESSIONS = self._saved["SESSIONS"]
        webserver.MD_STORE = self._saved["MD_STORE"]

    def test_waits_for_live_data_without_loading_sample_balances(self):
        accounts, source = webserver.get_served_snapshots()
        self.assertEqual(accounts, {})
        self.assertEqual(source, "kafka")
        html = webserver._render_account_details_content("en", ACCOUNT_SORT_NUMERIC_ID, "TSLA")
        self.assertIn("#1 - A", html)
        self.assertIn("Some accounts have no snapshot yet", html)

        webserver.store.upsert(AccountSnapshot(account_id="A", cash=250))
        webserver.store.upsert(AccountSnapshot(account_id="UNCONFIGURED", cash=999))
        accounts, source = webserver.get_served_snapshots()
        self.assertEqual(set(accounts), {"A"})
        self.assertEqual(accounts["A"].cash, 250)
        self.assertEqual(source, "kafka")

    def test_store_update_emits_fresh_account_details_event(self):
        webserver.MD_STORE.upsert_stock_quote(StockQuote(
            symbol="TSLA", last=125, trade_time=1_700_000_000, observed_at=1_700_000_001,
        ))

        async def exercise_stream() -> tuple[dict, dict]:
            webserver.store.upsert(
                AccountSnapshot(
                    account_id="A",
                    cash=100,
                    cash_by_currency={"USD": 100},
                    positions=[Position("TSLA", 5)],
                )
            )
            events = webserver._account_details_event_stream("en", ACCOUNT_SORT_NUMERIC_ID, "TSLA")
            try:
                first = event_payload(await anext(events))
                webserver.store.upsert(
                    AccountSnapshot(
                        account_id="A",
                        cash=250,
                        cash_by_currency={"USD": 250},
                        positions=[Position("TSLA", 8)],
                    )
                )
                updated = event_payload(await asyncio.wait_for(anext(events), timeout=1.0))
                return first, updated
            finally:
                await events.aclose()

        first, updated = asyncio.run(exercise_stream())
        self.assertIn("$100.00", first["html"])
        self.assertIn("5.00", first["html"])
        self.assertIn("id='portfolio-market-value-total'>625.00</div>", first["html"])
        self.assertIn("$250.00", updated["html"])
        self.assertIn("8.00", updated["html"])
        self.assertIn("id='portfolio-market-value-total'>1,000.00</div>", updated["html"])
        self.assertIn("data-account-order='market'", updated["html"])
        self.assertIn("data-account-order='limit'", updated["html"])
        self.assertNotEqual(first["html"], updated["html"])

    def test_live_page_connects_to_stream_without_page_refresh(self):
        html = render_live_account_details_page(
            "<section id='portfolio-summary'></section><div id='trading-account-grid'></div>"
        )

        self.assertIn('new EventSource("/api/account-details/stream")', html)
        self.assertIn('currentPortfolio.replaceWith(nextPortfolio)', html)
        self.assertIn('currentGrid.replaceWith(nextGrid)', html)
        self.assertIn('window.setInterval(refreshStaleStatuses, 15000)', html)
        self.assertIn('window.addEventListener("pagehide", closeUpdates', html)
        self.assertNotIn('http-equiv="refresh"', html.lower())
        self.assertNotIn("refreshCountdown", html)

    def test_quote_updates_refresh_held_symbols_without_an_account_update(self):
        # A held symbol can differ from the configured order-entry symbols.
        webserver.store.upsert(AccountSnapshot(
            account_id="A", cash=100, positions=[Position(" aapl ", 10, 90)],
        ))

        def update_quote(last, observed_at):
            webserver.MD_STORE.upsert_stock_quote(StockQuote(
                symbol="AAPL", last=last, trade_time=observed_at, observed_at=observed_at,
            ))

        async def exercise_stream():
            update_quote(100, 1_700_000_000)
            events = webserver._account_details_event_stream("en", ACCOUNT_SORT_NUMERIC_ID, "TSLA")
            try:
                first = event_payload(await anext(events))
                update_quote(125, 1_700_000_005)
                updated = event_payload(await asyncio.wait_for(anext(events), timeout=1.0))
                heartbeat = await asyncio.wait_for(anext(events), timeout=1.0)
                return first, updated, heartbeat
            finally:
                await events.aclose()

        with patch.object(webserver, "ACCOUNT_DETAILS_QUOTE_REFRESH_SECONDS", 0.01):
            first, updated, heartbeat = asyncio.run(exercise_stream())

        self.assertIn("100.0000", first["html"])
        self.assertIn("1,000.00", first["html"])
        self.assertIn("id='portfolio-market-value-total'>1,000.00</div>", first["html"])
        self.assertIn("125.0000", updated["html"])
        self.assertIn("1,250.00", updated["html"])
        self.assertIn("id='portfolio-market-value-total'>1,250.00</div>", updated["html"])
        self.assertEqual(heartbeat, ": keep-alive\n\n")

    def test_stream_route_requires_auth_and_disables_proxy_buffering(self):
        def request(cookie: bytes | None = None) -> Request:
            headers = [(b"cookie", cookie)] if cookie is not None else []
            return Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/api/account-details/stream",
                    "raw_path": b"/api/account-details/stream",
                    "query_string": b"",
                    "headers": headers,
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "root_path": "",
                }
            )

        unauthorized = webserver.api_account_details_stream(request())
        authorized = webserver.api_account_details_stream(request(b"auth_token=token"))

        self.assertIsInstance(unauthorized, JSONResponse)
        self.assertEqual(unauthorized.status_code, 401)
        self.assertIsInstance(authorized, StreamingResponse)
        self.assertEqual(authorized.media_type, "text/event-stream")
        self.assertEqual(authorized.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(authorized.headers["x-accel-buffering"], "no")

    def test_rapid_store_updates_are_coalesced_for_each_subscriber(self):
        async def exercise_subscription() -> tuple[int, bool]:
            token, _version, updates = webserver.store.subscribe_updates()
            try:
                for cash in (100, 200, 300):
                    webserver.store.upsert(AccountSnapshot(account_id="A", cash=cash))
                await asyncio.sleep(0)
                latest = await asyncio.wait_for(updates.get(), timeout=1.0)
                return latest, updates.empty()
            finally:
                webserver.store.unsubscribe_updates(token)

        latest, queue_is_empty = asyncio.run(exercise_subscription())

        self.assertEqual(latest, 3)
        self.assertTrue(queue_is_empty)

    def test_open_stream_stops_after_session_is_revoked(self):
        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/account-details/stream",
                "raw_path": b"/api/account-details/stream",
                "query_string": b"",
                "headers": [(b"cookie", b"auth_token=token")],
                "client": ("testclient", 123),
                "server": ("testserver", 80),
                "root_path": "",
            },
            receive=receive,
        )

        async def exercise_revocation() -> bool:
            events = webserver._account_details_event_stream(
                "en",
                ACCOUNT_SORT_NUMERIC_ID,
                "TSLA",
                request=request,
            )
            try:
                await anext(events)
                webserver.SESSIONS = {}
                webserver.store.upsert(AccountSnapshot(account_id="A", cash=100))
                try:
                    await asyncio.wait_for(anext(events), timeout=1.0)
                except StopAsyncIteration:
                    return True
                return False
            finally:
                await events.aclose()

        self.assertTrue(asyncio.run(exercise_revocation()))


if __name__ == "__main__":
    unittest.main()
