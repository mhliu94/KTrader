"""Explicitly reconcile a stopped child against reviewed terminal broker evidence.

This operator utility never submits, cancels, or publishes an order. It requires
an explicit client-to-broker order mapping; the live consumer still never guesses
ownership from symbol, price, quantity, or time. Preview is the CLI default.
"""

import argparse
from datetime import timezone
import json
from pathlib import Path
import sqlite3

from .services.smart_selling import SmartSellingEngine
from .services.smart_selling_feedback import (
    SmartSellingFeedback,
    parse_smart_selling_snapshot,
)
from .services.smart_selling_feedback_store import TERMINAL_STATUSES, canonical_order_symbol


def reconcile_stopped_child(state_db, feedback_db, client_order_id, snapshot, *, source, apply=False):
    """Apply only a reviewed final snapshot already present in the feedback store.

Lock both durable stores while validating and updating the affected rows. Run
the normal engine transition in memory: opening a second live engine would run
restart recovery and disturb unrelated sessions and pending outbox entries.
"""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("A source reference for the reviewed evidence is required")
    event = parse_smart_selling_snapshot(snapshot)
    payload = event.payload
    if payload["status"] not in TERMINAL_STATUSES:
        raise ValueError("Authoritative terminal broker status is required")
    for field in ("command_id", "client_order_id"):
        if payload.get(field) and payload[field] != client_order_id:
            raise ValueError("Snapshot identifies a different client order")
    state_path, feedback_path = Path(state_db).resolve(), Path(feedback_db).resolve()
    if not state_path.is_file() or not feedback_path.is_file() or state_path == feedback_path:
        raise ValueError("Existing, separate state and feedback databases are required")

    engine = SmartSellingEngine(":memory:")
    db = sqlite3.connect(state_path.as_uri() + "?mode=rw", uri=True, timeout=10,
                         isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("ATTACH DATABASE ? AS feedback", (feedback_path.as_uri() + "?mode=rw",))
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT parent_id,data FROM smart_orders WHERE id=?",
                         (client_order_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown Smart selling child")
        parent_row = db.execute("SELECT * FROM smart_parents WHERE id=?",
                                (row["parent_id"],)).fetchone()
        parent = json.loads(parent_row["data"])
        child = parent.get("order")
        if not (parent["active"] and parent.get("stop_requested") and child
                and child["client_order_id"] == client_order_id):
            raise ValueError("Only an outstanding child of a stopped session can be reconciled")
        if parent.get("hold") or not child.get("dispatch_prepared"):
            raise ValueError("Conflicting or undispatched children require separate investigation")
        if child["broker_order_id"] not in (None, payload["order_id"]):
            raise ValueError("Child is already bound to a different broker order")
        if event.created_at.timestamp() < child["send_wall"]:
            raise ValueError("Broker order predates the child submission")
        other = db.execute(
            "SELECT child.id FROM smart_orders child "
            "JOIN smart_parents parent ON parent.id=child.parent_id "
            "WHERE COALESCE(json_extract(child.data,'$.account_id'),parent.account_id)=? "
            "AND json_extract(child.data,'$.broker_order_id')=? AND child.id<>?",
            (payload["account_id"], payload["order_id"], client_order_id),
        ).fetchone()
        if other is not None:
            raise ValueError("Broker order is already bound to another child")
        recorded = db.execute(
            "SELECT * FROM feedback.feedback_orders WHERE account_id=? AND order_id=?",
            (payload["account_id"], payload["order_id"]),
        ).fetchone()
        expected = {
            "symbol": payload["symbol"], "side": payload["side"],
            "status": payload["status"], "sequence": payload["sequence"],
            "created_at": event.created_at.astimezone(timezone.utc).isoformat(),
            "updated_at": event.updated_at.astimezone(timezone.utc).isoformat(),
        }
        expected["symbol"] = canonical_order_symbol(expected["symbol"])
        if recorded is None or any(recorded[k] != v for k, v in expected.items()):
            raise ValueError("Snapshot must match the latest durable broker feedback")

        engine._db.execute("INSERT INTO smart_parents VALUES (?,?,?,?,?)", tuple(parent_row))
        engine._db.execute("INSERT INTO smart_orders VALUES (?,?,?)",
                           (client_order_id, row["parent_id"], row["data"]))
        context = engine.get_order_context(client_order_id)
        SmartSellingFeedback._validate_owned_snapshot(payload, context)
        engine.on_order_report(
            client_order_id, payload["status"], str(payload["filled_quantity"]),
            broker_order_id=payload["order_id"], sequence=payload["sequence"],
            authoritative=True, final=True,
        )
        result = engine.get_parent(row["parent_id"])
        if result["active"] or result["active_order"] or result["reserved"] != "0":
            raise ValueError("Terminal evidence did not resolve the stopped session")
        audit = {
            "source": source, "client_order_id": client_order_id,
            "broker_order_id": payload["order_id"], "snapshot": payload,
            "previous_order": child,
            "previous_ledger": {k: parent[k] for k in ("E", "C", "R", "F", "D")},
        }
        with engine._transaction():
            engine._event(engine._load(row["parent_id"]), "manual_terminal_reconciliation", audit)
        if apply:
            updated = engine._db.execute("SELECT active,data FROM smart_parents WHERE id=?",
                                         (row["parent_id"],)).fetchone()
            db.execute("UPDATE smart_parents SET active=?,data=? WHERE id=?",
                       (*updated, row["parent_id"]))
            order_data = engine._db.execute("SELECT data FROM smart_orders WHERE id=?",
                                            (client_order_id,)).fetchone()[0]
            db.execute("UPDATE smart_orders SET data=? WHERE id=?", (order_data, client_order_id))
            db.executemany("INSERT INTO smart_events(parent_id,ts,kind,data) VALUES (?,?,?,?)",
                           engine._db.execute("SELECT parent_id,ts,kind,data FROM smart_events"))
            db.execute("COMMIT")
        else:
            db.execute("ROLLBACK")
        return {"applied": apply, "parent": result, "source": source}
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.close()
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--feedback-db", required=True)
    parser.add_argument("--client-order-id", required=True)
    parser.add_argument("--snapshot-file", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = reconcile_stopped_child(
        args.state_db, args.feedback_db, args.client_order_id,
        json.loads(Path(args.snapshot_file).read_text()), source=args.source, apply=args.apply,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
