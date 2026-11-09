"""Read the controllers' unresolved records without changing their ledgers."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


def _connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def _command_time(command_id):
    match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", command_id)
    return match.group(0) if match else None


def _reason(status):
    return {"PENDING_NEW": "awaiting_acceptance", "PENDING_CANCEL": "awaiting_cancellation",
            "UNKNOWN": "status_unknown"}.get(status, "working_order")


class ReconciliationView:
    def __init__(self, sources, reporting_db_path):
        self.sources = sources
        self.reporting_db_path = reporting_db_path

    def snapshot(self, accounts):
        rows, sources = {}, []

        def merge(item, mode):
            account = item["account_id"]
            if account not in accounts:
                return
            key = (account, "order" if item.get("order_id") else "command",
                   item.get("order_id") or item["command_id"])
            if key in rows:
                row = rows[key]
                # Preserve blockers from either controller, even if their copies
                # disagree; never silently hide a still-persisted reservation.
                if item.get("kind") == "algo_order":
                    row.update({k: v for k, v in item.items() if v is not None and k not in ("created_at", "updated_at")})
                elif item.get("kind") == row.get("kind") == "broker_order" and item["sequence"] > row["sequence"]:
                    row.update(item)
                row["modes"] = sorted(set(row["modes"]) | {mode})
            else:
                rows[key] = {**item, "modes": [mode]}

        for source in self.sources:
            mode, state_path = source["mode"], source["state_db"]
            health = {"mode": mode}
            for part, path in (("feedback", state_path + ".feedback.sqlite3"), ("state", state_path)):
                if not Path(path).is_file():
                    health[part] = "missing"
                    continue
                try:
                    with closing(_connect(path)) as db:
                        items = self._feedback(db) if part == "feedback" else self._children(db)
                    for item in items:
                        if item["kind"] == "algo_order":
                            item["side"] = "SELL" if mode == "G" else "BUY"
                        merge(item, mode)
                    health[part] = "available"
                except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, OSError):
                    health[part] = "unavailable"
            sources.append(health)

        details_available = True
        try:
            with closing(_connect(self.reporting_db_path)) as db:
                for row in rows.values():
                    if not row.get("order_id"):
                        continue
                    found = db.execute("SELECT payload FROM activity_orders WHERE account_id=? AND order_id=?",
                                       (row["account_id"], row["order_id"])).fetchone()
                    if found:
                        detail = json.loads(found["payload"])
                        for field in ("quantity", "filled_quantity", "limit_price", "message", "order_type"):
                            row[field] = detail.get(field)
                        row["command_id"] = row.get("command_id") or detail.get("command_id") or detail.get("client_order_id")
                        row["reported_status"] = detail.get("status")
                        if detail.get("status") in TERMINAL:
                            row["reason"] = "controller_pending"
        except (sqlite3.Error, ValueError, TypeError, OSError):
            details_available = False

        now = datetime.now(timezone.utc)
        for row in rows.values():
            meta = accounts[row["account_id"]]
            row.update(account_num_id=meta.num_id, broker=meta.broker)
            try:
                stamp = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                row["age_seconds"] = max(0, (now - stamp).total_seconds())
            except (ValueError, TypeError, KeyError, AttributeError):
                row["age_seconds"] = None
        complete = bool(sources) and all(s.get(part) == "available" for s in sources for part in ("feedback", "state"))
        return {"generated_at": now.isoformat(), "complete": complete,
                "details_available": details_available, "sources": sources,
                "items": sorted(rows.values(), key=lambda r: (r.get("updated_at") or "", r["account_id"]), reverse=True)}

    @staticmethod
    def _feedback(db):
        items = []
        for record in db.execute("SELECT * FROM feedback_orders WHERE status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED')"):
            row = dict(record)
            row.update(kind="broker_order", reason=_reason(row["status"]))
            items.append(row)
        for record in db.execute("SELECT * FROM feedback_pending_commands WHERE pending=1"):
            row = dict(record)
            row.update(kind="pending_command", status="AWAITING_REPORT", reason="missing_feedback",
                       created_at=_command_time(row["command_id"]), updated_at=_command_time(row["command_id"]))
            items.append(row)
        return items

    @staticmethod
    def _children(db):
        items = []
        for record in db.execute("SELECT id,data FROM smart_parents WHERE active=1"):
            parent = json.loads(record["data"])
            order = parent.get("order")
            if not order or not order.get("dispatch_prepared") or order["state"] == "TERMINAL":
                continue
            sent = datetime.fromtimestamp(order["send_wall"], timezone.utc).isoformat()
            items.append({"kind": "algo_order", "account_id": order["account_id"],
                          "symbol": parent["symbol"], "side": parent.get("side"),
                          "order_id": order.get("broker_order_id"), "command_id": order["client_order_id"],
                          "session_id": record["id"], "session_state": parent["state"],
                          "status": order["state"], "quantity": order["quantity"],
                          "filled_quantity": order["cumulative_filled"], "limit_price": order["limit_price"],
                          "created_at": sent, "updated_at": sent,
                          "reason": "algo_reconciliation" if order["state"] == "RECONCILE" else "awaiting_terminal"})
        return items
