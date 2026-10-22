from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch

from market_data.trading_volume import (
    TradingVolumePublisher,
    daily_volume,
    normalized_symbols,
    select_quote,
    utc_iso,
)
from market_data.price_book_producer import TSQuotePoller, _day_volume_by_symbol

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc).timestamp()


def quote(symbol="AAPL", volume="1000", **overrides):
    result = {
        "Symbol": symbol,
        "Volume": volume,
        "TradeTime": utc_iso(NOW),
        "MarketFlags": {"IsDelayed": False, "IsHalted": False, "IsBats": False},
        "LastSize": "7",
        "TradeVolume": "8",
        "TotalVolume": "9",
    }
    result.update(overrides)
    return result


class TradingVolumePublisherTests(unittest.TestCase):
    def setUp(self):
        self.producer = Mock()
        self.publisher = TradingVolumePublisher(
            self.producer, ["AAPL", "MSFT"], feed_id="test-feed"
        )

    def publish(self, value=None, offset=0, **overrides):
        args = dict(
            request_started_at=NOW + offset - 0.1, observed_at=NOW + offset, request_duration_ms=100
        )
        args.update(overrides)
        return self.publisher.publish_quote("AAPL", quote() if value is None else value, **args)

    def emitted(self):
        return [json.loads(call.kwargs["value"]) for call in self.producer.send.call_args_list]

    def test_documented_daily_volume_is_used_not_last_size_or_bar_volume(self):
        event = self.publish()
        self.assertEqual(event["total_volume"], 1000)
        self.assertTrue(event["healthy"])
        self.assertEqual(event["source"], "TradeStation")
        self.assertEqual(event["type"], "TRADING_VOLUME")
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(self.producer.send.call_args.kwargs["topic"], "trading-volume")
        self.assertEqual(self.producer.send.call_args.kwargs["key"], "AAPL")
        self.producer.send.return_value.get.assert_called_once_with(timeout=5)

    def test_missing_documented_volume_never_uses_aliases(self):
        value = quote()
        del value["Volume"]
        event = self.publish(value)
        self.assertFalse(event["healthy"])
        self.assertIsNone(event["total_volume"])

    def test_per_symbol_sequence_and_shared_producer_identity(self):
        self.assertEqual(self.publish()["sequence"], 1)
        self.assertEqual(self.publish(offset=5)["sequence"], 2)
        other = self.publisher.publish_quote(
            "MSFT",
            quote("MSFT"),
            request_started_at=NOW,
            observed_at=NOW + 0.1,
            request_duration_ms=100,
        )
        self.assertEqual(other["sequence"], 1)
        self.assertEqual(other["feed_id"], "test-feed")

    def test_unchanged_illiquid_quote_emits_healthy_fresh_snapshot(self):
        old_trade = quote(TradeTime=utc_iso(NOW - 3600))
        first = self.publish(old_trade)
        second = self.publish(old_trade, offset=5)
        self.assertTrue(first["healthy"])
        self.assertTrue(second["healthy"])
        self.assertEqual(second["total_volume"], first["total_volume"])
        self.assertEqual(second["interval_start"], first["observed_at"])
        self.assertNotEqual(second["observed_at"], first["observed_at"])

    def test_volume_advance_with_old_trade_time_is_not_timely_credit(self):
        self.publish()
        event = self.publish(quote(volume="1200"), offset=5)
        # The last trade at the previous observation is borderline-valid;
        # a timestamp materially before the previous observation is not.
        self.assertTrue(event["healthy"])
        event = self.publish(quote(volume="1300"), offset=10)
        self.assertFalse(event["healthy"])
        self.assertIsNone(event["total_volume"])
        self.assertIn("old last trade", event["reason"])

    def test_delayed_halted_or_unknown_market_flags_are_unhealthy(self):
        flags_cases = [
            None,
            {},
            {"IsDelayed": True, "IsHalted": False},
            {"IsDelayed": False, "IsHalted": True},
            {"IsDelayed": "false", "IsHalted": False},
            {"IsDelayed": False},
            {"IsHalted": False},
        ]
        for flags in flags_cases:
            with self.subTest(flags=flags):
                event = self.publish(quote(MarketFlags=flags))
                self.assertFalse(event["healthy"])
                self.assertIsNone(event["total_volume"])

    def test_invalid_volume_is_unhealthy_without_truncation(self):
        for value in ["-1", "1.1", "NaN", "Infinity", None, True, "invalid", "1e100"]:
            with self.subTest(value=value):
                event = self.publish(quote(volume=value))
                self.assertFalse(event["healthy"])
                self.assertIsNone(event["total_volume"])
        self.assertEqual(daily_volume("0"), 0)
        self.assertEqual(daily_volume("1000.0"), 1000)

    def test_wrong_symbol_error_and_future_timestamp_rejected(self):
        cases = [
            quote("MSFT"),
            quote(Error="Symbol Error"),
            quote(TradeTime=utc_iso(NOW + 30)),
            quote(TradeTime="2026-10-22T15:00:00"),
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(self.publish(value)["healthy"])

    def test_halt_and_delay_flags_preserved_on_unhealthy_messages(self):
        event = self.publish(quote(MarketFlags={"IsDelayed": False, "IsHalted": True}))
        self.assertFalse(event["healthy"])
        self.assertTrue(event["is_halted"])
        self.assertFalse(event["is_delayed"])
        unknown = self.publish(quote(MarketFlags={"IsDelayed": "false"}))
        self.assertIsNone(unknown["is_halted"])
        self.assertIsNone(unknown["is_delayed"])
        failed = self.publisher.publish_error(
            "AAPL", "unavailable", request_started_at=NOW, observed_at=NOW, request_duration_ms=0
        )
        self.assertIsNone(failed["is_halted"])
        self.assertIsNone(failed["is_delayed"])
        healthy = self.publish(offset=5)
        self.assertFalse(healthy["is_halted"])
        self.assertFalse(healthy["is_delayed"])

    def test_absent_last_trade_time_is_permitted_on_snapshot(self):
        value = quote()
        value.pop("TradeTime")
        event = self.publish(value)
        self.assertTrue(event["healthy"])
        self.assertNotIn("trade_time", event)

    def test_volume_advance_requires_last_trade_timestamp(self):
        baseline = quote(TradeTime=None)
        self.assertTrue(self.publish(baseline)["healthy"])
        self.assertTrue(self.publish(baseline, offset=5)["healthy"])
        advanced = quote(volume="1100", TradeTime=None)
        event = self.publish(advanced, offset=10)
        self.assertFalse(event["healthy"])
        self.assertIsNone(event["total_volume"])
        self.assertIn("without a last trade timestamp", event["reason"])

    def test_slow_request_emits_unhealthy_status_without_synthetic_volume(self):
        self.publish()
        event = self.publish(offset=20, request_duration_ms=15000)
        self.assertFalse(event["healthy"])
        self.assertIsNone(event["total_volume"])
        self.assertIn("freshness", event["reason"])

    def test_invalid_request_duration_is_an_unhealthy_serializable_status(self):
        for duration in [None, float("nan"), float("inf"), "bad", -1]:
            with self.subTest(duration=duration):
                result = self.publish(request_duration_ms=duration)
                self.assertFalse(result["healthy"])
                json.dumps(result, allow_nan=False)

    def test_provider_error_resets_next_interval(self):
        self.publish()
        error = self.publisher.publish_error(
            "AAPL",
            "HTTPError",
            request_started_at=NOW + 5,
            observed_at=NOW + 6,
            request_duration_ms=1000,
        )
        self.assertFalse(error["healthy"])
        recovered = self.publish(quote(TradeTime=utc_iso(NOW + 10)), offset=10)
        self.assertEqual(recovered["sequence"], 3)
        self.assertEqual(recovered["interval_start"], utc_iso(NOW + 9.9))

    def test_kafka_ambiguous_failure_never_reuses_sequence(self):
        self.producer.send.return_value.get.side_effect = TimeoutError("unknown delivery")
        with self.assertRaises(TimeoutError):
            self.publish()
        self.producer.send.return_value.get.side_effect = None
        result = self.publish(offset=5)
        self.assertEqual(result["sequence"], 2)
        self.assertEqual(result["interval_start"], utc_iso(NOW + 4.9))

    def test_counter_decrease_is_published_as_snapshot_not_synthetic_increment(self):
        self.publish()
        reduced = self.publish(quote(volume="950", TradeTime=utc_iso(NOW + 5)), offset=5)
        self.assertEqual(reduced["total_volume"], 950)
        self.assertNotIn("quantity", reduced)
        self.assertNotIn("delta", reduced)

    def test_us_market_date_changes_at_local_midnight_and_resets_interval(self):
        before = datetime(2026, 10, 23, 3, 59, 59, tzinfo=timezone.utc).timestamp()
        first = self.publish(
            quote(TradeTime=utc_iso(before)), request_started_at=before - 0.1, observed_at=before
        )
        after = before + 2
        second = self.publish(
            quote(TradeTime=utc_iso(after)), request_started_at=after - 0.1, observed_at=after
        )
        self.assertEqual(first["session"], "2026-10-22")
        self.assertEqual(second["session"], "2026-10-23")
        self.assertEqual(second["interval_start"], utc_iso(after - 0.1))

    def test_unconfigured_volume_symbol_is_not_emitted(self):
        event = self.publisher.publish_quote(
            "NVDA", quote("NVDA"), request_started_at=NOW, observed_at=NOW, request_duration_ms=0
        )
        self.assertIsNone(event)
        self.producer.send.assert_not_called()

    def test_symbol_normalization_and_configuration_validation(self):
        self.assertEqual(normalized_symbols([" aapl ", "MSFT", "AAPL", ""]), ["AAPL", "MSFT"])
        with self.assertRaises(ValueError):
            normalized_symbols("AAPL")
        with self.assertRaises(ValueError):
            TradingVolumePublisher(self.producer, [], topic="")

    def test_exact_symbol_selection_handles_partial_errors(self):
        payload = {
            "Quotes": [quote("MSFT"), quote("AAPL")],
            "Errors": [{"Symbol": "INVALID", "Error": "bad symbol"}],
        }
        self.assertEqual(select_quote(payload, "AAPL")["Symbol"], "AAPL")
        with self.assertRaises(ValueError):
            select_quote(payload, "INVALID")
        with self.assertRaises(ValueError):
            select_quote({"Quotes": [quote(), quote()]}, "AAPL")
        with self.assertRaises(ValueError):
            select_quote({"Quotes": [quote("MSFT")]}, "AAPL")


class TSQuotePollerTests(unittest.TestCase):
    def setUp(self):
        self.tm = Mock()
        self.tm.get_access_token.return_value = "test-token"
        self.producer = Mock()
        self.publisher = TradingVolumePublisher(
            self.producer, ["AAPL", "VOLUMEONLY"], feed_id="test-feed"
        )
        self.session = Mock()
        self.errors = []
        _day_volume_by_symbol.clear()

    def poller(self, symbols=None):
        return TSQuotePoller(
            self.tm,
            symbols or ["AAPL", "BOOKONLY", "VOLUMEONLY"],
            on_error=self.errors.append,
            volume_publisher=self.publisher,
        )

    def run_poll(self, poller):
        with patch(
            "market_data.price_book_producer.time.time", side_effect=[NOW - 0.1, NOW]
        ), patch("market_data.price_book_producer.time.monotonic", side_effect=[0, 0.1]):
            poller.poll_once(self.session)

    def test_union_symbols_share_request_volume_emits_without_book_updates(self):
        self.session.get.return_value.json.return_value = {
            "Quotes": [
                quote("VOLUMEONLY", "3000"),
                quote("AAPL", "1000"),
                quote("BOOKONLY", "2000"),
            ]
        }
        self.run_poll(self.poller())
        self.session.get.assert_called_once()
        self.assertTrue(self.session.get.call_args.args[0].endswith("/AAPL,BOOKONLY,VOLUMEONLY"))
        events = [json.loads(call.kwargs["value"]) for call in self.producer.send.call_args_list]
        self.assertEqual([event["symbol"] for event in events], ["AAPL", "VOLUMEONLY"])
        self.assertEqual([event["total_volume"] for event in events], [1000, 3000])
        self.assertEqual(_day_volume_by_symbol["BOOKONLY"], 2000)
        self.assertEqual(self.errors, [])

    def test_invalid_symbol_does_not_borrow_another_symbols_volume(self):
        self.session.get.return_value.json.return_value = {
            "Quotes": [quote("VOLUMEONLY", "3000")],
            "Errors": [{"Symbol": "AAPL", "Error": "FAILED, INVALID SYMBOL"}],
        }
        self.run_poll(self.poller(["AAPL", "VOLUMEONLY"]))
        events = [json.loads(call.kwargs["value"]) for call in self.producer.send.call_args_list]
        self.assertFalse(events[0]["healthy"])
        self.assertIsNone(events[0]["total_volume"])
        self.assertTrue(events[1]["healthy"])
        self.assertNotIn("AAPL", _day_volume_by_symbol)

    def test_http_failure_publishes_status_for_configured_volume_symbols(self):
        self.session.get.side_effect = TimeoutError("test timeout")
        self.run_poll(self.poller())
        events = [json.loads(call.kwargs["value"]) for call in self.producer.send.call_args_list]
        self.assertEqual([event["symbol"] for event in events], ["AAPL", "VOLUMEONLY"])
        self.assertTrue(
            all(not event["healthy"] and event["total_volume"] is None for event in events)
        )
        self.assertEqual(_day_volume_by_symbol, {})

    def test_batches_never_exceed_documented_100_symbol_limit(self):
        symbols = ["S" + str(i) for i in range(101)]
        poller = TSQuotePoller(self.tm, symbols, on_error=self.errors.append)
        self.session.get.return_value.json.side_effect = [
            {"Quotes": [quote(symbol) for symbol in symbols[:100]]},
            {"Quotes": [quote(symbols[100])]},
        ]
        poller.poll_once(self.session)
        self.assertEqual(self.session.get.call_count, 2)
        first_symbols = self.session.get.call_args_list[0].args[0].rsplit("/", 1)[1].split(",")
        self.assertEqual(len(first_symbols), 100)
        self.assertEqual(len(_day_volume_by_symbol), 101)

    def test_delayed_quote_never_published_as_healthy_volume(self):
        self.session.get.return_value.json.return_value = {
            "Quotes": [quote(MarketFlags={"IsDelayed": True, "IsHalted": False})]
        }
        self.run_poll(self.poller(["AAPL"]))
        event = json.loads(self.producer.send.call_args.kwargs["value"])
        self.assertFalse(event["healthy"])
        self.assertIsNone(event["total_volume"])


class DepthHeartbeatTests(unittest.TestCase):
    def setUp(self):
        from market_data import price_book_producer

        self.module = price_book_producer
        self.module._depth_book_cache.clear()
        self.producer = Mock()
        self.producer_patch = patch.object(self.module, "producer", self.producer, create=True)
        self.topic_patch = patch.object(self.module, "kafka_topic", "price-books", create=True)
        self.producer_patch.start()
        self.topic_patch.start()
        self.addCleanup(self.producer_patch.stop)
        self.addCleanup(self.topic_patch.stop)

    def book(self, feed="connection-1"):
        event = {
            "Symbol": "AAPL",
            "FeedID": feed,
            "SequenceNumber": 1,
            "Bids": [{"Price": "10.00", "TotalSize": "1000", "TotalOrderCount": 1}],
            "Asks": [{"Price": "10.01", "TotalSize": "500", "TotalOrderCount": 1}],
        }
        with patch.object(self.module.time, "time", return_value=NOW):
            self.module.print_depth_event(event)

    def heartbeat(self, feed="connection-1", timestamp=None):
        event = {
            "Heartbeat": 1,
            "Timestamp": timestamp or utc_iso(NOW + 5),
            "Symbol": "AAPL",
            "FeedID": feed,
            "SequenceNumber": 2,
        }
        with patch.object(self.module.time, "time", return_value=NOW + 5):
            self.module.print_depth_event(event)

    def test_actual_provider_heartbeat_preserves_idle_book_with_source_time(self):
        self.book()
        book = json.loads(self.producer.send.call_args.kwargs["value"])
        self.heartbeat()
        heartbeat = json.loads(self.producer.send.call_args.kwargs["value"])
        self.assertEqual(heartbeat["bid_side"], book["bid_side"])
        self.assertEqual(heartbeat["offer_side"], book["offer_side"])
        self.assertEqual(heartbeat["kind"], "PRICE_BOOK_HEARTBEAT")
        self.assertEqual(heartbeat["book_observed_at"], utc_iso(NOW + 5))
        self.assertEqual(heartbeat["feed_id"], "connection-1")
        self.assertEqual(heartbeat["sequence_num"], 2)
        self.assertTrue(heartbeat["healthy"])

    def test_heartbeat_never_reuses_book_from_a_previous_connection(self):
        self.heartbeat()
        self.producer.send.assert_not_called()
        self.book()
        self.producer.send.reset_mock()
        self.heartbeat(feed="new-connection")
        self.producer.send.assert_not_called()

    def test_old_or_invalid_heartbeat_does_not_make_book_healthy(self):
        self.book()
        self.heartbeat(timestamp=utc_iso(NOW - 100))
        payload = json.loads(self.producer.send.call_args.kwargs["value"])
        self.assertFalse(payload["healthy"])
        self.heartbeat(timestamp="invalid")
        payload = json.loads(self.producer.send.call_args.kwargs["value"])
        self.assertFalse(payload["healthy"])

    def test_snapshot_control_message_does_not_publish_empty_book(self):
        self.module.print_depth_event(
            {
                "Symbol": "AAPL",
                "FeedID": "connection-1",
                "SequenceNumber": 1,
                "StreamStatus": "EndSnapshot",
            }
        )
        self.producer.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
