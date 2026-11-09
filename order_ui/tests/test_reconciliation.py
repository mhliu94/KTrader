from contextlib import closing
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from order_ui.config import load_config
from order_ui.reconciliation import ReconciliationView
from order_ui.webserver import create_app
from trading_ui.models import AccountMeta


@pytest.fixture
def databases(tmp_path):
    sources = []
    for mode in ("G", "H"):
        path = tmp_path / f"{mode}.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE smart_parents (id TEXT PRIMARY KEY, active INTEGER, data TEXT)")
        with closing(sqlite3.connect(str(path) + ".feedback.sqlite3")) as db:
            db.executescript("""
                CREATE TABLE feedback_orders (account_id TEXT, order_id TEXT, symbol TEXT,
                    side TEXT, status TEXT, sequence INTEGER, created_at TEXT, updated_at TEXT);
                CREATE TABLE feedback_pending_commands (command_id TEXT, account_id TEXT,
                    symbol TEXT, pending INTEGER);
            """)
        sources.append({"mode": mode, "state_db": str(path)})
    reporting = tmp_path / "activity.sqlite3"
    with closing(sqlite3.connect(reporting)) as db:
        db.execute("CREATE TABLE activity_orders (account_id TEXT, order_id TEXT, payload TEXT)")
    return sources, str(reporting)


def write(path, sql, args=()):
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, args)
        db.commit()


def broker(source, account="A", status="PENDING_NEW"):
    write(source["state_db"] + ".feedback.sqlite3", "INSERT INTO feedback_orders VALUES (?,?,?,?,?,?,?,?)",
          (account, "order-1", "AEHL", "SELL", status, 0, "2026-10-26T15:00:00Z", "2026-10-26T15:00:00Z"))


def pending(source, account="A", symbol="POM", command_id="limit_2026-10-26T15:32:47.123Z_123"):
    write(source["state_db"] + ".feedback.sqlite3", "INSERT INTO feedback_pending_commands VALUES (?,?,?,1)",
          (command_id, account, symbol))


def child(source, *, broker_id=None):
    data = {"symbol": "AEHL", "state": "STOPPING", "order": {
        "account_id": "A", "client_order_id": "smart-child", "broker_order_id": broker_id,
        "state": "RECONCILE", "dispatch_prepared": True, "quantity": "100", "cumulative_filled": "0",
        "limit_price": "17.4", "send_wall": 1793912348,
    }}
    write(source["state_db"], "INSERT INTO smart_parents VALUES (?,1,?)", ("session-1", json.dumps(data)))


ACCOUNTS = {"A": AccountMeta("A", 1, "GigaMoney")}


def test_persistent_blockers_all_dates_deduplicate_and_resolve_without_mutation(databases):
    sources, report = databases
    for source in sources:
        broker(source)
        broker(source, "HIDDEN")
        pending(source)
        pending(source, "HIDDEN")
    child(sources[0])
    message = "Broker status: VarietiesNotReported <img src=x onerror=alert(1)>"
    write(report, "INSERT INTO activity_orders VALUES (?,?,?)", ("A", "order-1", json.dumps({
        "status": "PENDING_NEW", "quantity": 39809, "filled_quantity": 0, "message": message,
    })))
    paths = [Path(source["state_db"]) for source in sources]
    paths += [Path(source["state_db"] + ".feedback.sqlite3") for source in sources]
    before = {path: path.read_bytes() for path in paths}
    view = ReconciliationView(sources, report)
    result = view.snapshot(ACCOUNTS)
    assert result["complete"] and result["details_available"]
    assert len(result["items"]) == 3
    assert {row["account_id"] for row in result["items"]} == {"A"}
    order = next(row for row in result["items"] if row["kind"] == "broker_order")
    assert order["modes"] == ["G", "H"]
    assert order["quantity"] == 39809 and order["message"] == message
    assert order["age_seconds"] > 86400
    command = next(row for row in result["items"] if row["kind"] == "pending_command")
    assert command["modes"] == ["G", "H"]
    assert command["updated_at"] == "2026-10-26T15:32:47.123Z"
    assert "quantity" not in command
    algo = next(row for row in result["items"] if row["kind"] == "algo_order")
    assert algo["order_id"] is None and algo["command_id"] == "smart-child"
    assert algo["reason"] == "algo_reconciliation" and algo["side"] == "SELL"
    assert before == {path: path.read_bytes() for path in paths}

    # A terminal update in only one controller must not hide another's blocker.
    write(sources[0]["state_db"] + ".feedback.sqlite3", "UPDATE feedback_orders SET status='CANCELED'")
    order = next(row for row in view.snapshot(ACCOUNTS)["items"] if row["kind"] == "broker_order")
    assert order["modes"] == ["H"]
    for source in sources:
        feedback = source["state_db"] + ".feedback.sqlite3"
        write(feedback, "UPDATE feedback_orders SET status='CANCELED'")
        write(feedback, "UPDATE feedback_pending_commands SET pending=0")
        write(source["state_db"], "UPDATE smart_parents SET active=0")
    assert view.snapshot(ACCOUNTS)["items"] == []


