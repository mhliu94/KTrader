"""Service wiring tests use no Kafka clients, threads, or production configuration."""

from unittest.mock import Mock

import pytest

from trading_ui import webserver
from trading_ui.models import AccountSnapshot
from trading_ui.store import AccountStore


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    config = {
        "accounts": [
            {"string_id": "VISIBLE", "numeric_id": 1, "broker": "Tiger"},
            {"string_id": "HIDDEN", "numeric_id": 2, "broker": "Other", "hidden": True},
        ],
        "symbols": ["AAPL"], "auth": {"users": {"admin": {"password": "test"}}},
        "kafka": {"bootstrap_servers": "unused.invalid:9092"},
        "market_data": {"historical_prices_csv": str(tmp_path / "prices.csv")},
        "smart_selling": {"enabled": True},
        "reporting": {"db_path": "state/activity.sqlite3"},
    }
    path = tmp_path / "config.json"
    monkeypatch.setenv("ACCOUNT_DASHBOARD_CONFIG", str(path))
    monkeypatch.setattr(webserver, "load_config", Mock(return_value=config))
    for name in (
        "APP_CONFIG", "ACCOUNT_METAS", "SYMBOLS", "AUTH_USERS", "ACCOUNT_CONSUMER",
        "MARKET_DATA_CONSUMER", "COMMANDS_PRODUCER", "FAST_TRADING_MANAGER",
        "SMART_SELLING_MANAGER", "SMART_SELLING_CONSUMER", "OPERATIONS_LOG",
        "ACTIVITY_REPORTING", "SYSTEM_SNAPSHOTS", "HIST_CLOSE_STORE",
    ):
        monkeypatch.setattr(webserver, name, None)
    store = AccountStore()
    for index, account in enumerate(("VISIBLE", "HIDDEN", "UNCONFIGURED"), start=1):
        store.upsert(AccountSnapshot(account, 100 * index, account_num_id=index))
    monkeypatch.setattr(webserver, "store", store)
    events, factories = [], {}
    for name in (
        "HistoricalCloseStore", "OperationsLog", "ActivityReporting", "SystemSnapshotRecorder",
        "AccountDetailsConsumer", "TradingCommandsProducer", "FastTradingStrategyManager",
        "SmartSellingRuntime", "SmartSellingConsumer", "PriceBookConsumer",
    ):
        instance = Mock(name=name + " instance")
        for method in ("start", "stop", "stop_all", "close", "flush"):
            getattr(instance, method).side_effect = (
                lambda *args, name=name, method=method, **kwargs: events.append((name, method))
            )
        factories[name] = Mock(name=name, return_value=instance)
        monkeypatch.setattr(webserver, name, factories[name])
    return config, path, factories, store, events


def test_startup_archives_every_account_and_keeps_hidden_accounts_out_of_trading(lifecycle):
    _, path, factories, store, _ = lifecycle
    try:
        webserver.on_startup()
        recorder = factories["SystemSnapshotRecorder"]
        args, kwargs = recorder.call_args
        assert args[0] == str(path.parent / "state" / "activity.sqlite3")
        assert set(kwargs["accounts_provider"]()) == {"VISIBLE", "HIDDEN"}
        assert kwargs.get("interval_seconds", 60) == 60
        snapshots, source = kwargs["snapshots_provider"]()
        assert snapshots == store.get_all()
        assert source == "kafka"
        consumer_accounts = factories["AccountDetailsConsumer"].call_args.args[2]
        assert set(consumer_accounts) == {"VISIBLE", "HIDDEN"}
        assert set(webserver.ACCOUNT_METAS) == {"VISIBLE"}
        assert set(webserver.get_served_snapshots()[0]) == {"VISIBLE"}
        for factory_name in ("FastTradingStrategyManager", "SmartSellingRuntime"):
            manager_args = factories[factory_name].call_args.kwargs
            assert set(manager_args["account_metas_provider"]()) == {"VISIBLE"}
            assert manager_args["reporting"] is factories["ActivityReporting"].return_value
        factories["AccountDetailsConsumer"].return_value.start.assert_called_once_with()
        recorder.return_value.start.assert_called_once_with()
        factories["SmartSellingRuntime"].return_value.start.assert_called_once_with()
    finally:
        webserver.on_shutdown()


