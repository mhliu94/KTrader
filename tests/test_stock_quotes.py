"""TradeStation snapshot -> Kafka record -> account valuation, without live services."""

import json
import re
import unittest
from unittest.mock import Mock, patch

from market_data.price_book_producer import TSQuotePoller
from market_data.stock_quotes import StockQuotePublisher
from market_data.trading_volume import TradingVolumePublisher, utc_iso
from trading_ui.kafka.consumer_market_data import PriceBookConsumer
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.market_data import MarketDataStore, stock_quote_from_payload
from trading_ui.store import AccountStore
import trading_ui.webserver as webserver


NOW = 1_792_858_400.0


def quote(symbol="AAPL", **overrides):
    result = {
        "Symbol": symbol, "Last": "125.1250", "LastSize": "7", "Volume": "1000",
        "TradeTime": utc_iso(NOW - 2), "Bid": "120", "Ask": "130",
        "MarketFlags": {"IsDelayed": False, "IsHalted": False},
    }
    result.update(overrides)
    return result


class StockQuotePublisherTests(unittest.TestCase):
    def setUp(self):
        self.producer = Mock()
        self.publisher = StockQuotePublisher(self.producer, [" aapl ", "MSFT"])

    def publish(self, value=None, *, observed_at=NOW, symbol="AAPL"):
        return self.publisher.publish_quote(
            symbol, quote() if value is None else value,
            request_started_at=observed_at - 0.1, observed_at=observed_at, request_duration_ms=100,
        )

    def test_publishes_actual_last_price_and_trade_time_with_symbol_key(self):
        event = self.publish()
        self.assertEqual(event["last"], "125.1250")
        self.assertEqual(event["last_size"], 7)
        self.assertEqual(event["trade_time"], utc_iso(NOW - 2))
        self.assertTrue(event["healthy"])
        self.assertEqual(event["source"], "TradeStation")
        call = self.producer.send.call_args.kwargs
        self.assertEqual((call["topic"], call["key"]), ("stock-quotes", "AAPL"))
        self.assertEqual(json.loads(call["value"]), event)
        self.producer.send.return_value.get.assert_called_once_with(timeout=5)

    def test_fixed_symbol_list_filters_quotes_and_errors(self):
        self.assertIsNone(self.publish(quote("UNKNOWN"), symbol="UNKNOWN"))
        self.assertIsNone(self.publisher.publish_error(
            "UNKNOWN", "no quote", request_started_at=NOW, observed_at=NOW, request_duration_ms=0,
        ))
        self.producer.send.assert_not_called()

    def test_missing_invalid_or_delayed_trades_publish_unavailable(self):
        cases = [
            {"Last": value}
            for value in (None, "", "bad", "NaN", "Infinity", "-Infinity", "1e-1000", 0, -1, True)
        ] + [
            {"TradeTime": None}, {"TradeTime": "2026-10-24T12:00:00"},
            {"TradeTime": utc_iso(NOW + 60)}, {"Symbol": "MSFT"}, {"Error": "bad quote"},
            {"MarketFlags": {"IsDelayed": True}}, {"MarketFlags": {}},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                event = self.publish(quote(**overrides))
                self.assertFalse(event["healthy"])
                self.assertIsNone(event["last"])
                self.assertIsNotNone(stock_quote_from_payload(event).error)

    def test_old_trade_is_valid_and_volume_or_size_is_not_required(self):
        event = self.publish(quote(TradeTime=utc_iso(NOW - 3 * 86400), LastSize=None, Volume=None))
        self.assertTrue(event["healthy"])
        self.assertIsNone(event["last_size"])
        self.assertEqual(stock_quote_from_payload(event).last, 125.125)

    def test_delivery_failure_is_not_reported_as_success(self):
        self.producer.send.return_value.get.side_effect = TimeoutError("delivery failed")
        with self.assertRaises(TimeoutError):
            self.publish()


class StockQuotePipelineTests(unittest.TestCase):
    def setUp(self):
        self.producer = Mock()
        self.publisher = StockQuotePublisher(self.producer, ["AAPL", "MSFT"])
        self.volume = TradingVolumePublisher(self.producer, ["AAPL", "VOLUMEONLY"])
        self.errors = []
        self.poller = TSQuotePoller(
            Mock(), ["AAPL", "MSFT", "VOLUMEONLY"], on_error=self.errors.append,
            volume_publisher=self.volume, stock_quote_publisher=self.publisher,
        )
        self.session = Mock()
        self.session.get.return_value.json.return_value = {
            "Quotes": [quote("AAPL"), quote("MSFT"), quote("VOLUMEONLY")],
        }
        self.store = MarketDataStore()
        self.callback = Mock()
        self.consumer = PriceBookConsumer(
            {"market_data_topic": "price-books"}, self.store, payload_callback=self.callback,
        )

    def poll(self, observed_at=NOW):
        with patch("market_data.price_book_producer.time.time", side_effect=[observed_at - 0.1, observed_at]):
            self.poller.poll_once(self.session)

    def emitted_quotes(self):
        return [
            json.loads(call.kwargs["value"]) for call in self.producer.send.call_args_list
            if call.kwargs["topic"] == "stock-quotes"
        ]

    def consume_quotes(self):
        for event in self.emitted_quotes():
            self.consumer._handle_payload(event, topic="stock-quotes")

    def summary_rows(self):
        accounts = AccountStore()
        accounts.upsert(AccountSnapshot(
            account_id="A", cash=0,
            positions=[Position("AAPL", 10, 100), Position("UNKNOWN", 5, 200)],
        ))
        with patch.multiple(
            webserver, store=accounts, MD_STORE=self.store,
            ACCOUNT_METAS={"A": AccountMeta("A", 1, "Tiger")}, SYMBOLS=["AAPL"],
            APP_CONFIG={"kafka": {"account_details_topic": "accounts"}},
        ):
            html = webserver._render_account_details_content("en", "numeric_id", "")
        summary = re.search(r"<section id='portfolio-summary'.*?</section>", html, re.S).group()
        return [
            [re.sub(r"<[^>]+>", "", cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row)]
            for row in re.findall(r"<tr>(.*?)</tr>", summary)[1:]
        ]

    def test_shared_request_publishes_fixed_set_and_values_only_traded_holdings(self):
        self.poll()
        self.session.get.assert_called_once()
        self.assertEqual([event["symbol"] for event in self.emitted_quotes()], ["AAPL", "MSFT"])
        self.consume_quotes()
        # Both stocks have depth; only AAPL has a trade snapshot.
        for symbol in ("AAPL", "UNKNOWN"):
            self.consumer._handle_payload({
                "symbol": symbol,
                "bid_side": {"levels": [{"price": 900}]},
                "offer_side": {"levels": [{"price": 902}]},
            }, topic="price-books")
        self.assertEqual(self.summary_rows(), [
            ["AAPL", "10.00", "100.0000", "125.1250", "1,251.25", "1"],
            ["UNKNOWN", "5.00", "200.0000", "--", "--", "1"],
        ])
        self.assertEqual(self.callback.call_count, 2)  # Only depth reaches Smart selling.
        self.assertEqual(self.store.get_for_symbols(["AAPL"])["AAPL"].last, 901)

    def test_provider_failure_clears_previous_price_and_older_replay_cannot_restore_it(self):
        self.poll()
        self.consume_quotes()
        old = self.emitted_quotes()[0]
        self.session.get.side_effect = TimeoutError("test")
        self.producer.reset_mock()
        self.poll(NOW + 5)
        self.consume_quotes()
        self.consumer._handle_payload(old, topic="stock-quotes")
        self.assertEqual(self.summary_rows()[0][-3:-1], ["--", "--"])

    def test_partial_response_does_not_borrow_another_symbols_price(self):
        self.session.get.return_value.json.return_value = {
            "Quotes": [quote("MSFT")], "Errors": [{"Symbol": "AAPL", "Error": "invalid"}],
        }
        self.poll()
        events = self.emitted_quotes()
        self.assertIsNone(events[0]["last"])
        self.assertFalse(events[0]["healthy"])
        self.assertEqual(events[1]["last"], "125.1250")

    def test_volume_publish_failure_does_not_block_stock_quotes(self):
        self.poller.volume_publisher = Mock()
        self.poller.volume_publisher.publish_quote.side_effect = RuntimeError("volume topic unavailable")
        self.poll()
        self.assertEqual(len(self.emitted_quotes()), 2)
        self.assertEqual(len(self.errors), 3)

    def test_consumer_subscribes_to_both_topics_on_existing_market_data_cluster(self):
        config = {
            "bootstrap_servers": "orders:9092", "market_data_bootstrap_servers": "market:9092",
            "market_data_group_id": "test", "market_data_auto_offset_reset": "earliest",
            "market_data_topic": "books", "stock_quotes_topic": "stock-quotes",
        }
        with patch("trading_ui.kafka.consumer_market_data.Consumer") as factory, patch(
            "trading_ui.kafka.consumer_market_data.threading.Thread"
        ):
            consumer = PriceBookConsumer(config, self.store)
            consumer.start()
            self.assertEqual(factory.call_args.args[0]["bootstrap.servers"], "market:9092")
            self.assertEqual(factory.return_value.subscribe.call_args.args[0], ["books", "stock-quotes"])
            consumer.stop()

    def test_malformed_records_are_ignored_or_clear_prices_without_falling_back_to_depth(self):
        for value in ([], {}, {"type": "STOCK_QUOTE", "schema_version": 9}):
            self.assertIsNone(stock_quote_from_payload(value))
        self.poll()
        event = self.emitted_quotes()[0]
        for last in (True, "NaN", "inf", 0, -2, None):
            with self.subTest(last=last):
                parsed = stock_quote_from_payload({**event, "last": last})
                self.assertIsNone(parsed.last)
                self.assertIsNotNone(parsed.error)


if __name__ == "__main__":
    unittest.main()
