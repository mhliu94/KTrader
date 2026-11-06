from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pytest

from order_ui.events import parse_order_message
from trading_ui.reconcile_prior_day import reconcile_prior_day
from trading_ui.services.smart_selling_feedback_store import OrderFeedbackStore


NOW = datetime.fromisoformat("2026-11-06T03:00:00+00:00")


def snapshot(**changes):
    payload = {"schema_version": 1, "account_id": "A", "order_id": "old-order", "symbol": "AEHL",
               "side": "SELL", "order_type": "LIMIT", "status": "PENDING_NEW", "sequence": 1,
               "quantity": 100, "filled_quantity": 0, "average_fill_price": None, "limit_price": 10,
               "created_at": "2026-10-28T00:11:20Z", "updated_at": "2026-10-28T00:11:20Z", **changes}
    return parse_order_message(json.dumps(payload).encode())


@pytest.fixture
def stores(tmp_path):
    sources, stores = [], []
    for mode in ("G", "H"):
        state = tmp_path / f"{mode}.sqlite3"
        with closing(sqlite3.connect(state)) as db:
            db.execute("CREATE TABLE smart_parents (id TEXT,active INTEGER,data TEXT)")
        sources.append({"mode": mode, "state_db": str(state)})
        stores.append(OrderFeedbackStore(str(state) + ".feedback.sqlite3"))
    yield sources, stores
    for store in stores:
        store.close()


def reconcile(sources, **kwargs):
    return reconcile_prior_day(sources, timezone_name="America/New_York", reason="Requested prior-day cleanup",
                               now=NOW, **kwargs)


def test_preview_current_day_boundary_apply_backups_and_idempotency(stores, tmp_path):
    sources, feedback = stores
    ids = ["cmd_2026-11-05T01:00:00Z_old", "cmd_2026-11-06T01:00:00Z_current", "undated", "cmd_2026-11-07T01:00:00Z_future"]
    for store in feedback:
        for identity in ids:
            store.track_external_order({"account_id": "A", "symbol": "POM", "command_id": identity})
        store.record_snapshot(snapshot())
        store.record_snapshot(snapshot(order_id="current-order", created_at="2026-11-05T15:00:00Z", updated_at="2026-11-05T15:00:00Z"))
    preview = reconcile(sources)
    assert preview["trading_day"] == "2026-11-05" and not preview["applied"]
    assert all(len(plan["commands"]) == len(plan["orders"]) == len(plan["undated_commands"]) == 1 for plan in preview["stores"])
    assert all(store._db.execute("SELECT COUNT(*) FROM feedback_orders").fetchone()[0] == 2 for store in feedback)
    backup = tmp_path / "backup"
    result = reconcile(sources, apply=True, backup_dir=backup)
    assert result["applied"] and len(list(backup.glob("*.sqlite3"))) == 4
    assert not json.loads((backup / "preview.json").read_text())["applied"]
    assert json.loads((backup / "result.json").read_text())["applied"]
    for store in feedback:
        assert store._db.execute("SELECT pending FROM feedback_pending_commands WHERE command_id=?", (ids[0],)).fetchone()[0] == 0
        assert [row[0] for row in store._db.execute("SELECT command_id FROM feedback_pending_commands WHERE pending=1 ORDER BY command_id")] == sorted(ids[1:])
        assert [row[0] for row in store._db.execute("SELECT order_id FROM feedback_orders")] == ["current-order"]
        archived = store._db.execute("SELECT previous_data FROM feedback_reconciled_orders").fetchone()[0]
        assert json.loads(archived)["status"] == "PENDING_NEW"
        assert store._db.execute("SELECT COUNT(*) FROM feedback_reconciliation_audit").fetchone()[0] == 1
    assert all(not plan["commands"] and not plan["orders"] for plan in reconcile(sources)["stores"])


def test_running_consumer_cannot_restore_retained_order_but_newer_evidence_can(stores, tmp_path):
    sources, feedback = stores
    live = feedback[0]
    live.record_snapshot(snapshot())
    assert live.has_open_orders("A", "AEHL")
    reconcile(sources, apply=True, backup_dir=tmp_path / "backup")
    assert not live.has_open_orders("A", "AEHL")
    # Use the same pre-existing connection and legacy INSERT OR REPLACE path.
    live.record_snapshot(snapshot())
    live.record_snapshot(snapshot(sequence=0))
    assert not live.has_open_orders("A", "AEHL")
    live.record_snapshot(snapshot(sequence=2, status="NEW", updated_at="2026-11-06T03:00:00Z"))
    assert live.has_open_orders("A", "AEHL")
    live.close()
    feedback[0] = OrderFeedbackStore(sources[0]["state_db"] + ".feedback.sqlite3")
    assert feedback[0].has_open_orders("A", "AEHL")
    feedback[0].record_snapshot(snapshot(sequence=1))
    assert feedback[0].has_open_orders("A", "AEHL")


def test_pending_command_markers_survive_repeated_handoff(stores, tmp_path):
    sources, feedback = stores
    command = {"account_id": "A", "symbol": "POM", "command_id": "limit_2026-10-26T15:00:00Z_1"}
    feedback[0].track_external_order(command)
    reconcile(sources, apply=True, backup_dir=tmp_path / "backup")
    feedback[0].track_external_order(command)
    assert not feedback[0].has_open_orders("A", "POM")


def test_active_prior_day_child_refuses_cleanup_without_changing_feedback(stores, tmp_path):
    sources, feedback = stores
    feedback[0].record_snapshot(snapshot())
    parent = {"order": {"account_id": "A", "broker_order_id": "old-order", "client_order_id": "smart-1",
                        "state": "RECONCILE", "send_wall": 1793130000}}
    with closing(sqlite3.connect(sources[0]["state_db"])) as db:
        db.execute("INSERT INTO smart_parents VALUES ('session',1,?)", (json.dumps(parent),))
        db.commit()
    with pytest.raises(ValueError, match="active algorithm child"):
        reconcile(sources, apply=True, backup_dir=tmp_path / "backup")
    assert feedback[0].has_open_orders("A", "AEHL")
    assert feedback[0]._db.execute("SELECT COUNT(*) FROM feedback_reconciliation_audit").fetchone()[0] == 0


def test_missing_store_or_backup_does_not_create_database_or_mutate(stores, tmp_path):
    sources, feedback = stores
    with pytest.raises(ValueError, match="backup directory"):
        reconcile(sources, apply=True)
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="Existing, separate"):
        reconcile([{"mode": "G", "state_db": str(missing)}], apply=True, backup_dir=tmp_path / "backup")
    assert not missing.exists()
