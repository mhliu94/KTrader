"""Optional browser coverage; install Playwright and Chromium to run this test.

Order data and tag mutations are intercepted locally. No Kafka or broker
connections are made and no production account data is used.
"""

import copy
from datetime import datetime, timedelta, timezone
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
            "USD": {"buy_cost": 450, "sell_proceeds": 240, "cash_change": -210, "complete": True,
                    "shares_bought": 4.5, "shares_sold": 2, "average_buy_price": 100, "average_sell_price": 120},
            "HKD": {"buy_cost": 0, "sell_proceeds": 20.0126, "cash_change": 20.0126, "complete": True,
                    "shares_bought": 0, "shares_sold": 0.1, "average_buy_price": None, "average_sell_price": 200.126},
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
    assert page.locator("[role=tab]").count() == 4
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
    assert "AVG. BUY PRICE" in text and "AVG. SELL PRICE" in text
    assert page.locator("#algo-accounts tbody tr").first.locator("td").nth(4).inner_text() == "HKD 200.13\nUSD 120.00"
    assert page.locator("#algo-accounts tbody tr").nth(1).locator("td").nth(2).inner_text() == "—"
    assert page.locator("#algo-accounts tfoot td").nth(2).inner_text() == "HKD —\nUSD 100.00"
    assert page.locator("#algo-accounts tfoot td").nth(4).inner_text() == "HKD 200.13\nUSD 120.00"
    assert "Price limit" in page.locator("#algo-parameters").inner_text()
    page.locator("[data-language=zh]").click()
    assert page.locator("#dashboard-title").inner_text() == "算法交易会话"
    assert "价格限制" in page.locator("#algo-parameters").inner_text()
    assert "买入均价" in page.locator("#algo-accounts").inner_text()
    assert "卖出均价" in page.locator("#algo-accounts").inner_text()

    usd = account["cash_by_currency"]["USD"]
    usd.update(buy_cost=None, average_buy_price=None, cash_change=None, complete=False)
    page.evaluate("refresh()")
    buy_cell = page.locator("#algo-accounts tbody tr").first.locator("td").nth(2)
    sell_cell = page.locator("#algo-accounts tbody tr").first.locator("td").nth(4)
    assert "USD —" in buy_cell.inner_text() and "成交价格数据不完整" in buy_cell.inner_text()
    assert sell_cell.inner_text() == "HKD 200.13\nUSD 120.00"
    usd.update(buy_cost=450, average_buy_price=100, cash_change=-210, complete=True)
    page.evaluate("refresh()")
    assert buy_cell.inner_text() == "HKD —\nUSD 100.00"

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


