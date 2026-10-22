import json
import tempfile
import unittest
from concurrent.futures import wait
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from trading_ui import webserver
from trading_ui.models import AccountMeta, AccountSnapshot, Position
from trading_ui.services.smart_selling import SmartSellingEngine
from trading_ui.services.smart_selling_runtime import SmartSellingRuntime
from trading_ui.smart_selling_config import normalize_smart_selling_config


class SmartSellingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 0.0
        self.epoch = 1800000000.0
        self.metas = {"A": AccountMeta("A", 1, "AnyBroker", "WEB")}
        self.snap = AccountSnapshot(
            "A",
            0,
            positions=[Position("US.XYZ", 1000, available_qty=1000)],
            ts=self.iso(),
            trading_enabled=True,
        )
        self.publish = Mock()
        self.engine = SmartSellingEngine(
            Path(self.temp.name) / "state.sqlite3",
            clock=lambda: self.now,
            wall_clock=lambda: self.epoch + self.now,
        )
        self.runtime = SmartSellingRuntime(
            {},
            account_metas_provider=lambda: self.metas,
            snapshots_provider=lambda: {"A": self.snap},
            publish_command=self.publish,
            engine=self.engine,
            clock=lambda: self.now,
            wall_clock=lambda: self.epoch + self.now,
        )
        self.runtime.feeds = {"orders": "live", "volume": "live"}

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def iso(self, seconds=0):
        return datetime.fromtimestamp(self.epoch + self.now + seconds, timezone.utc).isoformat()

    def start(self, **changes):
        settings = dict(
            account_id="A",
            symbol="US.XYZ",
            quantity="1000",
            mode="urgent",
            deadline=self.iso(300),
        )
        settings.update(changes)
        parent = self.runtime.start_parent(settings)
        self.parent_id = parent["parent_id"]
        self.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=self.epoch + self.now)
        return parent

    def submit(self):
        self.start()
        self.engine.on_trade("XYZ", 1000, "t1", source_time=self.epoch)
        self.runtime.tick()
        self.flush()
        return self.publish.call_args.args[0]

    def flush(self):
        wait(list(self.runtime._pending), timeout=5)

    def snapshot(self, command, **changes):
        payload = dict(
            schema_version=1,
            account_id="A",
            order_id="broker-1",
            sequence=1,
            created_at=command.get(
                "ts", datetime.fromtimestamp(self.epoch, timezone.utc).isoformat()
            ),
            updated_at=self.iso(),
            symbol="XYZ",
            side="SELL",
            order_type="LIMIT",
            status="NEW",
            quantity=command["qty_shares"],
            filled_quantity=0,
            average_fill_price=None,
            limit_price=command["limit_price"],
            command_id=command["command_id"],
        )
        payload.update(changes)
        return payload

    def test_portable_existing_limit_then_symbol_cancel_with_reserved_quantity(self):
        command = self.submit()
        self.assertEqual(command["type"], "LIMIT_ORDER")
        self.assertEqual(command["broker"], "AnyBroker")
        self.assertEqual(command["side"], "SELL")
        self.assertEqual(command["qty_shares"], 100)
        self.assertEqual(command["time_in_force"], "DAY")
        self.assertEqual(command["command_id"], command["client_order_id"])
        self.runtime.on_order_payload(self.snapshot(command))
        self.now = 2
        self.runtime.tick()
        self.flush()
        cancel = self.publish.call_args.args[0]
        self.assertEqual(cancel["type"], "CANCEL_OPEN_ORDERS")
        self.assertEqual((cancel["account_id"], cancel["symbol"]), ("A", "XYZ"))
        self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "100")
        report = self.snapshot(
            command,
            status="CANCELED",
            sequence=2,
            filled_quantity=40,
            average_fill_price=10,
            created_at=datetime.fromtimestamp(self.epoch, timezone.utc).isoformat(),
        )
        self.runtime.on_order_payload(report)
        parent = self.engine.get_parent(self.parent_id)
        self.assertEqual((parent["filled"], parent["reserved"]), ("40", "0"))

    def test_independent_volume_feed_drives_command_and_halt_cancellation(self):
        self.start()

        def book():
            self.runtime.on_price_book(
                {
                    "symbol": "XYZ",
                    "update_time": int((self.epoch + self.now) * 1e9),
                    "sequence_num": int(self.now) + 1,
                    "volume": 999999,
                    "bid_side": {"levels": [{"prc": 10, "qty": 1000}]},
                    "offer_side": {"levels": [{"prc": 10.01, "qty": 1000}]},
                }
            )

        def volume(total, sequence, halted=False):
            self.runtime.on_volume_payload(
                {
                    "schema_version": 1,
                    "type": "TRADING_VOLUME",
                    "symbol": "XYZ",
                    "feed_id": "test-feed",
                    "sequence": sequence,
                    "session": "2027-01-15",
                    "total_volume": total,
                    "observed_at": self.iso(),
                    "interval_start": self.iso(-5),
                    "healthy": not halted,
                    "is_halted": halted,
                    "is_delayed": False,
                }
            )

        book()
        volume(10000, 1)
        self.runtime.tick()
        self.flush()
        self.publish.assert_not_called()
        self.assertEqual(self.engine.get_parent(self.parent_id)["earned"], "0")

        self.now = 5
        book()
        volume(11000, 2)
        self.runtime.tick()
        self.flush()
        command = self.publish.call_args.args[0]
        self.assertEqual(command["type"], "LIMIT_ORDER")
        self.assertEqual(command["qty_shares"], 100)
        self.runtime.on_order_payload(self.snapshot(command))

        self.now = 6
        volume(None, 3, halted=True)
        book()
        self.runtime.tick()
        self.flush()
        self.assertEqual(self.publish.call_args.args[0]["type"], "CANCEL_OPEN_ORDERS")
        self.assertEqual(self.runtime.list_parents()[0]["market_state"], "HALTED")
        self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "100")

    def test_publication_timeout_never_refunds_or_sends_another_child(self):
        self.publish.side_effect = TimeoutError("ambiguous delivery")
        self.submit()
        self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "100")
        self.now = 20
        self.engine.on_quote("XYZ", 10, 10.01, 1000, source_time=self.epoch + self.now)
        self.runtime.tick()
        self.flush()
        self.assertEqual(self.publish.call_count, 1)
        self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "100")

    def test_queued_cancel_is_discarded_after_terminal_and_replacement_parent(self):
        command = self.submit()
        self.runtime.on_order_payload(self.snapshot(command))
        self.now = 2
        cancel = next(action for action in self.engine.tick() if action["type"] == "cancel")
        self.engine.request_stop(self.parent_id)
        self.runtime.on_order_payload(self.snapshot(command, status="CANCELED", sequence=2))
        self.start()
        self.publish.reset_mock()

        self.runtime._publish_action(cancel)

        self.publish.assert_not_called()

    def test_wire_price_precision_loss_aborts_before_publication(self):
        self.runtime.config["instruments"]["XYZ"] = {"tick_size": "0.0000000000000000001"}
        self.start()
        self.engine.on_quote(
            "XYZ",
            "10.0000000000000000001",
            "10.0000000000000000002",
            1000,
            source_time=self.epoch,
        )
        self.engine.on_trade("XYZ", 1000, "t1", source_time=self.epoch)

        self.runtime.tick()
        self.flush()

        self.publish.assert_not_called()
        self.assertEqual(self.engine.get_parent(self.parent_id)["reserved"], "0")
        self.assertEqual(self.engine.get_parent(self.parent_id)["state"], "STOPPED")

    def test_replay_must_finish_and_position_must_be_available(self):
        self.runtime.feeds["orders"] = "replaying"
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.start()
        self.runtime.feeds["orders"] = "live"
        with self.assertRaisesRegex(ValueError, "available position"):
            self.start(quantity=1001)
        self.snap.trading_enabled = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.start()
        self.snap.trading_enabled = True
        self.now = 100
        with self.assertRaisesRegex(ValueError, "stale"):
            self.start()

    def test_unacknowledged_manual_command_blocks_start_until_confirmed_terminal(self):
        external = dict(
            command_id="manual-1",
            account_id="A",
            symbol="XYZ",
            type="LIMIT_ORDER",
            qty_shares=10,
            limit_price=10,
        )
        self.runtime.track_external_order(external)
        with self.assertRaisesRegex(ValueError, "Existing orders"):
            self.start()
        self.runtime.on_order_payload(self.snapshot(external, status="CANCELED"))
        self.start()

    def test_http_cannot_override_execution_rules_or_choose_arbitrary_parent_id(self):
        parent = self.start(
            instrument_rules={"tick_size": "0.00001", "quantity_step": ".001"},
            parent_id="forged",
        )
        self.assertNotEqual(parent["parent_id"], "forged")
        self.assertEqual(self.engine._load(parent["parent_id"])["tick_size"], "0.01")

    def test_one_account_symbol_and_conflicting_fast_strategy_are_rejected(self):
        self.runtime.fast_conflict = lambda a, s: True
        with self.assertRaisesRegex(ValueError, "existing algorithm"):
            self.start()
        self.runtime.fast_conflict = lambda a, s: False
        self.start()
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.start(symbol="XYZ")

    def test_api_authentication_origin_validation_status_and_stop(self):
        with patch.multiple(
            webserver, SMART_SELLING_MANAGER=self.runtime, SESSIONS={"token": "trader"}
        ):
            client = TestClient(webserver.app)
            self.addCleanup(client.close)
            self.assertEqual(client.get("/api/smart-selling").status_code, 401)
            client.cookies.set("auth_token", "token")
            self.assertEqual(
                client.post(
                    "/api/smart-selling",
                    json={},
                    headers={"Origin": "https://evil.test"},
                ).status_code,
                403,
            )
            for bad in ([], None, {"quantity": "NaN"}):
                self.assertEqual(client.post("/api/smart-selling", json=bad).status_code, 400)
            response = client.post(
                "/api/smart-selling",
                json=dict(account_id="A", symbol="XYZ", quantity=100, deadline=self.iso(300)),
            )
            self.assertEqual(response.status_code, 200, response.text)
            parent = response.json()["parent"]
            self.assertEqual(len(client.get("/api/smart-selling").json()["parents"]), 1)
            response = client.post(f'/api/smart-selling/{parent["parent_id"]}/stop')
            self.assertEqual(response.json()["parent"]["state"], "STOPPED")
            self.assertEqual(client.post("/api/smart-selling/unknown/stop").status_code, 404)

    def test_existing_manual_entrypoints_cannot_place_into_owned_instrument(self):
        self.start()
        with patch.multiple(
            webserver, SMART_SELLING_MANAGER=self.runtime, COMMANDS_PRODUCER=Mock()
        ):
            with self.assertRaisesRegex(ValueError, "owns"):
                webserver._publish_control_command(
                    dict(type="MARKET_ORDER", account_id="A", symbol="XYZ"), "A"
                )

    def test_settings_reject_nonfinite_or_invalid_inputs(self):
        for setting in (
            {"quote_max_age": float("nan")},
            {"enabled": "true"},
            {"instruments": []},
            {"instrument_defaults": {"tick_size": "0"}},
            {"instrument_defaults": {"tick_szie": "0.01"}},
            {"instruments": {"XYZ": {"quantity_steps": "1"}}},
            {"instruments": {"XYZ": {"tick_ladder": []}}},
        ):
            with self.assertRaises(ValueError):
                normalize_smart_selling_config(setting)


if __name__ == "__main__":
    unittest.main()
