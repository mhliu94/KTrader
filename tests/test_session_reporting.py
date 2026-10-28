from concurrent.futures import wait
from datetime import datetime, timezone
import json
import threading
from unittest.mock import Mock, patch

import pytest

from order_ui.events import parse_order_message
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.activity_reporting import ActivityReporting
from trading_ui.services.fast_trading import FastTradingStrategyManager, _FastStrategy
from trading_ui.services.market_data import BookLevel, MarketDataStore, OrderBookSnapshot
from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_runtime import SmartSellingRuntime


def fast_command(**updates):
    return {
        "command_id": "fast-parent", "trading_mode": "E", "symbol": "AAPL",
        "fast_trading_price_limit": 10, "fast_trading_account_ids": ["A", "B"],
        "fast_trading_book_levels": 2, "fast_trading_target_percentage": 100,
        "fast_trading_cycle_seconds": 60, **updates,
    }


def fast_manager(reporting, publisher=None, monotonic=None):
    market = MarketDataStore()
    market.upsert_book(OrderBookSnapshot(
        symbol="AAPL", bids=[BookLevel(9, 1000)], asks=[BookLevel(10, 1000)]
    ))
    return FastTradingStrategyManager(
        market_data_store=market,
        account_metas_provider=lambda: {
            "A": AccountMeta("A", 1, "Tiger"), "B": AccountMeta("B", 2, "Tiger")
        },
        publish_command=publisher or Mock(), reporting=reporting,
        **({"monotonic": monotonic} if monotonic else {}),
    )


def test_fast_reports_start_parameters_each_child_and_stop():
    reporting = Mock()
    published = threading.Event()

    def publish(command, account):
        reporting.register_command.assert_any_call("fast-parent", command)
        published.set()

    manager = fast_manager(reporting, publish)
    command = fast_command()
    try:
        manager.start_strategy(command)
        assert published.wait(2)
    finally:
        manager.stop_all()
    start = reporting.start_session.call_args
    assert start.args == ("fast-parent", command, ["A", "B"])
    assert start.kwargs["started_at"]
    assert {call.args[1]["account_id"] for call in reporting.register_command.call_args_list} == {"A", "B"}
    reporting.heartbeat_session.assert_any_call("fast-parent", status="stopping")
    reporting.end_session.assert_called_once_with("fast-parent", status="stopped", reason="Manual stop")


def test_fast_test_mode_never_registers_simulated_orders():
    reporting, publish = Mock(), Mock()
    manager = fast_manager(reporting, publish)
    result = manager.run_cycle(fast_command(fast_trading_test_mode=True))
    assert result.commands
    reporting.register_command.assert_not_called()
    publish.assert_not_called()