def test_session_table_search_pagination_selection_and_live_updates(report_browser):
    browser, server = report_browser
    now = datetime(2026, 10, 28, 15, tzinfo=timezone.utc)
    sessions = [{
        "id": f"session-{i:02}", "symbol": "MSFT" if i == 24 else "AAPL",
        "parameters": {"trading_mode": "H" if i == 24 else "G"},
        "account_ids": ["account-B" if i == 24 else "account-A"],
        "status": "running" if i == 0 else "completed", "running": i == 0,
        "started_at": (now - timedelta(minutes=i)).isoformat(),
        "updated_at": now.isoformat(), "ended_at": None if i == 0 else now.isoformat(),
        "accounts": [], "totals": {"shares_bought": 0, "shares_sold": 0, "cash_by_currency": {}},
    } for i in range(25)]
    sessions[23]["id"] = '<img src=x onerror=alert(1)>'
    data = {
        "username": "tester", "date": "2026-10-28", "timezone": "America/New_York",
        "refresh_interval_seconds": 60, "accounts": [], "statuses": [], "orders": [],
        "feed": {"state": "live", "detail": "Live", "invalid_messages": 0},
        "reporting": {"sessions": sessions, "tags": [], "active_tag_id": None},
    }
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors, posts = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def api(route):
        if route.request.method != "GET": posts.append(route.request.url)
        route.fulfill(json=data)

    page.route("**/api/**", api)
    page.goto(f"http://127.0.0.1:{server.server_port}/static/index.html")
    page.locator("#dashboard").wait_for(state="visible")
    page.locator("#tab-algo").click()
    assert page.locator("#algo-select").count() == 0
    assert page.locator("#algo-rows tr").count() == 20
    assert page.locator("#algo-previous").is_disabled()
    assert "session-00" in page.locator("#algo-summary").inner_text()
    page.locator("#algo-next").click()
    assert page.locator("#algo-page").inner_text() == "Page 2 of 2"
    assert page.locator("#algo-rows tr").count() == 5
    assert page.locator("#algo-next").is_disabled()
    assert page.locator("#algo-rows img").count() == 0
    assert '<img src=x onerror=alert(1)>' in page.locator("#algo-rows").inner_text()
    selection = page.locator('[data-algo-session="session-24"]')
    selection.focus()
    page.keyboard.press("Enter")
    assert selection.get_attribute("aria-pressed") == "true"
    assert "session-24" in page.locator("#algo-summary").inner_text()
    assert "MSFT" in page.locator("#algo-summary").inner_text()
    assert page.locator("#algo-accounts .report-empty").get_attribute("colspan") == "8"
    page.evaluate("refresh()")
    assert page.evaluate("document.activeElement.dataset.algoSession") == "session-24"
    assert page.locator("#algo-page").inner_text() == "Page 2 of 2"

    page.locator("[data-language=zh]").click()
    assert page.locator("#algo-page").inner_text() == "第 2 页，共 2 页"
    assert selection.get_attribute("aria-pressed") == "true"
    assert "智能买入" in page.locator("#algo-rows").inner_text()
    sessions.insert(0, {**sessions[0], "id": "new-session", "started_at": (now + timedelta(minutes=1)).isoformat()})
    page.evaluate("refresh()")
    assert selection.get_attribute("aria-pressed") == "true"
    assert "session-24" in page.locator("#algo-summary").inner_text()
    page.locator("[data-language=en]").click()
    for query in ("msft", "session-24", "account-b"):
        page.locator("#algo-search").fill(query)
        assert page.locator("#algo-rows tr").count() == 1
        assert page.locator("#algo-page").inner_text() == "Page 1 of 1"
        assert "session-24" in page.locator("#algo-summary").inner_text()
    page.locator("#algo-search").fill("2026-10-28")
    assert page.locator("#algo-rows tr").count() == 20
    page.locator("#algo-status").select_option("running")
    assert page.locator("#algo-rows tr").count() == 2
    page.locator("#algo-search").fill("no-match")
    assert page.locator("#algo-rows tr").count() == 0
    assert page.locator("#algo-empty").inner_text() == "No sessions match these filters."
    assert page.locator("#algo-report").is_hidden()
    page.locator("#algo-search").fill("")
    page.locator("#algo-status").select_option("")
    page.locator("#algo-page-size").select_option("50")
    assert page.locator("#algo-rows tr").count() == 26
    page.locator("#algo-page-size").select_option("10")
    assert page.locator("#algo-rows tr").count() == 10
    page.locator("#algo-next").click()
    page.locator("#algo-next").click()
    assert page.locator("#algo-rows tr").count() == 6
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator("#algo-search").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".session-list .table-scroll").evaluate("node => node.scrollWidth > node.clientWidth")
    data["reporting"]["sessions"] = sessions[:2]
    page.evaluate("refresh()")
    assert page.locator("#algo-page").inner_text() == "Page 1 of 1"
    assert page.locator("#algo-rows tr").count() == 2
    assert "new-session" in page.locator("#algo-summary").inner_text()
    data["reporting"]["sessions"] = []
    page.evaluate("refresh()")
    assert page.locator("#algo-report").is_hidden()
    assert page.locator("#algo-empty").inner_text() == "No algorithm sessions recorded yet."
    assert page.locator("#algo-previous").is_disabled() and page.locator("#algo-next").is_disabled()
    assert not errors and not posts
    page.close()


