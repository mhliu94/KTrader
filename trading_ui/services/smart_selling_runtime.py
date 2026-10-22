"""Control-server wiring for Smart selling; only existing trading commands leave here."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import fcntl
import logging
from pathlib import Path
import threading
import time

from .smart_selling import SmartSellingEngine
from .smart_selling_feedback import SmartSellingFeedback
from .smart_selling_market import SmartSellingMarketAdapter
from ..smart_selling_config import canonical_symbol, normalize_smart_selling_config
from .smart_selling_commands import build_smart_selling_command

LOGGER = logging.getLogger("trading-ui.smart-selling")


class SmartSellingRuntime:
    def __init__(
        self,
        config,
        *,
        account_metas_provider,
        snapshots_provider,
        publish_command,
        fast_conflict=None,
        engine=None,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        self.config = normalize_smart_selling_config(config)
        self.account_metas_provider = account_metas_provider
        self.snapshots_provider = snapshots_provider
        self.publish_command = publish_command
        self.fast_conflict = fast_conflict or (lambda account, symbol: False)
        self.clock, self.wall_clock = clock, wall_clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._file_lock = None
        if engine is None:
            path = Path(self.config["state_db"])
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file_lock = open(str(path) + ".lock", "a")
            try:
                fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                engine = SmartSellingEngine(path, clock=clock, wall_clock=wall_clock)
            except BaseException:
                self._file_lock.close()
                raise
        self.engine = engine
        self.feedback = SmartSellingFeedback(
            engine,
            state_path=(
                self.config["state_db"] + ".feedback.sqlite3" if self._file_lock else ":memory:"
            ),
        )
        self.market = SmartSellingMarketAdapter(
            engine,
            clock=clock,
            wall_clock=wall_clock,
            max_source_age=self.config["trade_max_age"],
        )
        self.feeds = {"orders": "starting", "volume": "starting"}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smart-selling-publish"
        )
        self._pending = set()
        self._last_forecast = 0

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="smart-selling-controller", daemon=True
        )
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Smart selling controller heartbeat failed")
                self.on_feed_state("orders", "disconnected")
            self._stop.wait(max(0.01, 1 - (self.clock() - started)))

    def _available_position(self, account_id, symbol):
        snapshot = self.snapshots_provider().get(account_id)
        if snapshot is None or not snapshot.ts:
            raise ValueError("A recent account position snapshot is required")
        timestamp = datetime.fromisoformat(str(snapshot.ts).replace("Z", "+00:00"))
        if (
            timestamp.tzinfo is None
            or not -1 <= self.wall_clock() - timestamp.timestamp() <= self.config["account_max_age"]
        ):
            raise ValueError("Account position snapshot is stale")
        if not snapshot.trading_enabled:
            raise ValueError("Trading is disabled for this account")
        quantity = Decimal(0)
        for position in snapshot.positions:
            if canonical_symbol(position.symbol) != symbol:
                continue
            amount = position.available_qty if position.available_qty is not None else position.qty
            value = Decimal(str(amount))
            if not value.is_finite():
                raise ValueError("Invalid account position quantity")
            quantity += value
        return max(Decimal(0), quantity)

    def start_parent(self, settings):
        if not isinstance(settings, dict):
            raise ValueError("Smart selling settings must be an object")
        settings = dict(settings)
        account, symbol = str(settings.get("account_id", "")).strip(), canonical_symbol(
            settings.get("symbol")
        )
        if account not in self.account_metas_provider():
            raise ValueError("Unknown account")
        if not symbol:
            raise ValueError("A symbol is required")
        if type(settings.get("imminent_event", False)) is not bool:
            raise ValueError("imminent_event must be boolean")
        quantity = Decimal(str(settings.get("quantity")))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("Quantity must be positive and finite")
        with self._lock:
            if self.feeds["orders"] != "live":
                raise ValueError(
                    "Wait for the order-status feed to finish reconciliation before starting Smart selling"
                )
            if self.fast_conflict(account, symbol):
                raise ValueError("Stop the existing algorithm for this account and symbol first")
            if self.feedback.has_open_orders(account, symbol):
                raise ValueError(
                    "Existing orders for this account and instrument must reach terminal status first"
                )
            if quantity > self._available_position(account, symbol):
                raise ValueError("Quantity exceeds the account's available position")
            rules = {
                **self.config["instrument_defaults"],
                **self.config["instruments"].get(symbol, {}),
            }
            # Runtime rules cannot be overridden by an HTTP payload.
            permitted = {
                key: settings[key]
                for key in (
                    "quantity",
                    "mode",
                    "start_time",
                    "deadline",
                    "minimum_price",
                    "imminent_event",
                )
                if key in settings
            }
            permitted.update(
                account_id=account,
                symbol=symbol,
                instrument_rules=rules,
                quote_max_age=self.config["quote_max_age"],
                trade_max_age=self.config["trade_max_age"],
            )
            return self.engine.start_parent(permitted)

    def owns(self, account_id, symbol):
        return any(
            p["account_id"] == account_id and p["symbol"] == canonical_symbol(symbol)
            for p in self.engine.list_parents(active_only=True, limit=1000)
        )

    def list_parents(self):
        parents = self.engine.list_parents()
        if hasattr(self.market, "market_status"):
            for parent in parents:
                status = self.market.market_status(parent["symbol"])
                parent["market_state"] = status["state"]
                parent["market_reason"] = status.get("reason", "")
        return parents

    def stop_parent(self, parent_id):
        with self._lock:
            result = self.engine.request_stop(parent_id)
            self._dispatch(self.engine.drain_actions())
            return result

    def stop_all(self):
        parents = self.engine.list_parents(active_only=True, limit=1000)
        for parent in parents:
            self.stop_parent(parent["parent_id"])
        return len(parents)

    def on_price_book(self, payload, **context):
        with self._lock:
            self.market.apply_price_book(payload, **context)

    def on_price_book_health(self, healthy):
        with self._lock:
            self.market.set_connected(healthy)

    def on_volume_payload(self, payload, **context):
        with self._lock:
            self.market.apply_volume(payload, **context)

    def on_order_payload(self, raw):
        with self._lock:
            self.feedback.apply_snapshot(raw)
            self._dispatch(self.engine.drain_actions())

    def track_external_order(self, command):
        """A locally handed-off order remains uncertain until its status arrives."""
        with self._lock:
            self.feedback.track_external_order(command)

    def on_feed_state(self, feed, state):
        with self._lock:
            self.feeds[feed] = state
            if feed == "volume":
                self.market.set_volume_connected(state == "live")
            if state != "live":
                for parent in self.engine.list_parents(active_only=True, limit=1000):
                    self.engine.set_feed_health(parent["symbol"], False, f"{feed} feed {state}")

    def tick(self):
        with self._lock:
            parents = self.engine.list_parents(active_only=True, limit=1000)
            for parent in parents:
                if any(state != "live" for state in self.feeds.values()):
                    self.engine.set_feed_health(
                        parent["symbol"],
                        False,
                        "waiting for live order and volume feeds",
                    )
                try:
                    self._available_position(parent["account_id"], parent["symbol"])
                except (ValueError, TypeError):
                    self.engine.set_feed_health(
                        parent["symbol"],
                        False,
                        "account snapshot unavailable or trading disabled",
                    )
            if self.clock() - self._last_forecast >= 30:
                for parent in parents:
                    rate = self.market.volume_rate(parent["symbol"])
                    if rate is not None:
                        deadline = datetime.fromisoformat(
                            parent["deadline"].replace("Z", "+00:00")
                        ).timestamp()
                        self.engine.set_remaining_volume_forecast(
                            parent["parent_id"],
                            rate * Decimal(str(max(0, deadline - self.wall_clock()))),
                        )
                self._last_forecast = self.clock()
            self._dispatch(self.engine.tick())

    def _dispatch(self, actions):
        for action in actions:
            if action["type"] == "query":
                # Existing command vocabulary has no broker query operation.
                # Replaying evidence cannot prove an unknown order is gone.
                self.feedback.reconcile(action["client_order_id"])
                continue
            future = self._executor.submit(self._publish_action, action)
            self._pending.add(future)
            future.add_done_callback(self._pending.discard)

    def _prepare_submit(self, action):
        """Last local checks; a rejected unsent intent is not a broker cancellation."""
        client_id = action["client_order_id"]
        if any(state != "live" for state in self.feeds.values()):
            self.engine.abort_dispatch(client_id, "waiting for live order and volume feeds")
            return None

        account_id, symbol = action["account_id"], action["symbol"]
        available = self._available_position(account_id, symbol)
        if Decimal(action["quantity"]) > available:
            self.engine.abort_dispatch(client_id, "available position decreased", stop_parent=True)
            return None
        if self.feedback.has_open_orders(account_id, symbol):
            self.engine.abort_dispatch(client_id, "conflicting open order", stop_parent=True)
            return None

        # The engine rechecks stop/deadline, quote age, depth, price and budget
        # atomically. Only the returned quantity/price may be published.
        return self.engine.prepare_dispatch(client_id)

    def _publish_action(self, action):
        """Serialize final validation with the publisher's nonblocking enqueue."""
        with self._lock:
            self._handoff_action(action)

    def _handoff_action(self, action):
        """The injected publisher must enqueue locally, without awaiting delivery."""
        publish_started = False
        try:
            if action["type"] == "submit":
                prepared = self._prepare_submit(action)
                if prepared is None:
                    return
                action = prepared
            elif action["type"] == "cancel":
                context = self.engine.get_order_context(action["client_order_id"])
                if not context or not context["active"]:
                    return
                identity = ("parent_id", "account_id", "symbol", "broker_order_id")
                if any(context[key] != action[key] for key in identity):
                    return
            command = build_smart_selling_command(action, self.account_metas_provider())
            publish_started = True
            self.publish_command(command, action["account_id"])
        except Exception as exc:
            if action["type"] == "submit" and not publish_started:
                LOGGER.warning(
                    "Smart selling child rejected before publication: %s",
                    type(exc).__name__,
                )
                self.engine.abort_dispatch(action["client_order_id"], str(exc), stop_parent=True)
            else:
                LOGGER.exception("Smart selling %s dispatch uncertain", action["type"])
                self.engine.on_action_error(action["client_order_id"], action["type"], str(exc))

    def close(self):
        self.stop_all()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=True)
        self.feedback.close()
        self.engine.close()
        if self._file_lock:
            self._file_lock.close()
