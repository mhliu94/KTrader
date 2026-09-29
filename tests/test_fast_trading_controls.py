import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html import unescape
from pathlib import Path

from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page, render_layout


CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@unittest.skipUnless(CHROME, "Chrome/Chromium is required for UI behavior tests")
class FastTradingControlsBrowserTests(unittest.TestCase):
    def test_required_choices_and_submitted_configuration(self):
        for lang in ("en", "zh"):
            for mode in ("E", "F"):
                with self.subTest(lang=lang, mode=mode):
                    self.check_configuration(lang, mode)

    def check_configuration(self, lang, mode):
        metas = {
            account_id: AccountMeta(id=account_id, num_id=index, broker="Tiger")
            for index, account_id in enumerate(("A", "B", "C"), start=1)
        }
        prelude = """
        <script>
          window.uiErrors = [];
          window.submissions = [];
          window.addEventListener("error", event => uiErrors.push(event.message));
          window.addEventListener("unhandledrejection", event => uiErrors.push(String(event.reason)));
          window.fetch = async url => {
            if (url === "/api/accounts") return {ok: true, json: async () => ({accounts: []})};
            if (url === "/api/market-data") return {ok: true, json: async () => ({rows: {}})};
            throw new Error("Unexpected fetch: " + url);
          };
          window.setInterval = () => 0;
          HTMLFormElement.prototype.submit = function() {
            submissions.push(Object.fromEntries(new FormData(this)));
          };
          localStorage.clear();
        </script>
        """
        checks = """
        <pre id="test-result">pending</pre>
        <script>
        (async function() {
          const failures = [];
          const mode = __MODE__;
          function equal(actual, expected, context) {
            if (JSON.stringify(actual) !== JSON.stringify(expected)) {
              failures.push(context + ": " + JSON.stringify(actual) + " != " + JSON.stringify(expected));
            }
          }
          const form = document.getElementById("algo-form");
          const modal = document.getElementById("fast-trading-modal");
          const error = document.getElementById("fast-trading-error");
          const submit = document.getElementById("fast-submit-config");
          const groups = {
            fast_book_levels: [1, 2, 3, 4, 5],
            fast_target_percentage: [10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            fast_cycle_seconds: [5, 10, 15, 30, 60]
          };
          const chosen = mode === "E"
            ? {fast_book_levels: 5, fast_target_percentage: 100, fast_cycle_seconds: 60}
            : {fast_book_levels: 1, fast_target_percentage: 10, fast_cycle_seconds: 5};
          const inputs = name => Array.from(modal.querySelectorAll("input[name='" + name + "']"));
          function choose(name, value) {
            const input = inputs(name).find(input => Number(input.value) === value);
            const label = modal.querySelector("label[for='" + input.id + "']");
            equal(label.classList.contains("choice-btn"), true, name + " is a button choice");
            label.click();
          }
          function selectMode(value) {
            document.querySelector("label[for='algo-mode-" + value.toLowerCase() + "']").click();
          }
          // The application listener runs first. Intercept only submissions it permits,
          // including native requestSubmit(), so the browser never sends an order.
          form.addEventListener("submit", event => {
            if (!event.defaultPrevented) {
              event.preventDefault();
              form.submit();
            }
          });
          try {
            selectMode(mode);
            document.getElementById("algo_symbol").value = "AAPL";
            form.requestSubmit();
            equal(modal.hidden, false, "fast configuration opened");
            equal(document.getElementById("algo-generic-constraints").hidden, true, "generic constraints hidden");
            equal(submissions.length, 0, "opening configuration does not submit");
            equal(modal.querySelectorAll("input[name='fast_aggression_level']").length, 0, "aggression selector removed");

            for (const [name, options] of Object.entries(groups)) {
              equal(inputs(name).map(input => Number(input.value)), options, name + " available options");
              equal(inputs(name).every(input => input.type === "radio"), true, name + " single selection controls");
              equal(inputs(name).filter(input => input.checked).length, 0, name + " requires an explicit choice");
              for (const value of options) {
                choose(name, value);
                equal(inputs(name).filter(input => input.checked).map(input => Number(input.value)), [value],
                      name + " selecting " + value + " clears the previous choice");
              }
              choose(name, chosen[name]);
            }

            const price = mode === "E" ? 102.5 : 98.25;
            document.getElementById("fast-price-limit").value = String(price);
            modal.querySelector("[data-fast-account-id='A']").click();
            modal.querySelector("[data-fast-account-id='C']").click();
            document.getElementById("fast-test-mode").checked = mode === "E";

            for (const name of Object.keys(groups)) {
              inputs(name).forEach(input => { input.checked = false; });
              submit.click();
              equal(submissions.length, 0, "missing " + name + " prevents submission");
              equal(modal.hidden, false, "missing " + name + " keeps the dialog open");
              equal(error.textContent.trim().length > 0, true, "missing " + name + " shows an error");
              choose(name, chosen[name]);
            }

            submit.click();
            equal(submissions.length, 1, "complete configuration submits once");
            equal(modal.hidden, true, "complete configuration closes the dialog");
            const submitted = submissions[0];
            equal(submitted.trading_mode, mode, "submitted trading mode");
            equal(submitted.symbol, "AAPL", "submitted symbol");
            const config = JSON.parse(submitted.fast_trading_config);
            equal(Object.keys(config).sort(),
                  ["account_ids", "book_levels", "cycle_seconds", "price_limit", "target_percentage", "test_mode"],
                  "configuration contains the new fields and no aggression preset");
            equal(config.book_levels, chosen.fast_book_levels, "submitted levels");
            equal(config.target_percentage, chosen.fast_target_percentage, "submitted target percentage");
            equal(config.cycle_seconds, chosen.fast_cycle_seconds, "submitted minimum cycle time");
            equal(config.price_limit, price, "existing price limit preserved");
            equal(config.account_ids, ["A", "C"], "only selected accounts submitted");
            equal(config.test_mode, mode === "E", "existing test mode preserved");

            // Required fast controls must not prevent ordinary algo mode submissions.
            Object.keys(groups).forEach(name => inputs(name).forEach(input => { input.checked = false; }));
            document.getElementById("max_volume").value = "27";
            document.getElementById("price_target").value = "101.25";
            for (const genericMode of ["A", "B", "C", "D"]) {
              selectMode(genericMode);
              equal(document.getElementById("algo-generic-constraints").hidden, false,
                    genericMode + " generic constraints remain available");
              const previousCount = submissions.length;
              form.requestSubmit();
              equal(submissions.length, previousCount + 1, genericMode + " submits without fast choices");
              equal(modal.hidden, true, genericMode + " does not open fast configuration");
              const ordinary = submissions[submissions.length - 1];
              equal(ordinary.trading_mode, genericMode, genericMode + " mode preserved");
              equal(ordinary.fast_trading_config, "", genericMode + " clears fast configuration");
              equal(ordinary.max_volume, "27", genericMode + " volume preserved");
              equal(ordinary.price_target, "101.25", genericMode + " price target preserved");
            }
          } catch (error) {
            failures.push(error.stack || String(error));
          }
          await Promise.resolve();
          document.getElementById("test-result").textContent = JSON.stringify(failures.concat(uiErrors));
        })();
        </script>
        """.replace("__MODE__", json.dumps(mode))
        page = render_layout(lang, "control-panel", render_control_panel_page(lang, metas, ["AAPL"]))
        page = page.replace("<head>", "<head>" + prelude).replace("</body>", checks + "</body>")
        with tempfile.TemporaryDirectory(prefix="ktrader-fast-controls-test-") as directory:
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
