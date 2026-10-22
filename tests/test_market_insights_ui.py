import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html import unescape
from pathlib import Path

from trading_ui.templates import render_market_insights_page


CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@unittest.skipUnless(CHROME, "Chrome/Chromium is required for UI behavior tests")
class MarketInsightsBrowserTests(unittest.TestCase):
    def test_new_symbols_appear_without_refresh_and_preserve_selection(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                self.check_live_symbols(lang)

    def check_live_symbols(self, lang):
        prelude = """
        <script>
          window.uiErrors = [];
          window.addEventListener("error", event => uiErrors.push(event.message));
          window.addEventListener("unhandledrejection", event => uiErrors.push(String(event.reason)));
          window.publishedSymbols = ["AAPL", "NVDA"];
          window.requests = [];
          window.setInterval = callback => { window.pollMarketInsights = callback; return 0; };
          window.fetch = async url => {
            const symbol = new URL(url, "http://localhost").searchParams.get("symbol");
            requests.push(symbol);
            return {ok: true, json: async () => ({
              symbol, symbols: publishedSymbols.slice().sort(), asof_epoch: 1700000000,
              bids: [{price: {AAPL: 100, NVDA: 200, MSFT: 300}[symbol], quantity: 1250, order_count: 2}],
              asks: []
            })};
          };
        </script>
        """
        checks = """
        <pre id="test-result">pending</pre>
        <script>
        (async function() {
          const failures = [];
          function equal(actual, expected, context) {
            if (JSON.stringify(actual) !== JSON.stringify(expected)) {
              failures.push(context + ": " + JSON.stringify(actual) + " != " + JSON.stringify(expected));
            }
          }
          try {
            await new Promise(resolve => setTimeout(resolve, 0));
            const select = document.getElementById("mi-symbol");
            const bids = document.getElementById("mi-bids-body");
            const symbols = () => Array.from(select.options, option => option.value);
            equal(symbols(), ["AAPL", "NVDA"], "published symbols appear on the first poll");

            select.value = "NVDA";
            select.dispatchEvent(new Event("change"));
            await new Promise(resolve => setTimeout(resolve, 0));
            equal(requests[requests.length - 1], "NVDA", "selecting a discovered symbol fetches its book");
            const previousRow = bids.firstElementChild;

            publishedSymbols.push("MSFT");
            await pollMarketInsights();
            equal(symbols(), ["AAPL", "MSFT", "NVDA"], "later symbols appear in sorted order");
            equal(select.value, "NVDA", "new options preserve the selected symbol");
            equal(bids.firstElementChild === previousRow, true, "symbol discovery works with an unchanged book");
            equal(bids.rows[0].cells[0].textContent, (200).toLocaleString(undefined,
              {minimumFractionDigits: 4, maximumFractionDigits: 4}), "selected book remains visible");

            select.value = "MSFT";
            select.dispatchEvent(new Event("change"));
            await new Promise(resolve => setTimeout(resolve, 0));
            equal(requests[requests.length - 1], "MSFT", "the newly published symbol is selectable");
            equal(bids.rows[0].cells[0].textContent, (300).toLocaleString(undefined,
              {minimumFractionDigits: 4, maximumFractionDigits: 4}), "the new symbol's book is displayed");
          } catch (error) {
            failures.push(error.stack || String(error));
          }
          document.getElementById("test-result").textContent = JSON.stringify(failures.concat(uiErrors));
        })();
        </script>
        """
        page = "<!doctype html><meta charset='utf-8'>" + prelude
        page += render_market_insights_page(lang, ["AAPL"], 20) + checks
        with tempfile.TemporaryDirectory(prefix="ktrader-market-insights-test-") as directory:
            path = Path(directory) / "market-insights.html"
            path.write_text(page, encoding="utf-8")
            result = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                 "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
                 "--user-data-dir=" + str(Path(directory) / "profile"),
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
