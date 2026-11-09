import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from html import unescape
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote

from fastapi.testclient import TestClient

import trading_ui.webserver as webserver
from trading_ui.models import AccountMeta
from trading_ui.services.orders import validate_broker_orders_inputs
from trading_ui.templates import render_admin_control_page, render_layout


def metas():
    return {
        "A": AccountMeta(id="A", num_id=2, broker="Tiger", trading_medium="API"),
        "B": AccountMeta(id="B", num_id=1, broker="Other", trading_medium="API"),
    }


class BrokerOrdersValidationTests(unittest.TestCase):
    def validate(self, trading_date="2026-11-01", accounts=None, account_metas=None):
        with patch("trading_ui.services.orders.broker_orders_today", return_value=date(2026, 11, 9)):
            return validate_broker_orders_inputs(
                trading_date, ["A"] if accounts is None else accounts,
                metas() if account_metas is None else account_metas,
                "invalid date", "future date", "invalid account", "pick accounts",
            )

    def test_contract_deduplicates_and_orders_selected_accounts(self):
        command, error = self.validate(accounts=["A", "B", "A"])
        self.assertIsNone(error)
        self.assertEqual(set(command), {"type", "command_id", "ts", "trading_date", "timezone", "account_ids"})
        self.assertEqual(command["type"], "GET_BROKER_ORDERS")
        self.assertEqual(command["account_ids"], ["B", "A"])
        self.assertEqual(command["trading_date"], "2026-11-01")
        self.assertEqual(command["timezone"], "America/New_York")
        self.assertIsNotNone(datetime.fromisoformat(command["ts"].replace("Z", "+00:00")).tzinfo)
        another, _ = self.validate()
        self.assertNotEqual(command["command_id"], another["command_id"])

    def test_all_expands_to_configured_accounts_without_wildcard(self):
        for accounts in (["__ALL__"], ["__ALL__", "A"]):
            command, error = self.validate(accounts=accounts)
            self.assertIsNone(error)
            self.assertEqual(command["account_ids"], ["B", "A"])

    def test_rejects_invalid_or_future_dates(self):
        for day in ("", "2026-02-29", "2026-11-31", "20261101", "2026-W45-1", "2026-1-1", "2026-11-01T00:00:00Z"):
            with self.subTest(day=day):
                self.assertEqual(self.validate(trading_date=day), (None, "invalid date"))
        self.assertEqual(self.validate(trading_date="2026-11-10"), (None, "future date"))
        self.assertIsNone(self.validate(trading_date="2024-02-29")[1])
        self.assertIsNone(self.validate(trading_date="2026-11-09")[1])

    def test_empty_and_unknown_accounts_never_build_a_command(self):
        self.assertEqual(self.validate(accounts=[]), (None, "pick accounts"))
        self.assertEqual(self.validate(accounts=["__ALL__"], account_metas={}), (None, "pick accounts"))
        for accounts in (["A", "unknown"], ["__ALL__", "unknown"], [""]):
            self.assertEqual(self.validate(accounts=accounts), (None, "invalid account"))


