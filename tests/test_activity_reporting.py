from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from order_ui.events import parse_order_message
from trading_ui.models import AccountMeta
from trading_ui.services.activity_reporting import ActivityReporting


@pytest.fixture
def clock():
    return SimpleNamespace(now=datetime.fromisoformat("2026-10-28T14:00:00+00:00"))


@pytest.fixture
def accounts():
    return {"A": AccountMeta("A", 1, "Tiger"), "B": AccountMeta("B", 2, "Futu")}


@pytest.fixture
def reporting(tmp_path, clock):
    service = ActivityReporting(tmp_path / "activity.sqlite3", now=lambda: clock.now)
    try:
        yield service
    finally:
        service.close()


def order(**updates):
    payload = {
        "schema_version": 1,
        "account_id": "A",
        "order_id": "order-1",
        "sequence": 0,
        "created_at": "2026-10-28T14:00:00Z",
        "updated_at": "2026-10-28T14:00:00Z",
        "symbol": "AAPL",
        "side": "BUY",
        "order_type": "MARKET",
        "status": "PARTIALLY_FILLED",
        "quantity": 10,
        "filled_quantity": 2,
        "average_fill_price": 100,
        "currency": "USD",
        **updates,
    }
    if payload["currency"] is None:
        del payload["currency"]
    return parse_order_message(json.dumps(payload).encode())


def tag_report(reporting, accounts, tag_id):
    return next(tag for tag in reporting.snapshot(accounts)["tags"] if tag["id"] == tag_id)


def session_report(reporting, accounts, session_id="session-1"):
    return next(item for item in reporting.snapshot(accounts)["sessions"] if item["id"] == session_id)


def test_cumulative_fills_replace_duplicates_reordering_and_corrections(reporting, accounts):
    tag = reporting.start_tag("Opening buys", "aapl")
    assert reporting.record_order(order())
    assert not reporting.record_order(order())
    assert reporting.record_order(order(sequence=2, filled_quantity=6, average_fill_price=110))
    assert not reporting.record_order(order(sequence=1, filled_quantity=4))
    total = tag_report(reporting, accounts, tag)["totals"]
    assert total["shares_bought"] == 6
    assert total["cash_by_currency"]["USD"]["buy_cost"] == 660

    # A higher-sequence broker correction replaces cumulative totals, even downwards.
    assert reporting.record_order(order(sequence=3, filled_quantity=3, average_fill_price=105))
    total = tag_report(reporting, accounts, tag)["totals"]
    assert total["shares_bought"] == 3
    assert total["cash_by_currency"]["USD"]["cash_change"] == -315


def test_tag_boundaries_stop_and_restart_preserve_late_fills(reporting, accounts, clock):
    first = reporting.start_tag("First", "AAPL")
    reporting.record_order(order(order_id="too-early", created_at="2026-10-28T13:59:59Z"))
    reporting.record_order(order())
    clock.now += timedelta(minutes=1)
    reporting.stop_tag(first)
    reporting.stop_tag(first)
    second = reporting.start_tag("Second", "AAPL")
    boundary = order(order_id="boundary", created_at="2026-10-28T14:01:00Z", updated_at="2026-10-28T14:01:00Z")
    reporting.record_order(boundary)
    # A fill reported later is still attached to its original order's tagging period.
    reporting.record_order(order(sequence=1, filled_quantity=8, updated_at="2026-10-28T14:02:00Z"))
    assert tag_report(reporting, accounts, first)["totals"]["shares_bought"] == 8
    assert tag_report(reporting, accounts, second)["totals"]["shares_bought"] == 2
    assert reporting.snapshot(accounts)["active_tag_id"] == second
    assert reporting.order_tags([order().payload, boundary.payload]) == {
        ("A", "order-1"): [{"id": first, "name": "First"}],
        ("A", "boundary"): [{"id": second, "name": "Second"}],
    }


