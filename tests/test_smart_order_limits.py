"""G/H order guards exercise real reservations, depth snapshots and dispatch."""

import json
from decimal import Decimal

import pytest

from trading_ui.services.smart_buying import SmartBuyingEngine
from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_market import SmartSellingMarketAdapter


@pytest.fixture(params=["SELL", "BUY"])
def engine(request, tmp_path):
    kind = SmartSellingEngine if request.param == "SELL" else SmartBuyingEngine
    instance = kind(tmp_path / "orders.sqlite3", clock=lambda: 0, wall_clock=lambda: 1800000000)
    yield instance
    instance.close()


def start(engine, **overrides):
    settings = dict(
        account_id="A", symbol="XYZ", quantity="1000", mode="urgent", deadline=None,
        tick_size=".01", quantity_step="1", min_quantity="100", min_notional="0",
        account_budgets={"A": "100000"},
    )
    settings.update(overrides)
    parent = engine.start_parent(settings)
    if engine.side == "BUY":
        engine.cash_limits["A"] = Decimal(100000)
    quote(engine)
    return parent["parent_id"]


def quote(engine, **overrides):
    values = dict(
        bid="10", ask="10.1", bid_size=10000, ask_size=10000,
        bid_prices=["10", "9.9", "9.7", "9.3", "8.5", "6.9"],
        ask_prices=["10.1", "10.2", "10.4", "10.8", "11.6", "13.2"],
        source_time=1800000000,
    )
    values.update(overrides)
    engine.on_quote("XYZ", **values)


def fund(engine, amount="10000", event="tape"):
    engine.on_trade("XYZ", amount, event, source_time=1800000000)


def test_five_displayed_levels_block_without_reserving_or_spending_credit(engine):
    bound = {"minimum_price": "11.6"} if engine.side == "SELL" else {"maximum_price": "8.5"}
    parent = start(engine, **bound)
    fund(engine)
    before = engine.get_parent(parent)
    for _ in range(3):
        assert engine.tick() == []
    after = engine.get_parent(parent)
    assert "five displayed levels" in after["reason"]
    assert after["reserved"] == "0"
    assert after["free_credit"] == before["free_credit"]
    assert not any(e["kind"] == "submit_intent" for e in engine.events(parent))
    # The bound becomes marketable: accumulated credit can now fund an order.
    if engine.side == "SELL":
        quote(engine, bid="11.6", ask="11.7", ask_prices=["11.7"])
    else:
        quote(engine, bid="8.4", ask="8.5", bid_prices=["8.4"])
    assert engine.tick()[0]["type"] == "submit"


@pytest.mark.parametrize("distance", [4, 5, 6])
def test_dispatch_rechecks_real_levels_and_refunds_blocked_intents(engine, distance):
    bound = {"minimum_price": "11.6"} if engine.side == "SELL" else {"maximum_price": "8.5"}
    parent = start(engine, **bound)
    # Plan while the user bound is at the market.
    if engine.side == "SELL":
        quote(engine, bid="11.6", ask="11.7")
    else:
        quote(engine, bid="8.4", ask="8.5")
    fund(engine)
    action = engine.tick()[0]
    reserved = engine.get_parent(parent)
    # Gaps are deliberately far larger than a tick; repeated prices count once.
    levels = (
        ["10.1", "10.2", "10.4", "10.8", "11.0"][:distance - 1] + ["11.6", "13.2"]
        if engine.side == "SELL" else
        ["10", "9.9", "9.7", "9.3", "9.0"][:distance - 1] + ["8.5", "6.9"]
    )
    key = "ask_prices" if engine.side == "SELL" else "bid_prices"
    quote(engine, **{key: list(reversed(levels)) + [levels[0]] * 5})
    prepared = engine.prepare_dispatch(action["client_order_id"])
    after = engine.get_parent(parent)
    if distance < 5:
        assert prepared is not None
        assert prepared["limit_price"] == action["limit_price"]
    else:
        assert prepared is None
        assert after["reserved"] == "0"
        assert Decimal(after["free_credit"]) == Decimal(reserved["free_credit"]) + Decimal(reserved["reserved"])
        assert "five displayed levels" in after["reason"]
        assert engine.tick() == []


