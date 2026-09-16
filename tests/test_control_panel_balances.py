import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html import unescape
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page, render_layout


CHROME = shutil.which("google-chrome") or shutil.which("chromium")


class DelayedOrderRemovalTests(unittest.TestCase):
    def test_removed_routes_cannot_publish_orders(self):
        producer = Mock()
        with patch.object(webserver, "COMMANDS_PRODUCER", producer), patch.object(
            webserver, "SESSIONS", {"test-token": "trader"}
        ):
            client = TestClient(webserver.app)
            client.cookies.set("auth_token", "test-token")
            try:
                payload = {"account_id": "A", "symbol": "AAPL", "side": "BUY", "shares": 1, "delay_choice": "1"}
                self.assertEqual(client.post("/submit-delayed-order", data=payload).status_code, 404)
                self.assertEqual(client.post("/api/submit-delayed-order", json=payload).status_code, 404)
                producer.publish_order.assert_not_called()
            finally:
                client.close()

    def test_control_panel_has_no_delayed_order_controls(self):
        for lang in ("en", "zh"):
            html = render_control_panel_page(lang, {}, ["AAPL"])
            self.assertNotIn("delayed", html.lower())
            self.assertNotIn('name="execute_at"', html)


@unittest.skipUnless(CHROME, "Chrome/Chromium is required for UI behavior tests")
class ControlPanelBalanceBrowserTests(unittest.TestCase):
    def test_order_and_algo_balances_in_both_languages(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                self.check_balances(lang)

    def check_balances(self, lang):
        metas = {
            account_id: AccountMeta(id=account_id, num_id=index, broker="Tiger")
            for index, account_id in enumerate(("A", "B", "C", "MISSING"), start=1)
        }
        prelude = """
        <script>
          window.uiErrors = [];
          window.addEventListener("error", event => uiErrors.push(event.message));
          window.addEventListener("unhandledrejection", event => uiErrors.push(String(event.reason)));
          window.accountData = { accounts: [
            {account_id: "A", account_num_id: 1, cash: 9999,
             cash_by_currency: {" usd ": 1250.5, HKD: 900},
             available_cash_by_currency: {USD: 10},
             positions: [{symbol: " aapl ", qty: 2, available_qty: 1},
                         {symbol: "AAPL", qty: 3.5}, {symbol: "MSFT", qty: 9}]},
            {account_id: "B", account_num_id: 2, cash: 300,
             cash_by_currency: {USD: 0}, positions: [{symbol: "MSFT", qty: 7}]},
            {account_id: "C", account_num_id: 3, cash: -25, positions: []}
          ]};
          window.quoteData = {rows: {AAPL: {last: 100}, MSFT: {last: 250}}};
          window.failAccounts = false;
          window.fetch = async url => {
            if (url === "/api/accounts") {
              if (failAccounts) throw new Error("offline");
              return {ok: true, json: async () => structuredClone(accountData)};
            }
            if (url === "/api/market-data") return {ok: true, json: async () => structuredClone(quoteData)};
            throw new Error("Unexpected fetch: " + url);
          };
          window.refreshCallbacks = [];
          window.setInterval = callback => refreshCallbacks.push(callback);
          localStorage.clear();
          localStorage.setItem("trading_ui.limit_order.symbol", "MSFT");
          localStorage.setItem("trading_ui.limit_order.account_ids", JSON.stringify(["A", "B"]));
          localStorage.setItem("trading_ui.limit_order.side", "SELL");
        </script>
        """
        checks = """
        <pre id="test-result">pending</pre>
        <script>
        (async function() {
          const failures = [];
          function equal(actual, expected, context) {
            if (actual !== expected) failures.push(context + ": " + JSON.stringify(actual) + " != " + JSON.stringify(expected));
          }
          const text = id => document.getElementById(id).textContent;
          const change = element => element.dispatchEvent(new Event("change", {bubbles: true}));
          function side(prefix, value) {
            const input = document.getElementById(prefix + "-side-" + value.toLowerCase());
            input.checked = true;
            change(input);
          }
          function accounts(prefix, values) {
            document.querySelectorAll("#" + prefix + "-acct-1").forEach(first => {
              first.form.querySelectorAll("input[name='account_ids']").forEach(input => {
                input.checked = values.includes(input.value);
              });
              change(first);
            });
          }
          function symbol(prefix, value) {
            if (prefix === "limit") {
              const select = document.getElementById("limit_symbol");
              select.value = value;
              change(select);
            } else {
              const input = document.querySelector("form[action='/submit-quick-order'] input[name='symbol'][value='" + value + "']");
              input.checked = true;
              change(input);
            }
          }
          async function refresh() {
            await Promise.all(refreshCallbacks.map(callback => callback()));
          }
          try {
            await refresh();
            equal(text("limit-selected-balance"), "#1: 9, #2: 7", "restored SELL/accounts/ticker");
            equal(text("quick-selected-balance"), "#1: $1,250.50", "default BUY USD balance");
            for (const prefix of ["quick", "limit"]) {
              accounts(prefix, ["A", "B", "C", "MISSING"]);
              side(prefix, "BUY");
              equal(text(prefix + "-selected-balance"), "#1: $1,250.50, #2: $0.00, #3: $-25.00, MISSING: —", prefix + " all BUY balances");
              symbol(prefix, "AAPL");
              side(prefix, "SELL");
              equal(text(prefix + "-selected-balance"), "#1: 5.5, #2: 0, #3: 0, MISSING: —", prefix + " owned shares, duplicates and zero positions");
              equal(text(prefix + "-selected-balance-label").includes("(AAPL)"), true, prefix + " holding ticker label");
              symbol(prefix, "MSFT");
              equal(text(prefix + "-selected-balance"), "#1: 9, #2: 7, #3: 0, MISSING: —", prefix + " ticker change");
              accounts(prefix, []);
              equal(text(prefix + "-selected-balance"), "—", prefix + " no selection");
              accounts(prefix, ["B"]);
              equal(text(prefix + "-selected-balance"), "#2: 7", prefix + " one account");
              side(prefix, "BUY");
            }
            const algoForm = document.getElementById("algo-form");
            const algoSymbol = document.getElementById("algo_symbol");
            const modal = document.getElementById("fast-trading-modal");
            const balance = account => modal.querySelector("[data-fast-account-balance='" + account + "']").textContent;
            function open(mode, ticker) {
              document.getElementById("algo-mode-" + mode.toLowerCase()).checked = true;
              algoSymbol.value = ticker;
              algoForm.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
            }
            open("E", "AAPL");
            await refresh();
            equal(modal.hidden, false, "E dialog opened");
            equal(text("fast-trading-symbol"), "AAPL", "E ticker");
            equal(balance("A").endsWith(": $1,250.50"), true, "E total USD rather than available cash");
            equal(balance("B").endsWith(": $0.00"), true, "E zero cash");
            equal(balance("C").endsWith(": $-25.00"), true, "E legacy cash");
            equal(balance("MISSING").endsWith(": —"), true, "E missing snapshot");
            accountData.accounts[1].cash_by_currency.USD = 400;
            await refresh();
            equal(balance("B").endsWith(": $400.00"), true, "E live cash");
            equal(text("quick-selected-balance"), "#2: $400.00", "quick live cash");
            equal(text("limit-selected-balance"), "#2: $400.00", "limit live cash");
            document.getElementById("fast-cancel").click();
            open("F", "MSFT");
            await refresh();
            equal(text("fast-trading-symbol"), "MSFT", "F ticker on reopen");
            equal(balance("A").endsWith("(MSFT): 9"), true, "F holdings");
            equal(balance("B").endsWith("(MSFT): 7"), true, "F second account holdings");
            equal(balance("C").endsWith("(MSFT): 0"), true, "F no position");
            equal(balance("MISSING").endsWith("(MSFT): —"), true, "F missing account");
            algoSymbol.value = "AAPL";
            change(algoSymbol);
            equal(text("fast-trading-symbol"), "AAPL", "F ticker change");
            equal(balance("A").endsWith("(AAPL): 5.5"), true, "F ticker holdings change");
            accountData.accounts[0].positions[0].qty = 12;
            await refresh();
            equal(balance("A").endsWith("(AAPL): 15.5"), true, "F live holdings");
            for (const prefix of ["quick", "limit"]) {
              symbol(prefix, "AAPL");
              accounts(prefix, ["A"]);
              side(prefix, "SELL");
            }
            equal(text("quick-selected-balance"), "#1: 15.5", "quick live holdings");
            equal(text("limit-selected-balance"), "#1: 15.5", "limit live holdings");
            accountData.accounts[0].positions[0].qty = null;
            accountData.accounts[0].cash_by_currency[" usd "] = null;
            await refresh();
            equal(balance("A").endsWith("(AAPL): —"), true, "invalid holding unavailable");
            equal(text("quick-selected-balance"), "#1: —", "invalid quick holding");
            side("limit", "BUY");
            equal(text("limit-selected-balance"), "#1: —", "invalid USD does not fall back");
            failAccounts = true;
            await refresh();
            equal(text("quick-selected-balance"), "#1: —", "failed refresh preserves display");
            equal(document.querySelectorAll("form[action='/submit-delayed-order']").length, 0, "delayed form removed");
          } catch (error) {
            failures.push(error.stack || String(error));
          }
          document.getElementById("test-result").textContent = JSON.stringify(failures.concat(uiErrors));
        })();
        </script>
        """
        page = render_layout(lang, "control-panel", render_control_panel_page(lang, metas, ["AAPL", "MSFT"]))
        page = page.replace("<head>", "<head>" + prelude).replace("</body>", checks + "</body>")
        with tempfile.TemporaryDirectory(prefix="ktrader-ui-test-") as directory:
            path = Path(directory) / "control-panel.html"
            path.write_text(page, encoding="utf-8")
            result = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                 "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
                 "--lang=en-US", "--user-data-dir=" + str(Path(directory) / "profile"),
                 "--virtual-time-budget=1000", "--dump-dom", path.as_uri()],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, result.stderr)
        self.assertNotEqual(match.group(1), "pending", "Browser checks did not finish")
        self.assertEqual(json.loads(unescape(match.group(1))), [])


if __name__ == "__main__":
    unittest.main()
