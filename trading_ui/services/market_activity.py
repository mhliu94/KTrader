"""Persistent five-minute analytics from quote counters and broker snapshots.

Quote prices are samples, so VWAP is an estimate. Fills are attributed to the
broker report time; ambiguous intervals and corrections are marked partial.
"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from .smart_selling_feedback_store import canonical_order_symbol


WINDOW_SECONDS = 300
QUOTE_MAX_AGE = 30
HISTORY_SECONDS = 86400
MARKET_ZONE = ZoneInfo("America/New_York")


def window_start(epoch):
    return int(epoch // WINDOW_SECONDS) * WINDOW_SECONDS


def _day(epoch):
    return datetime.fromtimestamp(epoch, MARKET_ZONE).date()


class MarketActivity:
    def __init__(self, db_path, *, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._next_prune = 0
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS market_quote_samples (
                symbol TEXT NOT NULL, observed REAL NOT NULL, price REAL,
                volume INTEGER, trade_time REAL, last_size INTEGER, error TEXT,
                PRIMARY KEY(symbol, observed)
            );
            CREATE TABLE IF NOT EXISTS market_order_samples (
                account_id TEXT NOT NULL, order_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, filled TEXT NOT NULL,
                PRIMARY KEY(account_id, order_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS market_order_symbol_time
                ON market_order_samples(symbol, updated);
        """)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def record_quote(self, quote):
        now = self.clock()
        if not now - HISTORY_SECONDS <= quote.observed_at <= now + 1:
            return
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO market_quote_samples VALUES (?,?,?,?,?,?,?)",
                             (quote.symbol, quote.observed_at, quote.last, quote.volume,
                              quote.trade_time, quote.last_size, quote.error))
            if now >= self._next_prune:
                self._db.execute("DELETE FROM market_quote_samples WHERE observed < ?", (now - HISTORY_SECONDS,))
                # Retain one old cumulative baseline for each order. Replays do
                # not turn an order's lifetime fills into new trading volume.
                self._db.execute("""DELETE FROM market_order_samples AS old WHERE updated < ?
                    AND sequence < (SELECT MAX(new.sequence) FROM market_order_samples AS new
                        WHERE new.account_id=old.account_id AND new.order_id=old.order_id
                        AND new.updated < ?)""", (now - HISTORY_SECONDS, now - HISTORY_SECONDS))
                self._next_prune = now + WINDOW_SECONDS

    def record_order(self, event):
        """OrderStore calls this for all retained dates, before its own dedupe."""
        data = event.payload
        row = (data["account_id"], data["order_id"], data["sequence"],
               event.created_at.timestamp(), event.updated_at.timestamp(),
               canonical_order_symbol(data["symbol"]), data["side"], str(Decimal(str(data["filled_quantity"])).normalize()))
        with self._lock, self._db:
            existing = self._db.execute("""SELECT * FROM market_order_samples
                WHERE account_id=? AND order_id=? AND sequence=?""", row[:3]).fetchone()
            if existing:
                if tuple(existing) != row:
                    raise ValueError("Conflicting order volume snapshot")
                return False
            for comparator, direction in (("<", "DESC"), (">", "ASC")):
                neighbor = self._db.execute(f"""SELECT * FROM market_order_samples
                    WHERE account_id=? AND order_id=? AND sequence {comparator} ?
                    ORDER BY sequence {direction} LIMIT 1""", row[:3]).fetchone()
                if neighbor:
                    if (neighbor["created"], neighbor["symbol"], neighbor["side"]) != (row[3], row[5], row[6]):
                        raise ValueError("Order identity changed")
                    if (comparator == "<" and neighbor["updated"] > row[4]
                            or comparator == ">" and neighbor["updated"] < row[4]):
                        raise ValueError("Order report time decreased")
            self._db.execute("INSERT INTO market_order_samples VALUES (?,?,?,?,?,?,?,?)", row)
        return True

    def snapshot(self, symbol, accounts, *, order_feed, now=None):
        now = self.clock() if now is None else now
        current = window_start(now)
        oldest = current - 3 * WINDOW_SECONDS
        with self._lock:
            self._db.execute("BEGIN")
            try:
                quotes = self._db.execute("""SELECT * FROM market_quote_samples
                    WHERE symbol=? AND observed>=? AND observed<=? ORDER BY observed""",
                    (symbol, oldest - QUOTE_MAX_AGE, now)).fetchall()
                # Include earlier snapshots of orders active in these windows,
                # so repeated status messages never count the same fills twice.
                orders = self._db.execute("""SELECT sample.* FROM market_order_samples sample
                    WHERE sample.symbol=? AND sample.updated<=? AND EXISTS (
                        SELECT 1 FROM market_order_samples recent
                        WHERE recent.account_id=sample.account_id AND recent.order_id=sample.order_id
                        AND recent.updated>=? AND recent.updated<=?)
                    ORDER BY sample.account_id,sample.order_id,sample.sequence""",
                    (canonical_order_symbol(symbol), now, oldest, now)).fetchall()
                latest = self._db.execute("""SELECT * FROM market_quote_samples
                    WHERE symbol=? AND observed<=? ORDER BY observed DESC LIMIT 1""", (symbol, now)).fetchone()
            finally:
                self._db.rollback()
        orders = [row for row in orders if row["account_id"] in accounts]
        windows = [{"start": current - i * WINDOW_SECONDS, "end": current + (1 - i) * WINDOW_SECONDS,
                    "current": i == 0} for i in range(4)]
        for window in windows:
            window.update(self._market_window(quotes, window["start"], min(now, window["end"])))
            window.update(bought=Decimal(0), sold=Decimal(0), our_complete=order_feed == "live")
        self._our_windows(windows, orders, accounts)
        for window in windows:
            window["gross"] = float(window["bought"] + window["sold"])
            window["bought"], window["sold"] = float(window["bought"]), float(window["sold"])
            if order_feed != "live" and not orders:
                window.update(bought=None, sold=None, gross=None)
        live = latest is not None and not latest["error"] and now - latest["observed"] <= QUOTE_MAX_AGE
        return {"symbol": symbol, "generated_at": now, "timezone": MARKET_ZONE.key,
                "order_feed": order_feed, "quote_state": "live" if live else "unavailable",
                "last_trade": {"time": latest["trade_time"] if live else None,
                               "quantity": latest["last_size"] if live else None,
                               "price": latest["price"] if live else None},
                "windows": windows}

    @staticmethod
    def _market_window(quotes, start, end):
        baseline = next((q for q in reversed(quotes) if q["observed"] <= start), None)
        previous = baseline
        total, weighted = 0, Decimal(0)
        complete = bool(baseline is not None and not baseline["error"]
                        and baseline["volume"] is not None and start - baseline["observed"] <= QUOTE_MAX_AGE)
        observed = complete
        for quote in quotes:
            if not start < quote["observed"] <= end:
                continue
            valid = (previous is not None and not previous["error"] and not quote["error"]
                     and previous["volume"] is not None and quote["volume"] is not None
                     and quote["price"] is not None
                     and quote["observed"] - previous["observed"] <= QUOTE_MAX_AGE
                     and _day(quote["observed"]) == _day(previous["observed"])
                     and quote["volume"] >= previous["volume"])
            if valid:
                delta = quote["volume"] - previous["volume"]
                total += delta
                weighted += Decimal(str(quote["price"])) * delta
                observed = True
            else:
                complete = False
            previous = quote
        if previous is None or previous["error"] or end - previous["observed"] > QUOTE_MAX_AGE:
            complete = False
        return {"market_volume": total if observed else None,
                "vwap_estimate": float(weighted / total) if total else None,
                "market_complete": complete}

    @staticmethod
    def _our_windows(windows, orders, accounts):
        grouped = defaultdict(list)
        for order in orders:
            if order["account_id"] in accounts:
                grouped[(order["account_id"], order["order_id"])].append(order)
        for samples in grouped.values():
            previous_qty, previous_time = Decimal(0), samples[0]["created"]
            lots = []
            for order in samples:
                quantity = Decimal(order["filled"])
                delta = quantity - previous_qty
                at = order["updated"]
                if delta > 0:
                    lots.append([at, delta, order["side"]])
                    if window_start(previous_time) != window_start(at):
                        for window in windows:
                            if previous_time < window["end"] and at >= window["start"]:
                                window["our_complete"] = False
                elif delta < 0:
                    # Cumulative corrections reduce prior allocations. Without
                    # execution IDs their exact windows cannot be established.
                    remaining = -delta
                    for lot in reversed(lots):
                        removed = min(lot[1], remaining)
                        lot[1] -= removed
                        remaining -= removed
                        if removed:
                            for window in windows:
                                if window["start"] <= lot[0] < window["end"]:
                                    window["our_complete"] = False
                        if not remaining:
                            break
                previous_qty, previous_time = quantity, at
            for at, quantity, side in lots:
                for window in windows:
                    if window["start"] <= at < window["end"]:
                        window["bought" if side == "BUY" else "sold"] += quantity
