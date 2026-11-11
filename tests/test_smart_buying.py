import json
from concurrent.futures import wait
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from trading_ui import webserver
from trading_ui.models import AccountMeta, AccountSnapshot
from trading_ui.services.algo_sessions import AlgoSession, AlgoSessionCoordinator
from trading_ui.services.fast_trading import FastTradingStrategyManager
from trading_ui.services.market_data import BookLevel, MarketDataStore, OrderBookSnapshot
from trading_ui.services.smart_buying import SmartBuyingEngine
from trading_ui.services.smart_buying_runtime import SmartBuyingRuntime


@pytest.fixture
def buying(tmp_path):
    clock = [1800000000.0]
    iso = lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat()
    snapshots = {"A": AccountSnapshot("A", 10000, ts=iso(), trading_enabled=True)}
    metas = {"A": AccountMeta("A", 1, "Tiger", "API")}
    md = MarketDataStore()
    md.upsert_book(OrderBookSnapshot("XYZ", asks=[BookLevel(20, 10000)], asof_epoch=int(clock[0])))
    engine = SmartBuyingEngine(tmp_path / "buying.sqlite3", clock=lambda: clock[0], wall_clock=lambda: clock[0])
    publish = Mock()
    runtime = SmartBuyingRuntime(
        {}, engine=engine, market_data_store=md, account_metas_provider=lambda: metas,
        snapshots_provider=lambda: snapshots, publish_command=publish,
        clock=lambda: clock[0], wall_clock=lambda: clock[0],
    )
    runtime.feeds = {"orders": "live", "volume": "live"}

    class Harness:
        def start(self, **settings):
            self.parent = runtime.start_parent({"account_ids": ["A"], "symbol": "XYZ", "deadline": None, **settings})
            self.quote()
            return self.parent

        def quote(self, ask=20, size=10000, **depth):
            engine.on_quote("XYZ", Decimal(str(ask)) - Decimal(".01"), ask, 10000,
                            ask_size=size, source_time=clock[0], **depth)

        def volume(self, quantity, event="trade-1"):
            engine.on_trade("XYZ", quantity, event, source_time=clock[0])

        def tick(self):
            runtime.tick()
            wait(list(runtime._pending), timeout=5)
            return publish.call_args.args[0] if publish.called else None

        def report(self, command, *, status="FILLED", filled=None, sequence=1, **extra):
            payload = {
                "schema_version": 1, "account_id": command["account_id"], "order_id": command["command_id"],
                "sequence": sequence, "created_at": command["ts"], "updated_at": iso(), "symbol": "XYZ",
                "side": "BUY", "order_type": "LIMIT", "status": status,
                "quantity": command["qty_shares"],
                "filled_quantity": command["qty_shares"] if filled is None else filled,
                "average_fill_price": command["limit_price"], "limit_price": command["limit_price"],
                "command_id": command["command_id"], **extra,
            }
            runtime.on_order_payload(json.dumps(payload).encode())

        def status(self):
            p = engine.get_parent(self.parent["parent_id"])
            assert Decimal(p["earned"]) == sum(Decimal(p[k]) for k in ("free_credit", "reserved", "filled", "discarded"))
            return p

    h = Harness()
    h.runtime, h.engine, h.publish = runtime, engine, publish
    h.clock, h.snapshots, h.metas, h.md = clock, snapshots, metas, md
    yield h
    runtime.close()


@pytest.mark.parametrize("mode,volume", [("patient", 2000), ("normal", 1250), ("urgent", 1000)])
def test_fixed_participation_and_minimum_child(buying, mode, volume):
    p = buying.start(mode=mode, participation_percentage=99, min_quantity=1)
    expected = {"patient": ".05", "normal": ".08", "urgent": ".15"}[mode]
    assert Decimal(p["participation"]) == Decimal(expected)
    assert buying.tick() is None  # Startup never invents tape credit.
    buying.volume(volume)
    command = buying.tick()
    assert command["side"] == "BUY"
    assert command["trading_mode"] == "H"
    assert command["qty_shares"] == int(Decimal(volume) * Decimal(expected))
    assert command["limit_price"] == 20
    assert command["type"] == "LIMIT_ORDER"


@pytest.mark.parametrize("mode", ["ultra", "4"])
def test_selling_ultra_mode_is_not_available_for_buying(buying, mode):
    assert buying.runtime.config["participation_percentages"] == {
        "patient": "5", "normal": "8", "urgent": "15",
    }
    with pytest.raises(ValueError, match="mode must be"):
        buying.start(mode=mode)
    with pytest.raises(ValueError, match="mode must be"):
        buying.engine.start_parent({"account_id": "A", "symbol": "XYZ", "quantity": "100", "mode": mode})


