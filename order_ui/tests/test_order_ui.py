from datetime import datetime
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from confluent_kafka import KafkaError, OFFSET_BEGINNING, TopicPartition
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest

from order_ui.config import default_config_path, load_config
from order_ui.consumer import OrderStatusConsumer
from order_ui.events import SCHEMA, parse_order_message
from order_ui.store import OrderStore
from order_ui.webserver import COOKIE, SESSION_SECONDS, create_app
from trading_ui.models import AccountMeta


def payload(**updates):
    return {"schema_version": 1, "account_id": "A", "order_id": "order-1", "sequence": 0,
            "created_at": "2026-09-27T09:30:00-04:00", "updated_at": "2026-09-27T09:30:00-04:00",
            "symbol": "AAPL", "side": "BUY", "order_type": "LIMIT", "status": "NEW",
            "quantity": 10, "filled_quantity": 0, "average_fill_price": None, "limit_price": 200,
            **updates}


def event(**updates):
    return parse_order_message(json.dumps(payload(**updates)).encode())


@pytest.fixture
def clock():
    return SimpleNamespace(now=datetime.fromisoformat("2026-09-27T15:00:00+00:00"))


@pytest.fixture
def store(clock):
    return OrderStore({"A": AccountMeta("A", 1, "Tiger"), "B": AccountMeta("B", 2, "Other")},
                      "America/New_York", now=lambda: clock.now)


