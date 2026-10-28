"""Durable order, tag and algorithm reports shared by the two local UI servers.

Executions come exclusively from cumulative broker order snapshots. Commands
only establish session ownership; their requested quantities are never fills.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import uuid


DEFAULT_REPORTING_DB = Path(__file__).resolve().parents[2] / "reports" / "activity.sqlite3"


def reporting_db_path(config, config_path):
    settings = config.get("reporting", {})
    if not isinstance(settings, dict) or set(settings) - {"db_path"}:
        raise ValueError("reporting must be an object containing only db_path.")
    value = settings.get("db_path")
    if value is None:
        return str(DEFAULT_REPORTING_DB)
    if not isinstance(value, str) or not value.strip() or value == ":memory:":
        raise ValueError("reporting.db_path must be a persistent file path.")
    return str((Path(config_path).resolve().parent / value).resolve())


def _timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Reporting timestamps must have a timezone.")
    return value.astimezone(timezone.utc)


def _iso(value):
    return _timestamp(value).isoformat().replace("+00:00", "Z")


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class ActivityReporting:
    def __init__(self, db_path, *, now=None):
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS activity_sessions (
                id TEXT PRIMARY KEY, parameters TEXT NOT NULL, account_ids TEXT NOT NULL,
                started_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS activity_commands (
                account_id TEXT NOT NULL, command_id TEXT NOT NULL, session_id TEXT NOT NULL,
                PRIMARY KEY(account_id, command_id)
            );
            CREATE TABLE IF NOT EXISTS activity_orders (
                account_id TEXT NOT NULL, order_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL, symbol TEXT NOT NULL,
                session_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(account_id, order_id)
            );
            CREATE INDEX IF NOT EXISTS activity_orders_session ON activity_orders(session_id);
            CREATE INDEX IF NOT EXISTS activity_orders_symbol_time ON activity_orders(symbol, created_epoch);
            CREATE TABLE IF NOT EXISTS activity_tags (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, symbol TEXT NOT NULL,
                started_at TEXT NOT NULL, start_epoch REAL NOT NULL,
                stopped_at TEXT, stop_epoch REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS activity_one_active_tag
                ON activity_tags((1)) WHERE stopped_at IS NULL;
        """)
        self._db.commit()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def start_session(self, session_id, parameters, account_ids, *, started_at=None):
        now = _iso(self._now())
        with self._transaction() as db:
            db.execute("""INSERT OR IGNORE INTO activity_sessions
                (id,parameters,account_ids,started_at,updated_at,status) VALUES (?,?,?,?,?,?)""",
                (str(session_id), _json(parameters), _json(list(dict.fromkeys(account_ids))),
                 _iso(started_at) if started_at else now, now, "running"))

    def heartbeat_session(self, session_id, *, status="running"):
        with self._transaction() as db:
            db.execute("UPDATE activity_sessions SET updated_at=?,status=? WHERE id=? AND ended_at IS NULL",
                       (_iso(self._now()), status, str(session_id)))

    def end_session(self, session_id, *, status="stopped", reason=None):
        now = _iso(self._now())
        with self._transaction() as db:
            db.execute("""UPDATE activity_sessions SET ended_at=?,updated_at=?,status=?,reason=?
                          WHERE id=? AND ended_at IS NULL""", (now, now, status, reason, str(session_id)))

    def register_command(self, session_id, command):
        account_id = command.get("account_id")
        if not account_id:
            return
        identifiers = {command[key] for key in ("command_id", "client_order_id") if command.get(key)}
        with self._transaction() as db:
            for identifier in identifiers:
                owner = db.execute("SELECT session_id FROM activity_commands WHERE account_id=? AND command_id=?",
                                   (account_id, identifier)).fetchone()
                if owner and owner["session_id"] != str(session_id):
                    raise ValueError("An order command cannot belong to two algorithm sessions.")
                db.execute("INSERT OR IGNORE INTO activity_commands VALUES (?,?,?)",
                           (account_id, identifier, str(session_id)))
            # Repair a correlation if broker feedback beat command registration.
            for row in db.execute("SELECT order_id,payload FROM activity_orders WHERE account_id=? AND session_id IS NULL",
                                  (account_id,)).fetchall():
                order = json.loads(row["payload"])
                if identifiers.intersection({order.get("command_id"), order.get("client_order_id")}):
                    db.execute("UPDATE activity_orders SET session_id=? WHERE account_id=? AND order_id=?",
                               (str(session_id), account_id, row["order_id"]))

    def record_order(self, event):
        """Accept a validated OrderEvent, independently of the recent-orders date."""
        data = event.payload
        created, updated = event.created_at.timestamp(), event.updated_at.timestamp()
        with self._transaction() as db:
            old = db.execute("SELECT * FROM activity_orders WHERE account_id=? AND order_id=?",
                             (data["account_id"], data["order_id"])).fetchone()
            if old:
                if data["sequence"] <= old["sequence"]:
                    return False
                previous = json.loads(old["payload"])
                if created != old["created_epoch"]:
                    raise ValueError("created_at is immutable for an order.")
                if updated < old["updated_epoch"]:
                    raise ValueError("updated_at cannot decrease when sequence increases.")
                if data["symbol"].strip().upper() != old["symbol"] or data["side"] != previous["side"]:
                    raise ValueError("Order symbol and side are immutable.")
            session_id = old["session_id"] if old else None
            if session_id is None:
                for key in ("command_id", "client_order_id"):
                    if data.get(key):
                        owner = db.execute("SELECT session_id FROM activity_commands WHERE account_id=? AND command_id=?",
                                           (data["account_id"], data[key])).fetchone()
                        if owner:
                            session_id = owner["session_id"]
                            break
            db.execute("""INSERT INTO activity_orders VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,order_id) DO UPDATE SET sequence=excluded.sequence,
                updated_epoch=excluded.updated_epoch,session_id=excluded.session_id,payload=excluded.payload""",
                (data["account_id"], data["order_id"], data["sequence"], created, updated,
                 data["symbol"].strip().upper(), session_id, _json(data)))
        return True

    def start_tag(self, name, symbol):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Tag name must contain 1 to 100 characters.")
        if any(ord(char) < 32 for char in name):
            raise ValueError("Tag name cannot contain control characters.")
        if not isinstance(symbol, str) or not 1 <= len(symbol.strip()) <= 64:
            raise ValueError("Symbol must contain 1 to 64 characters.")
        symbol = symbol.strip().upper()
        if any(char.isspace() or ord(char) < 32 for char in symbol):
            raise ValueError("Symbol cannot contain whitespace or control characters.")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM activity_tags WHERE stopped_at IS NULL").fetchone():
                raise ValueError("Stop the active tag before starting another.")
            now = _timestamp(self._now())
            tag_id = uuid.uuid4().hex
            db.execute("INSERT INTO activity_tags (id,name,symbol,started_at,start_epoch) VALUES (?,?,?,?,?)",
                       (tag_id, name.strip(), symbol, _iso(now), now.timestamp()))
        return tag_id

    def stop_tag(self, tag_id):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM activity_tags WHERE id=?", (tag_id,)).fetchone()
            if row is None:
                raise KeyError(tag_id)
            if row["stopped_at"] is None:
                now = max(_timestamp(self._now()), _timestamp(row["started_at"]))
                db.execute("UPDATE activity_tags SET stopped_at=?,stop_epoch=? WHERE id=?",
                           (_iso(now), now.timestamp(), tag_id))

    def order_tags(self, orders):
        with self._lock:
            tags = self._db.execute("SELECT * FROM activity_tags ORDER BY start_epoch").fetchall()
        result = {}
        for order in orders:
            created = _timestamp(order["created_at"]).timestamp()
            result[(order["account_id"], order["order_id"])] = [
                {"id": tag["id"], "name": tag["name"]} for tag in tags
                if order["symbol"].strip().upper() == tag["symbol"] and created >= tag["start_epoch"]
                and (tag["stop_epoch"] is None or created < tag["stop_epoch"])
            ]
        return result

    def snapshot(self, accounts):
        # Read all report tables in one SQLite snapshot while the other server writes.
        with self._lock:
            self._db.execute("BEGIN")
            try:
                sessions = self._db.execute("SELECT * FROM activity_sessions ORDER BY started_at DESC").fetchall()
                tags = self._db.execute("SELECT * FROM activity_tags ORDER BY start_epoch DESC").fetchall()
                orders = self._db.execute("SELECT * FROM activity_orders").fetchall()
            finally:
                self._db.rollback()
        visible = [(row, json.loads(row["payload"])) for row in orders if row["account_id"] in accounts]
        session_reports = []
        for session in sessions:
            selected = [account for account in json.loads(session["account_ids"]) if account in accounts]
            if not selected:
                continue
            report = dict(session)
            report["parameters"] = json.loads(report["parameters"])
            report["account_ids"] = selected
            report["symbol"] = report["parameters"].get("symbol", "")
            stale = (not report["ended_at"] and
                     (_timestamp(self._now()) - _timestamp(report["updated_at"])).total_seconds() > 90)
            if stale:
                report["status"] = "unknown"
            report["running"] = None if stale else not bool(report["ended_at"])
            matches = [data for row, data in visible if row["session_id"] == session["id"] and row["account_id"] in selected]
            report.update(_summarize(matches, accounts, include_accounts=selected))
            session_reports.append(report)
        tag_reports = []
        for tag in tags:
            matches = [data for row, data in visible
                       if row["symbol"] == tag["symbol"] and row["created_epoch"] >= tag["start_epoch"]
                       and (tag["stop_epoch"] is None or row["created_epoch"] < tag["stop_epoch"])]
            report = {key: tag[key] for key in ("id", "name", "symbol", "started_at", "stopped_at")}
            report["active"] = tag["stopped_at"] is None
            report.update(_summarize(matches, accounts))
            tag_reports.append(report)
        return {"sessions": session_reports, "tags": tag_reports,
                "active_tag_id": next((tag["id"] for tag in tag_reports if tag["active"]), None)}


