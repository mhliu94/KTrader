from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_market import SmartSellingMarketAdapter


class Clock:
    def __init__(self):
        self.now = 0.0
        self.start = 1800000000.0

    def monotonic(self):
        return self.now

    def wall(self):
        return self.start + self.now

    def iso(self, age=0):
        return datetime.fromtimestamp(self.wall() - age, timezone.utc).isoformat()


class SmartSellingMarketTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.engine = SmartSellingEngine(
            Path(self.directory.name) / "smart.sqlite3",
            clock=self.clock.monotonic,
            wall_clock=self.clock.wall,
        )
        self.market = SmartSellingMarketAdapter(
            self.engine, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.parent = self.start()
        self.book_sequence = 0

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def start(self, **kwargs):
        return self.engine.start_parent(
            dict(
                account_id="A",
                symbol="XYZ",
                quantity="1000",
                mode="urgent",
                horizon_seconds=300,
                tick_size=".01",
                quantity_step="1",
                min_quantity="1",
                min_notional="0",
                **kwargs,
            )
        )["parent_id"]

    def book(self, **kwargs):
        self.book_sequence += 1
        payload = dict(
            symbol="XYZ",
            update_time=int(self.clock.wall() * 1_000_000_000),
            sequence_num=self.book_sequence,
            bid_side={"levels": [{"price": "10", "quantity": "1000"}]},
            offer_side={"levels": [{"price": "10.02", "quantity": "1000"}]},
        )
        payload.update(kwargs)
        return payload

    def volume(self, sequence, total, **kwargs):
        payload = dict(
            schema_version=1,
            type="TRADING_VOLUME",
            symbol="XYZ",
            source="TradeStation",
            feed_id="feed-a",
            sequence=sequence,
            total_volume=total,
            observed_at=self.clock.iso(),
            interval_start=self.clock.iso(age=1),
            session="2027-01-15",
            healthy=True,
            is_halted=False,
            is_delayed=False,
        )
        payload.update(kwargs)
        return payload

    def earned(self, parent=None):
        return Decimal(self.engine.get_parent(parent or self.parent)["earned"])

    def test_price_book_volume_and_embedded_trades_never_earn_credit(self):
        self.market.apply_price_book(
            self.book(
                volume=1_000_000,
                trades=[
                    {"event_id": "wrong-topic", "quantity": 1000, "source_time": self.clock.wall()}
                ],
            )
        )
        self.clock.now = 1
        self.market.apply_price_book(self.book(volume=2_000_000))
        self.assertEqual(self.earned(), 0)
        self.assertIsNone(self.market.volume_rate("XYZ"))

    def test_normalizes_best_prices_and_sums_same_price_depth_exactly(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.market.apply_price_book(
            self.book(
                bid_side={
                    "levels": [
                        {"prc": "9.99", "qty": "9000"},
                        {"Price": "10.10", "TotalSize": "100.1"},
                        {"price": "10.10", "total_qty": "200.2"},
                    ]
                },
                offer_side={"levels": [{"price": "10.30", "qty": 20}, {"prc": "10.20", "qty": 10}]},
            )
        )
        quote = self.engine._quotes["XYZ"]
        self.assertEqual(quote["bid"], Decimal("10.10"))
        self.assertEqual(quote["ask"], Decimal("10.20"))
        self.assertEqual(quote["bid_size"], Decimal("300.3"))
        self.assertTrue(quote["healthy"])

    def test_first_snapshot_baseline_and_exact_deltas_with_duplicate_rejection(self):
        self.market.apply_volume(self.volume(1, "12345.1"))
        self.assertEqual(self.earned(), 0)
        self.clock.now = 1
        batch = self.volume(2, "12445.6")
        self.assertTrue(self.market.apply_volume(batch))
        self.assertEqual(self.earned(), Decimal("10.05"))
        self.assertFalse(self.market.apply_volume(batch))
        self.clock.now = 2
        self.assertFalse(self.market.apply_volume(self.volume(1, "90000")))
        self.assertEqual(self.earned(), Decimal("10.05"))

    def test_straddling_batch_is_excluded_only_for_new_parent(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 0.5
        new_parent = self.engine.start_parent(
            dict(
                account_id="B",
                symbol="XYZ",
                quantity=1000,
                mode="urgent",
                tick_size=".01",
                quantity_step=1,
                min_quantity=1,
            )
        )["parent_id"]
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 1100))
        self.assertEqual(self.earned(), 10)
        self.assertEqual(self.earned(new_parent), 0)
        self.clock.now = 2
        self.market.apply_volume(self.volume(3, 1200))
        self.assertEqual(self.earned(), 20)
        self.assertEqual(self.earned(new_parent), 10)

    def test_sequence_gap_discards_uncertain_volume_and_resumes_after_baseline(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(3, 100000))
        self.assertEqual(self.earned(), 0)
        self.clock.now = 2
        self.market.apply_volume(self.volume(4, 100100))
        self.assertEqual(self.earned(), 10)

    def test_stale_future_and_long_intervals_never_create_catchup_credit(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 20
        self.assertFalse(
            self.market.apply_volume(self.volume(2, 9999, observed_at=self.clock.iso(age=15)))
        )
        self.market.apply_volume(self.volume(3, 10000))
        self.assertEqual(self.earned(), 0)
        self.clock.now = 21
        self.assertFalse(
            self.market.apply_volume(self.volume(4, 99999, observed_at=self.clock.iso(age=-10)))
        )
        self.clock.now = 40
        self.market.apply_volume(self.volume(5, 100000))
        self.assertEqual(self.earned(), 0)
        self.clock.now = 41
        self.market.apply_volume(self.volume(6, 100100))
        self.assertEqual(self.earned(), 10)
        self.clock.now = 60
        self.market.apply_volume(self.volume(7, 999999))
        self.assertEqual(self.earned(), 10)

    def test_volume_disconnect_blocks_saved_credit_despite_fresh_quote(self):
        self.market.apply_price_book(self.book())
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 2000))
        self.assertEqual(self.earned(), 100)
        self.market.set_volume_connected(False)
        self.clock.now = 2
        self.market.apply_price_book(self.book())
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.engine.get_parent(self.parent)["state"], "HOLD")
        self.market.set_volume_connected(True)
        self.market.apply_volume(self.volume(10, 20000))
        self.assertEqual(self.earned(), 100)
        self.clock.now = 3
        self.market.apply_volume(self.volume(11, 20100))
        self.assertEqual(self.earned(), 110)

    def test_unhealthy_observation_and_session_or_feed_changes_reset_baseline(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.assertFalse(self.market.apply_volume(self.volume(2, None, healthy=False)))
        self.clock.now = 2
        self.market.apply_volume(self.volume(3, 100000))
        self.clock.now = 3
        self.market.apply_volume(self.volume(4, 100100))
        self.assertEqual(self.earned(), 10)
        self.clock.now = 4
        self.market.apply_volume(self.volume(5, 10, session="2027-01-16"))
        self.clock.now = 5
        self.market.apply_volume(self.volume(1, 20, session="2027-01-16", feed_id="feed-b"))
        self.assertEqual(self.earned(), 10)
        self.assertNotIn("correction", self.engine.get_parent(self.parent)["reason"])

    def test_unknown_negative_correction_remains_held_after_quotes_recover(self):
        self.market.apply_price_book(self.book())
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 1100))
        self.clock.now = 2
        self.market.apply_volume(self.volume(3, 1000))
        self.clock.now = 3
        self.market.apply_price_book(self.book())
        self.market.apply_volume(self.volume(4, 1200))
        self.assertEqual(self.engine.tick(), [])
        parent = self.engine.get_parent(self.parent)
        self.assertEqual(parent["state"], "HOLD")
        self.assertIn("unknown tape correction", parent["reason"])

    def test_cumulative_decrease_across_sequence_gap_still_requires_reconciliation(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 1100))
        self.clock.now = 2
        self.market.apply_volume(self.volume(4, 1050))
        self.assertIn("unknown tape correction", self.engine.get_parent(self.parent)["reason"])

    def test_genuine_trade_ids_and_corrections_are_used_without_cumulative_double_credit(self):
        self.market.apply_volume(
            self.volume(
                1,
                1_000_000,
                trades=[
                    {"event_id": "trade-1", "quantity": "100.5", "source_time": self.clock.iso()}
                ],
            )
        )
        self.assertEqual(self.earned(), Decimal("10.05"))
        self.clock.now = 1
        self.market.apply_volume(
            self.volume(
                2,
                2_000_000,
                trades=[
                    {"event_id": "trade-1", "quantity": "100.5", "source_time": self.clock.iso()}
                ],
            )
        )
        self.assertEqual(self.earned(), Decimal("10.05"))
        self.clock.now = 2
        self.market.apply_volume(
            self.volume(
                3,
                2_000_000,
                trades=[
                    {
                        "event_id": "correct-1",
                        "quantity": "-20.5",
                        "correction_of": "trade-1",
                        "source_time": self.clock.iso(age=30),
                    }
                ],
            )
        )
        self.assertEqual(self.earned(), Decimal("8.00"))

    def test_raw_book_sequence_gaps_do_not_invent_provider_gaps(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.market.apply_price_book(self.book(sequence_num=1))
        self.clock.now = 1
        self.market.apply_price_book(self.book(sequence_num=50))
        self.assertTrue(self.engine._quotes["XYZ"]["healthy"])
        self.assertFalse(self.engine._quotes["XYZ"]["sequence_gap"])

    def test_explicit_book_gap_holds_then_recovers_on_next_full_snapshot(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.market.apply_price_book(self.book(book_sequence=1))
        self.clock.now = 1
        self.market.apply_price_book(self.book(book_sequence=3))
        self.assertFalse(self.engine._quotes["XYZ"]["healthy"])
        self.clock.now = 2
        self.market.apply_price_book(self.book(book_sequence=4))
        self.assertTrue(self.engine._quotes["XYZ"]["healthy"])
        self.assertFalse(self.engine._quotes["XYZ"]["sequence_gap"])

    def test_quote_recovery_cannot_clear_independent_trade_sequence_gap(self):
        self.market.apply_price_book(self.book())
        self.market.apply_volume(
            self.volume(
                1,
                None,
                trades=[
                    {
                        "event_id": "trade-1",
                        "quantity": 100,
                        "source_time": self.clock.iso(),
                        "sequence": 1,
                    }
                ],
            )
        )
        self.clock.now = 1
        self.market.apply_volume(
            self.volume(
                2,
                None,
                trades=[
                    {
                        "event_id": "trade-3",
                        "quantity": 100,
                        "source_time": self.clock.iso(),
                        "sequence": 3,
                    }
                ],
            )
        )
        self.clock.now = 2
        self.market.apply_price_book(self.book(feed_id="new-book-feed"))
        self.assertEqual(self.engine.tick(), [])
        self.assertIn("sequence gap", self.engine.get_parent(self.parent)["reason"])

    def test_missing_or_crossed_depth_holds_and_stale_tape_blocks_fresh_books(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.market.apply_price_book(self.book(bid_side={"levels": [{"price": 10}]}))
        self.assertFalse(self.engine._quotes["XYZ"]["healthy"])
        self.clock.now = 1
        self.market.apply_price_book(self.book(offer_side={"levels": [{"price": 9, "qty": 100}]}))
        self.assertFalse(self.engine._quotes["XYZ"]["healthy"])
        self.clock.now = 11
        self.market.apply_price_book(self.book())
        self.assertFalse(self.engine._quotes["XYZ"]["healthy"])

    def test_kafka_offsets_are_deduplicated_per_topic_partition(self):
        self.assertTrue(
            self.market.apply_price_book(self.book(), topic="price-book", partition=0, offset=4)
        )
        self.assertTrue(
            self.market.apply_volume(
                self.volume(1, 1000), topic="trading-volume", partition=0, offset=4
            )
        )
        self.clock.now = 1
        self.assertFalse(
            self.market.apply_volume(
                self.volume(2, 2000), topic="trading-volume", partition=0, offset=4
            )
        )
        self.assertTrue(
            self.market.apply_volume(
                self.volume(2, 1100), topic="trading-volume", partition=0, offset=5
            )
        )
        self.assertEqual(self.earned(), 10)

    def test_forecast_rate_is_bounded_fresh_and_counts_zero_volume_intervals(self):
        self.market = SmartSellingMarketAdapter(
            self.engine, clock=self.clock.monotonic, wall_clock=self.clock.wall, max_observations=3
        )
        self.market.apply_volume(self.volume(1, 1000))
        self.assertIsNone(self.market.volume_rate("XYZ"))
        for sequence in range(2, 7):
            self.clock.now = sequence - 1
            self.market.apply_volume(self.volume(sequence, 1000 + (sequence - 1) * 100))
        self.assertLessEqual(len(self.market._symbols["XYZ"].observations), 3)
        self.assertEqual(self.market.volume_rate("XYZ"), 100)
        self.clock.now = 6
        self.market.apply_volume(self.volume(7, 1500))
        self.assertEqual(self.market.volume_rate("XYZ"), Decimal(200) / 3)
        self.clock.now = 20
        self.assertIsNone(self.market.volume_rate("XYZ"))

    def test_new_adapter_after_restart_discards_first_cumulative_snapshot(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 1100))
        restarted = SmartSellingMarketAdapter(
            self.engine, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.clock.now = 2
        restarted.apply_volume(self.volume(50, 99999))
        self.assertEqual(self.earned(), 10)
        self.clock.now = 3
        restarted.apply_volume(self.volume(51, 100099))
        self.assertEqual(self.earned(), 20)

    def test_reconnect_or_restart_cannot_hide_same_session_volume_correction(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 2000))
        self.assertEqual(self.earned(), 100)
        self.market.set_volume_connected(False)
        self.clock.now = 2
        self.market.set_volume_connected(True)
        restarted = SmartSellingMarketAdapter(
            self.engine, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        restarted.apply_price_book(self.book())
        restarted.apply_volume(self.volume(1, 1900, feed_id="restarted-producer"))
        self.assertEqual(self.engine.tick(), [])
        self.assertIn("unknown tape correction", self.engine.get_parent(self.parent)["reason"])
        self.assertEqual(restarted.market_status("XYZ")["state"], "UNKNOWN")
        self.assertTrue(self.engine.get_market_checkpoint("XYZ")["counter_conflict"])

    def test_unknown_or_delayed_flags_do_not_earn_credit(self):
        self.assertEqual(self.market.market_status("XYZ")["state"], "UNKNOWN")
        self.market.apply_volume(self.volume(1, 1000, is_halted=None))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 2000, is_halted=False, is_delayed=None))
        self.clock.now = 2
        self.market.apply_volume(self.volume(3, 3000, is_delayed=True))
        self.assertEqual(self.earned(), 0)
        self.assertEqual(self.market.market_status("XYZ")["state"], "UNKNOWN")
        self.clock.now = 3
        self.market.apply_volume(self.volume(4, 4000))
        self.assertEqual(self.earned(), 0, "first explicit healthy observation is a new baseline")
        self.clock.now = 4
        self.market.apply_volume(self.volume(5, 4100))
        self.assertEqual(self.earned(), 10)

    def test_halt_requests_cancel_without_releasing_partial_fill_reservation(self):
        self.market.apply_price_book(self.book())
        self.market.apply_volume(self.volume(1, 1000))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 2000))
        submit = self.engine.tick()[0]
        self.engine.on_order_report(
            submit["client_order_id"], "PARTIALLY_FILLED", 20, broker_order_id="broker-1"
        )
        self.clock.now = 1.1
        self.market.apply_volume(self.volume(3, None, is_halted=True, healthy=False))
        self.market.apply_price_book(self.book())
        actions = self.engine.tick()
        self.assertTrue(any(action["type"] == "cancel" for action in actions))
        parent = self.engine.get_parent(self.parent)
        self.assertEqual(Decimal(parent["reserved"]), 80)
        self.assertEqual(Decimal(parent["filled"]), 20)
        self.assertEqual(self.market.market_status("XYZ")["state"], "HALTED")
        self.engine.on_order_report(
            submit["client_order_id"], "PARTIALLY_FILLED", 30, broker_order_id="broker-1"
        )
        self.assertEqual(Decimal(self.engine.get_parent(self.parent)["reserved"]), 70)
        self.assertEqual(self.earned(), 100)

    def test_reopening_discards_auction_and_waits_for_cooldown_and_fresh_book(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.market.apply_price_book(self.book())
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, None, is_halted=True, healthy=False))
        original_deadline = self.engine.get_parent(self.parent)["deadline"]
        self.clock.now = 2
        self.market.apply_volume(self.volume(3, 100000))
        self.assertEqual(self.market.market_status("XYZ")["state"], "RESUMING")
        for sequence in range(4, 14):
            self.clock.now = sequence - 1
            self.market.apply_price_book(self.book(kind="PRICE_BOOK_HEARTBEAT"))
            self.market.apply_volume(self.volume(sequence, 100000 + sequence * 1000))
        self.assertEqual(
            self.market.market_status("XYZ")["state"],
            "RESUMING",
            "heartbeats cannot substitute for a post-reopen depth snapshot",
        )
        self.assertEqual(self.earned(), 0)
        self.clock.now = 13
        self.market.apply_price_book(self.book(kind="PRICE_BOOK"))
        self.market.apply_volume(self.volume(14, 120000))
        self.assertEqual(self.market.market_status("XYZ")["state"], "TRADING")
        self.assertEqual(self.earned(), 0, "release snapshot remains an uncredited baseline")
        self.clock.now = 14
        self.market.apply_volume(self.volume(15, 120100))
        self.assertEqual(self.earned(), 10)
        self.assertEqual(self.engine.get_parent(self.parent)["deadline"], original_deadline)

    def test_stale_unknown_and_unhealthy_false_flags_cannot_release_halt(self):
        self.market.apply_volume(self.volume(1, None, is_halted=True, healthy=False))
        self.clock.now = 1
        self.market.apply_volume(self.volume(2, 1000, is_halted=None))
        self.assertEqual(self.market.market_status("XYZ")["state"], "HALTED")
        self.clock.now = 2
        self.market.apply_volume(self.volume(3, None, healthy=False))
        self.assertEqual(self.market.market_status("XYZ")["state"], "HALTED")
        self.clock.now = 20
        self.market.apply_volume(self.volume(4, 1000, observed_at=self.clock.iso(age=15)))
        self.assertEqual(self.market.market_status("XYZ")["state"], "HALTED")
        self.clock.now = 21
        self.market.apply_volume(self.volume(5, 1000))
        self.market.apply_price_book(self.book())
        self.assertEqual(self.market.market_status("XYZ")["state"], "RESUMING")
        self.clock.now = 30
        self.market.apply_volume(self.volume(6, 2000, is_halted=None))
        self.clock.now = 31
        self.market.apply_price_book(self.book())
        self.market.apply_volume(self.volume(7, 3000))
        self.assertEqual(
            self.market.market_status("XYZ")["state"],
            "RESUMING",
            "unknown flag breaks consecutive recovery confirmation",
        )
        self.clock.now = 32
        self.market.apply_volume(self.volume(8, 4000))
        self.assertEqual(self.market.market_status("XYZ")["state"], "TRADING")
        self.assertEqual(self.earned(), 0)

    def test_halt_latch_survives_adapter_restart(self):
        self.market.apply_volume(self.volume(1, None, is_halted=True, healthy=False))
        restarted = SmartSellingMarketAdapter(
            self.engine, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.assertEqual(restarted.market_status("XYZ")["state"], "HALTED")
        self.clock.now = 1
        restarted.apply_volume(self.volume(2, 10000))
        self.assertEqual(restarted.market_status("XYZ")["state"], "RESUMING")
        self.assertEqual(self.earned(), 0)

    def test_new_halt_sequence_latches_even_when_observation_timestamp_is_unchanged(self):
        self.market.apply_volume(self.volume(1, 1000))
        self.market.apply_price_book(self.book())
        self.market.apply_volume(self.volume(2, None, is_halted=True, healthy=False))
        self.assertEqual(self.market.market_status("XYZ")["state"], "HALTED")
        self.assertFalse(self.engine._quotes["XYZ"]["healthy"])

    def test_malformed_trade_correction_holds_even_while_halted(self):
        self.market.apply_volume(
            self.volume(
                1,
                None,
                trades=[{"event_id": "trade-1", "quantity": 100, "source_time": self.clock.iso()}],
            )
        )
        self.clock.now = 1
        self.market.apply_volume(
            self.volume(
                2,
                None,
                is_halted=True,
                healthy=False,
                trades=[
                    {"event_id": "broken-correction", "quantity": "NaN", "correction_of": "trade-1"}
                ],
            )
        )
        self.assertIn("unknown tape correction", self.engine.get_parent(self.parent)["reason"])
        self.assertEqual(self.earned(), 10)


if __name__ == "__main__":
    unittest.main()