@pytest.fixture
def config_file(tmp_path):
    accounts = {"accounts": [
        {"string_id": "A", "numeric_id": 1, "broker": "Tiger", "trading_medium": "API"},
        {"string_id": "HIDDEN", "numeric_id": 2, "broker": "Other", "trading_medium": "WEB", "hidden": True},
    ]}
    (tmp_path / "accounts.json").write_text(json.dumps(accounts))
    config = {"accounts_file": "accounts.json", "auth": {"users": {
        "admin": {"password": "test-admin"}, "trader": {"password": "test-trader"},
        "extra": {"password": "test-extra"}}}, "kafka": {"bootstrap_servers": "shared-broker:9092"},
        "server": {"host": "other-host", "port": 8000, "ssl_enabled": True, "ssl_certfile": "missing.crt"}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_schema_and_published_example():
    Draft202012Validator.check_schema(SCHEMA)
    example = Path(__file__).parents[1] / "sample" / "order-status.v1.json"
    parse_order_message(example.read_bytes())


@pytest.mark.parametrize("updates", [
    {"schema_version": 2}, {"schema_version": True}, {"sequence": -1},
    {"sequence": True}, {"sequence": 9007199254740992}, {"status": "made_up"},
    {"account_id": " A"}, {"account_id": ""}, {"quantity": 0}, {"quantity": None},
    {"filled_quantity": 11}, {"filled_quantity": -1}, {"quantity": float("nan")},
    {"limit_price": float("inf")}, {"average_fill_price": -1}, {"extra": "unknown field"},
    {"created_at": "2026-09-27T09:30:00"}, {"created_at": "2026-02-30T09:30:00Z"},
    {"updated_at": "2026-09-27T09:29:59-04:00"}, {"status": "FILLED"},
    {"status": "PARTIALLY_FILLED", "filled_quantity": 10},
    {"status": "PARTIALLY_FILLED", "filled_quantity": 0}, {"limit_price": None},
    {"order_type": "STOP", "stop_price": None},
])
def test_invalid_snapshots_rejected(updates):
    with pytest.raises(ValueError):
        event(**updates)


@pytest.mark.parametrize("raw", [b"no json", b"[]", b"null", b"\xff", b"{}", b" " * 65537],
                         ids=["invalid-json", "array", "null", "invalid-utf8", "missing-fields", "too-large"])
def test_malformed_messages_rejected(raw):
    with pytest.raises((ValueError, UnicodeError)):
        parse_order_message(raw)


def test_supports_notional_orders_and_fractional_fills():
    snap = event(quantity=None, notional=100, order_type="MARKET", status="FILLED",
                 filled_quantity=0.45, average_fill_price=222.222222)
    assert snap.payload["quantity"] is None
    assert event(quantity=0.5, status="PARTIALLY_FILLED", filled_quantity=0.25).payload["filled_quantity"] == 0.25


def test_latest_sequence_wins_and_accounts_have_separate_order_ids(store):
    assert store.upsert(event(sequence=2, status="FILLED", filled_quantity=10))
    assert not store.upsert(event(sequence=1))
    assert not store.upsert(event(sequence=2))
    assert store.upsert(event(account_id="B"))
    rows = store.snapshot()["orders"]
    assert len(rows) == 2
    assert next(row for row in rows if row["account_id"] == "A")["status"] == "FILLED"


def test_immutable_creation_time_and_monotonic_update_time(store):
    store.upsert(event(updated_at="2026-09-27T10:00:00-04:00"))
    with pytest.raises(ValueError, match="immutable"):
        store.upsert(event(sequence=1, created_at="2026-09-27T09:29:00-04:00"))
    with pytest.raises(ValueError, match="cannot decrease"):
        store.upsert(event(sequence=1))
    assert store.snapshot()["orders"][0]["sequence"] == 0


def test_only_configured_accounts_with_consistent_ids(store):
    assert not store.upsert(event(account_id="HIDDEN"))
    assert not store.upsert(event(account_id="UNKNOWN"))
    with pytest.raises(ValueError, match="account_num_id"):
        store.upsert(event(account_num_id=2))
    assert store.upsert(event(account_num_id=1))


def test_today_uses_creation_date_not_update_date_and_rolls_at_midnight(store, clock):
    assert not store.upsert(event(created_at="2026-09-27T03:59:59Z", updated_at="2026-09-27T14:00:00Z"))
    assert store.upsert(event(created_at="2026-09-27T04:00:00Z"))
    assert not store.upsert(event(order_id="tomorrow", created_at="2026-09-28T04:00:00Z", updated_at="2026-09-28T04:00:00Z"))
    clock.now = datetime.fromisoformat("2026-09-28T03:59:59+00:00")
    assert len(store.snapshot()["orders"]) == 1
    clock.now = datetime.fromisoformat("2026-09-28T04:00:00+00:00")
    assert store.snapshot()["date"] == "2026-09-28"
    assert store.snapshot()["orders"] == []
    assert not store.upsert(event(updated_at="2026-09-28T05:00:00Z"))


@pytest.mark.parametrize("now,start", [
    ("2026-03-08T12:00:00Z", "2026-03-08T05:00:00Z"),
    ("2026-11-01T12:00:00Z", "2026-11-01T04:00:00Z"),
])
def test_new_york_daylight_saving_boundaries(store, clock, now, start):
    clock.now = datetime.fromisoformat(now)
    assert store.upsert(event(created_at=start, updated_at=now))
    assert len(store.snapshot()["orders"]) == 1


def test_shared_config_reuses_accounts_auth_and_brokers_but_own_server(config_file):
    cfg = load_config(config_file)
    assert list(cfg["accounts"]) == ["A"]
    assert set(cfg["users"]) == {"admin", "trader", "extra"}
    assert cfg["server"]["port"] == 8030
    assert cfg["server"]["host"] == "127.0.0.1"
    assert cfg["server"]["ssl_certfile"] == ""
    assert cfg["kafka"]["bootstrap_servers"] == "shared-broker:9092"
    assert cfg["timezone"] == "America/New_York"


def test_config_aliases_inline_accounts_and_defaults(config_file, monkeypatch):
    cfg = json.loads(config_file.read_text())
    cfg["trading_accounts_file"] = cfg.pop("accounts_file")
    config_file.write_text(json.dumps(cfg))
    assert list(load_config(config_file)["accounts"]) == ["A"]
    cfg.pop("trading_accounts_file")
    monkeypatch.setenv("TRADING_ACCOUNTS_CONFIG", "accounts.json")
    config_file.write_text(json.dumps(cfg))
    assert list(load_config(config_file)["accounts"]) == ["A"]
    monkeypatch.delenv("TRADING_ACCOUNTS_CONFIG")
    cfg["accounts"] = [{"id": "INLINE", "num_id": 4, "broker": "Other"}]
    cfg["order_ui"] = {"timezone": "UTC", "server": {"port": 9000}, "kafka": {"topic": "custom-orders"}}
    config_file.write_text(json.dumps(cfg))
    loaded = load_config(config_file)
    assert list(loaded["accounts"]) == ["INLINE"]
    assert loaded["kafka"]["topic"] == "custom-orders"
    assert loaded["server"]["port"] == 9000
    monkeypatch.setenv("ACCOUNT_DASHBOARD_CONFIG", str(config_file))
    assert default_config_path() == config_file
    monkeypatch.setenv("ORDER_UI_CONFIG", "/explicit/order.json")
    assert default_config_path() == Path("/explicit/order.json")


@pytest.mark.parametrize("settings", [
    {"timezone": "Invalid/Zone"}, {"refresh_interval_seconds": 0}, {"refresh_interval_seconds": True},
    {"server": {"port": 0}}, {"server": {"port": 8000.5}}, {"server": {"ssl_certfile": "missing"}},
    {"kafka": {"poll_timeout_sec": 99}}, {"kafka": {"topic": ""}},
    {"kafka": {"client_config": {"group.id": "trading-ui"}}}, {"unknown": 1},
])
def test_invalid_config_fails_with_clear_error(config_file, settings):
    cfg = json.loads(config_file.read_text())
    cfg["order_ui"] = settings
    config_file.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        load_config(config_file)


def message(raw=None, error=None, partition=0):
    msg = Mock()
    msg.value.return_value = raw
    msg.error.return_value = error
    msg.topic.return_value = "order-status"
    msg.partition.return_value = partition
    msg.offset.return_value = 4
    return msg


def test_consumer_replays_from_beginning_and_uses_independent_groups(config_file, store):
    cfg = load_config(config_file)["kafka"]
    first, second = OrderStatusConsumer(cfg, store), OrderStatusConsumer(cfg, store)
    assert first.group_id != second.group_id
    conf = first._client_config()
    assert not conf["enable.auto.commit"]
    assert not conf["enable.auto.offset.store"]
    assert conf["auto.offset.reset"] == "earliest"
    assert not conf["allow.auto.create.topics"]
    client = Mock()
    partitions = [TopicPartition("order-status", 0, 100), TopicPartition("order-status", 1, 200)]
    first._on_assign(client, partitions)
    assert all(p.offset == OFFSET_BEGINNING for p in client.assign.call_args.args[0])
    eof = KafkaError(KafkaError._PARTITION_EOF)
    first._handle_message(message(error=eof))
    assert store.snapshot()["feed"]["state"] == "replaying"
    first._handle_message(message(error=eof, partition=1))
    assert store.snapshot()["feed"]["state"] == "live"
    first._on_error(KafkaError(KafkaError._ALL_BROKERS_DOWN))
    assert store.snapshot()["feed"]["state"] == "disconnected"
    first._on_stats(json.dumps({"brokers": {"one": {"nodeid": 1, "state": "UP"}}}))
    assert store.snapshot()["feed"]["state"] == "live"


def test_consumer_skips_bad_data_and_recovers(config_file, store):
    consumer = OrderStatusConsumer(load_config(config_file)["kafka"], store)
    for raw in [b"bad", b"[]", None, json.dumps(payload(account_num_id=99)).encode()]:
        consumer._handle_message(message(raw))
    consumer._handle_message(message(json.dumps(payload()).encode()))
    assert store.snapshot()["feed"]["invalid_messages"] == 4
    assert len(store.snapshot()["orders"]) == 1


def test_consumer_thread_closes_client_on_shutdown(config_file, store):
    client = Mock()
    factory = Mock(return_value=client)
    consumer = OrderStatusConsumer(load_config(config_file)["kafka"], store, factory)
    polled = Event()
    def poll(timeout):
        polled.set()
        consumer._stop.set()
        return message(json.dumps(payload()).encode())
    client.poll.side_effect = poll
    consumer.start()
    assert polled.wait(2)
    consumer.stop()
    assert not consumer._thread.is_alive()
    client.close.assert_called_once()
    assert len(store.snapshot()["orders"]) == 1


@pytest.fixture
def client(config_file, store):
    factory = Mock()
    app = create_app(config_file, consumer_factory=factory)
    with TestClient(app) as client:
        app.state.store = store
        yield client
    factory.return_value.start.assert_called_once()
    factory.return_value.stop.assert_called_once()


def login(client, username="trader"):
    return client.post("/api/login", json={"username": username, "password": f"test-{username}"},
                       headers={"X-Order-UI-Request": "1"})


def test_login_authentication_cookie_isolation_logout_and_no_secrets(client, store):
    assert client.get("/api/orders").status_code == 401
    html = client.get("/")
    assert html.status_code == 200
    assert "test-trader" not in html.text
    assert html.headers["cache-control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200
    client.cookies.set("auth_token", "existing-trading-ui-session")
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.cookies.get("auth_token") == "existing-trading-ui-session"
    store.upsert(event())
    data = client.get("/api/orders")
    assert data.json()["orders"][0]["order_id"] == "order-1"
    assert data.json()["accounts"] == [{"id": "A", "num_id": 1, "broker": "Tiger"}]
    assert "password" not in data.text and "shared-broker" not in data.text
    assert client.post("/api/logout", headers={"X-Order-UI-Request": "1"}).status_code == 200
    assert client.get("/api/orders").status_code == 401
    assert client.cookies.get("auth_token") == "existing-trading-ui-session"


def test_wrong_credentials_cross_origin_and_expired_sessions(client):
    body = {"username": "trader", "password": "wrong"}
    assert client.post("/api/login", json=body, headers={"X-Order-UI-Request": "1"}).status_code == 401
    assert client.post("/api/login", json=body).status_code == 403
    assert client.post("/api/login", json=body, headers={"X-Order-UI-Request": "1", "Origin": "https://other.example"}).status_code == 403
    assert login(client, "extra").status_code == 200
    assert client.get("/api/orders").json()["username"] == "extra"
    import time
    with patch("order_ui.webserver.time.time", return_value=time.time() + SESSION_SECONDS + 1):
        assert client.get("/api/orders").status_code == 401


def test_api_updates_without_navigation_and_handles_midnight(client, store, clock):
    login(client)
    assert client.get("/api/orders").json()["orders"] == []
    store.upsert(event())
    assert client.get("/api/orders").json()["orders"][0]["status"] == "NEW"
    store.upsert(event(sequence=1, status="FILLED", filled_quantity=10))
    assert client.get("/api/orders").json()["orders"][0]["status"] == "FILLED"
    clock.now = datetime.fromisoformat("2026-09-28T04:00:00Z")
    assert client.get("/api/orders").json()["orders"] == []