def test_tag_and_order_history_survive_restart_and_midnight(tmp_path, accounts, clock):
    path = tmp_path / "activity.sqlite3"
    clock.now = datetime.fromisoformat("2026-10-29T03:59:00+00:00")
    first = ActivityReporting(path, now=lambda: clock.now)
    tag = first.start_tag("Overnight", "AAPL")
    first.record_order(order(created_at="2026-10-29T03:59:00Z", updated_at="2026-10-29T03:59:00Z"))
    first.close()
    clock.now += timedelta(minutes=2)
    second = ActivityReporting(path, now=lambda: clock.now)
    try:
        second.record_order(order(sequence=1, filled_quantity=5, created_at="2026-10-29T03:59:00Z", updated_at="2026-10-29T04:01:00Z"))
        assert second.snapshot(accounts)["active_tag_id"] == tag
        assert tag_report(second, accounts, tag)["totals"]["shares_bought"] == 5
        second.stop_tag(tag)
        assert second.snapshot(accounts)["active_tag_id"] is None
    finally:
        second.close()


def test_tag_summarizes_buys_sells_and_only_changed_visible_accounts(reporting, accounts):
    tag = reporting.start_tag("Both sides", "AAPL")
    reporting.record_order(order(filled_quantity=3, average_fill_price=101))
    reporting.record_order(order(account_id="B", side="SELL", filled_quantity=5, average_fill_price=110))
    reporting.record_order(order(account_id="HIDDEN", filled_quantity=9))
    reporting.record_order(order(order_id="other-symbol", symbol="MSFT", filled_quantity=9))
    reporting.record_order(order(order_id="unfilled", status="NEW", filled_quantity=0, average_fill_price=None))
    report = tag_report(reporting, accounts, tag)
    assert [row["account_id"] for row in report["accounts"]] == ["A", "B"]
    assert report["accounts"][0]["shares_bought"] == 3
    assert report["accounts"][1]["shares_sold"] == 5
    assert report["totals"]["cash_by_currency"]["USD"] == {
        "buy_cost": 303,
        "sell_proceeds": 550,
        "cash_change": 247,
        "complete": True,
    }


def test_missing_fill_price_stays_unknown_until_authoritative_correction(reporting, accounts):
    tag = reporting.start_tag("Unknown cost", "AAPL")
    reporting.record_order(order(average_fill_price=None))
    reporting.record_order(order(order_id="sell", side="SELL", filled_quantity=1, average_fill_price=120))
    total = tag_report(reporting, accounts, tag)["totals"]
    assert total["shares_bought"] == 2
    assert total["cash_complete"] is False
    assert total["cash_by_currency"]["USD"] == {
        "buy_cost": None, "sell_proceeds": 120, "cash_change": None, "complete": False,
    }
    reporting.record_order(order(sequence=1, average_fill_price=100))
    total = tag_report(reporting, accounts, tag)["totals"]
    assert total["cash_complete"] is True
    assert total["cash_by_currency"]["USD"]["cash_change"] == -80


def test_cash_is_partitioned_by_currency_and_missing_currency_is_explicit(reporting, accounts):
    tag = reporting.start_tag("Currencies", "AAPL")
    reporting.record_order(order())
    reporting.record_order(order(order_id="hkd", side="SELL", currency="HKD", average_fill_price=800))
    reporting.record_order(order(order_id="unknown", currency=None, average_fill_price=50))
    cash = tag_report(reporting, accounts, tag)["totals"]["cash_by_currency"]
    assert set(cash) == {"USD", "HKD", "UNKNOWN"}
    assert cash["USD"]["cash_change"] == -200
    assert cash["HKD"]["cash_change"] == 1600
    assert cash["UNKNOWN"]["buy_cost"] is None
    assert cash["UNKNOWN"]["cash_change"] is None
    assert cash["UNKNOWN"]["complete"] is False
    assert tag_report(reporting, accounts, tag)["totals"]["cash_complete"] is False