def _empty_totals():
    return {"shares_bought": Decimal(0), "shares_sold": Decimal(0), "cash_by_currency": {}, "cash_complete": True}


def _accumulate(totals, order):
    qty = Decimal(str(order["filled_quantity"]))
    if not qty:
        return
    buy = order["side"] == "BUY"
    totals["shares_bought" if buy else "shares_sold"] += qty
    currency = order.get("currency") or "UNKNOWN"
    cash = totals["cash_by_currency"].setdefault(currency, {
        "buy_cost": Decimal(0), "sell_proceeds": Decimal(0), "cash_change": Decimal(0), "complete": True})
    field = "buy_cost" if buy else "sell_proceeds"
    price = order.get("average_fill_price")
    if price is None or currency == "UNKNOWN":
        cash[field] = None
    elif cash[field] is not None:
        cash[field] += qty * Decimal(str(price))
    cash["complete"] = cash["buy_cost"] is not None and cash["sell_proceeds"] is not None
    cash["cash_change"] = cash["sell_proceeds"] - cash["buy_cost"] if cash["complete"] else None
    totals["cash_complete"] = all(part["complete"] for part in totals["cash_by_currency"].values())


def _numbers(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numbers(item) for item in value]
    return value


def _summarize(orders, metas, *, include_accounts=()):
    accounts = {account: _empty_totals() for account in include_accounts}
    totals = _empty_totals()
    for order in orders:
        if not order["filled_quantity"]:
            continue
        account = accounts.setdefault(order["account_id"], _empty_totals())
        _accumulate(account, order)
        _accumulate(totals, order)
    rows = [{"account_id": account, "account_num_id": metas[account].num_id,
             "broker": metas[account].broker, **summary}
            for account, summary in sorted(accounts.items(), key=lambda item: metas[item[0]].num_id)]
    return _numbers({"accounts": rows, "totals": totals})
