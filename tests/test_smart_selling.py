import json
import os
import tempfile
import unittest
from decimal import Decimal

from trading_ui.services.smart_selling import SmartSellingEngine


class Clock:
    def __init__(self):
        self.now = 0.0
        self.wall_start = 1800000000.0

    def monotonic(self):
        return self.now

    def wall(self):
        return self.wall_start + self.now


class SmartSellingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "smart.sqlite3")
        self.clock = Clock()
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def start(self, **overrides):
        settings = dict(
            account_id="A",
            symbol="XYZ",
            quantity="1000",
            mode="urgent",
            horizon_seconds=300,
            tick_size=".01",
            quantity_step="1",
            min_quantity="1",
            min_notional="0",
        )
        settings.update(overrides)
        parent = self.engine.start_parent(settings)
        self.parent = parent["parent_id"]
        self.quote()
        return parent

    def quote(self, **overrides):
        values = dict(
            symbol="XYZ", bid="10", ask="10.01", bid_size="1000", source_time=self.clock.wall()
        )
        values.update(overrides)
        self.engine.on_quote(**values)

    def tape(self, quantity="1000", event_id="trade-1", **kwargs):
        self.engine.on_trade("XYZ", quantity, event_id, source_time=self.clock.wall(), **kwargs)

    def child(self):
        actions = self.engine.tick()
        submits = [a for a in actions if a["type"] == "submit"]
        self.assertEqual(len(submits), 1, actions)
        return submits[0]

    def report(self, child, status, quantity="0", **kwargs):
        self.engine.on_order_report(
            child["client_order_id"],
            status,
            quantity,
            broker_order_id="broker-" + child["client_order_id"],
            **kwargs,
        )

    def status(self):
        result = self.engine.get_parent(self.parent)
        self.assertEqual(
            Decimal(result["earned"]),
            sum(Decimal(result[k]) for k in ["free_credit", "reserved", "filled", "discarded"]),
        )
        self.assertLessEqual(
            Decimal(result["filled"]) + Decimal(result["reserved"]), Decimal(result["quantity"])
        )
        accounts = result["accounts"].values()
        self.assertEqual(sum(Decimal(a["filled"]) for a in accounts), Decimal(result["filled"]))
        self.assertEqual(sum(Decimal(a["reserved"]) for a in accounts), Decimal(result["reserved"]))
        for account in accounts:
            self.assertGreaterEqual(Decimal(account["reserved"]), 0)
            self.assertLessEqual(
                Decimal(account["filled"]) + Decimal(account["reserved"]),
                Decimal(account["quantity"]),
            )
        return result

    def test_three_fixed_modes_and_one_tick_midpoint(self):
        for mode, rate, price in [
            ("patient", ".03", "10.01"),
            ("normal", ".06", "10.01"),
            ("urgent", ".10", "10"),
        ]:
            with self.subTest(mode=mode):
                self.start(mode=mode, account_id=mode)
                self.tape("10000", mode)
                child = self.child()
                self.assertEqual(Decimal(child["limit_price"]), Decimal(price))
                self.assertEqual(Decimal(self.status()["participation"]), Decimal(rate))
                self.report(child, "FILLED", child["quantity"], final=True)
                self.engine.request_stop(self.parent)

    def test_configured_participation_is_exact_and_survives_restart_and_correction(self):
        self.start(mode="patient", participation_percentage="0.000000000000000000000001")
        self.tape("1e24", "original")
        self.assertEqual(Decimal(self.status()["participation"]), Decimal("1e-26"))
        self.assertEqual(Decimal(self.status()["earned"]), Decimal(".01"))

        self.engine.close()
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.assertEqual(Decimal(self.status()["participation"]), Decimal("1e-26"))
        self.tape("-1e23", "correction", correction_of="original")
        self.assertEqual(Decimal(self.status()["earned"]), Decimal(".009"))
        self.tape("1e23", "next")
        self.assertEqual(Decimal(self.status()["earned"]), Decimal(".01"))

    def test_configured_participation_accepts_full_volume_and_rejects_invalid_values(self):
        for percentage in (True, False, None, "NaN", "Infinity", 0, -1, "100.01", "1e-25"):
            with self.subTest(percentage=percentage), self.assertRaises(ValueError):
                self.start(participation_percentage=percentage)
        self.start(participation_percentage="100")
        self.tape("100")
        self.assertEqual(Decimal(self.status()["participation"]), Decimal(1))
        self.assertEqual(Decimal(self.status()["earned"]), Decimal(100))
        self.assertEqual(Decimal(self.child()["quantity"]), Decimal(100))

    def test_one_parent_per_account_symbol(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.start()

    def test_no_invented_initial_credit(self):
        self.start()
        self.assertEqual(self.engine.tick(), [])
        self.clock.now = 30
        self.quote()
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.status()["earned"], "0")

    def test_child_and_credit_cap_large_print_no_parallel_burst(self):
        self.start()
        self.tape("10000")
        child = self.child()
        self.assertEqual(Decimal(child["quantity"]), 100)
        status = self.status()
        self.assertEqual(Decimal(status["free_credit"]), 100)
        self.assertEqual(Decimal(status["discarded"]), 800)
        self.assertEqual(self.engine.tick(), [])

    def test_depth_cap_and_partial_budget_flush(self):
        self.start()
        self.quote(bid_size="800")
        self.tape("600")
        self.assertEqual(self.engine.tick(), [])
        self.clock.now = 29
        self.quote(bid_size="800")
        self.assertEqual(self.engine.tick(), [])
        self.clock.now = 30
        self.quote(bid_size="800")
        self.assertEqual(Decimal(self.child()["quantity"]), 60)

    def test_fractional_credit_and_quantity_step_never_round_up(self):
        self.start(quantity="100", quantity_step=".1", min_quantity=".1", horizon_seconds=300)
        self.quote(bid_size="100")
        self.tape("12.345")
        self.clock.now = 30
        self.quote(bid_size="100")
        child = self.child()
        self.assertEqual(Decimal(child["quantity"]), Decimal("1.2"))
        self.assertEqual(Decimal(self.status()["free_credit"]), Decimal(".0345"))

    def test_partial_fill_during_cancel_refunds_final_leaves_once(self):
        self.start()
        self.tape()
        child = self.child()
        self.clock.now = 0.6
        self.report(child, "PARTIALLY_FILLED", "40", sequence=1)
        self.clock.now = 2
        actions = self.engine.tick()
        self.assertEqual([a["type"] for a in actions], ["cancel"])
        self.assertEqual(Decimal(self.status()["reserved"]), 60)
        self.clock.now = 2.4
        self.report(child, "PARTIALLY_FILLED", "60", sequence=2)
        self.clock.now = 2.6
        self.report(child, "CANCELED", "60", sequence=3, final=True)
        self.report(child, "CANCELED", "60", sequence=3, final=True)
        self.report(child, "PARTIALLY_FILLED", "40", sequence=1)
        result = self.status()
        self.assertEqual(Decimal(result["filled"]), 60)
        self.assertEqual(Decimal(result["free_credit"]), 40)
        self.assertEqual(Decimal(result["reserved"]), 0)
        self.assertEqual(Decimal(result["fills_after_cancel"]), 20)

    def test_delayed_ack_queues_cancel_and_does_not_reset_age(self):
        self.start()
        self.tape()
        child = self.child()
        self.clock.now = 2
        self.assertEqual(self.engine.tick(), [])
        self.clock.now = 3
        self.assertEqual([a["type"] for a in self.engine.tick()], ["query"])
        self.clock.now = 4
        self.report(child, "NEW", sequence=1)
        self.assertEqual([a["type"] for a in self.engine.drain_actions()], ["cancel"])
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_unknown_submit_and_not_found_never_refund(self):
        self.start()
        self.tape()
        child = self.child()
        self.engine.on_action_error(child["client_order_id"], "submit", "lost connection")
        self.assertEqual([a["type"] for a in self.engine.drain_actions()], ["query"])
        self.engine.on_order_report(child["client_order_id"], "NOT_FOUND", "0", sequence=1)
        self.clock.now = 4
        self.assertEqual(self.engine.tick(), [])
        self.clock.now = 5
        self.assertEqual([a["type"] for a in self.engine.tick()], ["query"])
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_missing_final_cumulative_proof_preserves_reservation(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "CANCELED", "10", final=False)
        result = self.status()
        self.assertEqual(Decimal(result["filled"]), 10)
        self.assertEqual(Decimal(result["reserved"]), 90)
        self.assertEqual(Decimal(result["free_credit"]), 0)

    def test_inconsistent_filled_status_does_not_refund_missing_quantity(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "FILLED", "40", final=True)
        status = self.status()
        self.assertEqual(Decimal(status["filled"]), 40)
        self.assertEqual(Decimal(status["reserved"]), 60)
        self.assertEqual(status["order_state"], "RECONCILE")

    def test_non_authoritative_report_does_not_book_fill(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "FILLED", "100", authoritative=False, final=True)
        result = self.status()
        self.assertEqual(Decimal(result["filled"]), 0)
        self.assertEqual(Decimal(result["reserved"]), 100)

    def test_stale_and_reordered_reports_do_not_double_fill(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "PARTIALLY_FILLED", "40", sequence=3)
        self.report(child, "PARTIALLY_FILLED", "20", sequence=2)
        self.report(child, "PARTIALLY_FILLED", "40", sequence=3)
        self.assertEqual(Decimal(self.status()["filled"]), 40)

    def test_newer_cumulative_regression_holds_for_execution_bust(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "PARTIALLY_FILLED", "40", sequence=3)
        self.report(child, "PARTIALLY_FILLED", "20", sequence=4)
        self.assertIn("execution bust", self.status()["reason"])
        self.assertEqual(Decimal(self.status()["filled"]), 40)

    def test_archived_conflict_does_not_release_new_slot(self):
        self.start()
        self.tape("2000")
        first = self.child()
        self.report(first, "CANCELED", "40", sequence=1, final=True)
        self.clock.now = 5
        self.quote()
        second = self.child()
        self.report(first, "CANCELED", "50", sequence=2, final=True)
        result = self.status()
        self.assertEqual(result["active_order"]["client_order_id"], second["client_order_id"])
        self.assertEqual(Decimal(result["reserved"]), 100)
        self.assertIn("archived", result["reason"])

    def test_tape_dedup_corrections_original_rate_and_no_fill_credit(self):
        self.start(mode="normal")
        self.tape("1000")
        self.tape("1000")
        self.engine.on_trade("XYZ", "-100", "correction-1", correction_of="trade-1")
        self.engine.on_trade("XYZ", "-100", "correction-1", correction_of="trade-1")
        self.assertEqual(Decimal(self.status()["earned"]), 54)
        self.clock.now = 30
        self.quote()
        child = self.child()
        self.report(child, "FILLED", child["quantity"], final=True)
        self.assertEqual(Decimal(self.status()["earned"]), 54)

    def test_negative_correction_keeps_live_reservation_and_signed_credit(self):
        self.start()
        self.tape()
        child = self.child()
        self.engine.on_trade("XYZ", "-900", "correction-1", correction_of="trade-1")
        result = self.status()
        self.assertEqual(Decimal(result["free_credit"]), -90)
        self.assertEqual(Decimal(result["reserved"]), 100)
        self.report(child, "FILLED", "100", final=True)
        self.clock.now = 5
        self.quote()
        self.assertEqual(self.engine.tick(), [])
        self.assertIn("negative credit", self.status()["reason"])

    def test_unknown_correction_holds_without_guessing(self):
        self.start()
        self.engine.on_trade("XYZ", "-100", "correction", correction_of="missing")
        self.tape()
        self.assertEqual(self.engine.tick(), [])
        self.assertIn("unknown tape correction", self.status()["reason"])

    def test_send_spacing_and_one_shot_passive_fallback(self):
        self.start(mode="patient")
        self.tape("10000")
        first = self.child()
        self.report(first, "CANCELED", "20", final=True)
        self.assertTrue(self.status()["fallback"])
        self.clock.now = 4
        self.quote()
        self.assertEqual(self.engine.tick(), [])
        self.clock.now = 5
        second = self.child()
        self.assertEqual(Decimal(second["limit_price"]), 10)
        self.report(second, "CANCELED", "0", final=True)
        self.assertFalse(self.status()["fallback"])

    def test_behind_progress_changes_price_not_participation(self):
        self.start(mode="normal")
        self.tape("2000")
        self.clock.now = 31
        self.quote()
        child = self.child()
        self.assertEqual(Decimal(child["limit_price"]), 10)
        self.assertEqual(Decimal(self.status()["participation"]), Decimal(".06"))
        self.assertEqual(Decimal(self.status()["earned"]), 120)

    def test_price_floor_upward_tick_and_tiny_depth(self):
        self.start(minimum_price="10.025")
        self.tape()
        self.quote(bid_size="9")
        self.assertEqual(self.engine.tick(), [])
        self.quote(bid_size="1000")
        child = self.child()
        self.assertEqual(Decimal(child["limit_price"]), Decimal("10.03"))
        self.assertIn("minimum price", self.status()["reason"])

    def test_stale_sources_and_sequence_gap_block_until_snapshot(self):
        self.start()
        self.tape()
        self.quote(sequence=1)
        self.quote(sequence=3)
        self.assertEqual(self.engine.tick(), [])
        self.quote(sequence=4)
        self.assertEqual(self.engine.tick(), [])
        self.quote(sequence=10, reset_sequence=True, source_time=self.clock.wall() - 10)
        self.assertEqual(self.engine.tick(), [])
        self.quote(sequence=11)
        self.assertEqual(self.child()["type"], "submit")

    def test_tape_gap_requires_explicit_recovery(self):
        self.start()
        self.tape("500", event_id="one", sequence=1)
        self.tape("500", event_id="three", sequence=3)
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(Decimal(self.status()["earned"]), 50)
        self.engine.set_feed_health("XYZ", True, reset_sequence=True)
        self.tape("500", event_id="four", sequence=4)
        self.assertEqual(Decimal(self.child()["quantity"]), 100)

    def test_deadline_and_disconnect_keep_unknown_commitment(self):
        self.start(horizon_seconds=30)
        self.tape()
        child = self.child()
        self.report(child, "NEW")
        self.engine.set_feed_health("XYZ", False)
        actions = self.engine.tick()
        self.assertEqual([a["type"] for a in actions], ["cancel"])
        self.clock.now = 31
        self.quote()
        actions = self.engine.tick()
        self.assertEqual([a["type"] for a in actions], ["query"])
        result = self.status()
        self.assertEqual(result["state"], "STOPPING")
        self.assertTrue(result["residual_provisional"])
        self.assertEqual(Decimal(result["reserved"]), 100)
        self.report(child, "CANCELED", "30", final=True)
        self.assertEqual(self.status()["state"], "DEADLINE")
        self.assertFalse(self.status()["residual_provisional"])

    def test_restart_reconciles_without_replaying_submit(self):
        self.start()
        self.tape()
        child = self.child()
        self.engine.close()
        self.clock.now = 20
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.quote()
        actions = self.engine.tick()
        self.assertEqual([a["type"] for a in actions], ["query"])
        self.assertEqual(actions[0]["client_order_id"], child["client_order_id"])
        self.assertEqual(Decimal(self.status()["reserved"]), 100)
        self.report(child, "NEW")
        self.assertEqual([a["type"] for a in self.engine.drain_actions()], ["cancel"])

    def test_stop_does_not_refund_without_terminal(self):
        self.start()
        self.tape()
        child = self.child()
        stopped = self.engine.request_stop(self.parent)
        self.assertEqual(stopped["state"], "STOPPING")
        self.assertEqual(Decimal(stopped["reserved"]), 100)
        self.report(child, "NEW")
        self.assertEqual([a["type"] for a in self.engine.drain_actions()], ["cancel"])
        self.report(child, "CANCELED", "10", final=True)
        self.assertEqual(self.status()["state"], "STOPPED")

    def test_failed_cancel_retried_only_after_reconciled_working_and_throttle(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "NEW", sequence=1)
        self.clock.now = 2
        self.assertEqual([a["type"] for a in self.engine.tick()], ["cancel"])
        self.engine.on_action_error(child["client_order_id"], "cancel", "timeout")
        self.engine.drain_actions()
        self.clock.now = 4
        self.report(child, "NEW", sequence=2)
        self.assertEqual(self.engine.drain_actions(), [])
        self.clock.now = 7
        self.engine.tick()
        self.report(child, "NEW", sequence=3)
        self.assertEqual([a["type"] for a in self.engine.drain_actions()], ["cancel"])
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_capacity_counts_credit_and_reservation_with_stress_scenarios(self):
        self.start()
        self.tape("2000")
        self.child()
        self.engine.set_remaining_volume_forecast(self.parent, "10000")
        self.engine.tick()
        capacity = self.status()["capacity"]
        self.assertEqual(Decimal(capacity["unfunded"]), 800)
        self.assertFalse(capacity["scenarios"]["base"]["credit_shortfall"])
        self.assertTrue(capacity["scenarios"]["75_percent"]["credit_shortfall"])
        self.assertTrue(capacity["scenarios"]["50_percent"]["credit_shortfall"])

    def test_json_safe_status_and_durable_event_log(self):
        self.start()
        self.tape()
        self.child()
        self.assertIsInstance(json.dumps(self.engine.list_parents()), str)
        events = self.engine.events(self.parent)
        self.assertTrue(any(e["kind"] == "submit_intent" for e in events))
        self.assertTrue(any(e["kind"] == "tape_credit" for e in events))

    def test_minimum_notional_increases_minimum_legal_child_cap(self):
        self.start(quantity="10", horizon_seconds=7200, min_notional="100")
        self.assertEqual(Decimal(self.status()["child_cap"]), 10)
        self.tape("100")
        self.assertEqual(Decimal(self.child()["quantity"]), 10)

    def test_known_bid_consumption_survives_repeated_old_snapshot(self):
        self.start()
        self.tape("2000")
        first = self.child()
        self.report(first, "FILLED", "100", final=True)
        self.clock.now = 5
        self.quote(source_time=self.clock.wall_start)
        second = self.child()
        self.assertEqual(Decimal(second["quantity"]), 90)

    def test_new_source_snapshot_reconciles_known_consumption(self):
        self.start()
        self.tape("2000")
        first = self.child()
        self.report(first, "FILLED", "100", final=True)
        self.clock.now = 5
        self.quote()
        second = self.child()
        self.assertEqual(Decimal(second["quantity"]), 100)

    def test_broker_rejection_stops_parent_without_retry(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "REJECTED", "0", final=True)
        self.clock.now = 10
        self.quote()
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.status()["state"], "STOPPED")
        self.assertEqual(Decimal(self.status()["free_credit"]), 100)

    def test_persisted_conflict_blocks_a_fresh_parent_after_stop(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "CANCELED", "10", sequence=1, final=True)
        self.report(child, "CANCELED", "20", sequence=2, final=True)
        self.engine.request_stop(self.parent)
        with self.assertRaisesRegex(ValueError, "ledger conflict"):
            self.start()

    def test_historical_child_context_and_broker_lookup(self):
        self.start()
        self.tape()
        child = self.child()
        self.report(child, "CANCELED", "20", sequence=1, final=True)
        context = self.engine.get_order_context(child["client_order_id"])
        self.assertFalse(context["active"])
        self.assertEqual(context["state"], "TERMINAL")
        self.assertEqual(context["cumulative_filled"], "20")
        found = self.engine.find_order_by_broker_id("A", "broker-" + child["client_order_id"])
        self.assertEqual(found, context)
        self.assertIsNone(
            self.engine.find_order_by_broker_id("B", "broker-" + child["client_order_id"])
        )

    def test_submit_action_contains_original_send_time_and_deadline(self):
        parent = self.start()
        self.tape()
        child = self.child()
        self.assertEqual(child["send_timestamp"], self.clock.wall())
        self.assertEqual(child["deadline"], parent["deadline"])
        self.assertEqual(child["send_time"], parent["start"])

    def test_latency_metrics_report_count_median_and_observed_max(self):
        self.start()
        self.tape()
        child = self.child()
        self.clock.now = 0.6
        self.report(child, "NEW")
        metrics = self.status()["latencies"]["send_to_ack"]
        self.assertEqual(metrics["count"], 1)
        self.assertEqual(metrics["median"], 0.6)
        self.assertEqual(metrics["observed_max"], 0.6)
        self.assertNotIn("samples", metrics)

    def test_cumulative_interval_straddling_parent_start_is_not_credited(self):
        self.start()
        self.tape("1000", interval_start=self.clock.wall_start - 5)
        self.assertEqual(Decimal(self.status()["earned"]), 0)
        self.clock.now = 5
        self.tape("1000", event_id="next", interval_start=self.clock.wall_start)
        self.assertEqual(Decimal(self.status()["earned"]), 100)

    def test_final_dispatch_shrinks_to_fresh_depth_and_preserves_accounting(self):
        self.start()
        self.tape()
        original = self.child()
        self.quote(bid_size="400", bid="10.02", ask="10.03")
        updated = self.engine.prepare_dispatch(original["client_order_id"])
        self.assertEqual(Decimal(updated["quantity"]), 40)
        self.assertEqual(Decimal(updated["limit_price"]), Decimal("10.02"))
        self.assertEqual(updated["send_time"], original["send_time"])
        self.assertEqual(Decimal(self.status()["free_credit"]), 60)
        self.assertEqual(Decimal(self.status()["reserved"]), 40)
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))

    def test_final_dispatch_does_not_enlarge_child_on_deeper_book(self):
        self.start()
        self.tape()
        self.quote(bid_size="400")
        original = self.child()
        self.quote(bid_size="10000")
        updated = self.engine.prepare_dispatch(original["client_order_id"])
        self.assertEqual(Decimal(updated["quantity"]), 40)

    def test_halt_before_local_dispatch_releases_unhanded_reservation(self):
        self.start(mode="patient")
        self.tape("4000")
        original = self.child()
        self.engine.set_feed_health("XYZ", False, "halted")
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))
        status = self.status()
        self.assertEqual(Decimal(status["reserved"]), 0)
        self.assertEqual(Decimal(status["free_credit"]), 120)
        self.assertFalse(status["fallback"])
        self.assertEqual(status["state"], "HOLD")
        context = self.engine.get_order_context(original["client_order_id"])
        self.assertEqual(context["state"], "TERMINAL")

    def test_stop_before_dispatch_releases_without_broker_cancel_proof(self):
        self.start()
        self.tape()
        original = self.child()
        self.engine.request_stop(self.parent)
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))
        self.assertEqual(self.status()["state"], "STOPPED")
        self.assertEqual(Decimal(self.status()["reserved"]), 0)

    def test_deadline_before_dispatch_never_hands_off_order(self):
        self.start(horizon_seconds=1)
        self.tape()
        original = self.child()
        self.clock.now = 2
        self.quote()
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))
        self.assertEqual(self.status()["state"], "DEADLINE")
        self.assertEqual(Decimal(self.status()["reserved"]), 0)

    def test_pre_dispatch_shrink_below_notional_minimum_releases_unsent(self):
        self.start(min_notional="500")
        self.tape()
        original = self.child()
        self.quote(bid_size="400")
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))
        self.assertEqual(Decimal(self.status()["reserved"]), 0)
        self.assertIn("minimum", self.status()["reason"])

    def test_pre_dispatch_gate_never_releases_acknowledged_or_prepared_order(self):
        self.start()
        self.tape()
        original = self.child()
        self.report(original, "NEW")
        self.engine.set_feed_health("XYZ", False)
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_long_dispatch_queue_aborts_unsent_child_without_changing_fallback(self):
        self.start()
        self.tape()
        original = self.child()
        self.clock.now = 2
        self.assertIsNone(self.engine.prepare_dispatch(original["client_order_id"]))
        self.assertEqual(Decimal(self.status()["reserved"]), 0)
        self.assertIn("queue", self.status()["reason"])

    def test_market_watermark_persists_across_restart(self):
        checkpoint = {"session": "2026-10-22", "cumulative": "12345", "observed_at": 1800000000}
        self.engine.set_market_checkpoint("xyz", checkpoint)
        self.engine.close()
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.assertEqual(self.engine.get_market_checkpoint("XYZ"), checkpoint)
        self.assertIsNone(self.engine.get_market_checkpoint("OTHER"))

    def test_depth_heartbeat_does_not_replenish_known_bid_consumption(self):
        self.start()
        self.tape("2000")
        child = self.child()
        self.report(child, "FILLED", "100", final=True)
        self.clock.now = 5
        self.quote(preserve_consumption=True)
        self.assertEqual(Decimal(self.child()["quantity"]), 90)

    def test_abort_prepared_but_unpublished_child_preserves_fallback(self):
        self.start(mode="patient")
        self.tape("10000")
        first = self.child()
        self.report(first, "CANCELED", "0", final=True)
        self.clock.now = 5
        self.quote()
        child = self.child()
        self.assertTrue(self.status()["fallback"])
        self.assertIsNotNone(self.engine.prepare_dispatch(child["client_order_id"]))
        self.assertTrue(
            self.engine.abort_dispatch(child["client_order_id"], "local position check failed")
        )
        self.assertEqual(Decimal(self.status()["reserved"]), 0)
        self.assertTrue(self.status()["fallback"])
        self.assertFalse(self.engine.abort_dispatch(child["client_order_id"], "duplicate"))

    def test_abort_known_unsent_can_stop_parent(self):
        self.start()
        self.tape()
        child = self.child()
        self.assertTrue(
            self.engine.abort_dispatch(
                child["client_order_id"], "local holdings insufficient", stop_parent=True
            )
        )
        self.assertEqual(self.status()["state"], "STOPPED")
        self.assertEqual(Decimal(self.status()["reserved"]), 0)

    def test_abort_never_refunds_an_ambiguous_or_acknowledged_order(self):
        self.start()
        self.tape()
        child = self.child()
        self.engine.on_action_error(child["client_order_id"], "submit", "unknown delivery")
        self.assertFalse(self.engine.abort_dispatch(child["client_order_id"], "not proof"))
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_feedback_metadata_conflict_holds_without_releasing(self):
        self.start()
        self.tape()
        child = self.child()
        self.assertTrue(self.engine.hold_order(child["client_order_id"], "quantity mismatch"))
        self.assertEqual(self.status()["state"], "HOLD")
        self.assertEqual(Decimal(self.status()["reserved"]), 100)
        self.assertFalse(self.engine.hold_order("unknown", "conflict"))

    def test_timer_reconciliation_releases_only_never_prepared_queued_intent(self):
        self.start()
        self.tape()
        child = self.child()
        self.clock.now = 4
        self.engine.tick()
        self.assertEqual(self.status()["order_state"], "RECONCILE")
        self.assertIsNone(self.engine.prepare_dispatch(child["client_order_id"]))
        self.assertEqual(Decimal(self.status()["reserved"]), 0)
        self.assertEqual(Decimal(self.status()["free_credit"]), 100)

    def test_prepared_timer_reconciliation_keeps_ambiguous_reservation(self):
        self.start()
        self.tape()
        child = self.child()
        self.assertIsNotNone(self.engine.prepare_dispatch(child["client_order_id"]))
        self.clock.now = 4
        self.engine.tick()
        self.assertIsNone(self.engine.prepare_dispatch(child["client_order_id"]))
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_transport_uncertainty_never_becomes_a_local_queue_abort(self):
        self.start()
        self.tape()
        child = self.child()
        self.engine.on_action_error(child["client_order_id"], "submit", "ambiguous delivery")
        self.clock.now = 4
        self.engine.tick()
        self.assertIsNone(self.engine.prepare_dispatch(child["client_order_id"]))
        self.assertEqual(Decimal(self.status()["reserved"]), 100)

    def test_external_decimal_bounds_do_not_truncate_derived_fractional_credit(self):
        from trading_ui.services.smart_selling_policy import parse_input_decimal

        for value in ["1e25", "1e-25", "123456789012345678901234.1234567"]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "precision"):
                    parse_input_decimal(value)
        self.start(mode="patient", quantity="1", quantity_step="1e-24", min_quantity="1e-24")
        self.tape("1e-24")
        self.assertEqual(Decimal(self.status()["free_credit"]), Decimal("3e-26"))
        with self.assertRaisesRegex(ValueError, "precision"):
            self.tape("1e-25", event_id="out-of-bounds")

    def test_derived_volume_forecast_preserves_fractional_rate_precision(self):
        self.start()
        one_share_per_minute = Decimal(1) / Decimal(60)
        for remaining_seconds in [Decimal(7200), Decimal(300)]:
            forecast = one_share_per_minute * remaining_seconds
            with self.subTest(forecast=forecast):
                self.engine.set_remaining_volume_forecast(self.parent, forecast)
                self.engine.tick()
                capacity = self.status()["capacity"]
                self.assertTrue(capacity["forecast_available"])
                self.assertEqual(
                    Decimal(capacity["scenarios"]["base"]["eligible_volume"]), forecast
                )
        for invalid in [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.engine.set_remaining_volume_forecast(self.parent, invalid)

    def test_newer_working_report_after_terminal_quarantines_the_current_child(self):
        self.start()
        self.tape("2000")
        archived = self.child()
        self.report(archived, "CANCELED", "0", sequence=2, final=True)
        self.clock.now = 5
        self.quote()
        current = self.child()
        self.report(current, "NEW", "0", sequence=1)

        self.report(archived, "NEW", "0", sequence=1)
        self.assertEqual(self.status()["order_state"], "LIVE")
        self.assertNotEqual(self.status()["state"], "HOLD")

        self.report(archived, "NEW", "0", sequence=3)
        status = self.status()
        self.assertEqual(status["state"], "HOLD")
        self.assertEqual(status["order_state"], "RECONCILE")
        self.assertEqual(status["active_order"]["client_order_id"], current["client_order_id"])
        self.assertEqual(Decimal(status["reserved"]), 100)
        self.assertEqual(Decimal(status["filled"]), 0)
        self.assertIn("working after terminal", status["reason"])
        actions = self.engine.tick()
        self.assertTrue(
            any(
                action["type"] == "cancel"
                and action["client_order_id"] == current["client_order_id"]
                for action in actions
            )
        )

    def test_round_robin_uses_one_shared_slice_and_participation_budget(self):
        self.start(
            account_ids=["A", "B", "C"], account_quantities={"A": "400", "B": "400", "C": "400"}
        )
        self.tape("2000")
        first = self.child()
        self.assertEqual((first["account_id"], first["quantity"]), ("A", "100"))
        self.assertEqual(self.status()["earned"], "200.00")
        self.assertEqual(self.engine.tick(), [])
        for index, expected in enumerate(["B", "C", "A"], start=1):
            self.report(first, "FILLED", first["quantity"], final=True)
            self.clock.now = 5 * index
            self.quote()
            self.tape("1000", event_id=f"rotation-{index}")
            first = self.child()
            self.assertEqual(first["account_id"], expected)
            self.assertEqual(Decimal(first["quantity"]), 100)
            self.status()

    def test_short_inventory_spills_only_remainder_and_retires_empty_accounts(self):
        self.start(
            account_ids=["empty", "A", "B", "C"],
            account_quantities={"empty": "0", "A": "40", "B": "30", "C": "930"},
        )
        self.tape("2000")
        for now, expected_account, expected_quantity in [
            (0, "A", "40"),
            (5, "B", "30"),
            (10, "C", "30"),
            (15, "C", "100"),
        ]:
            self.clock.now = now
            self.quote()
            child = self.child()
            self.assertEqual(
                (child["account_id"], child["quantity"]), (expected_account, expected_quantity)
            )
            if now == 0:
                self.assertEqual(self.status()["accounts"]["A"]["reserved"], "40")
                self.clock.now = 1
                self.assertEqual(self.engine.tick(), [])
            self.report(child, "FILLED", child["quantity"], final=True)
            status = self.status()
        self.assertTrue(status["accounts"]["empty"]["done"])
        self.assertTrue(status["accounts"]["A"]["done"])
        self.assertTrue(status["accounts"]["B"]["done"])
        self.assertEqual(status["accounts"]["C"]["filled"], "130")

    def test_split_slice_keeps_remainder_when_fresh_depth_is_smaller(self):
        self.start(
            account_ids=["A", "B", "C"], account_quantities={"A": "40", "B": "500", "C": "460"}
        )
        self.tape("2000")
        first = self.child()
        self.report(first, "FILLED", "40", final=True)
        self.clock.now = 5
        self.quote(bid_size="500")
        second = self.child()
        self.assertEqual((second["account_id"], second["quantity"]), ("B", "50"))
        self.report(second, "FILLED", "50", final=True)
        self.clock.now = 10
        self.quote()
        third = self.child()
        self.assertEqual((third["account_id"], third["quantity"]), ("C", "10"))

    def test_partial_fill_cancel_refunds_only_selected_account_and_rotates(self):
        self.start(account_ids=["A", "B"], account_quantities={"A": "500", "B": "500"})
        self.tape("2000")
        first = self.child()
        self.report(first, "PARTIALLY_FILLED", "30")
        self.clock.now = 2
        actions = self.engine.tick()
        self.assertEqual([(a["type"], a["account_id"]) for a in actions], [("cancel", "A")])
        before = self.status()
        self.assertEqual(before["accounts"]["A"]["reserved"], "70")
        self.assertEqual(before["accounts"]["B"]["reserved"], "0")
        self.report(first, "CANCELED", "40", final=True)
        after = self.status()
        self.assertEqual(after["accounts"]["A"]["filled"], "40")
        self.assertEqual(after["accounts"]["A"]["reserved"], "0")
        self.clock.now = 5
        self.quote()
        second = self.child()
        self.assertEqual(second["account_id"], "B")
        self.report(second, "NEW")
        context = self.engine.get_order_context(second["client_order_id"])
        self.assertEqual(context["account_id"], "B")
        self.assertEqual(
            self.engine.find_order_by_broker_id("B", "broker-" + second["client_order_id"]), context
        )
        self.assertIsNone(
            self.engine.find_order_by_broker_id("A", "broker-" + second["client_order_id"])
        )

    def test_fractional_fill_never_creates_an_off_step_account_order(self):
        self.start(account_ids=["A", "B"], account_quantities={"A": "100", "B": "900"})
        self.tape("3000")
        first = self.child()
        self.report(first, "CANCELED", "20.5", final=True)
        self.clock.now = 5
        self.quote()
        second = self.child()
        self.assertEqual(second["account_id"], "B")
        self.report(second, "FILLED", second["quantity"], final=True)
        self.clock.now = 10
        self.quote()
        self.tape("1000", event_id="fractional-fill-followup")
        third = self.child()
        self.assertEqual((third["account_id"], third["quantity"]), ("A", "79"))
        account = self.status()["accounts"]["A"]
        self.assertEqual(account["remaining"], "79.5")
        self.assertEqual(Decimal(account["reserved"]), 79)

    def test_multi_account_pre_dispatch_refund_keeps_account_reservations_exact(self):
        self.start(account_ids=["A", "B"], account_quantities={"A": "500", "B": "500"})
        self.tape("2000")
        first = self.child()
        self.quote(bid_size="400")
        prepared = self.engine.prepare_dispatch(first["client_order_id"])
        self.assertEqual(prepared["account_id"], "A")
        self.assertEqual(self.status()["accounts"]["A"]["reserved"], "40")
        self.assertTrue(self.engine.abort_dispatch(first["client_order_id"], "not published"))
        self.assertEqual(self.status()["accounts"]["A"]["reserved"], "0")

    def test_round_robin_and_inventory_survive_restart(self):
        self.start(account_ids=["A", "B"], account_quantities={"A": "40", "B": "960"})
        self.tape("2000")
        first = self.child()
        self.report(first, "FILLED", "40", final=True)
        self.engine.close()
        self.clock.now = 5
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.quote()
        second = self.child()
        self.assertEqual((second["account_id"], second["quantity"]), ("B", "60"))
        self.assertTrue(self.status()["accounts"]["A"]["done"])

    def test_explicit_target_remains_supported_without_exceeding_inventory(self):
        self.start(
            quantity="150",
            horizon_seconds=30,
            account_ids=["A", "B"],
            account_quantities={"A": "40", "B": "960"},
        )
        self.tape("2000")
        for now, account, quantity in [(0, "A", "40"), (5, "B", "60"), (10, "B", "50")]:
            self.clock.now = now
            self.quote()
            child = self.child()
            self.assertEqual((child["account_id"], child["quantity"]), (account, quantity))
            self.report(child, "FILLED", quantity, final=True)
        status = self.status()
        self.assertEqual(status["state"], "COMPLETED")
        self.assertEqual(status["filled"], "150")
        self.assertEqual(status["accounts"]["B"]["remaining"], "850")
        self.assertEqual(self.engine.tick(), [])

    def test_selected_account_ownership_and_inventory_are_validated(self):
        self.start(account_ids=["A", "B"], account_quantities={"A": "500", "B": "500"})
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.start(account_id="B")
        with self.assertRaisesRegex(ValueError, "available inventory"):
            self.start(account_ids=["C", "D"], account_quantities={"C": "40", "D": "50"})
        with self.assertRaisesRegex(ValueError, "account quantity"):
            self.start(account_ids=["C", "D"], account_quantities={"C": "-1", "D": "1001"})

    def test_no_deadline_runs_past_sizing_horizon_and_survives_restart(self):
        parent = self.start(deadline=None, start_time="now")
        self.assertIsNone(parent["deadline"])
        self.engine.close()
        self.clock.now = 10000
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        self.quote()
        self.tape()
        child = self.child()
        self.assertIsNone(child["deadline"])
        self.assertTrue(self.status()["active"])
        self.assertEqual(self.status()["state"], "RUNNING")

    def test_no_deadline_does_not_create_schedule_pressure(self):
        self.start(mode="patient", deadline=None)
        self.clock.now = 10000
        self.quote(bid="10", ask="10.10")
        self.tape("4000")
        self.assertEqual(Decimal(self.child()["limit_price"]), Decimal("10.10"))

    def test_legacy_parent_is_upgraded_without_losing_reserved_inventory(self):
        self.start()
        self.tape()
        child = self.child()
        row = self.engine._db.execute(
            "SELECT data FROM smart_parents WHERE id=?", (self.parent,)
        ).fetchone()
        legacy = json.loads(row[0])
        for key in ["accounts", "account_ids", "next_account_index", "slice_remaining"]:
            legacy.pop(key)
        legacy["order"].pop("account_id")
        self.engine._db.execute(
            "UPDATE smart_parents SET data=? WHERE id=?", (json.dumps(legacy), self.parent)
        )
        self.engine.close()
        self.engine = SmartSellingEngine(
            self.path, clock=self.clock.monotonic, wall_clock=self.clock.wall
        )
        status = self.status()
        self.assertEqual(status["account_ids"], ["A"])
        self.assertEqual(status["accounts"]["A"]["reserved"], "100")
        self.assertEqual(status["active_order"]["account_id"], "A")
        self.report(child, "CANCELED", "20", final=True)
        self.assertEqual(self.status()["accounts"]["A"]["filled"], "20")

    def test_defaults_and_required_broker_rules(self):
        parent = self.start(mode="2", horizon_seconds=7200)
        self.assertEqual(parent["mode"], "normal")
        with self.assertRaises(ValueError):
            self.engine.start_parent(dict(account_id="B", symbol="XYZ", quantity="100"))
        with self.assertRaises(ValueError):
            self.start(account_id="B", quantity="100.5")


if __name__ == "__main__":
    unittest.main()