def test_subminimum_credit_accumulates_without_rounding_up(buying):
    buying.start()
    buying.volume(1249)
    assert buying.tick() is None
    buying.volume(1, "trade-2")
    assert buying.tick()["qty_shares"] == 100
    buying.volume(1250, "trade-2")  # Replay earns no more credit.
    assert Decimal(buying.status()["earned"]) == 100


def test_maximum_price_and_actual_ask_liquidity(buying):
    buying.start(maximum_price="19.99")
    buying.volume(10000)
    assert buying.tick() is None
    buying.quote(ask="19.99", size=99)
    assert buying.tick() is None
    buying.quote(ask="19.99", size=150)
    command = buying.tick()
    assert command["qty_shares"] == 150
    assert command["limit_price"] <= 19.99


def test_cash_resized_at_dispatch_and_limit_never_chases(buying):
    buying.start(maximum_price=20)
    buying.volume(10000)
    action = buying.engine.tick()[0]
    buying.snapshots["A"].available_cash_by_currency = {"USD": 2500}
    buying.quote(ask=21, bid_prices=["20.99", "20.5", "20"])
    buying.runtime._publish_action(action)
    command = buying.publish.call_args.args[0]
    assert command["qty_shares"] == 125
    assert command["limit_price"] == 20


def test_partial_fills_and_cancel_reserve_cash_until_terminal(buying):
    buying.snapshots["A"].cash = 5000
    buying.start()
    buying.volume(10000)
    command = buying.tick()
    assert command["qty_shares"] == 250
    buying.report(command, status="PARTIALLY_FILLED", filled=150)
    assert buying.status()["reserved"] == "100"
    assert buying.status()["accounts"]["A"]["spent"] == "3000"
    buying.runtime.stop_parent(buying.parent["parent_id"])
    assert buying.runtime.owns_account("A")
    with pytest.raises(ValueError, match="at most 1 buying strategy"):
        buying.start(symbol="OTHER")
    buying.report(command, status="CANCELED", filled=175, sequence=2)
    assert buying.status()["accounts"]["A"]["spent"] == "3500"
    assert not buying.runtime.owns_account("A")
    assert buying.status()["reserved"] == "0"


def test_one_child_at_a_time_and_subminimum_tail_stays_unspent(buying):
    buying.snapshots["A"].cash = 2500
    buying.start()
    buying.volume(1250)
    command = buying.tick()
    buying.volume(10000, "trade-2")
    buying.tick()
    assert buying.publish.call_count == 1
    buying.report(command)
    assert buying.status()["state"] == "COMPLETED"
    assert buying.status()["remaining"] == "25"
    assert buying.status()["accounts"]["A"]["spent"] == "2000"


def test_each_accounts_cash_caps_its_own_rotating_child(buying):
    buying.snapshots["A"].cash = 2000
    buying.metas["B"] = AccountMeta("B", 2, "Tiger", "API")
    buying.snapshots["B"] = AccountSnapshot("B", 6000, ts=buying.snapshots["A"].ts, trading_enabled=True)
    buying.start(account_ids=["A", "B"])
    buying.volume(5000)
    first = buying.tick()
    assert (first["account_id"], first["qty_shares"]) == ("A", 100)
    buying.report(first)
    buying.clock[0] += 5
    buying.quote()
    second = buying.tick()
    assert (second["account_id"], second["qty_shares"]) == ("B", 300)


def test_cash_budget_not_reused_when_account_snapshot_lags(buying):
    buying.start()
    buying.quote(ask=30)
    buying.volume(10000)
    command = buying.tick()
    assert command["qty_shares"] == 333
    buying.report(command)
    buying.clock[0] += 5
    buying.quote(ask=30)
    buying.volume(10000, "trade-2")
    buying.tick()
    assert buying.publish.call_count == 1  # Only $10 remains, despite the old $10k snapshot.


def test_stale_and_disabled_accounts_and_owned_accounts_rejected(buying):
    buying.snapshots["A"].trading_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        buying.start()
    buying.snapshots["A"].trading_enabled = True
    sessions = [AlgoSession("fast", "E", "OTHER", ["A"])]
    buying.runtime._session_coordinator.register(lambda: sessions)
    with pytest.raises(ValueError, match="at most 1 buying strategy"):
        buying.start()
    sessions.clear()
    buying.snapshots["A"].ts = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="stale"):
        buying.start()


