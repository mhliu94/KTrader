from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from order_ui.events import parse_order_message
from order_ui.webserver import create_app
from trading_ui.models import AccountMeta


REQUEST_HEADERS = {"X-Order-UI-Request": "1"}


class FakeConsumer:
    def __init__(self, config, store):
        self.store = store
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.store.update_feed(state="live", detail="Live")

    def stop(self):
        self.stopped = True


@pytest.fixture
def clock():
    return SimpleNamespace(now=datetime.fromisoformat("2026-10-28T14:00:00+00:00"))


@pytest.fixture
def config(tmp_path):
    return {
        "accounts": {"A": AccountMeta("A", 1, "Tiger"), "B": AccountMeta("B", 2, "Futu")},
        "users": {"trader": {"password": "test-password"}},
        "timezone": "America/New_York",
        "refresh_interval_seconds": 2,
        "server": {"ssl_certfile": ""},
        "kafka": {},
        "reporting_db_path": str(tmp_path / "activity.sqlite3"),
    }


@contextmanager
def running_client(config, clock):
    app = create_app(config=config, consumer_factory=FakeConsumer)
    with TestClient(app) as client:
        app.state.store._now = lambda: clock.now
        app.state.reporting._now = lambda: clock.now
        yield client
    assert app.state.consumer.started
    assert app.state.consumer.stopped


@pytest.fixture
def client(config, clock):
    with running_client(config, clock) as client:
        yield client


def login(client):
    response = client.post("/api/login", json={"username": "trader", "password": "test-password"}, headers=REQUEST_HEADERS)
    assert response.status_code == 200


def start_tag(client, name="Opening", symbol="AAPL"):
    response = client.post("/api/tags", json={"name": name, "symbol": symbol}, headers=REQUEST_HEADERS)
    assert response.status_code == 201
    return response.json()["id"]


def event(**updates):
    payload = {
        "schema_version": 1, "account_id": "A", "order_id": "order-1", "sequence": 0,
        "created_at": "2026-10-28T14:00:00Z", "updated_at": "2026-10-28T14:00:00Z",
        "symbol": "AAPL", "side": "BUY", "order_type": "MARKET", "status": "PARTIALLY_FILLED",
        "quantity": 10, "filled_quantity": 2, "average_fill_price": 100, "currency": "USD", **updates,
    }
    return parse_order_message(json.dumps(payload).encode())


def report(client):
    response = client.get("/api/orders")
    assert response.status_code == 200
    return response.json()


def test_tag_routes_require_authentication_and_same_origin_request_header(client):
    body = {"name": "Opening", "symbol": "AAPL"}
    assert client.get("/api/orders").status_code == 401
    assert client.post("/api/tags", json=body, headers=REQUEST_HEADERS).status_code == 401
    assert client.post("/api/tags/missing/stop", headers=REQUEST_HEADERS).status_code == 401
    login(client)
    assert client.post("/api/tags", json=body).status_code == 403
    assert client.post("/api/tags/missing/stop").status_code == 403
    foreign_headers = {**REQUEST_HEADERS, "Origin": "https://unrelated.example"}
    assert client.post("/api/tags", json=body, headers=foreign_headers).status_code == 403
    assert client.post("/api/tags/missing/stop", headers=foreign_headers).status_code == 403
    assert report(client)["reporting"]["tags"] == []


@pytest.mark.parametrize("body", [
    {"name": "", "symbol": "AAPL"},
    {"name": "   ", "symbol": "AAPL"},
    {"name": "Bad\nname", "symbol": "AAPL"},
    {"name": "Bad\x00name", "symbol": "AAPL"},
    {"name": "Opening", "symbol": ""},
    {"name": "Opening", "symbol": "   "},
    {"name": "Opening", "symbol": "AA PL"},
    {"name": "Opening", "symbol": "AA\x00PL"},
])
def test_invalid_tag_input_is_rejected_without_creating_a_tag(client, body):
    login(client)
    response = client.post("/api/tags", json=body, headers=REQUEST_HEADERS)
    assert response.status_code == 422
    assert report(client)["reporting"]["tags"] == []


