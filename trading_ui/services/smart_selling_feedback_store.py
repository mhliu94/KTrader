"""Durable latest-order state and unresolved external command handoffs.

There is one compact row per broker order and one per locally tracked command.
Terminal sequence watermarks remain available so an older retained snapshot
cannot reopen a closed order. This module never changes the strategy ledger.
"""

from datetime import timezone
from pathlib import Path
import sqlite3
import threading

TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})

# Operator reconciliation is separate from broker-reported terminal status.
# This database trigger also protects already-running consumers: adding an
# explicit watermark takes effect without restarting a trading session.
RECONCILIATION_SCHEMA = """
    CREATE TABLE IF NOT EXISTS feedback_reconciled_orders (
        account_id TEXT NOT NULL, order_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, created_at TEXT NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL,
        reconciled_at TEXT NOT NULL, reason TEXT NOT NULL,
        previous_data TEXT NOT NULL,
        PRIMARY KEY(account_id, order_id)
    );
    CREATE TABLE IF NOT EXISTS feedback_reconciliation_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reconciled_at TEXT NOT NULL,
        trading_day TEXT NOT NULL, timezone TEXT NOT NULL,
        reason TEXT NOT NULL, data TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS feedback_ignore_reconciled_replay
    BEFORE INSERT ON feedback_orders
    WHEN EXISTS (
        SELECT 1 FROM feedback_reconciled_orders r
        WHERE r.account_id=NEW.account_id AND r.order_id=NEW.order_id
            AND r.created_at=NEW.created_at AND r.symbol=NEW.symbol AND r.side=NEW.side
            AND r.sequence>=NEW.sequence
    )
    BEGIN SELECT RAISE(IGNORE); END;
"""


def canonical_order_symbol(value):
    symbol = str(value or "").strip().upper()
    for prefix in ("US.", "HK.", "CN.", "SH.", "SZ."):
        if symbol.startswith(prefix):
            symbol = symbol[len(prefix) :]
            break
    return symbol.split("/", 1)[0]


class OrderFeedbackStore:
    """Persist validated schema-v1 snapshots and exact external command IDs."""

    def __init__(self, state_path=":memory:"):
        if str(state_path) != ":memory:":
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(state_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS feedback_orders (
                account_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, order_id)
            );
            CREATE INDEX IF NOT EXISTS feedback_order_instrument
                ON feedback_orders(account_id, symbol, status);
            CREATE TABLE IF NOT EXISTS feedback_pending_commands (
                command_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                pending INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS feedback_pending_instrument
                ON feedback_pending_commands(account_id, symbol, pending);
        """)
        self._db.executescript(RECONCILIATION_SCHEMA)

    def _validate_external_identifiers(self, payload):
        pending_ids = set()
        for name in ("command_id", "client_order_id"):
            identity = payload.get(name)
            if not identity:
                continue
            row = self._db.execute(
                "SELECT 1 FROM feedback_pending_commands WHERE command_id=?",
                (identity,),
            ).fetchone()
            if row is not None:
                pending_ids.add(identity)
        if len(pending_ids) > 1:
            raise ValueError("Snapshot identifiers refer to different external commands")

    @staticmethod
    def _validate_immutable_identity(snapshot, previous):
        if previous is None:
            return
        if any(previous[name] != snapshot[name] for name in ("created_at", "symbol", "side")):
            raise ValueError("Order snapshot changed immutable order identity")
        if (
            snapshot["sequence"] > previous["sequence"]
            and snapshot["updated_at"] < previous["updated_at"]
        ):
            raise ValueError("Order snapshot source time decreased as sequence increased")

    @staticmethod
    def _is_current_terminal(snapshot, previous):
        if snapshot["status"] not in TERMINAL_STATUSES:
            return False
        if previous is None or snapshot["sequence"] > previous["sequence"]:
            return True
        return (
            snapshot["sequence"] == previous["sequence"]
            and snapshot["status"] == previous["status"]
        )

    def _complete_external_commands(self, payload, snapshot):
        for name in ("command_id", "client_order_id"):
            identity = payload.get(name)
            if identity:
                self._db.execute(
                    """
                    UPDATE feedback_pending_commands SET pending=0
                    WHERE command_id=? AND account_id=? AND symbol=?
                """,
                    (identity, snapshot["account_id"], snapshot["symbol"]),
                )

    def record_snapshot(self, event):
        """Record newer evidence; return the previous compact state, if any."""
        payload = event.payload
        snapshot = {
            "account_id": payload["account_id"],
            "order_id": payload["order_id"],
            "symbol": canonical_order_symbol(payload["symbol"]),
            "side": payload["side"],
            "status": payload["status"],
            "sequence": payload["sequence"],
            "created_at": event.created_at.astimezone(timezone.utc).isoformat(),
            "updated_at": event.updated_at.astimezone(timezone.utc).isoformat(),
        }
        with self._lock, self._db:
            self._validate_external_identifiers(payload)
            row = self._db.execute(
                """
                SELECT * FROM feedback_orders WHERE account_id=? AND order_id=?
            """,
                (payload["account_id"], payload["order_id"]),
            ).fetchone()
            previous = dict(row) if row else None
            self._validate_immutable_identity(snapshot, previous)
            if previous is None or snapshot["sequence"] > previous["sequence"]:
                self._db.execute(
                    """
                    INSERT OR REPLACE INTO feedback_orders VALUES (
                        :account_id, :order_id, :symbol, :side, :status,
                        :sequence, :created_at, :updated_at
                    )
                """,
                    snapshot,
                )
            if self._is_current_terminal(snapshot, previous):
                self._complete_external_commands(payload, snapshot)
            return previous

    def track_external_order(self, command):
        """Persist a handoff before publication; only exact terminal evidence closes it."""
        identity = str(command.get("command_id") or "").strip()
        account = str(command.get("account_id") or "").strip()
        symbol = canonical_order_symbol(command.get("symbol"))
        if not identity or not account or not symbol:
            raise ValueError("External order tracking requires command_id, account_id and symbol")
        with self._lock, self._db:
            previous = self._db.execute(
                """
                SELECT account_id, symbol FROM feedback_pending_commands WHERE command_id=?
            """,
                (identity,),
            ).fetchone()
            if previous and (previous["account_id"] != account or previous["symbol"] != symbol):
                raise ValueError("External command ID was reused for another account/instrument")
            # Retain closed markers: a repeated handoff notification must not
            # reopen a command whose authoritative terminal snapshot is known.
            self._db.execute(
                """
                INSERT OR IGNORE INTO feedback_pending_commands(command_id, account_id, symbol)
                VALUES (?,?,?)
            """,
                (identity, account, symbol),
            )

    def has_open_orders(self, account_id, symbol):
        """Include working/unknown broker orders and unresolved local handoffs."""
        instrument = (str(account_id), canonical_order_symbol(symbol))
        with self._lock:
            row = self._db.execute(
                """
                SELECT 1 FROM feedback_orders
                WHERE account_id=? AND symbol=?
                    AND status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED')
                LIMIT 1
            """,
                instrument,
            ).fetchone()
            if row is not None:
                return True
            row = self._db.execute(
                """
                SELECT 1 FROM feedback_pending_commands
                WHERE account_id=? AND symbol=? AND pending=1 LIMIT 1
            """,
                instrument,
            ).fetchone()
            return row is not None

    def close(self):
        with self._lock:
            self._db.close()