@pytest.mark.parametrize("smart_enabled", [True, False])
def test_shutdown_closes_reporting_after_algorithms_and_stops_snapshot_worker(lifecycle, smart_enabled):
    config, _, factories, _, events = lifecycle
    config["smart_selling"]["enabled"] = smart_enabled
    webserver.on_startup()
    events.clear()
    webserver.on_shutdown()
    factories["SystemSnapshotRecorder"].return_value.stop.assert_called_once_with()
    factories["AccountDetailsConsumer"].return_value.stop.assert_called_once_with()
    factories["PriceBookConsumer"].return_value.stop.assert_called_once_with()
    factories["FastTradingStrategyManager"].return_value.stop_all.assert_called_once_with()
    factories["ActivityReporting"].return_value.close.assert_called_once_with()
    factories["TradingCommandsProducer"].return_value.flush.assert_called_once_with(2.0)
    assert events.index(("FastTradingStrategyManager", "stop_all")) < events.index(("ActivityReporting", "close"))
    if smart_enabled:
        factories["SmartSellingRuntime"].return_value.close.assert_called_once_with()
        assert events.index(("SmartSellingRuntime", "close")) < events.index(("ActivityReporting", "close"))
    else:
        factories["SmartSellingRuntime"].assert_not_called()
        factories["SmartSellingConsumer"].assert_not_called()


def test_failed_startup_cleans_started_snapshot_worker_and_reporting(lifecycle):
    _, _, factories, _, _ = lifecycle
    factories["TradingCommandsProducer"].side_effect = RuntimeError("publisher startup failed")
    with pytest.raises(RuntimeError, match="publisher startup failed"):
        webserver.on_startup()
    factories["SystemSnapshotRecorder"].return_value.stop.assert_called_once_with()
    factories["AccountDetailsConsumer"].return_value.stop.assert_called_once_with()
    factories["ActivityReporting"].return_value.close.assert_called_once_with()
    assert webserver.SYSTEM_SNAPSHOTS is None
    assert webserver.ACCOUNT_CONSUMER is None
    assert webserver.ACTIVITY_REPORTING is None


def test_cleanup_failure_does_not_skip_other_services_or_mask_startup_error(lifecycle):
    _, _, factories, _, _ = lifecycle
    factories["TradingCommandsProducer"].side_effect = RuntimeError("publisher startup failed")
    factories["SystemSnapshotRecorder"].return_value.stop.side_effect = OSError("recorder stop failed")
    with pytest.raises(RuntimeError, match="publisher startup failed"):
        webserver.on_startup()
    factories["AccountDetailsConsumer"].return_value.stop.assert_called_once_with()
    factories["ActivityReporting"].return_value.close.assert_called_once_with()
    assert webserver.SYSTEM_SNAPSHOTS is None
    assert webserver.ACTIVITY_REPORTING is None


def test_shutdown_is_idempotent_and_disabled_restart_has_no_old_smart_callbacks(lifecycle):
    config, _, factories, _, _ = lifecycle
    webserver.on_startup()
    webserver.on_shutdown()
    webserver.on_shutdown()
    factories["ActivityReporting"].return_value.close.assert_called_once_with()
    factories["SystemSnapshotRecorder"].return_value.stop.assert_called_once_with()
    config["smart_selling"]["enabled"] = False
    try:
        webserver.on_startup()
        assert webserver.SMART_SELLING_MANAGER is None
        market_args = factories["PriceBookConsumer"].call_args.kwargs
        assert market_args["payload_callback"] is None
        assert market_args["health_callback"] is None
    finally:
        webserver.on_shutdown()