def test_tag_conflict_stop_idempotence_missing_tag_and_order_labels(client, clock):
    login(client)
    tag_id = start_tag(client, "  Opening buys  ", "aapl")
    assert client.post("/api/tags", json={"name": "Other", "symbol": "MSFT"}, headers=REQUEST_HEADERS).status_code == 409
    client.app.state.store.upsert(event())
    data = report(client)
    assert data["orders"][0]["tags"] == [{"id": tag_id, "name": "Opening buys"}]
    assert data["reporting"]["active_tag_id"] == tag_id
    clock.now += timedelta(minutes=1)
    assert client.post(f"/api/tags/{tag_id}/stop", headers=REQUEST_HEADERS).status_code == 200
    stopped_at = report(client)["reporting"]["tags"][0]["stopped_at"]
    clock.now += timedelta(minutes=1)
    assert client.post(f"/api/tags/{tag_id}/stop", headers=REQUEST_HEADERS).status_code == 200
    assert report(client)["reporting"]["tags"][0]["stopped_at"] == stopped_at
    assert report(client)["reporting"]["active_tag_id"] is None
    assert client.post("/api/tags/missing/stop", headers=REQUEST_HEADERS).status_code == 404
    new_id = start_tag(client, "Later buys")
    assert new_id != tag_id
    assert report(client)["orders"][0]["tags"] == [{"id": tag_id, "name": "Opening buys"}]


def test_active_tag_and_trade_totals_survive_order_server_restart(config, clock):
    with running_client(config, clock) as first:
        login(first)
        tag_id = start_tag(first)
        first.app.state.store.upsert(event())
    clock.now += timedelta(minutes=1)
    with running_client(config, clock) as second:
        assert second.get("/api/orders").status_code == 401
        login(second)
        data = report(second)
        assert data["reporting"]["active_tag_id"] == tag_id
        assert data["reporting"]["tags"][0]["totals"]["shares_bought"] == 2
        # The broker replays its retained latest snapshot after reconnecting.
        second.app.state.store.upsert(event())
        data = report(second)
        assert data["orders"][0]["tags"] == [{"id": tag_id, "name": "Opening"}]
        assert data["reporting"]["tags"][0]["totals"]["shares_bought"] == 2


def test_store_keeps_tagged_fills_across_midnight_after_recent_orders_expire(client, clock):
    login(client)
    clock.now = datetime.fromisoformat("2026-10-29T03:59:00+00:00")
    tag_id = start_tag(client)
    store = client.app.state.store
    assert store.upsert(event(created_at="2026-10-29T03:59:00Z", updated_at="2026-10-29T03:59:00Z"))
    assert len(report(client)["orders"]) == 1
    clock.now += timedelta(minutes=2)
    assert report(client)["orders"] == []
    assert not store.upsert(event(sequence=1, filled_quantity=6, created_at="2026-10-29T03:59:00Z", updated_at="2026-10-29T04:01:00Z"))
    data = report(client)
    assert data["date"] == "2026-10-29"
    assert data["orders"] == []
    assert data["reporting"]["active_tag_id"] == tag_id
    assert data["reporting"]["tags"][0]["totals"]["cash_by_currency"]["USD"]["buy_cost"] == 600


def test_store_excludes_future_hidden_and_mismatched_accounts_from_reports(client):
    login(client)
    start_tag(client)
    store = client.app.state.store
    assert not store.upsert(event(account_id="HIDDEN"))
    assert not store.upsert(event(created_at="2026-10-29T14:00:00Z", updated_at="2026-10-29T14:00:00Z"))
    with pytest.raises(ValueError, match="account_num_id"):
        store.upsert(event(account_num_id=2))
    data = report(client)
    assert data["orders"] == []
    assert data["reporting"]["tags"][0]["accounts"] == []
    assert data["reporting"]["tags"][0]["totals"]["shares_bought"] == 0


def test_session_report_includes_every_participating_account_and_confirmed_cash(client):
    login(client)
    reporting = client.app.state.reporting
    parameters = {"symbol": "AAPL", "trading_mode": "F", "fast_trading_price_limit": 100}
    reporting.start_session("session", parameters, ["A", "B"])
    reporting.register_command("session", {"account_id": "A", "command_id": "child-A"})
    reporting.register_command("session", {"account_id": "B", "command_id": "child-B"})
    client.app.state.store.upsert(event(command_id="child-A"))
    first = report(client)["reporting"]["sessions"][0]
    assert first["parameters"] == parameters
    assert first["running"] is True
    assert [row["account_id"] for row in first["accounts"]] == ["A", "B"]
    assert first["accounts"][1]["shares_sold"] == 0
    client.app.state.store.upsert(event(account_id="B", command_id="child-B", side="SELL", filled_quantity=3, average_fill_price=110))
    second = report(client)["reporting"]["sessions"][0]
    assert second["totals"]["shares_bought"] == 2
    assert second["totals"]["shares_sold"] == 3
    assert second["totals"]["cash_by_currency"]["USD"]["cash_change"] == 130