def test_failed_worker_start_does_not_leave_running_report():
    reporting = Mock()
    manager = fast_manager(reporting)
    with patch("trading_ui.services.fast_trading.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
        with pytest.raises(RuntimeError, match="thread unavailable"):
            manager.start_strategy(fast_command())
    assert not manager.has_active_session()
    reporting.end_session.assert_called_once_with(
        "fast-parent", status="failed", reason="Strategy worker failed to start"
    )


def test_fast_long_cycle_heartbeats_without_changing_deadline():
    now = [0.0]
    waits = []
    event = Mock(spec=threading.Event)
    event.is_set.return_value = False

    def wait_for(seconds):
        waits.append(seconds)
        now[0] += seconds
        return False

    event.wait.side_effect = wait_for
    reporting = Mock()
    manager = fast_manager(reporting, monotonic=lambda: now[0])
    strategy = _FastStrategy("E:AAPL", fast_command(), event)
    assert manager._wait_for_cycle(strategy, 60) is False
    assert now[0] == 60
    assert waits == [10] * 6
    assert reporting.heartbeat_session.call_count == 6


def test_fast_reporting_failure_does_not_prevent_order_publication():
    reporting, publish = Mock(), Mock()
    reporting.register_command.side_effect = OSError("report database unavailable")
    manager = fast_manager(reporting, publish)
    result = manager.run_cycle(fast_command())
    assert publish.call_count == len(result.commands) == 2


@pytest.fixture
def smart(tmp_path):
    now = [1800000000.0]
    metas = {"A": AccountMeta("A", 1, "Tiger"), "B": AccountMeta("B", 2, "Tiger")}
    snapshots = {
        account: AccountSnapshot(
            account, 0, positions=[Position("XYZ", 1000, available_qty=1000)],
            ts=datetime.fromtimestamp(now[0], timezone.utc).isoformat(), trading_enabled=True,
        ) for account in metas
    }
    reporting, publish = Mock(), Mock()
    engine = SmartSellingEngine(tmp_path / "smart.sqlite3", clock=lambda: 0, wall_clock=lambda: now[0])
    runtime = SmartSellingRuntime(
        {}, account_metas_provider=lambda: metas, snapshots_provider=lambda: snapshots,
        publish_command=publish, reporting=reporting, engine=engine,
        clock=lambda: 0, wall_clock=lambda: now[0],
    )
    runtime.feeds = {"orders": "live", "volume": "live"}
    yield runtime, reporting, publish, now
    runtime.close()


def start_smart(runtime, now):
    return runtime.start_parent({
        "account_ids": ["A", "B"], "symbol": "XYZ", "mode": "urgent",
        "deadline": datetime.fromtimestamp(now[0] + 300, timezone.utc).isoformat(),
        "minimum_price": "9.5",
    })


def test_smart_reports_effective_parameters_all_accounts_and_terminal_stop(smart):
    runtime, reporting, _, now = smart
    parent = start_smart(runtime, now)
    call = reporting.start_session.call_args
    assert call.args[0] == parent["parent_id"]
    assert call.args[2] == ["A", "B"]
    assert call.args[1]["quantity"] == "2000"
    assert call.args[1]["minimum_price"] == "9.5"
    assert call.args[1]["participation_percentage"]
    assert call.args[1]["instrument_rules"]["quantity_step"]
    runtime.on_feed_state("volume", "disconnected")
    reporting.heartbeat_session.assert_any_call(parent["parent_id"], status="paused")
    runtime.stop_parent(parent["parent_id"])
    assert reporting.end_session.call_args.args == (parent["parent_id"],)
    assert reporting.end_session.call_args.kwargs["status"] == "stopped"
    assert reporting.start_session.call_count == 1


def test_smart_registers_confirmable_child_before_publication(smart):
    runtime, reporting, publish, now = smart
    parent = start_smart(runtime, now)

    def on_publish(command, account):
        reporting.register_command.assert_any_call(parent["parent_id"], command)

    publish.side_effect = on_publish
    runtime.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=now[0])
    runtime.engine.on_trade("XYZ", 1000, "t1", source_time=now[0])
    runtime.tick()
    wait(list(runtime._pending), timeout=2)
    assert publish.call_count == 1
    child = publish.call_args.args[0]
    assert child["command_id"] == child["client_order_id"]
    assert child["parent_id"] == parent["parent_id"]
    runtime.stop_parent(parent["parent_id"])
    reporting.heartbeat_session.assert_any_call(parent["parent_id"], status="stopping")
    reporting.end_session.assert_not_called()


def test_smart_reporting_failure_preserves_session_and_orders(smart):
    runtime, reporting, publish, now = smart
    reporting.start_session.side_effect = OSError("report database unavailable")
    reporting.register_command.side_effect = OSError("report database unavailable")
    parent = start_smart(runtime, now)
    assert runtime.has_active_session()
    runtime.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=now[0])
    runtime.engine.on_trade("XYZ", 1000, "t1", source_time=now[0])
    runtime.tick()
    wait(list(runtime._pending), timeout=2)
    assert publish.call_count == 1
    assert publish.call_args.args[0]["parent_id"] == parent["parent_id"]


def test_smart_recovers_active_session_and_child_correlation(smart, tmp_path):
    runtime, _, publish, now = smart
    parent = start_smart(runtime, now)
    runtime.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=now[0])
    runtime.engine.on_trade("XYZ", 1000, "t1", source_time=now[0])
    runtime.tick()
    wait(list(runtime._pending), timeout=2)
    child = publish.call_args.args[0]
    recovered_engine = SmartSellingEngine(
        tmp_path / "smart.sqlite3", clock=lambda: 0, wall_clock=lambda: now[0]
    )
    reporting = Mock()
    recovered = SmartSellingRuntime(
        {}, account_metas_provider=runtime.account_metas_provider,
        snapshots_provider=runtime.snapshots_provider, publish_command=Mock(),
        reporting=reporting, engine=recovered_engine,
        clock=lambda: 0, wall_clock=lambda: now[0],
    )
    try:
        assert reporting.start_session.call_args.args[0] == parent["parent_id"]
        assert reporting.start_session.call_args.args[2] == ["A", "B"]
        reporting.heartbeat_session.assert_any_call(parent["parent_id"], status="reconciling")
        assert reporting.register_command.call_args.args[1]["command_id"] == child["command_id"]
        reporting.end_session.assert_not_called()
    finally:
        recovered.close()