def test_missing_deep_levels_wait_instead_of_guessing_ticks(engine):
    bound = {"minimum_price": "11.6"} if engine.side == "SELL" else {"maximum_price": "8.5"}
    parent = start(engine, **bound)
    quote(engine, bid_prices=["10", "9.9"], ask_prices=["10.1", "10.2"])
    fund(engine)
    assert engine.tick() == []
    assert "waiting for displayed depth" in engine.get_parent(parent)["reason"]


@pytest.mark.parametrize("with_bound", [False, True])
def test_no_bound_or_marketable_bound_does_not_require_deep_book(engine, with_bound):
    bound = {"minimum_price": "1"} if engine.side == "SELL" else {"maximum_price": "100"}
    start(engine, **(bound if with_bound else {}))
    quote(engine, bid_prices=[], ask_prices=[])
    fund(engine)
    assert engine.tick()[0]["type"] == "submit"


def test_final_dispatch_never_shrinks_below_100(engine):
    parent = start(engine)
    fund(engine)
    action = engine.tick()[0]
    before = engine.get_parent(parent)
    quote(engine, bid_size=990, ask_size=99)
    assert engine.prepare_dispatch(action["client_order_id"]) is None
    after = engine.get_parent(parent)
    assert after["reserved"] == "0"
    assert Decimal(after["free_credit"]) == Decimal(before["free_credit"]) + Decimal(before["reserved"])


def test_selling_accumulates_100_credit_even_after_partial_flush_timer(tmp_path):
    now = [0]
    instance = SmartSellingEngine(tmp_path / "selling.sqlite3", clock=lambda: now[0], wall_clock=lambda: 1800000000)
    try:
        parent = start(instance, min_quantity="1")
        fund(instance, "999")
        now[0] = 30
        quote(instance)
        assert instance.tick() == []
        assert Decimal(instance.get_parent(parent)["free_credit"]) == Decimal("99.9")
        fund(instance, "1", "extra")
        assert Decimal(instance.tick()[0]["quantity"]) == 100
    finally:
        instance.close()


def test_existing_small_slice_is_upgraded_without_releasing_working_reservation(tmp_path):
    instance = SmartSellingEngine(tmp_path / "recovery.sqlite3", clock=lambda: 0, wall_clock=lambda: 1800000000)
    try:
        parent = start(instance)
        fund(instance)
        child = instance.tick()[0]
        stored = json.loads(instance._db.execute("SELECT data FROM smart_parents WHERE id=?", (parent,)).fetchone()[0])
        stored.update(min_quantity="1", child_cap="10", base_child_cap="10")
        instance._db.execute("UPDATE smart_parents SET data=? WHERE id=?", (json.dumps(stored), parent))
        instance.close()
        instance = SmartSellingEngine(tmp_path / "recovery.sqlite3", clock=lambda: 0, wall_clock=lambda: 1800000000)
        recovered = instance.get_parent(parent)
        assert Decimal(recovered["child_cap"]) == 100
        assert recovered["reserved"] == child["quantity"]
        assert recovered["active_order"]["client_order_id"] == child["client_order_id"]
        quote(instance)
        assert all(a["type"] != "submit" for a in instance.tick())
    finally:
        instance.close()


def test_adapter_counts_distinct_positive_displayed_levels(tmp_path):
    instance = SmartSellingEngine(tmp_path / "depth.sqlite3", clock=lambda: 0, wall_clock=lambda: 1800000000)
    try:
        parent = start(instance, minimum_price="11.6")
        adapter = SmartSellingMarketAdapter(instance, clock=lambda: 0, wall_clock=lambda: 1800000000)
        adapter.apply_price_book({
            "symbol": "XYZ", "book_observed_at": 1800000000,
            "bid_side": {"levels": [{"price": "10", "quantity": 10000}]},
            "offer_side": {"levels": [
                {"prc": p, "qty": 10000} for p in ["11.6", "10.1", "10.4", "10.8", "10.1"]
            ] + [{"price": "10.2", "quantity": 0}, {"price": "10.3", "quantity": "bad"}]},
        })
        # Enable the independent tape feed after applying this depth snapshot.
        instance.set_feed_health("XYZ", True)
        fund(instance)
        assert instance.tick()[0]["type"] == "submit"  # Four real prices, despite gaps/duplicates.
        instance.abort_dispatch(instance.get_parent(parent)["active_order"]["client_order_id"], "test cleanup")
    finally:
        instance.close()
