"""Control-server wiring for Smart selling; only existing trading commands leave here."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fcntl
import logging
from pathlib import Path
import threading
import time

from .smart_selling import SmartSellingEngine
from .smart_selling_feedback import SmartSellingFeedback
from .smart_selling_market import SmartSellingMarketAdapter
from .smart_selling_policy import SMART_SELLING_DEFAULTS, round_down_to_step
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
        fast_active=None,
        engine=None,
        clock=time.monotonic,
        wall_clock=time.time,
        reporting=None,
    ):
        self.config = normalize_smart_selling_config(config)
        self.account_metas_provider = account_metas_provider
        self.snapshots_provider = snapshots_provider
        self.publish_command = publish_command
        self._reporting = reporting
        self._reported_sessions = set()
        self._reported_terminal = set()
        self.fast_conflict = fast_conflict or (lambda account, symbol: False)
        self.fast_active = fast_active or (lambda: False)
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
        self._sync_reporting()

    def _report(self, method, *args, **kwargs):
        if self._reporting is None:
            return False
        try:
            getattr(self._reporting, method)(*args, **kwargs)
            return True
        except Exception:
            LOGGER.exception("Failed updating Smart selling reporting method=%s", method)
            return False

    def _sync_reporting(self, parents=None, parameters=None):
        if self._reporting is None:
            return
        try:
            parents = parents if parents is not None else self.engine.list_parents(limit=1000)
            for parent in parents:
                session_id = parent["parent_id"]
                if session_id not in self._reported_sessions:
                    # Existing durable rows retain their original parameters on recovery.
                    starting = {
                        key: parent.get(key) for key in (
                            "symbol", "quantity", "mode", "participation", "start",
                            "deadline", "minimum_price", "child_cap",
                        )
                    }
                    starting.update(
                        account_ids=self._parent_accounts(parent),
                        account_quantities={
                            account: values["quantity"]
                            for account, values in parent.get("accounts", {}).items()
                        },
                        trading_mode="G",
                    )
                    starting.update(parameters or {})
                    if self._report(
                        "start_session", session_id, starting, self._parent_accounts(parent),
                        started_at=parent.get("start"),
                    ):
                        self._reported_sessions.add(session_id)
                    # A recovered outstanding child can still receive late fills.
                    order = parent.get("active_order")
                    if order:
                        self._report("register_command", session_id, {
                            "account_id": order.get("account_id", parent["account_id"]),
                            "symbol": parent["symbol"],
                            "command_id": order["client_order_id"],
                            "client_order_id": order["client_order_id"],
                        })
                if parent["active"]:
                    state = str(parent["state"]).lower()
                    if state != "stopping" and parent.get("order_state") == "RECONCILE":
                        state = "reconciling"
                    elif state == "hold" or (state == "running" and any(
                        value != "live" for value in self.feeds.values()
                    )):
                        state = "paused"
                    self._report("heartbeat_session", session_id, status=state)
                elif session_id not in self._reported_terminal:
                    if self._report(
                        "end_session", session_id, status=str(parent["state"]).lower(),
                        reason=parent.get("reason"),
                    ):
                        self._reported_terminal.add(session_id)
        except Exception:
            LOGGER.exception("Failed reading Smart selling sessions for reporting")

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
            try:
                held = Decimal(str(position.qty))
                available = Decimal(str(
                    position.available_qty if position.available_qty is not None else position.qty
                ))
            except InvalidOperation as exc:
                raise ValueError("Invalid account position quantity") from exc
            if not held.is_finite() or not available.is_finite():
                raise ValueError("Invalid account position quantity")
            # Available inventory must never permit selling more than is held.
            quantity += min(held, available)
        return max(Decimal(0), quantity)

    def start_parent(self, settings):
        if not isinstance(settings, dict):
            raise ValueError("Smart selling settings must be an object")
        settings = dict(settings)
        accounts = settings.get("account_ids")
        if accounts is None:
            accounts = [settings.get("account_id", "")]
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("Select at least one account")
        if any(not isinstance(account, str) or not account.strip() for account in accounts):
            raise ValueError("Invalid account selection")
        accounts = [account.strip() for account in accounts]
        if len(set(accounts)) != len(accounts):
            raise ValueError("Select each account only once")
        metas = self.account_metas_provider()
        if any(account not in metas for account in accounts):
            raise ValueError("Unknown account")
        symbol = canonical_symbol(settings.get("symbol"))
        if not symbol:
            raise ValueError("A symbol is required")
        # imminent_event has NO UI component; omitted values remain OFF (False).
        # Keep explicit backend requests supported for future interfaces.
        if type(settings.get("imminent_event", False)) is not bool:
            raise ValueError("imminent_event must be boolean")
        with self._lock:
            if self.has_active_session():
                raise ValueError("Smart selling already owns an active session; stop it before starting another")
            if self.fast_active() or any(self.fast_conflict(account, symbol) for account in accounts):
                raise ValueError("Stop the existing algorithm before starting another session")
            if self.feeds["orders"] != "live":
                raise ValueError(
                    "Wait for the order-status feed to finish reconciliation before starting Smart selling"
                )
            rules = {
                **self.config["instrument_defaults"],
                **self.config["instruments"].get(symbol, {}),
            }
            quantity_step = Decimal(str(rules["quantity_step"]))
            quantities = {}
            for account in accounts:
                if self.feedback.has_open_orders(account, symbol):
                    raise ValueError(
                        "Existing orders for this account and instrument must reach terminal status first"
                    )
                # Preserve legal explicit targets when holdings include fractional
                # dust, and never combine unsellable fractions across accounts.
                quantities[account] = round_down_to_step(
                    self._available_position(account, symbol), quantity_step
                )
            available = sum(quantities.values(), Decimal(0))
            # quantity has NO UI component: the UI omits it to sell all sellable
            # inventory. Explicit backend targets remain supported.
            try:
                quantity = (
                    available if settings.get("quantity") is None
                    else Decimal(str(settings["quantity"]))
                )
            except InvalidOperation as exc:
                raise ValueError("Quantity must be positive and finite") from exc
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError("Quantity must be positive and finite; no available position to sell")
            if quantity > available:
                raise ValueError("Quantity exceeds the selected accounts' available position")
            # Runtime rules and inventory limits cannot be overridden by an HTTP payload.
            permitted = {
                key: settings[key]
                for key in (
                    "mode",
                    "start_time",
                    "deadline",
                    "minimum_price",
                    "imminent_event",
                )
                if key in settings
            }
            mode = str(settings.get("mode", SMART_SELLING_DEFAULTS["mode"])).lower()
            mode = {"1": "patient", "2": "normal", "3": "urgent"}.get(mode, mode)
            permitted.update(
                # Trusted application settings only; HTTP rate overrides are ignored.
                participation_percentage=self.config["participation_percentages"].get(mode),
                account_id=accounts[0],
                account_ids=accounts,
                account_quantities={account: str(amount) for account, amount in quantities.items()},
                quantity=str(quantity),
                symbol=symbol,
                instrument_rules=rules,
                quote_max_age=self.config["quote_max_age"],
                trade_max_age=self.config["trade_max_age"],
            )
            parent = self.engine.start_parent(permitted)
            self._sync_reporting([parent], parameters=permitted)
            return parent

    @staticmethod
    def _parent_accounts(parent):
        return parent.get("account_ids") or [parent["account_id"]]

    def has_active_session(self):
        # Stopping and reconciling sessions retain ownership until all children resolve.
        return bool(self.engine.list_parents(active_only=True, limit=1))

    def owns_account(self, account_id):
        return any(
            account_id in self._parent_accounts(parent)
            for parent in self.engine.list_parents(active_only=True, limit=1000)
        )

    def owns(self, account_id, symbol):
        return any(
            account_id in self._parent_accounts(parent) and parent["symbol"] == canonical_symbol(symbol)
            for parent in self.engine.list_parents(active_only=True, limit=1000)
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
            self._sync_reporting([self.engine.get_parent(parent_id)])
            return result

    def stop_all(self):
        with self._lock:
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
            self._sync_reporting()

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
            self._sync_reporting()

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
                    for account in self._parent_accounts(parent):
                        balance = parent.get("accounts", {}).get(account, {})
                        if balance and Decimal(balance["remaining"]) <= 0:
                            continue
                        self._available_position(account, parent["symbol"])
                except (ValueError, TypeError):
                    self.engine.set_feed_health(
                        parent["symbol"],
                        False,
                        "account snapshot unavailable or trading disabled",
                    )
            if self.clock() - self._last_forecast >= 30:
                for parent in parents:
                    if parent.get("deadline") is None:
                        continue
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
            self._sync_reporting()

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
            if action["type"] == "submit":
                self._report("register_command", action["parent_id"], dict(command))
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
        finally:
            self._sync_reporting()

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
