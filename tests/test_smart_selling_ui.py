import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html import unescape
from pathlib import Path

from trading_ui.i18n import I18N
from trading_ui.models import AccountMeta
from trading_ui.templates import render_control_panel_page, render_layout

CHROME = shutil.which("google-chrome") or shutil.which("chromium")


def account_metas():
    return {
        "A": AccountMeta(id="A", num_id=1, broker="Tiger", trading_medium="API"),
        "B": AccountMeta(id="B", num_id=2, broker="Other", trading_medium="API"),
        "C": AccountMeta(id="C", num_id=3, broker="Tiger", trading_medium="WINDOWS"),
    }


class SmartSellingTemplateTests(unittest.TestCase):
    def test_smart_settings_are_in_a_modal_with_all_accounts_and_no_quantity_input(self):
        page = render_control_panel_page("en", account_metas(), ["AAPL"])
        self.assertIn('id="algo-mode-g"', page)
        self.assertIn("G Smart selling", page)
        self.assertIn('id="smart-selling-modal" hidden', page)
        self.assertIn('id="smart-selling-settings" disabled', page)
        form = re.search(r'<form[^>]*id="algo-form".*?</form>', page, re.S).group()
        self.assertNotIn('id="smart-selling-settings"', form)
        for account in ("A", "B", "C"):
            self.assertIn(f'type="checkbox" data-smart-account-id="{account}"', page)
            self.assertIn(f'data-smart-account-balance="{account}"', page)
        self.assertNotIn('id="smart-quantity"', page)
        self.assertNotIn("Total shares to sell", page)
        self.assertIn('id="smart-start-now"', page)
        self.assertIn('id="smart-no-deadline"', page)
        self.assertIn('id="smart-set-deadline"', page)
        self.assertNotIn('id="smart-imminent-event"', page)
        self.assertNotIn("Adverse event is imminent", page)
        self.assertNotIn('id="smart-event-help"', page)
        self.assertIn('type="datetime-local"', page)
        self.assertIn("including Smart selling", page)
        urgency = re.search(r'<select id="smart-mode".*?</select>', page, re.S).group()
        self.assertNotIn("%", urgency)
        self.assertNotIn("best ask", urgency)

    def test_all_smart_selling_labels_have_both_translations(self):
        english = {key for key in I18N["en"] if key.startswith("smart_") or key == "mode_g"}
        chinese = {key for key in I18N["zh"] if key.startswith("smart_") or key == "mode_g"}
        self.assertEqual(english, chinese)
        for key in english:
            self.assertTrue(I18N["en"][key])
            self.assertTrue(I18N["zh"][key])
        page = render_control_panel_page("zh", account_metas(), ["AAPL"])
        self.assertIn("G 智能卖出", page)
        self.assertIn("不保证在截止时间前全部成交", page)
        self.assertIn("浏览器本地时区", page)


