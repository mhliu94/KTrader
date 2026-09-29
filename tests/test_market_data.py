import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver

from trading_ui.services.market_data import (
    BookLevel,
    MarketDataStore,
    OrderBookSnapshot,
    order_book_from_price_book,
    quote_row_from_price_book,
)


class MarketDataBookTests(unittest.TestCase):
    def test_order_book_from_price_book_caps_from_most_aggressive_prices(self):
        payload = {
            "symbol": "AAPL",
            "update_time": 1_700_000_000_000_000_000,
            "bid_side": {
                "levels": [
                    {"price": 95, "quantity": 10, "order_count": 1},
                    {"price": 105, "quantity": 20, "order_count": 2},
                    {"price": 100, "quantity": 30, "order_count": 3},
                ],
            },
            "offer_side": {
                "levels": [
                    {"price": 120, "quantity": 40, "order_count": 4},
                    {"price": 101, "quantity": 50, "order_count": 5},
                    {"price": 108, "quantity": 60, "order_count": 6},
                ],
            },
        }

        book = order_book_from_price_book(payload, depth_limit=2)

        self.assertIsNotNone(book)
        assert book is not None
        self.assertEqual([level.price for level in book.bids], [105, 100])
        self.assertEqual([level.price for level in book.asks], [101, 108])

    def test_market_data_store_get_book_caps_from_most_aggressive_existing_snapshot(self):
        store = MarketDataStore()
        store.upsert_book(
            OrderBookSnapshot(
                symbol="AAPL",
                bids=[
                    BookLevel(price=95, quantity=10),
                    BookLevel(price=105, quantity=20),
                    BookLevel(price=100, quantity=30),
                ],
                asks=[
                    BookLevel(price=120, quantity=40),
                    BookLevel(price=101, quantity=50),
                    BookLevel(price=108, quantity=60),
                ],
            )
        )

        book = store.get_book("AAPL", depth_limit=2)

        self.assertEqual([level.price for level in book.bids], [105, 100])
        self.assertEqual([level.price for level in book.asks], [101, 108])

    def test_quote_row_uses_best_prices_from_each_side(self):
        payload = {
            "symbol": "AAPL",
            "bid_side": {"levels": [{"price": 95}, {"price": 105}]},
            "offer_side": {"levels": [{"price": 120}, {"price": 101}]},
        }

        row = quote_row_from_price_book(payload)

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.last, 103)


class MarketInsightsSymbolTests(unittest.TestCase):
    def test_custom_symbol_can_fetch_book_without_being_configured(self):
        store = MarketDataStore()
        store.upsert_book(OrderBookSnapshot(symbol="NVDA", bids=[BookLevel(price=100, quantity=10)], asks=[]))
        with patch.multiple(webserver, MD_STORE=store, SYMBOLS=["AAPL"], SESSIONS={"token": "trader"}):
            client = TestClient(webserver.app)
            try:
                client.cookies.set("auth_token", "token")
                response = client.get("/api/market-insights", params={"symbol": " nvda "})
            finally:
                client.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["symbol"], "NVDA")
        self.assertEqual(response.json()["bids"][0]["price"], 100)


if __name__ == "__main__":
    unittest.main()