def test_sessions_use_exact_account_command_correlation_and_keep_zero_fill_accounts(reporting, accounts):
    params = {"symbol": "AAPL", "trading_mode": "E", "fast_trading_price_limit": 120}
    reporting.start_session("session-1", params, ["A", "B", "HIDDEN"])
    reporting.register_command("session-1", {"account_id": "A", "command_id": "child-1", "client_order_id": "client-1"})
    reporting.record_order(order(command_id="child-1"))
    reporting.record_order(order(order_id="manual", command_id="unrelated", filled_quantity=9))
    reporting.record_order(order(account_id="B", command_id="child-1", filled_quantity=8))
    report = session_report(reporting, accounts)
    assert report["parameters"] == params
    assert report["account_ids"] == ["A", "B"]
    assert report["running"] is True
    assert report["totals"]["shares_bought"] == 2
    assert [row["account_id"] for row in report["accounts"]] == ["A", "B"]
    assert report["accounts"][1]["shares_bought"] == 0
    assert report["accounts"][1]["cash_by_currency"] == {}


def test_late_command_registration_repairs_client_id_correlation(reporting, accounts):
    reporting.start_session("session-1", {"symbol": "AAPL"}, ["A"])
    reporting.record_order(order(client_order_id="late-client"))
    assert session_report(reporting, accounts)["totals"]["shares_bought"] == 0
    reporting.register_command("session-1", {"account_id": "A", "command_id": "late-command", "client_order_id": "late-client"})
    assert session_report(reporting, accounts)["totals"]["shares_bought"] == 2
    # Publishers may omit optional correlation fields in later full snapshots.
    reporting.record_order(order(sequence=1, filled_quantity=4))
    assert session_report(reporting, accounts)["totals"]["shares_bought"] == 4


def test_tag_labels_accept_lowercase_rfc3339_timestamps(reporting):
    tag = reporting.start_tag("Opening", "AAPL")
    event = order(created_at="2026-10-28t14:00:00z", updated_at="2026-10-28t14:00:00z")
    reporting.record_order(event)
    assert reporting.order_tags([event.payload])[("A", "order-1")] == [{"id": tag, "name": "Opening"}]


def test_command_correlation_cannot_move_to_another_session(reporting, accounts):
    reporting.start_session("session-1", {"symbol": "AAPL"}, ["A"])
    command = {"account_id": "A", "command_id": "child-1"}
    reporting.register_command("session-1", command)
    with pytest.raises(ValueError, match="two algorithm sessions"):
        reporting.register_command("session-2", command)
    reporting.record_order(order(command_id="child-1"))
    assert session_report(reporting, accounts)["totals"]["shares_bought"] == 2


def test_stale_session_heartbeat_is_unknown_and_ended_session_stays_stopped(reporting, accounts, clock):
    reporting.start_session("session-1", {"symbol": "AAPL"}, ["A"])
    clock.now += timedelta(seconds=91)
    report = session_report(reporting, accounts)
    assert report["status"] == "unknown"
    assert report["running"] is None
    reporting.heartbeat_session("session-1", status="stopping")
    assert session_report(reporting, accounts)["status"] == "stopping"
    reporting.end_session("session-1", reason="Manual stop")
    clock.now += timedelta(days=1)
    reporting.heartbeat_session("session-1")
    report = session_report(reporting, accounts)
    assert report["status"] == "stopped"
    assert report["running"] is False
    assert report["reason"] == "Manual stop"


def test_two_instances_cannot_start_competing_active_tags(tmp_path, accounts, clock):
    path = tmp_path / "activity.sqlite3"
    first = ActivityReporting(path, now=lambda: clock.now)
    second = ActivityReporting(path, now=lambda: clock.now)
    barrier = Barrier(2)

    def start(service, name):
        barrier.wait(timeout=2)
        try:
            return service.start_tag(name, "AAPL")
        except ValueError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start, first, "One"), executor.submit(start, second, "Two")]
            results = [future.result(timeout=5) for future in futures]
        assert sum(result is not None for result in results) == 1
        assert first.snapshot(accounts) == second.snapshot(accounts)
        tag = first.snapshot(accounts)["active_tag_id"]
        second.stop_tag(tag)
        assert first.snapshot(accounts)["active_tag_id"] is None
    finally:
        first.close()
        second.close()
