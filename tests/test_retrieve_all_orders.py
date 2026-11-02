import csv
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("retrieve_all_orders", Path(__file__).resolve().parents[1] / "retrieve-all-orders.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def order(identifier, timestamp=1000, account="123", market="US", sec_type="STK"):
    return SimpleNamespace(
        id=identifier, account=account, order_time=timestamp, status="FILLED",
        contract=SimpleNamespace(symbol="AAPL", sec_type=sec_type, market=market, currency="USD"),
        reason="Unicode note: 成交, confirmed", secret_key="MUST_NOT_APPEAR",
    )


def page(orders, token=None):
    return SimpleNamespace(result=orders, next_page_token=token)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def fetch(self, client):
        with redirect_stdout(io.StringIO()):
            return exporter.retrieve_orders(client, "123", "TIGER_TEST", 10000, pause=lambda _: None)

    def test_cursor_pagination_preserves_same_time_orders_and_deduplicates(self):
        client = Mock()
        client.get_orders.side_effect = [page([order(1, 2000), order(2)], "next"),
                                         page([order(2), order(3)]), page([])]
        rows, pages = self.fetch(client)
        self.assertEqual([row["id"] for row in rows], ["2", "3", "1"])
        calls = [call.kwargs for call in client.get_orders.call_args_list]
        self.assertEqual(calls[0]["end_time"], calls[1]["end_time"])
        self.assertEqual(calls[1]["page_token"], "next")
        self.assertEqual(calls[2]["end_time"], 1000)
        self.assertEqual(len(pages), 3)
        for call in calls:
            self.assertEqual((call["account"], call["market"], call["sec_type"]), ("123", "US", "STK"))
            self.assertNotIn("start_time", call)
            self.assertNotIn("states", call)

    def test_older_boundary_check_can_recover_additional_history(self):
        client = Mock()
        client.get_orders.side_effect = [page([order(1, 2000)]), page([order(2, 1000)]), page([])]
        rows, _ = self.fetch(client)
        self.assertEqual([row["id"] for row in rows], ["2", "1"])

    def test_repeated_cursor_fails_instead_of_sending_partial_data(self):
        client = Mock()
        client.get_orders.side_effect = [page([order(1)], "same"), page([order(1)], "same")]
        with self.assertRaisesRegex(exporter.ExportError, "did not progress"):
            self.fetch(client)

    def test_full_page_without_cursor_is_not_assumed_complete(self):
        client = Mock()
        client.get_orders.return_value = page([order(i) for i in range(exporter.PAGE_SIZE)])
        with self.assertRaisesRegex(exporter.ExportError, "full page"):
            self.fetch(client)

    def test_bad_account_market_type_or_boundary_stops_export(self):
        for bad in [order(1, account="999"), order(1, market="HK"), order(1, sec_type="OPT"), order(1, timestamp=10000)]:
            client = Mock()
            client.get_orders.return_value = page([bad])
            with self.subTest(order=bad), self.assertRaises(exporter.ExportError):
                self.fetch(client)

    def test_csv_retains_exact_ids_unicode_and_excludes_authentication(self):
        row = exporter.order_row(order(987654321012345678), "123", "TIGER_TEST")
        path, summary = exporter.save_export([row], [], "123", "TIGER_TEST", self.as_of, self.root)
        with path.open(encoding="utf-8-sig", newline="") as f:
            loaded = list(csv.DictReader(f))
        self.assertEqual(loaded[0]["id"], "987654321012345678")
        self.assertEqual(loaded[0]["reason"], "Unicode note: 成交, confirmed")
        self.assertNotIn(b"MUST_NOT_APPEAR", path.read_bytes())
        self.assertNotIn(b"secret_key", path.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        message = exporter.build_message(path, summary, "sender@example.com", exporter.RECIPIENT)
        self.assertIn("TIGER_TEST", message["Subject"])
        attachment = list(message.iter_attachments())[0]
        self.assertIn("TIGER_TEST", attachment.get_filename())
        self.assertEqual(attachment.get_payload(decode=True), path.read_bytes())
        self.assertEqual(message["To"], exporter.RECIPIENT)

    def test_empty_history_is_explicit_and_writes_header(self):
        client = Mock()
        client.get_orders.return_value = page([])
        rows, pages = self.fetch(client)
        path, summary = exporter.save_export(rows, pages, "123", "TIGER_EMPTY", self.as_of, self.root)
        self.assertEqual(summary["order_count"], 0)
        self.assertIsNone(summary["earliest_order_utc"])
        message = exporter.build_message(path, summary, "sender@example.com", exporter.RECIPIENT)
        self.assertIn("No US stock orders returned", message.get_body().get_content())

    def test_string_id_is_read_without_executing_listener(self):
        listener = self.root / "start_tiger_listener.sh"
        marker = self.root / "executed"
        listener.write_text(f'export TIGER_UI_ACCOUNT_IDS="${{TIGER_UI_ACCOUNT_IDS:-TIGER_OTHER_BOX}}" # local\ntouch {marker}\n')
        with patch.object(exporter, "ROOT", self.root), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(exporter.resolve_string_id(SimpleNamespace(string_id=None)), "TIGER_OTHER_BOX")
        self.assertFalse(marker.exists())

    def test_ambiguous_account_labels_require_explicit_choice(self):
        with patch.dict(os.environ, {"TIGER_UI_ACCOUNT_IDS": "TIGER_A,TIGER_B"}, clear=True):
            with self.assertRaises(exporter.ExportError):
                exporter.resolve_string_id(SimpleNamespace(string_id=None))
            self.assertEqual(exporter.resolve_string_id(SimpleNamespace(string_id="TIGER_B")), "TIGER_B")

    def test_fetch_failure_never_sends_email_or_writes_csv(self):
        settings = dict(host="smtp.example.com", username="user", password="pw", sender="a@b.com")
        with patch.object(exporter, "smtp_settings", return_value=settings), \
                patch.object(exporter, "resolve_string_id", return_value="TIGER_TEST"), \
                patch.object(exporter, "make_client", return_value=(Mock(), "123")), \
                patch.object(exporter, "retrieve_orders", side_effect=RuntimeError("API failed")), \
                patch.object(exporter, "send_email") as send, redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                exporter.main(["--output-dir", str(self.root), "--email-config", str(self.root / "missing.json")])
        send.assert_not_called()
        self.assertEqual(list(self.root.glob("*.csv")), [])

    def test_send_failure_keeps_csv_and_records_unconfirmed_status(self):
        row = exporter.order_row(order(1), "123", "TIGER_TEST")
        with patch.object(exporter, "smtp_settings", return_value={"sender": "a@b.com"}), \
                patch.object(exporter, "resolve_string_id", return_value="TIGER_TEST"), \
                patch.object(exporter, "make_client", return_value=(Mock(), "123")), \
                patch.object(exporter, "retrieve_orders", return_value=([row], [])), \
                patch.object(exporter, "send_email", side_effect=TimeoutError) as send, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(exporter.ExportError, "delivery is unconfirmed"):
                exporter.main(["--output-dir", str(self.root), "--email-config", str(self.root / "missing.json")])
        send.assert_called_once()
        self.assertEqual(len(list(self.root.glob("*.csv"))), 1)
        summary = json.loads(next(self.root.glob("*.json")).read_text())
        self.assertEqual(summary["email_status"], "failed_or_unconfirmed")


class EmailTests(unittest.TestCase):
    def test_setup_saves_owner_only_config_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retrieve-all-orders.local.json"
            with patch("builtins.input", return_value=""), \
                    patch.object(exporter.getpass, "getpass", return_value="abcd efgh ijkl mnop"), \
                    patch.object(exporter, "send_email") as send, redirect_stdout(io.StringIO()):
                exporter.configure_email(path)
            settings = exporter.smtp_settings(json.loads(path.read_text()), environ={})
            self.assertEqual(settings["password"], "abcdefghijklmnop")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            send.assert_not_called()

    def test_missing_settings_give_setup_command(self):
        with self.assertRaisesRegex(exporter.ExportError, "configure-email"):
            exporter.smtp_settings({}, environ={})

    def test_environment_overrides_saved_smtp_config(self):
        settings = exporter.smtp_settings({"username": "old@example.com", "password": "old"},
                                          environ={"SMTP_USER": "new@example.com", "SMTP_PASSWORD": "new"})
        self.assertEqual(settings["username"], "new@example.com")
        self.assertEqual(settings["password"], "new")

    def test_starttls_is_established_before_authentication_and_send(self):
        settings = exporter.smtp_settings({}, environ={"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw", "SMTP_SECURITY": "starttls"})
        with patch.object(exporter.smtplib, "SMTP") as smtp:
            connection = smtp.return_value
            connection.send_message.return_value = {}
            exporter.send_email(Mock(), settings)
            names = [call[0] for call in connection.mock_calls]
            self.assertLess(names.index("starttls"), names.index("login"))
            self.assertLess(names.index("login"), names.index("send_message"))
            smtp.assert_called_once_with("smtp.gmail.com", 587, timeout=60)

    def test_ssl_success_is_not_reported_as_failure_if_quit_disconnects(self):
        settings = exporter.smtp_settings({}, environ={"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw"})
        with patch.object(exporter.smtplib, "SMTP_SSL") as smtp:
            connection = smtp.return_value
            connection.send_message.return_value = {}
            connection.quit.side_effect = exporter.smtplib.SMTPServerDisconnected
            exporter.send_email(Mock(), settings)
            connection.send_message.assert_called_once()
            connection.close.assert_called_once()


@unittest.skipUnless(importlib.util.find_spec("tigeropen"), "Tiger SDK required for config integration tests")
class TigerConfigTests(unittest.TestCase):
    def test_local_properties_and_environment_mapping_select_the_intended_account(self):
        config = SimpleNamespace(account="props-account", tiger_id="app-id", private_key="fake-key")
        args = SimpleNamespace(properties=None, account=None)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(exporter, "ROOT", Path(directory)), \
                patch.dict(os.environ, {"TIGER_ACCOUNT_MAP": "TIGER_A:111,TIGER_B:222"}, clear=True), \
                patch("tigeropen.tiger_open_config.TigerOpenClientConfig", return_value=config) as constructor, \
                patch("tigeropen.trade.trade_client.TradeClient") as client:
            result, account = exporter.make_client(args, "TIGER_B")
            self.assertEqual(account, "222")
            self.assertIs(result, client.return_value)
            constructor.assert_called_once_with(props_path=str(Path(directory) / "tiger_openapi_config.properties"))

    def test_explicit_properties_override_sdk_environment_path(self):
        config = SimpleNamespace(account="props-account", tiger_id="app-id", private_key="fake-key")
        with tempfile.TemporaryDirectory() as directory:
            properties = Path(directory) / "selected.properties"
            properties.touch()
            args = SimpleNamespace(properties=str(properties), account="explicit-account")
            with patch.dict(os.environ, {"TIGEROPEN_PROPS_PATH": "/wrong/path"}, clear=True), \
                    patch("tigeropen.tiger_open_config.TigerOpenClientConfig", return_value=config), \
                    patch("tigeropen.trade.trade_client.TradeClient"):
                _, account = exporter.make_client(args, "TIGER_EXPLICIT")
                self.assertEqual(account, "explicit-account")
                self.assertEqual(os.environ["TIGEROPEN_PROPS_PATH"], str(properties))


if __name__ == "__main__":
    unittest.main()
