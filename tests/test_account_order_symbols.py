import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html import unescape
from pathlib import Path

from trading_ui.templates import render_layout, render_live_account_details_page


CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@unittest.skipUnless(CHROME, "Chrome/Chromium is required for UI behavior tests")
class AccountOrderSymbolBrowserTests(unittest.TestCase):
    def test_symbol_buttons_and_custom_input_in_both_dialogs(self):
        for lang in ("en", "zh"):
            for symbols in (["MSFT", " aapl ", "MSFT"], []):
                with self.subTest(lang=lang, symbols=symbols):
                    self.check_symbols(lang, symbols)

    def check_symbols(self, lang, symbols):
        prelude = """
        <script>
          window.uiErrors = [];
          window.addEventListener("error", event => uiErrors.push(event.message));
          window.addEventListener("unhandledrejection", event => uiErrors.push(String(event.reason)));
          window.EventSource = undefined;
          window.setInterval = () => 0;
          window.orders = [];
          window.quoteSymbols = [];
          window.fetch = async (url, options = {}) => {
            const path = new URL(url, "http://localhost");
            if (options.method === "POST") {
              orders.push({path: path.pathname, data: options.body});
              return {ok: true, json: async () => ({ok: true, message: "Submitted"})};
            }
            if (path.pathname === "/api/accounts") {
              return {ok: true, json: async () => ({accounts: [{account_id: "A", cash: 1000}]})};
            }
            if (path.pathname === "/api/market-data") {
              const symbol = path.searchParams.get("symbol");
              quoteSymbols.push(symbol);
              return {ok: true, json: async () => ({rows: {
                [symbol]: {last: {AAPL: 100, MSFT: 250, NVDA: 150}[symbol]}
              }})};
            }
            throw new Error("Unexpected fetch: " + url);
          };
        </script>
        """
        triggers = """
        <button data-account-order="market" data-account-id="A">Market</button>
        <button data-account-order="limit" data-account-id="A">Limit</button>
        """
        checks = """
        <pre id="test-result">pending</pre>
        <script>
        (async function() {
          const failures = [];
          function equal(actual, expected, label) {
            if (actual !== expected) throw new Error(label + ": " + actual + " !== " + expected);
          }
          const settle = () => new Promise(resolve => setTimeout(resolve, 0));
          try {
            for (const [kind, prefix] of [["market", "quick"], ["limit", "limit"]]) {
              const trigger = document.querySelector("[data-account-order='" + kind + "']");
              trigger.click();
              await settle();
              const modal = document.getElementById("account-" + prefix + "-order-modal");
              const form = modal.querySelector("form");
              const input = form.querySelector("[name='symbol']");
              const buttons = Array.from(form.querySelectorAll("[data-account-order-symbol]"));
              const selected = () => buttons.filter(button => button.getAttribute("aria-pressed") === "true");
              form.querySelector("[name='shares']").value = "10";
              if (kind === "limit") form.querySelector("[name='limit_price']").value = "123.45";
              async function submit(symbol) {
                const count = orders.length;
                equal(form.checkValidity(), true, kind + " valid form");
                form.requestSubmit();
                await settle();
                equal(orders.length, count + 1, kind + " publishes once");
                const order = orders[orders.length - 1];
                equal(order.path, "/api/account-orders/" + (kind === "market" ? "quick-market" : "limit"), "endpoint");
                equal(order.data.getAll("symbol").length, 1, "single submitted symbol");
                equal(order.data.get("symbol"), symbol, kind + " submitted symbol");
                equal(order.data.get("account_id"), "A", "account preserved");
              }
              async function quote(symbol, price) {
                await settle();
                equal(quoteSymbols[quoteSymbols.length - 1], symbol, kind + " quote request");
                equal(Number(modal.querySelector("[data-account-order-last]").textContent), price, kind + " quote display");
              }
              function type(value) {
                input.value = value;
                input.dispatchEvent(new Event("input", {bubbles: true}));
                input.dispatchEvent(new Event("change", {bubbles: true}));
              }
              equal(input.value, "", "empty custom input on open");
              if (buttons.length) {
                equal(buttons.length, 2, "normalized and deduplicated buttons");
                equal(selected().length, 1, "one initial selection");
                equal(selected()[0].dataset.accountOrderSymbol, "AAPL", "default symbol preserved");
                await submit("AAPL");
                type(" nvda ");
                await quote("AAPL", 100);
                await submit("AAPL");
                buttons[1].click();
                equal(selected().length, 1, "switching keeps single selection");
                equal(buttons[0].classList.contains("active"), false, "previous button unstyled");
                equal(buttons[1].classList.contains("active"), true, "new button styled");
                equal(input.value, " nvda ", "custom input preserved");
                await quote("MSFT", 250);
                await submit("MSFT");
                buttons[1].click();
                equal(selected().length, 0, "second click deselects");
                equal(buttons[1].classList.contains("active"), false, "deselected button unstyled");
              } else {
                equal(form.checkValidity(), false, "symbol needed with no configured buttons");
                type(" nvda ");
              }
              await quote("NVDA", 150);
              await submit("NVDA");
              type("");
              equal(form.checkValidity(), false, "symbol required when no button selected");
              const count = orders.length;
              form.requestSubmit();
              await settle();
              equal(orders.length, count, "empty symbol blocks submission");
              if (buttons.length) {
                buttons[1].click();
                await submit("MSFT");
              }
              modal.querySelector("[data-account-order-close]").click();
              trigger.click();
              await settle();
              equal(input.value, "", "custom input reset on reopen");
              equal(selected().length, buttons.length ? 1 : 0, "selection reset on reopen");
              equal(input.required, !buttons.length, "validation reset on reopen");
              if (buttons.length) equal(selected()[0].dataset.accountOrderSymbol, "AAPL", "default restored");
              modal.querySelector("[data-account-order-close]").click();
            }
          } catch (error) {
            failures.push(error.stack || String(error));
          }
          document.getElementById("test-result").textContent = JSON.stringify(failures.concat(uiErrors));
        })();
        </script>
        """
        content = render_live_account_details_page(triggers, lang=lang, symbols=symbols)
        page = render_layout(lang, "account-details", content)
        page = page.replace("<head>", "<head>" + prelude).replace("</body>", checks + "</body>")
        with tempfile.TemporaryDirectory(prefix="ktrader-order-symbol-test-") as directory:
            path = Path(directory) / "account-orders.html"
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