def test_unreconciled_filters_pagination_translation_and_live_resolution(report_browser):
    browser, server = report_browser
    base = {
        "kind": "broker_order", "account_id": "A", "account_num_id": 1, "broker": "GigaMoney",
        "symbol": "AEHL", "side": "SELL", "status": "PENDING_NEW", "reported_status": "PENDING_NEW",
        "quantity": 20000, "filled_quantity": 0, "limit_price": None, "modes": ["G", "H"],
        "updated_at": "2026-10-26T15:00:00Z", "age_seconds": 172800,
        "reason": "awaiting_acceptance", "message": "<img src=x onerror=alert(1)>",
    }
    items = [{**base, "order_id": f"old-order-{i}"} for i in range(25)]
    items.append({**base, "kind": "pending_command", "account_id": "B", "account_num_id": 2,
                  "symbol": "POM", "order_id": None, "command_id": "missing-command",
                  "quantity": None, "filled_quantity": None, "status": "AWAITING_REPORT",
                  "reported_status": None, "reason": "missing_feedback"})
    data = {
        "username": "tester", "date": "2026-10-28", "timezone": "UTC", "refresh_interval_seconds": 60,
        "accounts": [{"id": "A", "num_id": 1, "broker": "GigaMoney"}, {"id": "B", "num_id": 2, "broker": "GigaMoney"}],
        "statuses": [], "orders": [], "feed": {"state": "live", "detail": "Live", "invalid_messages": 0},
        "unreconciled": {"items": items, "complete": True, "details_available": True, "generated_at": "2026-10-28T15:00:00Z"},
    }
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors, posts = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def api(route):
        if route.request.method != "GET": posts.append(route.request.url)
        route.fulfill(json=data)

    page.route("**/api/**", api)
    page.goto(f"http://127.0.0.1:{server.server_port}/static/index.html")
    page.locator("#dashboard").wait_for(state="visible")
    page.locator("#tab-orders").focus()
    page.keyboard.press("End")
    assert page.locator("#panel-unreconciled").is_visible()
    assert page.locator("#unreconciled-badge").inner_text() == "26"
    assert page.locator("#unreconciled-rows tr").count() == 20
    assert page.locator("#unreconciled-rows img").count() == 0
    assert "<img src=x" in page.locator("#unreconciled-rows").inner_text()
    assert "10/26/2026" in page.locator("#unreconciled-rows").inner_text()
    page.locator("#unreconciled-next").click()
    assert page.locator("#unreconciled-rows tr").count() == 6
    page.evaluate("refresh()")
    assert page.locator("#unreconciled-page").inner_text() == "Page 2 of 2"
    page.locator("#unreconciled-account").select_option("B")
    assert page.locator("#unreconciled-rows tr").count() == 1
    assert "missing-command" in page.locator("#unreconciled-rows").inner_text()
    assert "— / —" in page.locator("#unreconciled-rows").inner_text()
    page.locator("#unreconciled-symbol").select_option("POM")
    page.locator("[data-language=zh]").click()
    assert page.locator("#dashboard-title").inner_text() == "未对账订单"
    assert page.locator("#unreconciled-account").input_value() == "B"
    assert "未确认指令" in page.locator("#unreconciled-rows").inner_text()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator("#unreconciled-search").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # Resolution removes the record without resetting the selected filters.
    data["unreconciled"]["items"] = items[:25]
    page.evaluate("refresh()")
    assert page.locator("#unreconciled-rows tr").count() == 0
    assert page.locator("#unreconciled-account").input_value() == "B"
    assert page.locator("#unreconciled-symbol").input_value() == "POM"
    page.locator("[data-language=en]").click()
    page.locator("#unreconciled-account").select_option("")
    page.locator("#unreconciled-symbol").select_option("")
    page.locator("#unreconciled-search").fill("old-order-24")
    assert page.locator("#unreconciled-rows tr").count() == 1
    data["unreconciled"].update(items=[], complete=False)
    page.evaluate("refresh()")
    assert page.locator("#unreconciled-notice").is_visible()
    assert "incomplete" in page.locator("#unreconciled-current").inner_text()
    assert "No unresolved records" not in page.locator("#unreconciled-empty").inner_text()
    assert not posts and not errors
    page.close()
