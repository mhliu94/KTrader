import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trading_ui.reconcile_smart_selling import reconcile_stopped_child
from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_feedback import SmartSellingFeedback


class StoppedChildReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.feedback_path = Path(self.temp.name) / "feedback.sqlite3"
        self.epoch = 1800000000
        self.engine = SmartSellingEngine(self.path, clock=lambda: 0,
                                         wall_clock=lambda: self.epoch)
        self.feedback = SmartSellingFeedback(self.engine, state_path=self.feedback_path)
        self.parent_id = self.engine.start_parent({
            "account_id": "A", "symbol": "XYZ", "quantity": 1000,
            "mode": "urgent", "horizon_seconds": 300,
            "tick_size": ".01", "quantity_step": "1", "min_quantity": "100", "min_notional": "0",
        })["parent_id"]
        self.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=self.epoch)
        self.engine.on_trade("XYZ", 1000, "trade", source_time=self.epoch)
        self.child = next(a for a in self.engine.tick() if a["type"] == "submit")
        self.client_id = self.child["client_order_id"]
        self.engine.prepare_dispatch(self.client_id)
        self.engine.request_stop(self.parent_id)
        timestamp = datetime.fromtimestamp(self.epoch + 8, timezone.utc).isoformat()
        self.snapshot = {
            "schema_version": 1, "account_id": "A", "order_id": "broker-1",
            "sequence": 1, "created_at": timestamp, "updated_at": timestamp,
            "symbol": "XYZ", "side": "SELL", "order_type": "LIMIT",
            "status": "FILLED", "quantity": 100, "filled_quantity": 100,
            "average_fill_price": 10, "limit_price": 10,
        }
        # A publisher that omits both client IDs reproduces the stuck lock.
        self.assertFalse(self.feedback.apply_snapshot(self.snapshot))
        self.assertTrue(self.engine.get_parent(self.parent_id)["active"])

    def tearDown(self):
        self.feedback.close()
        self.engine.close()
        self.temp.cleanup()

    def repair(self, **kwargs):
        return reconcile_stopped_child(self.path, self.feedback_path, self.client_id,
                                       self.snapshot, source="order-status[0]@123", **kwargs)

    def test_preview_preserves_live_state_and_apply_releases_account_with_fill(self):
        before = self.engine.get_parent(self.parent_id)
        preview = self.repair()
        self.assertFalse(preview["applied"])
        self.assertFalse(preview["parent"]["active"])
        self.assertEqual(self.engine.get_parent(self.parent_id), before)
        result = self.repair(apply=True)
        after = self.engine.get_parent(self.parent_id)
        self.assertTrue(result["applied"])
        self.assertFalse(after["active"])
        self.assertEqual(after["filled"], "100")
        self.assertEqual(after["reserved"], "0")
        self.assertIsNone(after["active_order"])
        self.assertEqual(self.engine.get_order_context(self.client_id)["broker_order_id"], "broker-1")
        audit = next(e for e in self.engine.events(self.parent_id)
                     if e["kind"] == "manual_terminal_reconciliation")
        self.assertEqual(audit["data"]["snapshot"], self.snapshot)
        self.assertEqual(audit["data"]["source"], "order-status[0]@123")
        self.assertEqual(audit["data"]["previous_ledger"]["R"], "100")
        with self.assertRaisesRegex(ValueError, "outstanding child"):
            self.repair(apply=True)
        self.assertEqual(self.engine.get_parent(self.parent_id)["filled"], "100")

    def test_preserves_other_sessions_and_outbox(self):
        other = self.engine.start_parent({
            "account_id": "B", "symbol": "ABC", "quantity": 1000,
            "tick_size": ".01", "quantity_step": "1", "min_quantity": "100", "min_notional": "0",
        })
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO smart_outbox(data,dispatched) VALUES ('{}',0)")
            before = db.execute("SELECT * FROM smart_outbox").fetchall()
        self.repair(apply=True)
        self.assertEqual(self.engine.get_parent(other["parent_id"]), other)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM smart_outbox").fetchall(), before)

    def test_rejects_nonterminal_conflicting_and_mismatched_evidence(self):
        original = dict(self.snapshot)
        for changes in (
            {"status": "UNKNOWN"}, {"command_id": "another-command"},
            {"quantity": 101, "filled_quantity": 101}, {"limit_price": 11},
            {"account_id": "B"}, {"symbol": "ABC"}, {"sequence": 0},
            {"created_at": datetime.fromtimestamp(self.epoch - 1, timezone.utc).isoformat()},
        ):
            with self.subTest(changes=changes):
                self.snapshot = {**original, **changes}
                with self.assertRaises(ValueError):
                    self.repair(apply=True)
                self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "100")

    def test_newer_unknown_feedback_blocks_old_terminal_snapshot(self):
        self.feedback.apply_snapshot({**self.snapshot, "sequence": 2, "status": "UNKNOWN"})
        with self.assertRaisesRegex(ValueError, "latest durable"):
            self.repair(apply=True)

    def test_broker_order_already_owned_by_another_child_is_rejected(self):
        with sqlite3.connect(self.path) as db:
            raw = db.execute("SELECT data FROM smart_orders WHERE id=?", (self.client_id,)).fetchone()[0]
            archived = json.loads(raw)
            archived.update(client_order_id="different-child", broker_order_id="broker-1")
            db.execute("INSERT INTO smart_orders VALUES (?,?,?)",
                       ("different-child", self.parent_id, json.dumps(archived)))
        with self.assertRaisesRegex(ValueError, "another child"):
            self.repair(apply=True)
        self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "100")

    def test_running_session_cannot_be_repaired(self):
        with sqlite3.connect(self.path) as db:
            parent = json.loads(db.execute("SELECT data FROM smart_parents WHERE id=?",
                                           (self.parent_id,)).fetchone()[0])
            parent["stop_requested"] = False
            db.execute("UPDATE smart_parents SET data=? WHERE id=?",
                       (json.dumps(parent), self.parent_id))
        with self.assertRaisesRegex(ValueError, "stopped session"):
            self.repair(apply=True)


if __name__ == "__main__":
    unittest.main()