class AdminControlEndpointTests(unittest.TestCase):
    def setUp(self):
        self.producer = Mock()
        self.sessions = {f"token-{i}": name for i, name in enumerate(["lmh", "admin", "trader", "LMH", " lmh "])}
        self.patches = [
            patch.object(webserver, "ACCOUNT_METAS", metas()),
            patch.object(webserver, "COMMANDS_PRODUCER", self.producer),
            patch.object(webserver, "SESSIONS", self.sessions),
            patch.object(webserver, "FAST_TRADING_MANAGER", Mock()),
            patch.object(webserver, "SMART_SELLING_MANAGER", Mock()),
            patch.object(webserver, "SMART_BUYING_MANAGER", Mock()),
            patch.object(webserver, "broker_orders_today", return_value=date(2026, 11, 9)),
            patch("trading_ui.services.orders.broker_orders_today", return_value=date(2026, 11, 9)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        # No context manager: production startup and Kafka connections must not run.
        self.client = TestClient(webserver.app)
        self.addCleanup(self.client.close)

    def login(self, user):
        token = next(token for token, name in self.sessions.items() if name == user)
        self.client.cookies.set("auth_token", token)

    def post(self, **data):
        return self.client.post("/submit-broker-orders", data={"trading_date": "2026-11-01", "account_ids": ["A"], **data}, follow_redirects=False)

    def test_anonymous_requests_require_login_and_never_publish(self):
        for response in (self.client.get("/admin-control", follow_redirects=False), self.post()):
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith("/login?next="))
        self.producer.publish_order.assert_not_called()

    def test_only_exact_lmh_can_view_or_submit_even_if_other_user_is_admin(self):
        for user in ["admin", "trader", "LMH", " lmh "]:
            with self.subTest(user=user):
                self.login(user)
                self.assertEqual(self.client.get("/admin-control").status_code, 403)
                self.assertEqual(self.post().status_code, 403)
                self.assertNotIn('href="/admin-control"', self.client.get("/control-panel").text)
        self.producer.publish_order.assert_not_called()
        self.login("lmh")
        page = self.client.get("/admin-control")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/admin-control"', page.text)
        self.assertIn("Admin Control", page.text)
        self.assertNotIn("Currency Conversion", page.text)

    def test_publishes_one_multi_account_request_without_touching_active_algorithms(self):
        self.login("lmh")
        response = self.post(account_ids=["B", "A", "B"])
        self.assertEqual(response.status_code, 303)
        command = self.producer.publish_order.call_args.args[0]
        self.producer.publish_order.assert_called_once_with(command, key="GET_BROKER_ORDERS")
        self.assertEqual(command["account_ids"], ["B", "A"])
        self.assertEqual(command["trading_date"], "2026-11-01")
        self.assertEqual(command["type"], "GET_BROKER_ORDERS")
        for manager in (webserver.FAST_TRADING_MANAGER, webserver.SMART_SELLING_MANAGER, webserver.SMART_BUYING_MANAGER):
            self.assertEqual(manager.mock_calls, [])
        self.assertIn("Request queued for 2 account(s)", unquote(response.headers["location"]))
        self.assertIn(command["command_id"], response.headers["location"])

    def test_all_and_invalid_selections_are_validated_before_publish(self):
        self.login("lmh")
        for data in ({"account_ids": []}, {"account_ids": ["A", "missing"]},
                     {"account_ids": ["__ALL__", "missing"]}, {"trading_date": "2026-02-30"},
                     {"trading_date": "2026-11-10"}):
            response = self.post(**data)
            self.assertEqual(response.status_code, 400)
        self.producer.publish_order.assert_not_called()
        self.assertEqual(self.post(account_ids=["__ALL__"]).status_code, 303)
        self.assertEqual(self.producer.publish_order.call_args.args[0]["account_ids"], ["B", "A"])

    def test_invalid_form_preserves_inputs_and_escapes_untrusted_text(self):
        self.login("lmh")
        response = self.post(trading_date='"><script>alert(1)</script>', account_ids=["B"])
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('<script>alert(1)</script>', response.text)
        self.assertIn('value="B" data-broker-orders-account checked', response.text)
        self.producer.publish_order.assert_not_called()

    def test_transport_failure_returns_error_without_claiming_success(self):
        self.login("lmh")
        self.producer.publish_order.side_effect = RuntimeError("private transport detail")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertIn("Unable to queue", response.text)
        self.assertNotIn("private transport detail", response.text)
        self.assertNotIn("Request queued", response.text)

    def test_currency_conversion_routes_are_removed_and_get_cannot_publish(self):
        self.login("lmh")
        self.assertEqual(self.client.get("/currency-conversion").status_code, 404)
        self.assertEqual(self.client.post("/submit-currency-conversion").status_code, 404)
        self.assertEqual(self.client.get("/submit-broker-orders").status_code, 405)
        self.producer.publish_order.assert_not_called()


@unittest.skipUnless(shutil.which("google-chrome") or shutil.which("chromium"), "Chrome is not installed")
class AdminControlBrowserTests(unittest.TestCase):
    def test_multi_selection_all_selection_and_empty_selection_in_both_languages(self):
        checks = r"""
        <script>
        try {
          const all = document.getElementById('broker-orders-all');
          const inputs = [...document.querySelectorAll('[data-broker-orders-account]')];
          const form = document.getElementById('broker-orders-form');
          const button = form.querySelector('button[type=submit]');
          function check(value, message) { if (!value) throw new Error(message); }
          function ids() { return new FormData(form).getAll('account_ids'); }
          check(all.checked && !button.disabled, 'All defaults selected');
          inputs[0].click();
          check(!all.checked && JSON.stringify(ids()) === '["B"]', 'Individual choice clears All');
          inputs[1].click();
          check(JSON.stringify(ids()) === '["B","A"]', 'Multiple accounts submit together');
          inputs.forEach(input => input.click());
          check(button.disabled, 'Empty selection disables request');
          all.click();
          check(!button.disabled && JSON.stringify(ids()) === '["__ALL__"]', 'All submits sentinel only');
          inputs[0].click(); all.click();
          check(inputs.every(input => !input.checked), 'All clears individual selections');
          const date = document.getElementById('broker-orders-date');
          date.value = '2026-11-10'; check(!date.checkValidity(), 'Future date blocked');
          date.value = '2026-11-01'; check(form.checkValidity(), 'Historical day accepted');
          const submitted = form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
          check(submitted && button.disabled, 'Submitting disables request button');
          window.dispatchEvent(new Event('pageshow'));
          check(!button.disabled, 'Back navigation enables request button');
          const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = 'PASS'; document.body.append(result);
        } catch(error) {
          const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = 'FAIL: ' + error.message; document.body.append(result);
        }
        </script>
        """
        for lang in ("en", "zh"):
            with self.subTest(lang=lang), tempfile.TemporaryDirectory() as tmp:
                page = render_layout(lang, "admin-control", render_admin_control_page(lang, metas(), today="2026-11-09"), current_user="lmh", can_access_admin_control=True)
                path = Path(tmp) / 'page.html'
                path.write_text(page.replace('</body>', checks + '</body>'))
                proc = subprocess.run([shutil.which('google-chrome') or shutil.which('chromium'),
                                       '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                                       '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                                       '--user-data-dir=' + str(Path(tmp) / 'profile'), '--dump-dom', path.as_uri()],
                                      capture_output=True, text=True, timeout=25)
                self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
                result = re.search(r'<pre id="test-result">(.*?)</pre>', proc.stdout, re.S)
                self.assertIsNotNone(result, proc.stderr[-1000:])
                self.assertEqual(unescape(result.group(1)), 'PASS')
