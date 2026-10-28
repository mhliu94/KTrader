"""Optional browser coverage; install Playwright and Chromium to run this test.

Order data and tag mutations are intercepted locally. No Kafka or broker
connections are made and no production account data is used.
"""

import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading

import pytest


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture
def report_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    directory = str(Path(__file__).resolve().parents[1])
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=directory))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            executable = (shutil.which("google-chrome") or shutil.which("chromium")
                          or shutil.which("chromium-browser"))
            try:
                browser = runtime.chromium.launch(
                    executable_path=executable, headless=True, args=["--no-sandbox"], timeout=10000
                )
            except playwright.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Install a Playwright Chromium browser to run this test")
                raise
            try:
                yield browser, server
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_reports_and_tag_controls_in_browser(report_browser):
    browser, server = report_browser
    errors, posts = [], []
    account = {
        "account_id": "A", "account_num_id": 1, "broker": "Tiger",
        "shares_bought": 4.5, "shares_sold": 2, "cash_complete": True,
        "cash_by_currency": {
            "USD": {"buy_cost": 450, "sell_proceeds": 240, "cash_change": -210, "complete": True},
            "HKD": {"buy_cost": 0, "sell_proceeds": 20, "cash_change": 20, "complete": True},
        },
    }
    order = {
        "account_id": "A", "account_num_id": 1, "broker": "Tiger", "order_id": "test-order",
        "created_at": "2026-10-28T13:30:00Z", "updated_at": "2026-10-28T13:31:00Z",
        "symbol": "AAPL", "side": "BUY", "order_type": "LIMIT", "status": "FILLED",
        "filled_quantity": 4.5, "quantity": 4.5, "limit_price": 100,
        "average_fill_price": 100, "currency": "USD",
        "tags": [{"id": "t1", "name": "<img src=x onerror=alert(1)>"}],
    }
    session = {
        "id": "s1", "symbol": "AAPL", "account_ids": ["A", "B"],
        "parameters": {"fast_trading_account_ids": ["A", "B"],
                       "fast_trading_price_limit": 100, "fast_trading_test_mode": False},
        "status": "running", "running": True, "started_at": "2026-10-28T13:00:00Z",
        "ended_at": None, "updated_at": "2026-10-28T13:31:00Z", "reason": None,
        "accounts": [account, {"account_id": "B", "account_num_id": 2, "broker": "Other",
                               "shares_bought": 0, "shares_sold": 0,
                               "cash_by_currency": {}, "cash_complete": True}],
        "totals": account,
    }
    tag = {
        "id": "t1", "name": "<img src=x onerror=alert(1)>", "symbol": "AAPL",
        "started_at": "2026-10-28T13:00:00Z", "stopped_at": None, "active": True,
        "accounts": [account], "totals": account,
    }
    data = {
        "username": "tester", "date": "2026-10-28", "timezone": "America/New_York",
        "refresh_interval_seconds": 60,
        "accounts": [{"id": "A", "num_id": 1, "broker": "Tiger"}],
        "statuses": ["FILLED"], "orders": [order],
        "feed": {"state": "live", "detail": "Kafka connected", "invalid_messages": 0},
        "reporting": {"sessions": [session], "tags": [tag], "active_tag_id": "t1"},
    }
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page.on("pageerror", lambda err: errors.append(str(err)))

    def api(route):
        path = route.request.url.split("/api/", 1)[1]
        if route.request.method == "POST":
            posts.append((path, route.request.post_data_json))
            if path == "tags/t1/stop":
                data["reporting"]["active_tag_id"] = None
                tag.update(active=False, stopped_at="2026-10-28T14:00:00Z")
            elif path == "tags":
                new_tag = {
                    **copy.deepcopy(tag), "id": "t2", "active": True, "stopped_at": None,
                    "name": route.request.post_data_json["name"],
                    "symbol": route.request.post_data_json["symbol"],
                }
                data["reporting"]["tags"].insert(0, new_tag)
                data["reporting"]["active_tag_id"] = "t2"
            route.fulfill(json={"ok": True})
        else:
            route.fulfill(json=data)

    page.route("**/api/**", api)
    page.goto(f"http://127.0.0.1:{server.server_port}/static/index.html")
    page.locator("#dashboard").wait_for(state="visible", timeout=5000)
    assert page.locator("[role=tab]").count() == 3
    assert page.locator("#orders img").count() == 0
    assert "<img src=x" in page.locator("#orders").inner_text()

    page.locator("#tab-orders").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#tab-algo").get_attribute("aria-selected") == "true"
    assert page.locator("#panel-algo").is_visible()
    assert page.locator("#algo-current").inner_text() == "1 active session(s)"
    text = page.locator("#algo-accounts").inner_text()
    assert "#2 · B" in text and "USD -210" in text and "HKD +20" in text
    assert "All accounts total" in text
    assert "Price limit" in page.locator("#algo-parameters").inner_text()
    page.locator("[data-language=zh]").click()
    assert page.locator("#dashboard-title").inner_text() == "算法交易会话"
    assert "价格限制" in page.locator("#algo-parameters").inner_text()

    page.locator("[data-language=en]").click()
    page.locator("#tab-tags").click()
    assert page.locator("#tag-start").is_disabled()
    assert page.locator("#tag-stop").is_visible()
    assert page.locator("#panel-tags img").count() == 0
    page.locator("#tag-stop").click()
    page.wait_for_function("!document.getElementById('tag-start').disabled")
    assert page.locator("#tag-current").inner_text() == "No tag in effect"
    assert len(posts) == 1 and posts[0][0] == "tags/t1/stop"
    page.locator("#tag-name").fill("Morning rebalance")
    page.locator("#tag-symbol").fill("msft")
    page.locator("#tag-start").click()
    page.wait_for_function(
        "document.getElementById('tag-current').textContent.includes('Morning rebalance')"
    )
    assert posts[-1] == ("tags", {"name": "Morning rebalance", "symbol": "MSFT"})

    # Starting a tag selects it; history selection survives polling and translation.
    assert page.locator("#tag-select").input_value() == "t2"
    page.locator("#tag-select").select_option("t1")
    page.evaluate("refresh()")
    assert page.locator("#tag-select").input_value() == "t1"
    page.locator("#tag-select").select_option("t2")
    page.locator("[data-language=zh]").click()
    assert page.locator("#tag-select").input_value() == "t2"
    assert "Morning rebalance" in page.locator("#tag-summary").inner_text()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator("#tag-stop").is_visible()

    # A missing price stays explicitly incomplete and never becomes zero cash.
    data["reporting"]["tags"][0]["accounts"][0]["cash_by_currency"]["USD"] = {
        "buy_cost": None, "sell_proceeds": 240, "cash_change": None, "complete": False,
    }
    page.evaluate("refresh()")
    assert "现金数据不完整" in page.locator("#tag-accounts").inner_text()
    assert "USD —" in page.locator("#tag-accounts").inner_text()

    # API connectivity loss warns and disables tag changes.
    page.route("**/api/orders", lambda route: route.abort())
    page.evaluate("refresh()")
    assert page.locator("#tag-stop").is_disabled()
    assert page.locator("#algo-current").inner_text() == "会话状态暂不可用"
    assert not errors, errors
