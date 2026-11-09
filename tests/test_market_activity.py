"""Window accounting and access checks use temporary DBs and fake feeds."""

from datetime import datetime, timezone
import json
from unittest.mock import Mock

from confluent_kafka import TopicPartition, OFFSET_END, KafkaException, KafkaError
from fastapi.testclient import TestClient
import pytest

from order_ui.events import parse_order_message
from order_ui.store import OrderStore
from trading_ui import webserver
from trading_ui.kafka.consumer_market_data import PriceBookConsumer
from trading_ui.models import AccountMeta
from trading_ui.services.market_activity import MarketActivity
from trading_ui.services.market_data import MarketDataStore, StockQuote, stock_quote_from_payload
from trading_ui.templates import render_layout


START = datetime(2026, 11, 9, 15, 20, tzinfo=timezone.utc).timestamp()
ACCOUNTS = {"A": AccountMeta("A", 1, "Tiger"), "B": AccountMeta("B", 2, "Fangde")}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def quote(at, volume, price=10, symbol="XYZ", error=None):
    return StockQuote(symbol, price, at - 1, at, is_delayed=False,
                      volume=volume, last_size=25, error=error)


def order(at, filled, *, sequence=1, order_id="one", side="BUY", account="A", created=START, symbol="XYZ"):
    return parse_order_message(json.dumps({
        "schema_version": 1, "account_id": account, "order_id": order_id,
        "sequence": sequence, "created_at": iso(created), "updated_at": iso(at),
        "symbol": symbol, "side": side, "order_type": "LIMIT", "status": "NEW" if not filled else "PARTIALLY_FILLED",
        "quantity": 1000, "filled_quantity": filled, "average_fill_price": 10 if filled else None,
        "limit_price": 10, "currency": "USD",
    }).encode())


@pytest.fixture
def history(tmp_path):
    db = MarketActivity(tmp_path / "market.sqlite3", clock=lambda: START + 120)
    yield db
    db.close()


def snapshot(history, now=START + 120, feed="live", symbol="XYZ"):
    return history.snapshot(symbol, ACCOUNTS, now=now, order_feed=feed)


def test_four_aligned_windows_and_current_rollover(history):
    result = snapshot(history)
    assert [row["start"] for row in result["windows"]] == [START, START-300, START-600, START-900]
    assert all(row["end"] - row["start"] == 300 for row in result["windows"])
    assert [row["current"] for row in result["windows"]] == [True, False, False, False]
    assert snapshot(history, START + 300)["windows"][0]["start"] == START + 300
    assert snapshot(history, START - .001)["windows"][0]["start"] == START - 300
    assert result["timezone"] == "America/New_York"


def test_counter_deltas_weight_prices_and_do_not_use_last_trade_size(history):
    for at, volume, price in ((0, 1000, 99), (10, 1100, 10), (20, 1400, 20), (30, 1400, 999)):
        history.record_quote(quote(START + at, volume, price))
    result = snapshot(history, START + 30)
    row = result["windows"][0]
    assert row["market_volume"] == 400
    assert row["vwap_estimate"] == 17.5
    assert row["market_complete"] is True
    assert result["last_trade"] == {"time": START+29, "quantity": 25, "price": 999}
    assert result["quote_state"] == "live"


def test_no_history_and_no_trades_are_distinct(history):
    row = snapshot(history)["windows"][0]
    assert row["market_volume"] is None and row["vwap_estimate"] is None
    assert not row["market_complete"]
    history.record_quote(quote(START, 500))
    history.record_quote(quote(START + 20, 500))
    row = snapshot(history, START + 20)["windows"][0]
    assert row["market_volume"] == 0 and row["vwap_estimate"] is None
    assert row["market_complete"]


def test_market_volume_at_boundary_is_not_double_counted(history):
    for at, volume in ((-20, 100), (0, 120), (20, 150)):
        history.record_quote(quote(START + at, volume))
    rows = snapshot(history, START + 20)["windows"]
    assert rows[0]["market_volume"] == 30
    assert rows[1]["market_volume"] == 20
    assert not rows[1]["market_complete"]


def test_duplicates_and_out_of_order_quotes_are_idempotent_across_restart(history, tmp_path):
    for at in (20, 0, 10, 20):
        history.record_quote(quote(START + at, 100 + at))
    before = snapshot(history, START + 20)
    reopened = MarketActivity(tmp_path / "market.sqlite3", clock=lambda: START + 120)
    try:
        assert snapshot(reopened, START + 20) == before
        assert before["windows"][0]["market_volume"] == 20
    finally:
        reopened.close()


@pytest.mark.parametrize("middle", [quote(START+10, 900), quote(START+10, None, error="unavailable")])
def test_counter_resets_and_bad_quotes_do_not_invent_volume(history, middle):
    history.record_quote(quote(START, 1000))
    history.record_quote(middle)
    history.record_quote(quote(START+20, 1000))
    row = snapshot(history, START+20)["windows"][0]
    assert row["market_volume"] == (100 if middle.volume is not None else 0)
    assert row["market_complete"] is False