def test_bound_child_shares_broker_row_and_terminal_report_does_not_clear_controller(databases):
    sources, report = databases
    broker(sources[0])
    child(sources[0], broker_id="order-1")
    write(report, "INSERT INTO activity_orders VALUES (?,?,?)", ("A", "order-1", json.dumps({
        "status": "FILLED", "quantity": 100, "filled_quantity": 100,
    })))
    rows = ReconciliationView(sources, report).snapshot(ACCOUNTS)["items"]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["reason"] == "controller_pending"
    assert rows[0]["reported_status"] == "FILLED"


def test_unavailable_sources_are_explicit_and_missing_databases_are_not_created(databases, tmp_path):
    sources, report = databases
    broker(sources[0])
    Path(sources[1]["state_db"]).unlink()
    Path(sources[1]["state_db"] + ".feedback.sqlite3").write_bytes(b"not a database")
    result = ReconciliationView(sources, report).snapshot(ACCOUNTS)
    assert not result["complete"] and len(result["items"]) == 1
    assert result["sources"][1] == {"mode": "H", "feedback": "unavailable", "state": "missing"}
    assert not Path(sources[1]["state_db"]).exists()
    assert str(tmp_path) not in json.dumps(result)


def test_shared_config_resolves_explicit_state_paths(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "accounts": [{"string_id": "A", "numeric_id": 1, "broker": "GigaMoney", "trading_medium": "API"}],
        "auth": {"users": {"admin": {"password": "test"}, "trader": {"password": "test"}}},
        "smart_selling": {"state_db": "custom/sell.sqlite3"},
        "smart_buying": {"state_db": str(tmp_path / "buy.sqlite3")},
    }))
    sources = load_config(path)["reconciliation_sources"]
    assert sources[0]["state_db"] == str(tmp_path / "custom/sell.sqlite3")
    assert sources[1]["state_db"] == str(tmp_path / "buy.sqlite3")


def test_api_requires_login_and_refreshes_persisted_blockers(databases, tmp_path):
    sources, _ = databases
    broker(sources[0])

    class Consumer:
        def __init__(self, *_): pass
        def start(self): pass
        def stop(self): pass

    config = {"accounts": ACCOUNTS, "users": {"trader": {"password": "test"}},
              "timezone": "UTC", "refresh_interval_seconds": 2, "server": {"ssl_certfile": ""},
              "kafka": {}, "reporting_db_path": str(tmp_path / "api-report.sqlite3"),
              "reconciliation_sources": sources}
    with TestClient(create_app(config=config, consumer_factory=Consumer)) as client:
        assert client.get("/api/orders").status_code == 401
        assert client.post("/api/login", json={"username": "trader", "password": "test"},
                           headers={"X-Order-UI-Request": "1"}).status_code == 200
        result = client.get("/api/orders").json()["unreconciled"]
        assert result["complete"] and len(result["items"]) == 1
        write(sources[0]["state_db"] + ".feedback.sqlite3", "UPDATE feedback_orders SET status='FILLED'")
        assert client.get("/api/orders").json()["unreconciled"]["items"] == []