def filled_event(command, *, order_id, filled=2, price=9.75, **changes):
    return parse_order_message(json.dumps({
        "schema_version": 1, "account_id": command["account_id"], "order_id": order_id,
        "sequence": 1, "created_at": command["ts"], "updated_at": command["ts"],
        "symbol": command["symbol"], "side": command["side"], "order_type": "LIMIT",
        "quantity": command["qty_shares"], "filled_quantity": filled,
        "average_fill_price": price, "limit_price": command["limit_price"],
        "status": "PARTIALLY_FILLED", "currency": "USD",
        "command_id": command["command_id"], **changes,
    }).encode())


def test_fast_real_reporting_persists_parameters_and_only_correlated_fills(tmp_path):
    path = tmp_path / "activity.sqlite3"
    writer, reader = ActivityReporting(path), ActivityReporting(path)
    published, commands = threading.Event(), []

    def publish(command, account):
        commands.append(dict(command))
        if len(commands) == 2:
            published.set()

    manager = fast_manager(writer, publish)
    metas = manager._account_metas_provider()
    command = fast_command()
    try:
        manager.start_strategy(command)
        assert published.wait(2)
        session = reader.snapshot(metas)["sessions"][0]
        assert session["parameters"] == command
        assert session["account_ids"] == ["A", "B"]
        assert session["running"] is True
        assert session["totals"]["shares_bought"] == 0
        for index, child in enumerate(commands):
            reader.record_order(filled_event(child, order_id=f"broker-{index}"))
        unrelated = {**commands[0], "command_id": "unrelated-manual-command"}
        reader.record_order(filled_event(unrelated, order_id="manual", filled=100))
        manager.stop_all()
        session = reader.snapshot(metas)["sessions"][0]
        assert session["status"] == "stopped"
        assert session["running"] is False
        assert session["totals"]["shares_bought"] == 4
        assert session["totals"]["cash_by_currency"]["USD"]["cash_change"] == -39
        assert {row["account_id"]: row["shares_bought"] for row in session["accounts"]} == {"A": 2, "B": 2}
    finally:
        manager.stop_all()
        writer.close()
        reader.close()
    reopened = ActivityReporting(path)
    try:
        session = reopened.snapshot(metas)["sessions"][0]
        assert session["parameters"] == command
        assert session["ended_at"]
        assert session["totals"]["shares_bought"] == 4
    finally:
        reopened.close()


def test_smart_real_reporting_serializes_effective_settings_and_tracks_fill_then_stop(smart, tmp_path):
    runtime, original_reporting, publish, now = smart
    path = tmp_path / "activity.sqlite3"
    clock = lambda: datetime.fromtimestamp(now[0], timezone.utc)
    writer, reader = ActivityReporting(path, now=clock), ActivityReporting(path, now=clock)
    runtime._reporting = writer
    metas = runtime.account_metas_provider()
    try:
        parent = start_smart(runtime, now)
        initial = reader.snapshot(metas)["sessions"][0]
        assert initial["id"] == parent["parent_id"]
        assert initial["parameters"]["quantity"] == "2000"
        assert initial["parameters"]["minimum_price"] == "9.5"
        assert initial["parameters"]["instrument_rules"]["quantity_step"]
        assert initial["parameters"]["account_quantities"] == {"A": "1000", "B": "1000"}
        assert initial["account_ids"] == ["A", "B"]
        assert initial["running"] is True
        runtime.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=now[0])
        runtime.engine.on_trade("XYZ", 1000, "t1", source_time=now[0])
        runtime.tick()
        wait(list(runtime._pending), timeout=2)
        child = publish.call_args.args[0]
        event = filled_event(child, order_id="smart-broker", filled=3, price=10)
        reader.record_order(event)
        runtime.on_order_payload(json.dumps(event.payload).encode())
        runtime.stop_parent(parent["parent_id"])
        wait(list(runtime._pending), timeout=2)
        stopping = reader.snapshot(metas)["sessions"][0]
        assert stopping["status"] == "stopping"
        final = filled_event(child, order_id="smart-broker", filled=3, price=10,
                             status="CANCELED", sequence=2)
        reader.record_order(final)
        runtime.on_order_payload(json.dumps(final.payload).encode())
        stopped = reader.snapshot(metas)["sessions"][0]
        assert stopped["status"] == "stopped"
        assert stopped["running"] is False
        assert stopped["parameters"] == initial["parameters"]
        assert stopped["started_at"] == initial["started_at"]
        assert stopped["totals"]["shares_sold"] == 3
        assert stopped["totals"]["cash_by_currency"]["USD"]["cash_change"] == 30
        assert len(stopped["accounts"]) == 2
    finally:
        runtime._reporting = original_reporting
        writer.close()
        reader.close()
