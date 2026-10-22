"""Existing order-status snapshots drive the control server without broker I/O."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_feedback import (
    SmartSellingFeedback,
    parse_smart_selling_snapshot,
)


class SmartSellingFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "smart.sqlite3"
        self.now = 0.0
        self.wall_start = 1800000000
        self.engine = self.make_engine()
        self.feedback = SmartSellingFeedback(self.engine)
        self.parent = self.engine.start_parent(
            {
                "account_id": "A",
                "symbol": "XYZ",
                "quantity": "1000",
                "mode": "urgent",
                "horizon_seconds": 300,
                "tick_size": ".01",
                "quantity_step": "1",
                "min_quantity": "1",
                "min_notional": "0",
            }
        )["parent_id"]
        self.quote()
        self.engine.on_trade("XYZ", "1000", "trade-1", source_time=self.wall_start)
        self.child = next(action for action in self.engine.tick() if action["type"] == "submit")

    def tearDown(self):
        self.feedback.close()
        self.engine.close()
        self.temp.cleanup()

    def make_engine(self):
        return SmartSellingEngine(
            self.path, clock=lambda: self.now, wall_clock=lambda: self.wall_start + self.now
        )

    def quote(self):
        self.engine.on_quote("XYZ", "10", "10.01", "1000", source_time=self.wall_start + self.now)

    def status(self):
        return self.engine.get_parent(self.parent)

    def snapshot(self, **changes):
        timestamp = datetime.fromtimestamp(self.wall_start, timezone.utc).isoformat()
        payload = {
            "schema_version": 1,
            "account_id": "A",
            "order_id": "broker-1",
            "sequence": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "symbol": "XYZ",
            "side": "SELL",
            "order_type": "LIMIT",
            "status": "NEW",
            "quantity": 100,
            "filled_quantity": 0,
            "average_fill_price": None,
            "limit_price": 10,
            "command_id": self.child["client_order_id"],
        }
        payload.update(changes)
        return payload

    def test_existing_command_id_correlates_without_custom_protocol(self):
        self.assertTrue(self.feedback.apply_snapshot(self.snapshot()))
        self.assertEqual(self.status()["active_order"]["broker_order_id"], "broker-1")
        self.assertEqual(self.status()["reserved"], "100")

    def test_existing_client_order_id_can_correlate_instead(self):
        payload = self.snapshot(client_order_id=self.child["client_order_id"])
        payload.pop("command_id")
        self.assertTrue(self.feedback.apply_snapshot(json.dumps(payload).encode()))

    def test_later_broker_id_only_snapshot_uses_persisted_ownership(self):
        self.feedback.apply_snapshot(self.snapshot())
        payload = self.snapshot(
            sequence=2, status="PARTIALLY_FILLED", filled_quantity=40, average_fill_price=10
        )
        payload.pop("command_id")
        self.assertTrue(self.feedback.apply_snapshot(payload))
        self.assertEqual(self.status()["filled"], "40")
        self.assertEqual(self.status()["reserved"], "60")

    def test_no_guess_from_same_symbol_price_quantity(self):
        payload = self.snapshot(command_id="unrelated-manual-order", order_id="other-broker-order")
        self.assertFalse(self.feedback.apply_snapshot(payload))
        self.assertIsNone(self.status()["active_order"]["broker_order_id"])
        self.assertEqual(self.status()["filled"], "0")

    def test_strict_account_symbol_side_type_quantity_and_price_matching(self):
        for field, value in (
            ("account_id", "B"),
            ("symbol", "OTHER"),
            ("side", "BUY"),
            ("order_type", "MARKET"),
            ("quantity", 101),
            ("limit_price", 9.99),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.feedback.apply_snapshot(self.snapshot(**{field: value}))
                self.assertEqual(self.status()["filled"], "0")
                self.assertEqual(self.status()["reserved"], "100")

    def test_broker_id_cannot_change_after_binding(self):
        self.feedback.apply_snapshot(self.snapshot())
        with self.assertRaises(ValueError):
            self.feedback.apply_snapshot(
                self.snapshot(
                    sequence=2, order_id="different-broker-id", status="FILLED", filled_quantity=100
                )
            )
        self.assertEqual(self.status()["reserved"], "100")

    def test_partial_fill_during_cancel_only_final_cumulative_refunds_leaves(self):
        self.feedback.apply_snapshot(
            self.snapshot(sequence=1, status="PARTIALLY_FILLED", filled_quantity=40)
        )
        self.feedback.apply_snapshot(
            self.snapshot(sequence=2, status="PENDING_CANCEL", filled_quantity=60)
        )
        self.assertEqual(self.status()["filled"], "60")
        self.assertEqual(self.status()["reserved"], "40")
        self.feedback.apply_snapshot(
            self.snapshot(sequence=3, status="CANCELED", filled_quantity=60)
        )
        self.assertEqual(self.status()["filled"], "60")
        self.assertEqual(self.status()["reserved"], "0")
        self.assertEqual(Decimal(self.status()["free_credit"]), 40)

    def test_duplicate_and_reordered_snapshots_do_not_double_count(self):
        partial = self.snapshot(sequence=2, status="PARTIALLY_FILLED", filled_quantity=40)
        self.feedback.apply_snapshot(partial)
        self.feedback.apply_snapshot(partial)
        self.feedback.apply_snapshot(self.snapshot(sequence=1))
        self.assertEqual(self.status()["filled"], "40")
        terminal = self.snapshot(sequence=3, status="CANCELED", filled_quantity=40)
        self.feedback.apply_snapshot(terminal)
        self.feedback.apply_snapshot(terminal)
        self.assertEqual(Decimal(self.status()["free_credit"]), 60)
        self.assertEqual(self.status()["reserved"], "0")

    def test_stale_sequence_with_contradictory_higher_fill_triggers_core_hold(self):
        self.feedback.apply_snapshot(
            self.snapshot(sequence=2, status="PARTIALLY_FILLED", filled_quantity=40)
        )
        self.feedback.apply_snapshot(
            self.snapshot(sequence=1, status="PARTIALLY_FILLED", filled_quantity=60)
        )
        self.assertIn("conflicting reordered fill", self.status()["reason"])
        self.assertEqual(self.status()["filled"], "40")

    def test_newer_lower_fill_triggers_execution_bust_hold(self):
        self.feedback.apply_snapshot(
            self.snapshot(sequence=1, status="PARTIALLY_FILLED", filled_quantity=40)
        )
        self.feedback.apply_snapshot(
            self.snapshot(sequence=2, status="PARTIALLY_FILLED", filled_quantity=20)
        )
        self.assertIn("execution bust", self.status()["reason"])
        self.assertEqual(self.status()["reserved"], "60")

    def test_unknown_is_not_new_fill_authority_or_terminal_evidence(self):
        self.feedback.apply_snapshot(
            self.snapshot(sequence=2, status="UNKNOWN", filled_quantity=40)
        )
        self.assertEqual(self.status()["filled"], "0")
        self.assertEqual(self.status()["reserved"], "100")
        self.assertEqual(self.status()["order_state"], "RECONCILE")
        # An older terminal encountered in replay cannot override uncertainty.
        self.feedback.apply_snapshot(self.snapshot(sequence=1, status="CANCELED"))
        self.assertEqual(self.status()["reserved"], "100")

    def test_reconciliation_only_replays_known_evidence(self):
        self.assertFalse(self.feedback.reconcile(self.child["client_order_id"]))
        self.assertEqual(self.status()["reserved"], "100")
        self.feedback.apply_snapshot(self.snapshot())
        previous = self.feedback.latest_snapshot(self.child["client_order_id"])
        self.now = 30
        self.assertTrue(self.feedback.reconcile(self.child["client_order_id"]))
        self.assertEqual(previous, self.feedback.latest_snapshot(self.child["client_order_id"]))
        self.assertEqual(self.status()["reserved"], "100")

    def test_restart_replays_terminal_and_reuses_durable_broker_binding(self):
        self.feedback.apply_snapshot(self.snapshot(status="PARTIALLY_FILLED", filled_quantity=40))
        self.engine.close()
        self.feedback.close()
        self.engine = self.make_engine()
        self.feedback = SmartSellingFeedback(self.engine)
        terminal = self.snapshot(sequence=2, status="CANCELED", filled_quantity=60)
        terminal.pop("command_id")
        self.assertTrue(self.feedback.apply_snapshot(terminal))
        self.assertEqual(self.status()["filled"], "60")
        self.assertEqual(self.status()["reserved"], "0")

    def test_archived_duplicate_cannot_release_newer_child(self):
        terminal = self.snapshot(status="CANCELED")
        self.feedback.apply_snapshot(terminal)
        self.now = 5
        self.quote()
        second = next(action for action in self.engine.tick() if action["type"] == "submit")
        self.assertNotEqual(second["client_order_id"], self.child["client_order_id"])
        self.feedback.apply_snapshot(terminal)
        self.assertEqual(self.status()["reserved"], "100")
        self.assertEqual(
            self.status()["active_order"]["client_order_id"], second["client_order_id"]
        )
        self.feedback.apply_snapshot(
            self.snapshot(sequence=2, status="CANCELED", filled_quantity=10)
        )
        self.assertIn("conflicting archived fill", self.status()["reason"])
        self.assertEqual(self.status()["reserved"], "100")

    def test_snapshot_schema_rejects_nonfinite_and_inconsistent_terminal(self):
        with self.assertRaises(ValueError):
            parse_smart_selling_snapshot(self.snapshot(filled_quantity=float("nan")))
        with self.assertRaises(ValueError):
            parse_smart_selling_snapshot(self.snapshot(status="FILLED", filled_quantity=99))
        with self.assertRaises(ValueError):
            parse_smart_selling_snapshot(self.snapshot(authoritative=True))

    def test_source_time_and_original_creation_time_remain_consistent(self):
        self.feedback.apply_snapshot(self.snapshot(updated_at="2027-01-15T08:00:01+00:00"))
        with self.assertRaises(ValueError):
            self.feedback.apply_snapshot(self.snapshot(sequence=2))
        with self.assertRaises(ValueError):
            self.feedback.apply_snapshot(
                self.snapshot(
                    sequence=2,
                    created_at="2027-01-15T08:00:01+00:00",
                    updated_at="2027-01-15T08:00:01+00:00",
                )
            )

    def test_latest_snapshot_returns_a_copy(self):
        self.feedback.apply_snapshot(self.snapshot())
        saved = self.feedback.latest_snapshot(self.child["client_order_id"])
        saved["filled_quantity"] = 100
        self.assertEqual(
            self.feedback.latest_snapshot(self.child["client_order_id"])["filled_quantity"], 0
        )

    def test_manual_open_orders_are_tracked_without_owning_the_child(self):
        manual = self.snapshot(command_id="manual-1", order_id="manual-broker-1")
        self.assertFalse(self.feedback.apply_snapshot(manual))
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))
        self.assertFalse(self.feedback.has_open_orders("B", "XYZ"))
        self.assertFalse(self.feedback.has_open_orders("A", "OTHER"))
        self.feedback.apply_snapshot(dict(manual, sequence=2, status="CANCELED"))
        self.assertFalse(self.feedback.has_open_orders("A", "XYZ"))
        self.feedback.apply_snapshot(manual)
        self.assertFalse(self.feedback.has_open_orders("A", "XYZ"))

    def test_unknown_manual_order_is_an_unresolved_commitment(self):
        self.feedback.apply_snapshot(self.snapshot(command_id="manual", status="UNKNOWN"))
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))

    def test_terminal_sequence_watermark_survives_feedback_restart(self):
        self.feedback.close()
        feedback_path = Path(self.temp.name) / "feedback.sqlite3"
        self.feedback = SmartSellingFeedback(self.engine, state_path=feedback_path)
        manual = self.snapshot(command_id="manual-1", order_id="manual-broker-1")
        self.feedback.apply_snapshot(dict(manual, sequence=2, status="CANCELED"))
        self.feedback.close()
        self.feedback = SmartSellingFeedback(self.engine, state_path=feedback_path)
        self.feedback.apply_snapshot(manual)
        self.assertFalse(self.feedback.has_open_orders("A", "XYZ"))

    def test_pending_manual_handoff_requires_exact_terminal_correlation(self):
        command = {"command_id": "manual-pending", "account_id": "A", "symbol": "US.XYZ"}
        self.feedback.track_external_order(command)
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))
        unrelated = self.snapshot(
            command_id="someone-else", order_id="unrelated", status="CANCELED"
        )
        self.feedback.apply_snapshot(unrelated)
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))
        wrong_account = self.snapshot(
            command_id="manual-pending", account_id="B", order_id="wrong", status="CANCELED"
        )
        self.feedback.apply_snapshot(wrong_account)
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))
        terminal = self.snapshot(
            command_id="manual-pending", order_id="manual-broker", status="CANCELED"
        )
        self.feedback.apply_snapshot(terminal)
        self.assertFalse(self.feedback.has_open_orders("A", "XYZ"))
        self.feedback.track_external_order(command)
        self.assertFalse(self.feedback.has_open_orders("A", "XYZ"))

    def test_pending_manual_handoff_and_terminal_watermark_are_durable(self):
        self.feedback.close()
        path = Path(self.temp.name) / "feedback.sqlite3"
        self.feedback = SmartSellingFeedback(self.engine, state_path=path)
        self.feedback.track_external_order(
            {"command_id": "manual", "account_id": "A", "symbol": "XYZ"}
        )
        self.feedback.close()
        self.feedback = SmartSellingFeedback(self.engine, state_path=path)
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))
        self.feedback.apply_snapshot(self.snapshot(command_id="manual", status="NEW", sequence=2))
        self.feedback.apply_snapshot(
            self.snapshot(command_id="manual", status="CANCELED", sequence=1)
        )
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))
        self.feedback.apply_snapshot(
            self.snapshot(command_id="manual", status="CANCELED", sequence=3)
        )
        self.assertFalse(self.feedback.has_open_orders("A", "XYZ"))

    def test_prefixed_manual_symbols_block_same_canonical_instrument(self):
        self.feedback.apply_snapshot(self.snapshot(command_id="manual", symbol="US.XYZ"))
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))

    def test_one_terminal_snapshot_cannot_clear_two_external_commands(self):
        for identity in ("first-manual", "second-manual"):
            self.feedback.track_external_order(
                {"command_id": identity, "account_id": "A", "symbol": "XYZ"}
            )
        with self.assertRaises(ValueError):
            self.feedback.apply_snapshot(
                self.snapshot(
                    command_id="first-manual", client_order_id="second-manual", status="CANCELED"
                )
            )
        self.assertTrue(self.feedback.has_open_orders("A", "XYZ"))

    def test_archived_metadata_conflict_holds_newer_parent_without_refunding(self):
        self.feedback.apply_snapshot(self.snapshot(status="CANCELED"))
        self.engine.request_stop(self.parent)
        next_parent = self.engine.start_parent(
            {
                "account_id": "A",
                "symbol": "XYZ",
                "quantity": "1000",
                "mode": "urgent",
                "horizon_seconds": 300,
                "tick_size": ".01",
                "quantity_step": "1",
                "min_quantity": "1",
            }
        )["parent_id"]
        self.quote()
        self.engine.on_trade("XYZ", "1000", "next-parent-print", source_time=self.wall_start)
        self.engine.tick()
        with self.assertRaises(ValueError):
            self.feedback.apply_snapshot(
                self.snapshot(sequence=2, status="CANCELED", filled_quantity=10, limit_price=9.99)
            )
        current = self.engine.get_parent(next_parent)
        self.assertIn("conflicting order metadata", current["reason"])
        self.assertEqual(current["reserved"], "100")
        self.assertEqual(current["filled"], "0")

    def test_wrong_account_cannot_quarantine_a_different_accounts_child(self):
        with self.assertRaises(ValueError):
            self.feedback.apply_snapshot(self.snapshot(account_id="B"))
        self.assertNotIn("conflicting order", self.status()["reason"])


if __name__ == "__main__":
    unittest.main()
