"""Durable, minute-by-minute account holdings and cash snapshots."""

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time


LOGGER = logging.getLogger("trading-ui.system-snapshots")


class SystemSnapshotRecorder:
    """Record all configured accounts, even before their first broker update.

    A recorded account contains the latest observed values, not a new broker
    query. ``ts`` and ``snapshot_age_seconds`` preserve their actual freshness.
    Missing or invalid values are explicitly unavailable rather than zero.
    """

    def __init__(
        self,
        db_path,
        accounts_provider,
        snapshots_provider,
        interval_seconds=60,
        *,
        clock=time.monotonic,
        now=None,
    ):
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("Snapshot interval must be a positive finite number.")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.accounts_provider = accounts_provider
        self.snapshots_provider = snapshots_provider
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS system_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "recorded_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS system_snapshots_recorded_at "
                "ON system_snapshots(recorded_at)"
            )

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="system-snapshots", daemon=True
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _run(self):
        next_run = self._clock()
        while not self._stop.is_set():
            try:
                self.record_snapshot()
            except Exception:
                LOGGER.exception("Failed recording system snapshot path=%s", self.db_path)
            next_run += self.interval_seconds
            current = self._clock()
            # Do not create bursts of duplicate snapshots after slow storage.
            if next_run <= current:
                missed = math.floor((current - next_run) / self.interval_seconds) + 1
                next_run += missed * self.interval_seconds
            if self._stop.wait(max(0, next_run - self._clock())):
                return

    def record_snapshot(self):
        recorded_at = self._now()
        if recorded_at.tzinfo is None:
            raise ValueError("Snapshot clock must return a timezone-aware datetime.")
        recorded_at = recorded_at.astimezone(timezone.utc)
        accounts = dict(self.accounts_provider())
        source = None
        provider_error = None
        try:
            provided = self.snapshots_provider()
            if isinstance(provided, tuple) and len(provided) == 2:
                snapshots, source = provided
            else:
                snapshots = provided
            if not isinstance(snapshots, dict):
                raise ValueError("Snapshot provider must return an account mapping.")
        except Exception:
            LOGGER.exception("Failed reading accounts for system snapshot")
            snapshots = {}
            provider_error = "snapshot_provider_error"

        document = {
            "schema_version": 1,
            "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
            "source": str(source) if source is not None else None,
            "account_count": len(accounts),
            "accounts": [
                self._account(meta, snapshots.get(account_id), recorded_at)
                for account_id, meta in sorted(
                    accounts.items(), key=lambda item: (item[1].num_id, item[0])
                )
            ],
        }
        if provider_error:
            document["error"] = provider_error
        payload = json.dumps(document, allow_nan=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO system_snapshots(recorded_at, payload) VALUES (?, ?)",
                (document["recorded_at"], payload),
            )
        return document

    @staticmethod
    def _account(meta, snapshot, recorded_at):
        account = {
            "account_id": meta.id,
            "account_num_id": meta.num_id,
            "broker": meta.broker,
            "trading_medium": meta.trading_medium,
            "data_status": "missing",
            "cash": None,
            "cash_by_currency": None,
            "available_cash_by_currency": None,
            "positions": None,
            "ts": None,
            "trading_enabled": None,
            "snapshot_age_seconds": None,
        }
        if snapshot is None:
            return account
        try:
            if snapshot.account_id != meta.id or snapshot.account_num_id not in (None, meta.num_id):
                raise ValueError("Account snapshot identifiers do not match configuration.")
            values = snapshot.to_dict()
            # One malformed account must not prevent recording all other accounts.
            json.dumps(values, allow_nan=False)
        except (AttributeError, TypeError, ValueError):
            account["data_status"] = "invalid"
            return account
        account.update(values)
        account["account_num_id"] = meta.num_id
        account["data_status"] = "available"
        if snapshot.ts:
            try:
                source_time = datetime.fromisoformat(str(snapshot.ts).replace("Z", "+00:00"))
                if source_time.tzinfo is not None:
                    account["snapshot_age_seconds"] = (recorded_at - source_time).total_seconds()
            except ValueError:
                pass
        return account