def test_stale_feed_and_large_gaps_are_explicit(history):
    history.record_quote(quote(START, 1000))
    history.record_quote(quote(START+120, 5000))
    row = snapshot(history)["windows"][0]
    assert row["market_volume"] == 0
    assert row["vwap_estimate"] is None and not row["market_complete"]
    stale = snapshot(history, START+151)
    assert stale["quote_state"] == "unavailable"
    assert set(stale["last_trade"].values()) == {None}


def test_daily_counter_reset_has_no_negative_volume(history):
    midnight = datetime(2026, 11, 10, 5, tzinfo=timezone.utc).timestamp()
    history.clock = lambda: midnight+20
    for at, volume in ((-10, 50000), (0, 0), (10, 100)):
        history.record_quote(quote(midnight + at, volume))
    rows = snapshot(history, midnight+10)["windows"]
    assert rows[0]["market_volume"] == 100
    assert rows[1]["market_volume"] is None
    assert not rows[1]["market_complete"]


def test_own_fills_are_deltas_by_report_time_with_buy_sell_gross_and_account_filter(history):
    events = [order(START, 0), order(START+20, 10, sequence=2),
              order(START+30, 10, sequence=3), order(START+301, 25, sequence=4),
              order(START+302, 5.5, side="SELL", order_id="two", account="B", created=START+300),
              order(START+310, 900, account="HIDDEN", order_id="hidden", created=START+300)]
    for event in events + [events[1], events[3]]:
        history.record_order(event)
    rows = snapshot(history, START+320)["windows"]
    assert (rows[0]["bought"], rows[0]["sold"], rows[0]["gross"]) == (15, 5.5, 20.5)
    assert (rows[1]["bought"], rows[1]["sold"], rows[1]["gross"]) == (10, 0, 10)
    assert rows[0]["our_complete"] is False


def test_fill_replay_order_does_not_change_totals_or_move_fills_on_status_only_updates(history):
    events = [order(START, 0), order(START+10, 10, sequence=2),
              order(START+310, 20, sequence=3), order(START+320, 20, sequence=4)]
    for event in reversed(events):
        history.record_order(event)
    rows = snapshot(history, START+320)["windows"]
    assert [row["bought"] for row in rows[:2]] == [10, 10]
    assert history.record_order(events[-1]) is False


def test_cumulative_corrections_remove_prior_fills_without_negative_gross(history):
    for event in (order(START, 0), order(START+10, 10, sequence=2),
                  order(START+310, 30, sequence=3), order(START+320, 5, sequence=4)):
        history.record_order(event)
    rows = snapshot(history, START+320)["windows"]
    assert rows[0]["gross"] == 0 and rows[1]["gross"] == 5
    assert not rows[0]["our_complete"] and not rows[1]["our_complete"]


def test_missing_order_feed_never_reports_an_all_clear_zero(history):
    result = snapshot(history, feed="replaying")
    assert all(row["gross"] is None and not row["our_complete"] for row in result["windows"])
    history.record_order(order(START+10, 5))
    result = snapshot(history, feed="disconnected")
    assert result["windows"][0]["gross"] == 5
    assert not result["windows"][0]["our_complete"]


def test_late_fill_on_prior_day_order_and_prefixed_symbols_are_included(history):
    history.record_order(order(START-86400, 10, created=START-86400, symbol="US.XYZ"))
    history.record_order(order(START+10, 20, sequence=2, created=START-86400, symbol="US.XYZ"))
    assert snapshot(history)["windows"][0]["bought"] == 10


def test_conflicting_sequences_and_identity_changes_are_rejected(history):
    history.record_order(order(START+20, 10, sequence=2))
    for event in (order(START+20, 20, sequence=2), order(START+10, 20, sequence=3),
                  order(START+30, 20, sequence=3, side="SELL")):
        with pytest.raises(ValueError):
            history.record_order(event)


def test_equivalent_numeric_fills_are_deduplicated(history):
    assert history.record_order(order(START+10, 10))
    assert history.record_order(order(START+10, 10.0)) is False
    assert snapshot(history)["windows"][0]["gross"] == 10


def test_hidden_account_history_does_not_mask_unavailable_own_volume(history):
    history.record_order(order(START+10, 10, account="HIDDEN"))
    assert snapshot(history, feed="disconnected")["windows"][0]["gross"] is None


def test_retention_preserves_an_old_order_baseline(history):
    created = START - 3*86400
    history.record_order(order(created, 10, created=created))
    history.record_order(order(created+30, 20, sequence=2, created=created))
    history.record_quote(quote(START, 100))  # Runs cleanup.
    assert history._db.execute("SELECT COUNT(*) FROM market_order_samples").fetchone()[0] == 1
    history.record_order(order(START+10, 25, sequence=3, created=created))
    assert snapshot(history)["windows"][0]["bought"] == 5