@unittest.skipUnless(CHROME, "Chrome/Chromium is required for UI behavior tests")
class SmartSellingBrowserTests(unittest.TestCase):
    def test_submission_status_stop_and_validation_in_both_languages(self):
        for language in ("en", "zh"):
            with self.subTest(language=language):
                self.check_browser(language)

    def check_browser(self, language):
        prelude = """
        <script>
          window.uiErrors = [];
          window.requests = [];
          window.intervals = [];
          window.nativeSubmissions = [];
          window.stopFailure = false;
          window.createFailure = false;
          window.pollFailure = false;
          window.blockCreate = false;
          window.releaseCreate = null;
          window.accounts = [{account_id: "A", positions: [{symbol: "AAPL", qty: "100"}, {symbol: "us.aapl", qty: 50}]},
            {account_id: "B", positions: [{symbol: "AAPL", qty: 0}]}];
          window.parents = [{parent_id: "p/a", account_id: "A", symbol: "AAPL", quantity: "1000",
            account_ids: ["A", "B"],
            filled: "10", remaining: "990", reserved: "20", state: "RUNNING", reason: "earning volume credit",
            active_order: {state: "LIVE"}}];
          window.addEventListener("error", event => uiErrors.push(event.message));
          window.addEventListener("unhandledrejection", event => uiErrors.push(String(event.reason)));
          window.fetch = async function(url, options = {}) {
            if (url === "/api/accounts") return {ok: true, json: async () => ({accounts: accounts})};
            if (url === "/api/market-data") return {ok: true, json: async () => ({rows: {}})};
            if (url === "/api/smart-selling" && options.method !== "POST") {
              if (pollFailure) throw new Error("offline");
              return {ok: true, json: async () => ({ok: true, parents: parents})};
            }
            requests.push({url, options});
            if (url.endsWith("/stop")) {
              if (stopFailure) return {ok: false, json: async () => ({ok: false, error: "stop failed"})};
              parents[0].state = "STOPPING";
              parents[0].active_order.state = "CANCEL_PENDING";
              return {ok: true, json: async () => ({ok: true, parent: parents[0]})};
            }
            if (url === "/api/smart-selling" && options.method === "POST") {
              if (blockCreate) await new Promise(resolve => { releaseCreate = resolve; });
              if (createFailure) return {ok: false, json: async () => ({ok: false, error: "existing parent"})};
              return {ok: true, json: async () => ({ok: true, parent: parents[0]})};
            }
            throw new Error("Unexpected request: " + url);
          };
          window.setInterval = callback => { intervals.push(callback); return intervals.length; };
          HTMLFormElement.prototype.submit = function() { nativeSubmissions.push(this.action); };
          localStorage.clear();
        </script>
        """
        checks = """
        <pre id="test-result">pending</pre>
        <script>
        (async function() {
          const failures = [];
          const labels = __LABELS__;
          const el = id => document.getElementById(id);
          const settle = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); };
          function equal(actual, expected, context) {
            if (JSON.stringify(actual) !== JSON.stringify(expected)) {
              failures.push(context + ": " + JSON.stringify(actual) + " != " + JSON.stringify(expected));
            }
          }
          function submit() {
            el("algo-form").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
          }
          function confirm() { el("smart-selling-confirm").click(); }
          const account = id => document.querySelector('[data-smart-account-id="' + id + '"]');
          const balance = id => document.querySelector('[data-smart-account-balance="' + id + '"]').textContent;
          async function refresh() {
            intervals.forEach(callback => callback());
            await settle();
          }
          try {
            await settle();
            equal(el("smart-selling-modal").hidden, true, "smart modal hidden for E");
            equal(el("smart-selling-settings").disabled, true, "smart inputs cannot block E/F form validation");
            document.querySelector("label[for='algo-mode-g']").click();
            equal(el("smart-selling-modal").hidden, true, "selecting G does not open settings");
            equal(el("algo-submit").textContent, labels.algo_submit, "main action stays Start");
            submit();
            await settle();
            equal(el("smart-selling-modal").hidden, false, "Start opens smart configuration");
            equal(el("smart-selling-settings").disabled, false, "opening modal enables its settings");
            equal(requests.length, 0, "opening the modal does not place orders");
            equal(el("fast-trading-modal").hidden, true, "G does not open fast configuration");
            equal(el("smart-mode").value, "normal", "default participation mode");
            equal(Array.from(el("smart-mode").options, option => option.value), ["patient", "normal", "urgent"], "urgency options");
            equal(Array.from(el("smart-mode").options, option => option.textContent),
              [labels.smart_patient, labels.smart_normal, labels.smart_urgent], "plain urgency labels");
            equal(document.querySelectorAll("[data-smart-account-id]:checked").length, 0, "accounts require explicit selection");
            equal(balance("A").endsWith(": 150"), true, "prefixed and unprefixed account shares summed and shown");
            equal(balance("B").endsWith(": 0"), true, "empty inventory is shown as zero");
            equal(balance("C").endsWith(": —"), true, "unknown inventory remains unknown");
            el("algo_symbol").add(new Option("US.AAPL", "US.AAPL"));
            el("algo_symbol").value = "US.AAPL";
            el("algo_symbol").dispatchEvent(new Event("change"));
            equal(balance("A").endsWith(": 150"), true, "prefixed symbol selection matches the same inventory");
            el("algo_symbol").value = "AAPL";
            el("algo_symbol").dispatchEvent(new Event("change"));
            equal(el("smart-quantity"), null, "no share quantity field");
            const start = new Date(el("smart-start").value);
            const deadline = new Date(el("smart-deadline").value);
            equal(Math.abs(start.getTime() - Date.now()) < 2000, true, "default start is now in local time");
            equal(deadline.getTime() - start.getTime(), 7200000, "default horizon is two hours");
            equal(el("smart-start-now").checked, true, "Now is selected by default");
            equal(el("smart-start").disabled, true, "Now disables scheduled input");
            equal(Array.from(document.querySelectorAll("input[name='smart_deadline_mode']"), input => input.value),
              ["none", "scheduled"], "deadline offers mutually exclusive choices");
            equal(el("smart-set-deadline").checked, true, "Set deadline is selected by default");
            equal(el("smart-no-deadline").checked, false, "No deadline is initially unselected");
            equal(el("smart-deadline").disabled, false, "default deadline input is enabled");
            equal(el("smart-deadline").hidden, false, "default deadline input is visible");
            equal(el("smart-imminent-event"), null, "no adverse-event UI component");
            equal(el("smart-selling-confirm").textContent, labels.smart_submit, "dialog confirmation label");

            confirm();
            equal(el("smart-selling-error").textContent, labels.smart_invalid_account, "missing accounts rejected");
            account("A").click();
            account("B").click();
            el("smart-start-scheduled").click();
            equal(el("smart-start").disabled, false, "scheduled input enabled");
            equal(el("smart-start").hidden, false, "scheduled input visible");
            const originalDeadline = el("smart-deadline").value;
            el("smart-deadline").value = el("smart-start").value;
            confirm();
            equal(el("smart-selling-error").textContent, labels.smart_invalid_times, "invalid horizon rejected");
            el("smart-deadline").value = originalDeadline;
            el("smart-minimum-price").value = "-1";
            confirm();
            equal(el("smart-selling-error").textContent, labels.smart_invalid_price, "negative price rejected");
            equal(requests.length, 0, "validation never sends an order");

            el("smart-minimum-price").value = "";
            blockCreate = true;
            confirm();
            confirm();
            await settle();
            equal(requests.length, 1, "double submission sends only once");
            equal(el("algo-submit").disabled, true, "pending submission disabled");
            equal(el("smart-selling-settings").disabled, true, "pending configuration disabled");
            document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
            equal(el("smart-selling-modal").hidden, false, "pending request remains visible");
            equal(el("fast-trading-modal").hidden, true, "smart submit avoids fast modal");
            const payload = JSON.parse(requests[0].options.body);
            equal(payload, {account_ids: ["A", "B"], symbol: "AAPL", mode: "normal",
              start_time: start.toISOString(), deadline: deadline.toISOString(), minimum_price: null},
              "submitted payload and local time conversion");
            equal(requests[0].options.credentials, "same-origin", "authenticated same-origin request");
            equal(requests[0].options.headers["Content-Type"], "application/json", "JSON content type");
            releaseCreate();
            blockCreate = false;
            await settle();
            equal(el("algo-submit").disabled, false, "submit re-enabled after response");
            equal(el("smart-selling-modal").hidden, true, "successful confirmation closes dialog");
            equal(el("smart-selling-message").textContent, labels.smart_started, "localized success");
            equal(nativeSubmissions.length, 0, "smart mode never submits the legacy form");

            submit();
            el("smart-minimum-price").value = "777";
            accounts[0].positions[0].qty = "80";
            parents[0].reason = '<img src="x" onerror="window.injection=true">';
            await refresh();
            equal(el("smart-minimum-price").value, "777", "polling preserves editing");
            equal(account("A").checked && account("B").checked, true, "polling preserves multiselection");
            equal(balance("A").endsWith(": 130"), true, "shares refresh while the dialog is open");
            equal(el("smart-status-rows").querySelectorAll("img").length, 0, "status text cannot inject markup");
            const cells = el("smart-status-rows").querySelectorAll("td");
            equal(Array.from(cells).slice(0, 5).map(cell => cell.textContent),
                  ["A, B / AAPL", "1000", "10", "990", "20"], "session accounts and reservation shown");

            stopFailure = true;
            el("smart-status-rows").querySelector("button").click();
            await settle();
            equal(el("smart-status-action-error").textContent.includes("stop failed"), true, "failed stop remains visible after refresh");
            stopFailure = false;
            el("smart-status-rows").querySelector("button").click();
            await settle();
            equal(requests[requests.length - 1].url, "/api/smart-selling/p%2Fa/stop", "stop encodes parent ID");
            equal(requests[requests.length - 1].options.method, "POST", "stop is a mutation request");
            equal(el("smart-status-rows").querySelectorAll("td")[4].textContent, "20", "stop does not release reserved shares");
            equal(el("smart-status-rows").querySelector("button").disabled, true, "stopping cannot be requested twice");
            equal(el("smart-status-rows").textContent.includes(labels.smart_state_cancel_pending), true, "pending cancellation is explicit");
            equal(el("smart-selling-message").textContent, labels.smart_stopping, "stop explains broker confirmation");
            parents[0].market_state = "HALTED";
            parents[0].market_reason = "exchange halt reported";
            await refresh();
            equal(el("smart-status-rows").textContent.includes(labels.smart_market_halted), true, "market halt localized");
            equal(el("smart-status-rows").textContent.includes("exchange halt reported"), true, "market halt reason displayed");

            pollFailure = true;
            await refresh();
            equal(el("smart-status-error").textContent.includes(labels.smart_status_failed), true, "stale status flagged");
            equal(el("smart-status-rows").querySelectorAll("tr").length, 1, "poll failures preserve last status");
            pollFailure = false;
            await refresh();
            equal(el("smart-status-error").textContent, "", "status warning clears on recovery");

            createFailure = true;
            el("smart-mode").value = "urgent";
            el("smart-minimum-price").value = "12.5";
            el("smart-start-now").click();
            document.querySelector("label[for='smart-no-deadline']").click();
            equal(el("smart-no-deadline").checked, true, "No deadline choice selected");
            equal(el("smart-set-deadline").checked, false, "No deadline deselects Set deadline");
            equal(el("smart-deadline").disabled, true, "No deadline disables date input");
            equal(el("smart-deadline").hidden, true, "No deadline hides date input");
            el("smart-start").value = "";
            el("smart-deadline").value = "";
            confirm();
            await settle();
            const finalPayload = JSON.parse(requests[requests.length - 1].options.body);
            equal(finalPayload.mode, "urgent", "explicit urgency submitted");
            equal(finalPayload.minimum_price, 12.5, "minimum price submitted");
            equal("imminent_event" in finalPayload, false, "adverse event omitted to keep backend default off");
            equal(finalPayload.start_time, null, "Now is resolved by the server at submission");
            equal(finalPayload.deadline, null, "No deadline sends null");
            equal("quantity" in finalPayload, false, "quantity omitted to sell all inventory");
            equal(el("smart-selling-error").textContent.includes("existing parent"), true, "server rejection is visible");
            equal(el("smart-minimum-price").value, "12.5", "failed submit preserves price");
            equal(el("smart-selling-modal").hidden, false, "server rejection keeps dialog open");
            document.querySelector("label[for='smart-set-deadline']").click();
            equal(el("smart-set-deadline").checked, true, "Set deadline choice selected");
            equal(el("smart-no-deadline").checked, false, "Set deadline deselects No deadline");
            equal(el("smart-deadline").disabled, false, "deadline can be configured again");
            equal(el("smart-deadline").hidden, false, "Set deadline reveals date input");
            confirm();
            equal(el("smart-selling-error").textContent, labels.smart_invalid_times, "missing configured deadline rejected");
            document.querySelector("label[for='smart-no-deadline']").click();
            el("smart-start-scheduled").click();
            confirm();
            equal(el("smart-selling-error").textContent, labels.smart_invalid_times, "missing scheduled start rejected even without deadline");
            el("smart-selling-cancel").click();
            equal(el("smart-selling-modal").hidden, true, "cancel closes dialog");
            submit();
            document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
            equal(el("smart-selling-modal").hidden, true, "Escape closes dialog");
            submit();
            el("smart-selling-modal").click();
            equal(el("smart-selling-modal").hidden, true, "backdrop closes dialog");
            submit();

            document.querySelector("label[for='algo-mode-f']").click();
            equal(el("smart-selling-modal").hidden, true, "F hides smart dialog");
            equal(el("smart-selling-settings").disabled, true, "F disables smart validation");
            submit();
            equal(el("fast-trading-modal").hidden, false, "F still opens existing modal");
            el("fast-cancel").click();
            el("smart-selling-accounts").replaceChildren();
            document.querySelector("label[for='algo-mode-g']").click();
            equal(el("algo-submit").disabled, true, "cannot start without a configured account");
            equal(el("smart-selling-message").textContent, labels.smart_no_accounts, "missing accounts explained outside the modal");
          } catch (failure) {
            failures.push(failure.stack || String(failure));
          }
          await settle();
          el("test-result").textContent = JSON.stringify(failures.concat(uiErrors));
        })();
        </script>
        """.replace("__LABELS__", json.dumps(I18N[language]))
        page = render_layout(
            language,
            "control-panel",
            render_control_panel_page(language, account_metas(), ["AAPL"]),
        )
        page = page.replace("<head>", "<head>" + prelude).replace("</body>", checks + "</body>")
        with tempfile.TemporaryDirectory(prefix="ktrader-smart-selling-ui-") as directory:
            path = Path(directory) / "control-panel.html"
            path.write_text(page, encoding="utf-8")
            result = subprocess.run(
                [
                    CHROME,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--lang=en-US",
                    "--user-data-dir=" + str(Path(directory) / "profile"),
                    "--virtual-time-budget=1000",
                    "--dump-dom",
                    path.as_uri(),
                ],
                env={**os.environ, "TZ": "Asia/Shanghai"},
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, result.stderr)
        self.assertNotEqual(match.group(1), "pending", "Browser checks did not finish")
        self.assertEqual(json.loads(unescape(match.group(1))), [])


if __name__ == "__main__":
    unittest.main()