def test_scheduled_start_and_deadline_do_not_invent_volume(buying):
    buying.start(start_time=buying.clock[0] + 10, deadline=buying.clock[0] + 20)
    buying.volume(10000)
    assert buying.tick() is None
    buying.clock[0] += 10
    buying.quote()
    assert buying.tick() is None
    buying.volume(1250, "trade-2")
    command = buying.tick()
    assert command["qty_shares"] == 100
    buying.clock[0] += 10
    buying.tick()
    assert buying.status()["state"] == "STOPPING"
    assert buying.status()["reserved"] == "100"


def test_buy_feedback_rejects_sell_side_and_restart_keeps_reservation(buying, tmp_path):
    buying.start()
    buying.volume(1250)
    command = buying.tick()
    with pytest.raises(ValueError):
        buying.report(command, side="SELL")
    recovered = SmartBuyingEngine(tmp_path / "buying.sqlite3", clock=lambda: buying.clock[0], wall_clock=lambda: buying.clock[0])
    coordinator = AlgoSessionCoordinator()
    recovered_runtime = SmartBuyingRuntime(
        {}, engine=recovered, market_data_store=buying.md, account_metas_provider=lambda: buying.metas,
        snapshots_provider=lambda: buying.snapshots, publish_command=Mock(), session_coordinator=coordinator,
    )
    fast = FastTradingStrategyManager(
        MarketDataStore(), lambda: buying.metas, Mock(), session_coordinator=coordinator,
    )
    try:
        parent = recovered.get_parent(buying.parent["parent_id"])
        assert parent["reserved"] == "100"
        assert parent["accounts"]["A"]["cash_budget"] == "10000"
        assert not any(a["type"] == "submit" for a in recovered.tick())
        with pytest.raises(ValueError, match="at most 1 buying strategy"):
            fast.start_strategy({
                "trading_mode": "E", "symbol": "OTHER", "fast_trading_price_limit": 10,
                "fast_trading_account_ids": ["A"], "fast_trading_book_levels": 1,
                "fast_trading_target_percentage": 100, "fast_trading_cycle_seconds": 5,
            })
    finally:
        recovered_runtime.close()
        fast.stop_all()


def test_authenticated_same_origin_api_starts_and_stops_mode_h(buying):
    with patch.multiple(webserver, SMART_BUYING_MANAGER=buying.runtime, SESSIONS={"token": "trader"}):
        client = TestClient(webserver.app)
        try:
            payload = {"symbol": "XYZ", "account_ids": ["A"], "deadline": None}
            assert client.post("/api/smart-buying", json=payload).status_code == 401
            client.cookies.set("auth_token", "token")
            assert client.post("/api/smart-buying", json=payload, headers={"Origin": "https://elsewhere.invalid"}).status_code == 403
            created = client.post("/api/smart-buying", json=payload)
            assert created.status_code == 200
            parent = created.json()["parent"]
            assert parent["participation"] == "0.08"
            assert client.get("/api/smart-buying").json()["parents"][0]["parent_id"] == parent["parent_id"]
            assert client.post('/api/smart-buying/' + parent['parent_id'] + '/stop').status_code == 200
        finally:
            client.close()


def test_live_volume_adapter_earns_credit_once_and_uses_ask_depth(buying):
    buying.start()

    def volume(sequence, total):
        return {
            "schema_version": 1, "type": "TRADING_VOLUME", "symbol": "XYZ", "source": "TradeStation",
            "feed_id": "live", "sequence": sequence, "total_volume": total,
            "observed_at": datetime.fromtimestamp(buying.clock[0], timezone.utc).isoformat(),
            "interval_start": datetime.fromtimestamp(buying.clock[0] - 1, timezone.utc).isoformat(),
            "session": "2027-01-15", "healthy": True, "is_halted": False, "is_delayed": False,
        }

    buying.runtime.on_volume_payload(volume(1, 10000))
    assert Decimal(buying.status()["earned"]) == 0
    buying.clock[0] += 1
    update = volume(2, 11250)
    buying.runtime.on_volume_payload(update)
    buying.runtime.on_volume_payload(update)
    buying.runtime.on_price_book({
        "symbol": "XYZ", "update_time": int(buying.clock[0] * 1e9), "sequence_num": 1,
        "bid_side": {"levels": [{"price": "19.99", "quantity": "10"}]},
        "offer_side": {"levels": [{"price": "20", "quantity": "150"}]},
    })
    assert Decimal(buying.status()["earned"]) == 100
    assert buying.tick()["qty_shares"] == 100  # Uses ask liquidity, not 10% of the bid.


def test_broker_minimum_above_100_is_preserved(buying):
    buying.runtime.config["instruments"]["XYZ"] = {"min_quantity": "200", "quantity_step": "100"}
    buying.start()
    buying.volume(1250)
    assert buying.tick() is None
    buying.volume(1250, "trade-2")
    assert buying.tick()["qty_shares"] == 200