@pytest.fixture
def client(monkeypatch, history):
    monkeypatch.setattr(webserver, "SESSIONS", {"token-"+str(i): user for i, user in enumerate(("lmh", "admin", "trader", "LMH", " lmh "))})
    monkeypatch.setattr(webserver, "APP_CONFIG", {"market_data": {"symbols": ["XYZ", "ABC"]}})
    monkeypatch.setattr(webserver, "ACCOUNT_METAS", ACCOUNTS)
    monkeypatch.setattr(webserver, "MARKET_ACTIVITY", history)
    order_store = OrderStore(ACCOUNTS, "America/New_York", reporting=history)
    order_store.update_feed(state="live")
    monkeypatch.setattr(webserver, "MARKET_ORDER_STORE", order_store)
    monkeypatch.setattr(webserver, "MD_STORE", MarketDataStore())
    # Deliberately no context manager: never start production consumers.
    instance = TestClient(webserver.app)
    yield instance
    instance.close()


def test_market_page_api_and_navigation_require_exact_lmh(client):
    assert client.get("/market-data", follow_redirects=False).status_code == 303
    assert client.get("/api/market-data").status_code == 401
    for user in ("admin", "trader", "LMH", " lmh "):
        client.cookies.set("auth_token", next(token for token, name in webserver.SESSIONS.items() if name == user))
        assert client.get("/market-data").status_code == 403
        assert client.get("/api/market-data?symbol=XYZ").status_code == 403
        assert 'href="/market-data"' not in render_layout("en", "account-details", "", current_user=user)
        assert client.get("/api/quotes?symbol=XYZ").status_code == 200
    client.cookies.set("auth_token", "token-0")
    response = client.get("/market-data")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'href="/market-data"' in response.text
    assert 'id="md-symbol"' in response.text
    assert "TWAP" not in response.text


def test_api_validates_symbol_returns_four_rows_and_last_trade(client, history):
    client.cookies.set("auth_token", "token-0")
    history.record_quote(quote(START+120, 100))
    response = client.get("/api/market-data?symbol= xyz ")
    assert response.status_code == 200
    data = response.json()
    assert len(data["windows"]) == 4
    assert data["last_trade"] == {"time": START+119, "quantity": 25, "price": 10}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/market-data?symbol=UNCONFIGURED").status_code == 400
    assert client.get("/api/market-data").json()["symbol"] == "XYZ"
    assert client.get("/api/market-data?symbol=ABC").json()["last_trade"]["price"] is None


def test_missing_services_and_bad_order_feed_are_explicit(client, monkeypatch):
    client.cookies.set("auth_token", "token-0")
    webserver.MARKET_ORDER_STORE.reject_message()
    data = client.get("/api/market-data").json()
    assert data["order_feed"] == "incomplete"
    monkeypatch.setattr(webserver, "MARKET_ACTIVITY", None)
    assert client.get("/api/market-data").status_code == 503


@pytest.mark.parametrize("offset,expected", [(123, 123), (-1, OFFSET_END)])
def test_stock_quote_replay_uses_time_window_without_changing_book_replay(monkeypatch, offset, expected):
    monkeypatch.setattr("trading_ui.kafka.consumer_market_data.time.time", lambda: START)
    kafka = Mock()
    kafka.offsets_for_times.return_value = [TopicPartition("stock-quotes", 0, offset)]
    kafka.get_watermark_offsets.return_value = (0, 1000)
    consumer = PriceBookConsumer({}, MarketDataStore(), quote_history_seconds=1500)
    parts = [TopicPartition("stock-quotes", 0), TopicPartition("price-books", 0)]
    consumer._on_assign(kafka, parts)
    assert [part.offset for part in parts] == [expected, 900]
    assert kafka.offsets_for_times.call_args.args[0][0].offset == (START-1500)*1000


def test_quote_history_lookup_failure_falls_back_to_bounded_replay():
    kafka = Mock()
    kafka.offsets_for_times.side_effect = KafkaException(KafkaError(KafkaError._TIMED_OUT))
    kafka.get_watermark_offsets.return_value = (0, 1000)
    consumer = PriceBookConsumer({}, MarketDataStore(), quote_history_seconds=1500)
    parts = [TopicPartition("stock-quotes", 0)]
    consumer._on_assign(kafka, parts)
    assert parts[0].offset == 900


def test_consumer_persists_quote_and_missing_size_does_not_invalidate_price(history):
    consumer = PriceBookConsumer({"market_data_topic": "books"}, MarketDataStore(), quote_callback=history.record_quote)
    payload = {"type": "STOCK_QUOTE", "schema_version": 1, "symbol": "XYZ", "healthy": True,
               "is_delayed": False, "observed_at": iso(START+120), "trade_time": iso(START+119),
               "last": "10", "volume": 100, "last_size": 42}
    consumer._handle_payload(payload, topic="stock-quotes")
    assert snapshot(history)["last_trade"]["quantity"] == 42
    for missing in (None, -1, True, "bad"):
        parsed = stock_quote_from_payload({**payload, "last_size": missing})
        assert parsed.last_size is None and parsed.last == 10
