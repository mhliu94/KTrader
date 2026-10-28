from datetime import datetime, timezone
import json
import sqlite3
from threading import Event

import pytest

from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.system_snapshots import SystemSnapshotRecorder


NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


def recorder(tmp_path, snapshots_provider=None, **kwargs):
    return SystemSnapshotRecorder(
        tmp_path / "activity.sqlite3",
        accounts_provider=lambda: {
            "B": AccountMeta("B", 2, "Futu"),
            "A": AccountMeta("A", 1, "Tiger"),
        },
        snapshots_provider=snapshots_provider or (lambda: {}),
        now=lambda: NOW,
        **kwargs,
    )


def test_records_every_account_and_preserves_cash_holdings_and_source_age(tmp_path):
    snapshot = AccountSnapshot(
        account_id="A",
        cash=500,
        cash_by_currency={"USD": 500, "HKD": 1000},
        available_cash_by_currency={"USD": 450},
        positions=[Position("AAPL", 1.5, avg_price=200, available_qty=1)],
        ts="2026-10-28T14:55:00Z",
        trading_enabled=True,
    )
    service = recorder(tmp_path, lambda: ({"A": snapshot, "UNKNOWN": snapshot}, "kafka"))
    document = service.record_snapshot()

    assert document["source"] == "kafka"
    assert document["account_count"] == 2
    first, missing = document["accounts"]
    assert first["account_id"] == "A"
    assert first["account_num_id"] == 1
    assert first["cash"] == 500
    assert first["cash_by_currency"] == {"USD": 500, "HKD": 1000}
    assert first["available_cash_by_currency"] == {"USD": 450}
    assert first["positions"] == [{"symbol": "AAPL", "qty": 1.5, "avg_price": 200, "available_qty": 1}]
    assert first["ts"] == snapshot.ts
    assert first["snapshot_age_seconds"] == 300
    assert first["data_status"] == "available"
    assert missing["account_id"] == "B"
    assert missing["data_status"] == "missing"
    assert missing["cash"] is None
    assert missing["positions"] is None

    with sqlite3.connect(service.db_path) as connection:
        timestamp, payload = connection.execute(
            "SELECT recorded_at, payload FROM system_snapshots"
        ).fetchone()
        assert timestamp == "2026-10-28T15:00:00Z"
        assert json.loads(payload) == document
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_snapshots_survive_recorder_restart_and_preserve_other_tables(tmp_path):
    first = recorder(tmp_path)
    first.record_snapshot()
    with sqlite3.connect(first.db_path) as connection:
        connection.execute("CREATE TABLE activity_test (id INTEGER)")
        connection.execute("INSERT INTO activity_test VALUES (42)")
    second = recorder(tmp_path)
    second.record_snapshot()
    with sqlite3.connect(first.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM system_snapshots").fetchone()[0] == 2
        assert connection.execute("SELECT id FROM activity_test").fetchone()[0] == 42


def test_provider_failure_is_documented_without_fabricating_empty_balances(tmp_path):
    def unavailable():
        raise RuntimeError("account feed unavailable")

    document = recorder(tmp_path, unavailable).record_snapshot()
    assert document["error"] == "snapshot_provider_error"
    assert document["account_count"] == 2
    assert all(account["data_status"] == "missing" for account in document["accounts"])
    assert all(account["cash"] is None for account in document["accounts"])


@pytest.mark.parametrize("invalid", [
    AccountSnapshot("A", float("nan")),
    AccountSnapshot("WRONG", 1),
    AccountSnapshot("A", 1, account_num_id=99),
])
def test_invalid_account_does_not_prevent_other_accounts_from_being_recorded(tmp_path, invalid):
    document = recorder(
        tmp_path, lambda: {"A": invalid, "B": AccountSnapshot("B", 25)}
    ).record_snapshot()
    assert document["accounts"][0]["data_status"] == "invalid"
    assert document["accounts"][0]["cash"] is None
    assert document["accounts"][1]["cash"] == 25


def test_start_records_immediately_and_stop_interrupts_minute_wait(tmp_path):
    captured = Event()

    def snapshots():
        captured.set()
        return {}

    service = recorder(tmp_path, snapshots)
    assert service.interval_seconds == 60
    service.start()
    thread = service._thread
    service.start()
    assert service._thread is thread
    try:
        assert captured.wait(2)
    finally:
        service.stop()
    assert not thread.is_alive()
    with sqlite3.connect(service.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM system_snapshots").fetchone()[0] == 1


def test_worker_retries_after_storage_failure(tmp_path):
    service = recorder(tmp_path, interval_seconds=0.01)
    original = service.record_snapshot
    succeeded = Event()
    attempts = []

    def flaky_record():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database temporarily locked")
        original()
        succeeded.set()

    service.record_snapshot = flaky_record
    service.start()
    try:
        assert succeeded.wait(2)
    finally:
        service.stop()
    assert len(attempts) >= 2


@pytest.mark.parametrize("record_duration, expected_wait", [(7, 53), (135, 45)])
def test_cadence_does_not_drift_or_burst_after_slow_recording(tmp_path, record_duration, expected_wait):
    ticks = [0]
    waits = []
    service = recorder(tmp_path, clock=lambda: ticks[0])

    class StopAfterTwoSnapshots:
        def is_set(self):
            return False

        def wait(self, seconds):
            waits.append(seconds)
            ticks[0] += seconds
            return len(waits) == 2

    def record():
        ticks[0] += record_duration

    service._stop = StopAfterTwoSnapshots()
    service.record_snapshot = record
    service._run()
    assert waits == [expected_wait, expected_wait]
