"""Explicitly reconcile prior-day external orders and commands; preview by default.

Example (use the same config as the running trading UI):
    python -m trading_ui.reconcile_prior_day --config trading_ui/sample/config.local.json \
        --reason 'Operator requested reconciliation of records before the current trading day'
Add --apply to back up and update the feedback stores. No commands are published,
broker statuses invented, or services restarted. This is a one-time operation,
not an automatic expiry policy. Newer broker evidence can reopen an order.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

from .config import load_config
from .services.smart_selling_feedback_store import RECONCILIATION_SCHEMA


def _timestamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return stamp


def reconcile_prior_day(sources, *, timezone_name, reason, backup_dir=None, apply=False, now=None):
    """Mark only prior-day records, preserving current/undated records and history.

Lock the state stores too, so no new parent can claim a selected outstanding
child while the cleanup is being validated. Active children require their own
reviewed reconciliation; this utility never changes a strategy's credit ledger.
"""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("An operator reason is required")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("The current time must include a timezone")
    zone = ZoneInfo(timezone_name)
    trading_day = now.astimezone(zone).date()
    paths = [Path(source["state_db"]).resolve() for source in sources]
    feedback_paths = [Path(str(path) + ".feedback.sqlite3") for path in paths]
    all_paths = paths + feedback_paths
    if not paths or len(set(all_paths)) != len(all_paths) or not all(path.is_file() for path in all_paths):
        raise ValueError("Existing, separate state and feedback databases are required")
    backup = Path(backup_dir).resolve() if backup_dir else None
    if apply and backup is None:
        raise ValueError("A backup directory is required when applying reconciliation")
    if apply:
        backup.mkdir(parents=True, exist_ok=False)
        for index, path in enumerate(all_paths):
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(backup / f"{index}-{path.name}")) as target:
                    source.backup(target)

    connections = []
    plans = []
    active_children = []
    try:
        # Consistent lock order with the existing stopped-child repair utility.
        for path in all_paths:
            db = sqlite3.connect(path.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            connections.append(db)
            db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        for db in connections[:len(paths)]:
            for record in db.execute("SELECT data FROM smart_parents WHERE active=1"):
                parent = json.loads(record["data"])
                child = parent.get("order")
                if child and child.get("state") != "TERMINAL":
                    active_children.append(child)

        for source, db in zip(sources, connections[len(paths):]):
            plan = {"mode": source["mode"], "orders": [], "commands": [], "undated_commands": []}
            for record in db.execute("SELECT * FROM feedback_orders WHERE status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED')"):
                row = dict(record)
                if _timestamp(row["created_at"]).astimezone(zone).date() >= trading_day:
                    continue
                if any(child.get("account_id") == row["account_id"] and child.get("broker_order_id") == row["order_id"] for child in active_children):
                    raise ValueError("A prior-day broker order belongs to an active algorithm child; reconcile that child first")
                plan["orders"].append(row)
            for record in db.execute("SELECT * FROM feedback_pending_commands WHERE pending=1"):
                row = dict(record)
                match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", row["command_id"])
                if match is None:
                    plan["undated_commands"].append(row)
                    continue
                if _timestamp(match.group()).astimezone(zone).date() >= trading_day:
                    continue
                if any(child.get("client_order_id") == row["command_id"] for child in active_children):
                    raise ValueError("A prior-day command belongs to an active algorithm child; reconcile that child first")
                plan["commands"].append(row)
            # Never silently leave an old dispatched child outside the cleanup.
            if any(datetime.fromtimestamp(child["send_wall"], zone).date() < trading_day for child in active_children if child.get("send_wall") is not None):
                raise ValueError("A prior-day active algorithm child requires separate reconciliation")
            plans.append(plan)

        result = {"applied": apply, "as_of": now.isoformat(), "trading_day": str(trading_day),
                  "timezone": timezone_name, "reason": reason, "stores": plans}
        if apply:
            (backup / "preview.json").write_text(json.dumps({**result, "applied": False}, indent=2) + "\n")
            for db, plan in zip(connections[len(paths):], plans):
                # executescript would implicitly commit; execute each complete
                # statement so schema, watermark, deletion and audit stay atomic.
                statement = ""
                for line in RECONCILIATION_SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                for row in plan["orders"]:
                    db.execute("""INSERT OR REPLACE INTO feedback_reconciled_orders
                        VALUES (?,?,?,?,?,?,?,?,?)""", (row["account_id"], row["order_id"], row["sequence"],
                        row["created_at"], row["symbol"], row["side"], now.isoformat(), reason, json.dumps(row)))
                    db.execute("DELETE FROM feedback_orders WHERE account_id=? AND order_id=?",
                               (row["account_id"], row["order_id"]))
                for row in plan["commands"]:
                    db.execute("UPDATE feedback_pending_commands SET pending=0 WHERE command_id=?", (row["command_id"],))
                if plan["orders"] or plan["commands"]:
                    db.execute("INSERT INTO feedback_reconciliation_audit (reconciled_at,trading_day,timezone,reason,data) VALUES (?,?,?,?,?)",
                               (now.isoformat(), str(trading_day), timezone_name, reason, json.dumps(plan)))
            # Each feedback file commits atomically. The preview and backups
            # support recovery if a host failure interrupts between files.
            for db in connections[len(paths):]:
                db.commit()
            (backup / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        for db in reversed(connections):
            db.rollback()
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    config = load_config(args.config)
    root = Path(__file__).resolve().parents[1]
    sources = [{"mode": mode, "state_db": str(root / config[name]["state_db"])}
               for mode, name in (("G", "smart_selling"), ("H", "smart_buying"))]
    zone = config.get("order_ui", {}).get("timezone", "America/New_York")
    result = reconcile_prior_day(sources, timezone_name=zone,
                                 reason=args.reason, apply=args.apply, backup_dir=args.backup_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
