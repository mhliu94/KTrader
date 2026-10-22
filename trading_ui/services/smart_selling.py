"""Durable fixed-POV liquidation controller; broker I/O belongs to its caller.

All quantities/prices cross this boundary as Decimal-compatible values and leave
as decimal strings. A terminal report must be authoritative AND contain the final
cumulative fill (``final=True``). A timeout or not-found response never frees an
order. The durable outbox is at-most-once dispatched: ambiguous delivery is
reconciled by client ID, never automatically resubmitted.
"""

from contextlib import contextmanager
from decimal import Decimal, localcontext
import json
import sqlite3
import statistics
import threading
import time
import uuid


from .smart_selling_policy import (
    ZERO,
    PARTICIPATION_RATES,
    SMART_SELLING_DEFAULTS,
    ChildPlan,
    decimal_string,
    meets_order_minimum,
    parse_decimal,
    parse_input_decimal,
    parse_timestamp,
    plan_child_order,
    round_down_to_step,
    round_up_to_step,
    utc_timestamp_string,
)

TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
WORKING_ORDER_STATUSES = {"NEW", "OPEN", "LIVE", "ACCEPTED", "PARTIALLY_FILLED", "PENDING_CANCEL"}


class SmartSellingEngine:
    """Serialize lifecycle transitions and persist one child slot per parent.

    Every saved parent satisfies E = C + R + F + D and 0 <= F + R <= Q0.
    R includes all potentially fillable leaves until authoritative terminal
    evidence arrives. Network I/O happens after transaction commit in callers.
    Pure sizing and pricing live in :mod:`smart_selling_policy`.
    """

    def __init__(
        self, db_path, *, clock=time.monotonic, wall_clock=time.time, max_active_parents=100
    ):
        self.clock = clock
        self.wall_clock = wall_clock
        self.max_active_parents = max_active_parents
        self._lock = threading.RLock()
        self._quotes = {}
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS smart_parents (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
                active INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS smart_active_slot
                ON smart_parents(account_id,symbol) WHERE active=1;
            CREATE TABLE IF NOT EXISTS smart_orders (
                id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS smart_tape (
                parent_id TEXT NOT NULL, event_id TEXT NOT NULL, quantity TEXT NOT NULL,
                rate TEXT NOT NULL, net_quantity TEXT NOT NULL, original_id TEXT,
                PRIMARY KEY(parent_id,event_id));
            CREATE INDEX IF NOT EXISTS smart_tape_original ON smart_tape(event_id);
            CREATE TABLE IF NOT EXISTS smart_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id TEXT, ts REAL NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS smart_market_checkpoints (
                symbol TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS smart_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL,
                dispatched INTEGER NOT NULL DEFAULT 0);
        """)
        with self._transaction():
            # A persisted intent may have reached the broker even if dispatch was
            # interrupted. Never replay a submit after process restart.
            self._db.execute("UPDATE smart_outbox SET dispatched=1 WHERE dispatched=0")
            now = self.clock()
            wall = self.wall_clock()
            for parent in self._active():
                parent["start_mono"] = now + parent["start_wall"] - wall
                parent["deadline_mono"] = now + parent["deadline_wall"] - wall
                if parent["last_send_wall"] is not None:
                    parent["last_send"] = now - max(0, wall - parent["last_send_wall"])
                parent["last_diagnostic"] = None
                if parent["order"]:
                    order = parent["order"]
                    order["send_mono"] = now - max(0, wall - order["send_wall"])
                    order["state"] = "RECONCILE"
                    order["reconcile_cause"] = "restart"
                    order["last_query"] = None
                    order["cancel_intent"] = True
                    order["cancel_at"] = None
                    parent["reason"] = "restart reconciliation required"
                self._save(parent)
                self._event(parent, "restart", {"reserved": parent["R"]})

    @contextmanager
    def _transaction(self):
        with self._lock, localcontext() as arithmetic:
            # Broker quantities have bounded precision; ample guard digits keep
            # decimal ledgers exact even across many fractional tape admissions.
            arithmetic.prec = 80
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def _active(self, symbol=None):
        query = "SELECT data FROM smart_parents WHERE active=1"
        args = ()
        if symbol is not None:
            query += " AND symbol=?"
            args = (symbol,)
        return [json.loads(row[0]) for row in self._db.execute(query, args)]

    def _load(self, parent_id):
        row = self._db.execute("SELECT data FROM smart_parents WHERE id=?", (parent_id,)).fetchone()
        if row is None:
            raise KeyError(parent_id)
        return json.loads(row[0])

    def _save(self, parent):
        """Enforce credit conservation and the parent position bound on every write."""
        earned, credit, reserved, filled, discarded = (
            parse_decimal(parent[k]) for k in ("E", "C", "R", "F", "D")
        )
        if (
            earned != credit + reserved + filled + discarded
            or reserved < 0
            or filled < 0
            or filled + reserved > parse_decimal(parent["quantity"])
        ):
            raise AssertionError("Smart selling ledger invariant violated")
        self._db.execute(
            "INSERT INTO smart_parents(id,account_id,symbol,active,data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET active=excluded.active,data=excluded.data",
            (
                parent["parent_id"],
                parent["account_id"],
                parent["symbol"],
                int(parent["active"]),
                json.dumps(parent),
            ),
        )
        if parent["order"]:
            self._save_order(parent["parent_id"], parent["order"])

    def _save_order(self, parent_id, order):
        self._db.execute(
            "INSERT INTO smart_orders(id,parent_id,data) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (order["client_order_id"], parent_id, json.dumps(order)),
        )

    def _event(self, parent, kind, detail=None):
        detail = dict(detail or {})
        detail.update({key: parent[key] for key in ("E", "C", "R", "F", "D")})
        self._db.execute(
            "INSERT INTO smart_events(parent_id,ts,kind,data) VALUES(?,?,?,?)",
            (parent["parent_id"], self.wall_clock(), kind, json.dumps(detail)),
        )

    def _trim(self, parent):
        excess = max(ZERO, parse_decimal(parent["C"]) - 2 * parse_decimal(parent["child_cap"]))
        if excess:
            parent["C"] = decimal_string(parse_decimal(parent["C"]) - excess)
            parent["D"] = decimal_string(parse_decimal(parent["D"]) + excess)
            self._event(parent, "credit_discarded", {"quantity": decimal_string(excess)})

    def start_parent(self, settings):
        now = self.clock()
        wall = self.wall_clock()
        account = str(settings.get("account_id") or "").strip()
        symbol = str(settings.get("symbol") or "").strip().upper()
        if not account or not symbol:
            raise ValueError("account_id and symbol are required")
        quantity = parse_input_decimal(settings.get("quantity"), "quantity", positive=True)
        mode = str(settings.get("mode", SMART_SELLING_DEFAULTS["mode"])).lower()
        mode = {"1": "patient", "2": "normal", "3": "urgent"}.get(mode, mode)
        if mode not in PARTICIPATION_RATES:
            raise ValueError("mode must be patient, normal, or urgent")
        rules = settings.get("instrument_rules", settings)
        quantity_step = parse_input_decimal(
            rules.get("quantity_step"), "quantity_step", positive=True
        )
        tick_size = parse_input_decimal(rules.get("tick_size"), "tick_size", positive=True)
        minimum_quantity = parse_input_decimal(
            rules.get("min_quantity"), "min_quantity", positive=True
        )
        minimum_quantity = round_up_to_step(minimum_quantity, quantity_step)
        min_notional = parse_input_decimal(
            rules.get("min_notional", 0), "min_notional", nonnegative=True
        )
        if quantity != round_down_to_step(quantity, quantity_step) or quantity < minimum_quantity:
            raise ValueError("quantity must satisfy the instrument minimum and quantity step")
        start = parse_timestamp(settings.get("start", settings.get("start_time")), wall)
        horizon = float(
            parse_input_decimal(
                settings.get("horizon_seconds", SMART_SELLING_DEFAULTS["horizon_seconds"]),
                "horizon_seconds",
                positive=True,
            )
        )
        deadline = parse_timestamp(settings.get("deadline"), start + horizon)
        if deadline <= start or deadline <= wall:
            raise ValueError("deadline must be later than start and current time")
        minimum_price = settings.get("minimum_price")
        minimum_price = (
            decimal_string(parse_input_decimal(minimum_price, "minimum_price", positive=True))
            if minimum_price not in (None, "")
            else None
        )
        child_cap = max(
            minimum_quantity,
            round_down_to_step(
                quantity * Decimal(30) / parse_input_decimal(deadline - start), quantity_step
            ),
        )
        quote_max_age = float(
            parse_input_decimal(
                settings.get("quote_max_age", SMART_SELLING_DEFAULTS["quote_max_age"]),
                "quote_max_age",
                positive=True,
            )
        )
        trade_max_age = float(
            parse_input_decimal(
                settings.get("trade_max_age", SMART_SELLING_DEFAULTS["trade_max_age"]),
                "trade_max_age",
                positive=True,
            )
        )
        parent = {
            "parent_id": str(settings.get("parent_id") or uuid.uuid4().hex),
            "account_id": account,
            "symbol": symbol,
            "quantity": decimal_string(quantity),
            "mode": mode,
            "participation": decimal_string(PARTICIPATION_RATES[mode]),
            "minimum_price": minimum_price,
            "child_cap": decimal_string(child_cap),
            "base_child_cap": decimal_string(child_cap),
            "tick_size": decimal_string(tick_size),
            "quantity_step": decimal_string(quantity_step),
            "min_quantity": decimal_string(minimum_quantity),
            "min_notional": decimal_string(min_notional),
            "time_in_force": str(
                settings.get("time_in_force", SMART_SELLING_DEFAULTS["time_in_force"])
            ).upper(),
            "start_wall": start,
            "deadline_wall": deadline,
            "start_mono": now + start - wall,
            "deadline_mono": now + deadline - wall,
            "last_send": None,
            "last_send_wall": None,
            "E": "0",
            "C": "0",
            "R": "0",
            "F": "0",
            "D": "0",
            "order": None,
            "fallback": False,
            "imminent_event": bool(settings.get("imminent_event", False)),
            "active": True,
            "stop_requested": False,
            "stop_reason": "",
            "hold": "",
            "state": "SCHEDULED" if start > wall else "RUNNING",
            "reason": "waiting for eligible tape volume",
            "quote_max_age": quote_max_age,
            "trade_max_age": trade_max_age,
            "trade_sequence": None,
            "tape_gap": False,
            "remaining_volume_forecast": None,
            "capacity": None,
            "last_diagnostic": None,
            "depth_cap_count": 0,
            "fills_after_cancel": "0",
            "last_tick": None,
            "latencies": {},
        }
        if parent["time_in_force"] not in {"DAY", "GTC"}:
            raise ValueError("Smart selling requires ordinary DAY or GTC limits")
        self._register_parent(parent)
        return self._public(parent)

    def _register_parent(self, parent):
        """Claim the account/instrument slot and persist the initial zero ledger."""
        with self._transaction():
            if len(self._active()) >= self.max_active_parents:
                raise ValueError("maximum active Smart selling parents reached")
            if self._db.execute(
                "SELECT 1 FROM smart_parents WHERE id=?", (parent["parent_id"],)
            ).fetchone():
                raise ValueError("parent_id already exists")
            if self._db.execute(
                "SELECT 1 FROM smart_parents WHERE account_id=? AND symbol=? AND active=1",
                (parent["account_id"], parent["symbol"]),
            ).fetchone():
                raise ValueError(
                    "an active Smart selling parent already owns this account/instrument"
                )
            if self._db.execute(
                "SELECT 1 FROM smart_parents WHERE account_id=? AND symbol=? "
                "AND json_extract(data, '$.hold') != '' LIMIT 1",
                (parent["account_id"], parent["symbol"]),
            ).fetchone():
                raise ValueError("unresolved Smart selling ledger conflict requires reconciliation")
            self._save(parent)
            self._event(
                parent,
                "parent_started",
                {"mode": parent["mode"], "participation": parent["participation"]},
            )

    def on_quote(
        self,
        symbol,
        bid,
        ask,
        bid_size,
        *,
        source_time=None,
        sequence=None,
        healthy=True,
        accessible=True,
        received_at=None,
        reset_sequence=False,
        preserve_consumption=False,
    ):
        symbol = str(symbol).upper()
        now = self.clock() if received_at is None else float(received_at)
        with self._transaction():
            parents = self._active(symbol)
            if not parents:
                return
            old = self._quotes.get(symbol)
            sequence_number = int(sequence) if sequence is not None else None
            if (
                old
                and sequence_number is not None
                and old["sequence"] is not None
                and not reset_sequence
            ):
                if sequence_number <= old["sequence"]:
                    return
                if sequence_number != old["sequence"] + 1:
                    healthy = False
                if old.get("sequence_gap"):
                    healthy = False
            try:
                bid = parse_input_decimal(bid, "bid", positive=True)
                ask = parse_input_decimal(ask, "ask", positive=True)
                size = parse_input_decimal(bid_size, "bid_size", nonnegative=True)
                if ask < bid:
                    healthy = False
            except ValueError:
                bid = ZERO
                ask = ZERO
                size = ZERO
                healthy = False
            source = parse_timestamp(source_time)
            consumed = ZERO
            consumed_wall = None
            if old and old["bid"] == bid and old.get("consumed_wall") is not None:
                if preserve_consumption or source is None or source <= old["consumed_wall"]:
                    consumed = old["consumed"]
                    consumed_wall = old["consumed_wall"]
            self._quotes[symbol] = {
                "bid": bid,
                "ask": ask,
                "bid_size": size,
                "consumed": consumed,
                "consumed_wall": consumed_wall,
                "received_at": now,
                "source_time": source,
                "sequence": sequence_number,
                "healthy": bool(healthy),
                "accessible": bool(accessible),
                "sequence_gap": bool(
                    old
                    and sequence_number is not None
                    and old["sequence"] is not None
                    and not reset_sequence
                    and (sequence_number != old["sequence"] + 1 or old.get("sequence_gap"))
                ),
                "version": (old["version"] + 1) if old else 1,
            }
            if healthy and accessible and bid > 0:
                for parent in parents:
                    price = round_up_to_step(
                        max(
                            bid,
                            (
                                parse_decimal(parent["minimum_price"])
                                if parent["minimum_price"]
                                else ZERO
                            ),
                        ),
                        parse_decimal(parent["tick_size"]),
                    )
                    legal_minimum = max(
                        parse_decimal(parent["min_quantity"]),
                        round_up_to_step(
                            parse_decimal(parent["min_notional"]) / price,
                            parse_decimal(parent["quantity_step"]),
                        ),
                    )
                    parent["child_cap"] = decimal_string(
                        max(
                            parse_decimal(parent.get("base_child_cap", parent["child_cap"])),
                            legal_minimum,
                        )
                    )
                    self._trim(parent)
                    if source is not None:
                        self._latency(parent, "feed", max(0, self.wall_clock() - source))
                    self._save(parent)

    def set_feed_health(self, symbol, healthy, reason="", *, reset_sequence=False):
        symbol = str(symbol).upper()
        with self._transaction():
            quote = self._quotes.get(symbol)
            if quote:
                quote["healthy"] = bool(healthy)
                if healthy:
                    quote["received_at"] = self.clock()
                if reset_sequence:
                    quote["sequence_gap"] = False
            for parent in self._active(symbol):
                if reset_sequence:
                    parent["tape_gap"] = False
                    parent["trade_sequence"] = None
                if not healthy:
                    parent["reason"] = reason or "feed disconnected"
                    if parent["order"]:
                        parent["order"]["cancel_intent"] = True
                self._save(parent)
                self._event(parent, "feed_health", {"healthy": bool(healthy), "reason": reason})

    def on_trade(
        self,
        symbol,
        quantity,
        event_id,
        *,
        source_time=None,
        sequence=None,
        correction_of=None,
        healthy=True,
        received_at=None,
        interval_start=None,
    ):
        """Admit each tape event once; corrections keep their original rate."""
        symbol = str(symbol).upper()
        event_id = str(event_id or "")
        if not event_id:
            raise ValueError("a stable tape event_id is required")
        quantity = parse_input_decimal(quantity, "trade quantity")
        if correction_of is None and quantity <= 0:
            raise ValueError("trade quantity must be positive")
        now = self.clock() if received_at is None else float(received_at)
        wall = self.wall_clock()
        source = parse_timestamp(source_time)
        interval_start = parse_timestamp(interval_start)
        with self._transaction():
            for parent in self._tape_parents(symbol, correction_of):
                duplicate = self._db.execute(
                    "SELECT 1 FROM smart_tape WHERE parent_id=? AND event_id=?",
                    (parent["parent_id"], event_id),
                ).fetchone()
                if duplicate:
                    continue
                if correction_of is None:
                    rate = self._new_trade_rate(
                        parent,
                        event_id,
                        now=now,
                        wall=wall,
                        source=source,
                        sequence=sequence,
                        healthy=healthy,
                        interval_start=interval_start,
                    )
                else:
                    rate = self._correction_rate(parent, event_id, correction_of, quantity)
                if rate is not None:
                    self._credit_tape(
                        parent,
                        event_id,
                        quantity,
                        rate,
                        correction_of=correction_of,
                        source=source,
                        now=now,
                    )

    def _tape_parents(self, symbol, correction_of):
        parents = self._active(symbol)
        if correction_of is None:
            return parents
        # A final parent's historical admission may still receive a correction.
        found = self._db.execute(
            "SELECT DISTINCT parent_id FROM smart_tape " "WHERE event_id=? AND original_id IS NULL",
            (str(correction_of),),
        ).fetchall()
        historical = {parent["parent_id"]: parent for parent in parents}
        for row in found:
            parent = self._load(row[0])
            if parent["symbol"] == symbol:
                historical[parent["parent_id"]] = parent
        return list(historical.values())

    def _new_trade_rate(
        self, parent, event_id, *, now, wall, source, sequence, healthy, interval_start
    ):
        """Exclude stale/pre-start volume and retain unresolved sequence gaps."""
        if parent["stop_requested"] or now < parent["start_mono"] or now >= parent["deadline_mono"]:
            return None
        if interval_start is not None and interval_start < parent["start_wall"]:
            self._event(
                parent,
                "tape_interval_excluded",
                {
                    "event_id": event_id,
                    "interval_start": interval_start,
                },
            )
            return None
        invalid_source = source is not None and (
            wall - source > parent["trade_max_age"]
            or source > wall + 1
            or source < parent["start_wall"]
        )
        if not healthy or invalid_source:
            parent["reason"] = "stale or unhealthy tape event excluded"
            self._save(parent)
            self._event(parent, "tape_excluded", {"event_id": event_id})
            return None
        if sequence is not None:
            sequence_number = int(sequence)
            previous_sequence = parent["trade_sequence"]
            if previous_sequence is not None and sequence_number <= previous_sequence:
                return None
            if previous_sequence is not None and sequence_number != previous_sequence + 1:
                parent["tape_gap"] = True
            parent["trade_sequence"] = sequence_number
        if parent["tape_gap"]:
            parent["reason"] = "tape sequence gap requires reconciliation"
            self._save(parent)
            return None
        return parse_decimal(parent["participation"])

    def _correction_rate(self, parent, event_id, correction_of, quantity):
        """Update the original admission's net volume or hold on missing proof."""
        original = self._db.execute(
            "SELECT rate,net_quantity FROM smart_tape "
            "WHERE parent_id=? AND event_id=? AND original_id IS NULL",
            (parent["parent_id"], str(correction_of)),
        ).fetchone()
        if original is None:
            parent["hold"] = "unknown tape correction requires reconciliation"
            self._save(parent)
            self._event(
                parent,
                "correction_unknown",
                {
                    "event_id": event_id,
                    "original_id": str(correction_of),
                },
            )
            return None
        corrected_volume = parse_decimal(original["net_quantity"]) + quantity
        if corrected_volume < 0:
            parent["hold"] = "invalid tape correction requires reconciliation"
            self._save(parent)
            return None
        self._db.execute(
            "UPDATE smart_tape SET net_quantity=? WHERE parent_id=? AND event_id=?",
            (decimal_string(corrected_volume), parent["parent_id"], str(correction_of)),
        )
        return parse_decimal(original["rate"])

    def _credit_tape(self, parent, event_id, quantity, rate, *, correction_of, source, now):
        """Persist dedup identity and both sides of E = C + R + F + D together."""
        original_id = str(correction_of) if correction_of is not None else None
        net_quantity = quantity if correction_of is None else ZERO
        self._db.execute(
            "INSERT INTO smart_tape(parent_id,event_id,quantity,rate,net_quantity,original_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                parent["parent_id"],
                event_id,
                decimal_string(quantity),
                decimal_string(rate),
                decimal_string(net_quantity),
                original_id,
            ),
        )
        earned = rate * quantity
        parent["E"] = decimal_string(parse_decimal(parent["E"]) + earned)
        parent["C"] = decimal_string(parse_decimal(parent["C"]) + earned)
        self._trim(parent)
        self._save(parent)
        self._event(
            parent,
            "tape_correction" if correction_of is not None else "tape_credit",
            {
                "event_id": event_id,
                "quantity": decimal_string(quantity),
                "rate": decimal_string(rate),
                "source_time": source,
                "received_at": now,
            },
        )

    def _action_dict(self, parent, kind):
        order = parent["order"]
        action = {
            "type": kind,
            "parent_id": parent["parent_id"],
            "account_id": parent["account_id"],
            "symbol": parent["symbol"],
            "client_order_id": order["client_order_id"],
            "broker_order_id": order["broker_order_id"],
        }
        if kind == "submit":
            action.update(
                {
                    "quantity": order["quantity"],
                    "limit_price": order["limit_price"],
                    "side": "SELL",
                    "order_type": "LIMIT",
                    "time_in_force": parent["time_in_force"],
                    "send_time": utc_timestamp_string(order["send_wall"]),
                    "send_timestamp": order["send_wall"],
                    "deadline": utc_timestamp_string(parent["deadline_wall"]),
                }
            )
        elif kind == "query":
            action["include_open_orders_and_executions"] = True
        return action

    def _action(self, parent, kind):
        action = self._action_dict(parent, kind)
        self._db.execute("INSERT INTO smart_outbox(data) VALUES(?)", (json.dumps(action),))
        self._event(parent, kind + "_intent", action)

    def prepare_dispatch(self, client_order_id):
        """Atomically validate an unsent intent just before its FIRST handoff.

        The caller must use this result for exactly one network submission and
        must never dispatch an intent without this gate. Preparation is durable;
        a lost/failed handoff thereafter is ambiguous and requires reconciliation.
        A restart never replays a prepared or unprepared persisted submission.
        """
        now = self.clock()
        with self._transaction():
            row = self._db.execute(
                "SELECT parent_id FROM smart_orders WHERE id=?", (str(client_order_id),)
            ).fetchone()
            if row is None:
                return None
            parent = self._load(row[0])
            order = parent["order"]
            if (
                not parent["active"]
                or order is None
                or order["client_order_id"] != str(client_order_id)
                or order.get("dispatch_prepared")
                or order["ack_at"] is not None
                or order["accepted"]
                or order["sequence"] is not None
                or order["broker_order_id"] is not None
                or parse_decimal(order["cumulative_filled"]) != 0
            ):
                return None
            if order["state"] == "RECONCILE" and order.get("reconcile_cause") == "timer":
                self._abort_unsent(
                    parent, order, "submission queue exceeded acknowledgement wait", now
                )
                return None
            if order["state"] != "SUBMIT_PENDING":
                return None
            self._finish_if_needed(parent, now)
            reason = (
                parent["stop_reason"]
                if parent["stop_requested"]
                else parent["hold"] or self._quote_reason(parent, now)
            )
            if not reason and parse_decimal(parent["C"]) < 0:
                reason = "negative credit before dispatch"
            if not reason:
                quote = self._quotes[parent["symbol"]]
                old_quantity = parse_decimal(order["quantity"])
                step = parse_decimal(parent["quantity_step"])
                other_reserved = parse_decimal(parent["R"]) - old_quantity
                remaining = (
                    parse_decimal(parent["quantity"]) - parse_decimal(parent["F"]) - other_reserved
                )
                depth = max(ZERO, quote["bid_size"] - quote["consumed"]) * Decimal(".10")
                quantity = round_down_to_step(
                    min(old_quantity, parse_decimal(parent["child_cap"]), remaining, depth), step
                )
                floor = parse_decimal(parent["minimum_price"]) if parent["minimum_price"] else ZERO
                price = round_up_to_step(
                    max(parse_decimal(order["limit_price"]), quote["bid"], floor),
                    parse_decimal(parent["tick_size"]),
                )
                aggressive = (
                    order["aggressive"] or parse_decimal(order["limit_price"]) <= quote["bid"]
                )
                age_limit = 2 if aggressive else 5
                if not meets_order_minimum(parent, quantity, price):
                    reason = "refreshed depth below broker minimum before dispatch"
                elif now - order["send_mono"] >= age_limit:
                    reason = "submission queue exceeded child cancellation age"
                else:
                    refund = old_quantity - quantity
                    parent["C"] = decimal_string(parse_decimal(parent["C"]) + refund)
                    parent["R"] = decimal_string(parse_decimal(parent["R"]) - refund)
                    order.update(
                        quantity=decimal_string(quantity),
                        limit_price=decimal_string(price),
                        aggressive=bool(aggressive),
                        age_limit=age_limit,
                        dispatch_prepared=True,
                        quote_version=quote["version"],
                        quote_bid=decimal_string(quote["bid"]),
                        quote_ask=decimal_string(quote["ask"]),
                        quote_bid_size=decimal_string(quote["bid_size"]),
                        quote_source_time=quote["source_time"],
                        quote_received_at=quote["received_at"],
                    )
                    self._trim(parent)
                    self._save(parent)
                    action = self._action_dict(parent, "submit")
                    self._event(parent, "submit_dispatch_prepared", action)
                    return action
            self._abort_unsent(parent, order, reason, now)
            return None

    def _abort_unsent(self, parent, order, reason, now, *, stop_parent=False):
        # Callers have proved that this child was never handed to the network.
        # Broker timeouts and ambiguous sends must never use this release path.
        refund = parse_decimal(order["quantity"])
        parent["C"] = decimal_string(parse_decimal(parent["C"]) + refund)
        parent["R"] = decimal_string(parse_decimal(parent["R"]) - refund)
        order.update(state="TERMINAL", terminal_at=now, terminal_source="LOCAL_NOT_DISPATCHED")
        self._save_order(parent["parent_id"], order)
        parent["order"] = None
        parent["state"] = "HOLD"
        parent["reason"] = str(reason)
        if stop_parent:
            parent["stop_requested"] = True
            parent["stop_reason"] = str(reason)
        self._trim(parent)
        self._finish_if_needed(parent, now)
        self._save(parent)
        self._event(
            parent,
            "submit_not_dispatched",
            {
                "client_order_id": order["client_order_id"],
                "reason": str(reason),
                "refund": decimal_string(refund),
            },
        )

    def abort_dispatch(self, client_order_id, reason, *, stop_parent=False):
        """Release a child the caller PROVES has never been handed off.

        May follow prepare_dispatch if an independent local check fails before
        the network call. Never use after an attempted publish, a timeout, or a
        transport error: delivery then is uncertain, and reservations must stay.
        """
        with self._transaction():
            row = self._db.execute(
                "SELECT parent_id FROM smart_orders WHERE id=?", (str(client_order_id),)
            ).fetchone()
            if row is None:
                return False
            parent = self._load(row[0])
            order = parent["order"]
            if (
                not parent["active"]
                or order is None
                or order["client_order_id"] != str(client_order_id)
                or order["state"] != "SUBMIT_PENDING"
                or order["ack_at"] is not None
                or order["accepted"]
                or order["sequence"] is not None
                or order["broker_order_id"] is not None
                or parse_decimal(order["cumulative_filled"]) != 0
            ):
                return False
            self._abort_unsent(parent, order, reason, self.clock(), stop_parent=stop_parent)
            return True

    def hold_order(self, client_order_id, reason):
        """Quarantine a conflicting active or archived child's account/symbol."""
        with self._transaction():
            row = self._db.execute(
                "SELECT parent_id FROM smart_orders WHERE id=?", (str(client_order_id),)
            ).fetchone()
            if row is None:
                return False
            self._hold_conflict(self._load(row[0]), str(reason))
            return True

    def drain_actions(self):
        with self._transaction():
            rows = self._db.execute(
                "SELECT id,data FROM smart_outbox WHERE dispatched=0 ORDER BY id"
            ).fetchall()
            self._db.execute("UPDATE smart_outbox SET dispatched=1 WHERE dispatched=0")
            return [dict(json.loads(row["data"]), action_id=row["id"]) for row in rows]

    def _query(self, parent, now):
        order = parent["order"]
        if order["last_query"] is None or now - order["last_query"] >= 5:
            order["last_query"] = now
            self._action(parent, "query")

    def _cancel(self, parent, now):
        order = parent["order"]
        order["cancel_intent"] = True
        if not order["broker_order_id"] or order["cancel_at"] is not None:
            return
        if order["last_cancel"] is not None and now - order["last_cancel"] < 5:
            return
        order["cancel_at"] = now
        order["last_cancel"] = now
        order["cancel_requested_ever"] = True
        order["state"] = "CANCEL_PENDING"
        self._action(parent, "cancel")

    def on_action_error(self, client_order_id, action, error, *, definitive=False, now=None):
        # Even a transport's "definitive" error is not terminal broker evidence.
        now = self.clock() if now is None else float(now)
        with self._transaction():
            row = self._db.execute(
                "SELECT parent_id FROM smart_orders WHERE id=?", (client_order_id,)
            ).fetchone()
            if row is None:
                return
            parent = self._load(row[0])
            order = parent["order"]
            if order is None or order["client_order_id"] != client_order_id:
                return
            order["state"] = "RECONCILE"
            order["reconcile_cause"] = "transport"
            if action == "cancel":
                order["cancel_failed"] = True
            parent["reason"] = f"{action} uncertain: {str(error)[:240]}"
            self._event(parent, "action_error", {"action": action, "message": str(error)[:500]})
            self._query(parent, now)
            self._save(parent)

    def on_order_report(
        self,
        client_order_id,
        status,
        cumulative_filled,
        *,
        broker_order_id=None,
        sequence=None,
        authoritative=True,
        final=False,
        now=None,
    ):
        """Book cumulative increases once; release leaves only on proven finality."""
        now = self.clock() if now is None else float(now)
        status = str(status).upper()
        cumulative = parse_input_decimal(cumulative_filled, "cumulative_filled", nonnegative=True)
        with self._transaction():
            row = self._db.execute(
                "SELECT parent_id,data FROM smart_orders WHERE id=?",
                (str(client_order_id),),
            ).fetchone()
            if row is None:
                return
            parent = self._load(row["parent_id"])
            order = parent["order"]
            if order is None or order["client_order_id"] != str(client_order_id):
                archived = json.loads(row["data"])
                self._check_archived_report(
                    parent, archived, status, cumulative, sequence, authoritative
                )
                return
            if broker_order_id is not None:
                broker_id = str(broker_order_id)
                if order["broker_order_id"] not in (None, broker_id):
                    self._hold_conflict(parent, "broker order ID conflict requires reconciliation")
                    return
                order["broker_order_id"] = broker_id
            if not authoritative:
                order["state"] = "RECONCILE"
                order["reconcile_cause"] = "order_report"
                self._query(parent, now)
                self._save(parent)
                return

            sequence_number = int(sequence) if sequence is not None else None
            if not self._accept_cumulative_report(parent, order, cumulative, sequence_number):
                return
            self._book_cumulative_fill(parent, order, cumulative)
            if sequence_number is not None:
                order["sequence"] = sequence_number
            self._acknowledge_order(parent, order, status, cumulative, now)
            if status == "FILLED" and cumulative != parse_decimal(order["quantity"]):
                # A fully-filled status with incomplete cumulative quantity is
                # contradictory, not proof that the reservation remainder died.
                final = False
            if status in TERMINAL_ORDER_STATUSES and final:
                self._apply_terminal_report(parent, order, status, cumulative, now)
            elif status in TERMINAL_ORDER_STATUSES or status in {"UNKNOWN", "NOT_FOUND"}:
                order["state"] = "RECONCILE"
                order["reconcile_cause"] = "order_report"
                parent["reason"] = "authoritative final cumulative fill required"
                self._query(parent, now)
            elif status in WORKING_ORDER_STATUSES:
                self._apply_working_report(parent, order, now)
            self._save(parent)
            self._event(
                parent,
                "order_report",
                {
                    "client_order_id": client_order_id,
                    "status": status,
                    "cumulative_filled": decimal_string(cumulative),
                    "sequence": sequence,
                    "final": bool(final),
                },
            )

    def _check_archived_report(self, parent, archived, status, cumulative, sequence, authoritative):
        """An archived duplicate cannot release or replace a newer child slot."""
        previous_cumulative = parse_decimal(archived["cumulative_filled"])
        if not authoritative:
            return
        previous_sequence = archived.get("sequence")
        newer_report = (
            sequence is not None
            and previous_sequence is not None
            and int(sequence) > previous_sequence
        )
        working_again = status in WORKING_ORDER_STATUSES or status == "PENDING_NEW"
        if newer_report and working_again:
            # Even with unchanged fills, newer working evidence contradicts the
            # terminal leaves refund. Quarantine any newer child in this slot.
            self._hold_conflict(parent, "archived order reported working after terminal status")
            return
        if cumulative == previous_cumulative:
            return
        # Reordered smaller snapshots are harmless. An increase after finality,
        # or a newer execution bust, makes the account/instrument uncertain.
        if cumulative > previous_cumulative or newer_report:
            self._hold_conflict(parent, "conflicting archived fill requires reconciliation")

    def _accept_cumulative_report(self, parent, order, cumulative, sequence_number):
        previous_cumulative = parse_decimal(order["cumulative_filled"])
        previous_sequence = order["sequence"]
        if (
            sequence_number is not None
            and previous_sequence is not None
            and sequence_number <= previous_sequence
        ):
            if cumulative > previous_cumulative:
                self._hold_conflict(parent, "conflicting reordered fill requires reconciliation")
            return False
        if cumulative < previous_cumulative:
            if (
                sequence_number is not None
                and previous_sequence is not None
                and sequence_number > previous_sequence
            ):
                self._hold_conflict(parent, "execution bust requires reconciliation")
            return False
        if cumulative > parse_decimal(order["quantity"]):
            self._hold_conflict(parent, "cumulative fill exceeds child quantity")
            return False
        return True

    def _book_cumulative_fill(self, parent, order, cumulative):
        """Move only the new confirmed fill from reservation R to fills F."""
        delta = cumulative - parse_decimal(order["cumulative_filled"])
        if not delta:
            return
        parent["F"] = decimal_string(parse_decimal(parent["F"]) + delta)
        parent["R"] = decimal_string(parse_decimal(parent["R"]) - delta)
        order["cumulative_filled"] = decimal_string(cumulative)
        if order["cancel_requested_ever"] or order["cancel_at"] is not None:
            parent["fills_after_cancel"] = decimal_string(
                parse_decimal(parent["fills_after_cancel"]) + delta
            )
        quote = self._quotes.get(parent["symbol"])
        if quote and order["aggressive"] and quote["bid"] == parse_decimal(order["quote_bid"]):
            quote["consumed"] += delta
            quote["consumed_wall"] = self.wall_clock()

    def _acknowledge_order(self, parent, order, status, cumulative, now):
        order["accepted"] = (
            order["accepted"]
            or status in WORKING_ORDER_STATUSES
            or status in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED"}
            or cumulative > 0
        )
        if order["ack_at"] is None and order["accepted"]:
            order["ack_at"] = now
            latency = max(0, now - order["send_mono"])
            self._latency(parent, "send_to_ack", latency)
            self._event(parent, "ack", {"latency_seconds": latency})

    def _apply_terminal_report(self, parent, order, status, cumulative, now):
        """Atomically archive final fills, refund verified leaves, and free slot."""
        refund = parse_decimal(order["quantity"]) - cumulative
        parent["R"] = decimal_string(parse_decimal(parent["R"]) - refund)
        parent["C"] = decimal_string(parse_decimal(parent["C"]) + refund)
        if status == "REJECTED":
            parent["stop_requested"] = True
            parent["stop_reason"] = "broker rejected child; review required"
        elif order["accepted"]:
            parent["fallback"] = not (order["aggressive"] or refund == 0)
        order["state"] = "TERMINAL"
        order["terminal_at"] = now
        cancel_latency = None
        if order["last_cancel"] is not None:
            cancel_latency = max(0, now - order["last_cancel"])
            self._latency(parent, "cancel_to_terminal", cancel_latency)
        self._save_order(parent["parent_id"], order)
        parent["order"] = None
        self._trim(parent)
        self._finish_if_needed(parent, now)
        self._event(
            parent,
            "terminal",
            {
                "client_order_id": order["client_order_id"],
                "status": status,
                "cumulative_filled": decimal_string(cumulative),
                "refund": decimal_string(refund),
                "cancel_to_terminal": cancel_latency,
            },
        )

    def _apply_working_report(self, parent, order, now):
        if order["state"] == "RECONCILE":
            # A fresh authoritative working snapshot permits a throttled retry
            # of a failed/lost cancellation, never a replacement child.
            if (
                order["cancel_intent"]
                and order["last_cancel"] is not None
                and now - order["last_cancel"] >= 5
            ):
                order["cancel_at"] = None
        if order["cancel_at"] is None:
            order["state"] = "LIVE"
        if order["cancel_intent"] or now - order["send_mono"] >= order["age_limit"]:
            self._cancel(parent, now)

    def _hold_conflict(self, parent, reason):
        parent["hold"] = reason
        if parent["active"]:
            parent["state"] = "STOPPING" if parent["stop_requested"] else "HOLD"
        if parent["order"]:
            parent["order"]["cancel_intent"] = True
            parent["order"]["state"] = "RECONCILE"
            parent["order"]["reconcile_cause"] = "conflict"
        self._save(parent)
        self._event(parent, "order_conflict", {"reason": reason})
        for current in self._active(parent["symbol"]):
            if (
                current["parent_id"] != parent["parent_id"]
                and current["account_id"] == parent["account_id"]
            ):
                current["hold"] = reason
                if current["order"]:
                    current["order"]["cancel_intent"] = True
                self._save(current)

    def request_stop(self, parent_id, *, now=None, reason="manual stop"):
        now = self.clock() if now is None else float(now)
        with self._transaction():
            parent = self._load(parent_id)
            parent["stop_requested"] = True
            parent["stop_reason"] = str(reason)
            if parent["order"]:
                self._cancel(parent, now)
            self._finish_if_needed(parent, now)
            self._save(parent)
            self._event(parent, "stop_requested", {"reason": reason})
            return self._public(parent)

    def set_remaining_volume_forecast(self, parent_id, quantity):
        """Store a derived diagnostic estimate without external quantity precision limits."""
        with self._transaction():
            parent = self._load(parent_id)
            parent["remaining_volume_forecast"] = (
                decimal_string(parse_decimal(quantity, "remaining volume", nonnegative=True))
                if quantity is not None
                else None
            )
            parent["last_diagnostic"] = None
            self._save(parent)

    def _finish_if_needed(self, parent, now):
        if now >= parent["deadline_mono"]:
            parent["stop_requested"] = True
            parent["stop_reason"] = "deadline reached"
        if parent["order"]:
            if parent["stop_requested"]:
                parent["state"] = "STOPPING"
                parent["reason"] = parent["stop_reason"]
            return
        if parse_decimal(parent["F"]) == parse_decimal(parent["quantity"]):
            parent["state"] = "COMPLETED"
            parent["reason"] = "parent fully filled"
            parent["active"] = False
        elif parent["stop_requested"]:
            parent["state"] = (
                "DEADLINE" if parent["stop_reason"] == "deadline reached" else "STOPPED"
            )
            parent["reason"] = parent["stop_reason"]
            parent["active"] = False

    def _quote_reason(self, parent, now):
        quote = self._quotes.get(parent["symbol"])
        if quote is None:
            return "market quote unavailable"
        if quote["sequence_gap"] or parent["tape_gap"]:
            return "market data sequence gap requires reconciliation"
        if not quote["healthy"] or not quote["accessible"]:
            return "market feed unhealthy or depth inaccessible"
        if now - quote["received_at"] > parent["quote_max_age"]:
            return "market quote heartbeat stale"
        source = quote["source_time"]
        if source is not None and (
            self.wall_clock() - source > parent["quote_max_age"] or source > self.wall_clock() + 1
        ):
            return "market quote source timestamp stale"
        return ""

    @staticmethod
    def _latency(parent, kind, seconds):
        metrics = parent.setdefault("latencies", {})
        metric = metrics.setdefault(kind, {"count": 0, "samples": [], "observed_max": 0})
        metric["count"] += 1
        metric["samples"] = (metric["samples"] + [seconds])[-128:]
        metric["observed_max"] = max(metric["observed_max"], seconds)
        metric["median"] = statistics.median(metric["samples"])
        metric["sample_window"] = 128

    def _diagnostic(self, parent, now):
        if parent["last_diagnostic"] is not None and now - parent["last_diagnostic"] < 30:
            return
        remaining = parse_decimal(parent["quantity"]) - parse_decimal(parent["F"])
        unfunded = max(ZERO, remaining - parse_decimal(parent["C"]) - parse_decimal(parent["R"]))
        forecast = parent["remaining_volume_forecast"]
        scenarios = None
        if forecast is not None:
            rate = parse_decimal(parent["participation"])
            scenarios = {
                label: {
                    "eligible_volume": decimal_string(parse_decimal(forecast) * factor),
                    "credit_shortfall": unfunded > rate * parse_decimal(forecast) * factor,
                }
                for label, factor in (
                    ("base", Decimal(1)),
                    ("75_percent", Decimal(".75")),
                    ("50_percent", Decimal(".5")),
                )
            }
        parent["capacity"] = {
            "remaining": decimal_string(remaining),
            "unfunded": decimal_string(unfunded),
            "required_future_volume": decimal_string(
                unfunded / parse_decimal(parent["participation"])
            ),
            "scenarios": scenarios,
            "forecast_available": forecast is not None,
            "depth_cap_count": parent["depth_cap_count"],
            "loop_lag_seconds": (
                max(0, now - parent["last_tick"] - 1) if parent["last_tick"] is not None else 0
            ),
        }
        parent["last_diagnostic"] = now
        self._event(parent, "capacity", parent["capacity"])

    def tick(self, now=None):
        """Service recovery first, then reserve at most one child per parent."""
        now = self.clock() if now is None else float(now)
        with self._transaction():
            for parent in self._active():
                self._tick_parent(parent, now)
            active_symbols = {parent["symbol"] for parent in self._active()}
            self._quotes = {
                symbol: quote for symbol, quote in self._quotes.items() if symbol in active_symbols
            }
        return self.drain_actions()

    def _tick_parent(self, parent, now):
        if parent["last_tick"] is not None:
            self._latency(parent, "loop_lag", max(0, now - parent["last_tick"] - 1))
        self._diagnostic(parent, now)
        parent["last_tick"] = now
        self._finish_if_needed(parent, now)

        if parent["order"]:
            self._service_working_order(parent, now)
        elif parent["active"]:
            plan = self._plan_child(parent, now)
            if plan is not None:
                self._reserve_child(parent, plan, now)
                return
        self._save(parent)

    def _service_working_order(self, parent, now):
        """Timers request cancellation/query; they never release reservation."""
        order = parent["order"]
        quote_invalid = self._quote_reason(parent, now)
        cancellation_due = now - order["send_mono"] >= order["age_limit"]
        if parent["stop_requested"] or parent["hold"] or quote_invalid or cancellation_due:
            self._cancel(parent, now)
        if order["cancel_at"] is not None:
            order["cancel_requested_ever"] = True

        submit_unresolved = order["ack_at"] is None and now - order["send_mono"] >= 3
        cancel_unresolved = order["cancel_at"] is not None and now - order["cancel_at"] >= 3
        if submit_unresolved or cancel_unresolved or order["state"] == "RECONCILE":
            if order["state"] != "RECONCILE":
                order["reconcile_cause"] = "timer"
            order["state"] = "RECONCILE"
            self._query(parent, now)
        if parent["stop_requested"]:
            return
        if parent["hold"] or order["state"] == "RECONCILE":
            parent["state"] = "HOLD"
        else:
            parent["state"] = "RUNNING"
        if parent["hold"]:
            parent["reason"] = parent["hold"]
        elif order["state"] == "RECONCILE":
            parent["reason"] = "order reconciliation required"
        else:
            parent["reason"] = "waiting for child terminal status"

    def _plan_child(self, parent, now):
        """Apply lifecycle gates before asking the pure policy for a proposal."""
        if now < parent["start_mono"]:
            parent["state"] = "SCHEDULED"
            parent["reason"] = "waiting for parent start"
            return None
        reason = parent["hold"] or self._quote_reason(parent, now)
        if reason or parse_decimal(parent["C"]) < 0:
            parent["state"] = "HOLD"
            parent["reason"] = reason or "negative credit after tape correction"
            return None
        parent["state"] = "RUNNING"
        if parent["last_send"] is not None and now - parent["last_send"] < 5:
            parent["reason"] = "minimum five-second send spacing"
            return None

        plan = plan_child_order(parent, self._quotes[parent["symbol"]], now)
        if plan.depth_limited:
            parent["depth_cap_count"] += 1
        if plan.quantity is None:
            parent["reason"] = plan.reason
            return None
        return plan

    def _reserve_child(self, parent, plan: ChildPlan, now):
        """Move C to R and durably record the unique intent before dispatch."""
        quantity = plan.quantity
        assert quantity is not None
        quote = self._quotes[parent["symbol"]]
        wall = self.wall_clock()
        parent["C"] = decimal_string(parse_decimal(parent["C"]) - quantity)
        parent["R"] = decimal_string(parse_decimal(parent["R"]) + quantity)
        parent["last_send"] = now
        parent["last_send_wall"] = wall
        parent["order"] = {
            "client_order_id": "smart-" + uuid.uuid4().hex,
            "broker_order_id": None,
            "state": "SUBMIT_PENDING",
            "dispatch_prepared": False,
            "reconcile_cause": None,
            "quantity": decimal_string(quantity),
            "limit_price": decimal_string(plan.limit_price),
            "cumulative_filled": "0",
            "sequence": None,
            "aggressive": plan.aggressive,
            "age_limit": 2 if plan.aggressive else 5,
            "send_mono": now,
            "send_wall": wall,
            "ack_at": None,
            "accepted": False,
            "cancel_intent": False,
            "cancel_at": None,
            "last_cancel": None,
            "last_query": None,
            "cancel_failed": False,
            "cancel_requested_ever": False,
            "quote_version": quote["version"],
            "quote_bid": decimal_string(quote["bid"]),
            "quote_ask": decimal_string(quote["ask"]),
            "quote_bid_size": decimal_string(quote["bid_size"]),
            "quote_source_time": quote["source_time"],
            "quote_received_at": quote["received_at"],
        }
        if plan.aggressive and plan.limit_price > quote["bid"]:
            parent["reason"] = "minimum price blocks immediate execution"
        elif plan.aggressive:
            parent["reason"] = "aggressive child submitted"
        else:
            parent["reason"] = "passive child submitted"
        self._save(parent)
        self._action(parent, "submit")

    def _public(self, parent):
        return {
            "parent_id": parent["parent_id"],
            "account_id": parent["account_id"],
            "symbol": parent["symbol"],
            "quantity": parent["quantity"],
            "mode": parent["mode"],
            "participation": parent["participation"],
            "filled": parent["F"],
            "remaining": decimal_string(
                parse_decimal(parent["quantity"]) - parse_decimal(parent["F"])
            ),
            "reserved": parent["R"],
            "free_credit": parent["C"],
            "earned": parent["E"],
            "discarded": parent["D"],
            "state": parent["state"],
            "reason": parent["hold"] or parent["reason"],
            "active": parent["active"],
            "start": utc_timestamp_string(parent["start_wall"]),
            "deadline": utc_timestamp_string(parent["deadline_wall"]),
            "minimum_price": parent["minimum_price"],
            "child_cap": parent["child_cap"],
            "fallback": parent["fallback"],
            "active_order": parent["order"],
            "order_state": parent["order"]["state"] if parent["order"] else "IDLE",
            "capacity": parent["capacity"],
            "fills_after_cancel": parent["fills_after_cancel"],
            "latencies": {
                kind: {k: v for k, v in metric.items() if k != "samples"}
                for kind, metric in parent.get("latencies", {}).items()
            },
            "residual_provisional": parent["order"] is not None or bool(parent["hold"]),
        }

    def get_market_checkpoint(self, symbol):
        """Read an adapter's durable feed/counter checkpoint."""
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM smart_market_checkpoints WHERE symbol=?",
                (str(symbol).strip().upper(),),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def set_market_checkpoint(self, symbol, checkpoint):
        """Persist conservative watermarks BEFORE admitting associated volume.

        The adapter owns the schema. Persist-before-admission means a crash can
        cause a conservative hold, never hide a correction to credited volume.
        """
        if not isinstance(checkpoint, dict):
            raise ValueError("market checkpoint must be an object")
        data = json.dumps(checkpoint, allow_nan=False)
        if len(data) > 16384:
            raise ValueError("market checkpoint exceeds size limit")
        with self._transaction():
            self._db.execute(
                "INSERT INTO smart_market_checkpoints(symbol,data) VALUES(?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET data=excluded.data",
                (str(symbol).strip().upper(), data),
            )

    def get_order_context(self, client_order_id):
        """Read persisted child identity, including archived terminal children."""
        with self._lock:
            row = self._db.execute(
                "SELECT parent_id,data FROM smart_orders WHERE id=?", (str(client_order_id),)
            ).fetchone()
            if row is None:
                return None
            parent = self._load(row["parent_id"])
            order = json.loads(row["data"])
            return {
                "parent_id": parent["parent_id"],
                "account_id": parent["account_id"],
                "symbol": parent["symbol"],
                "quantity": order["quantity"],
                "limit_price": order["limit_price"],
                "broker_order_id": order["broker_order_id"],
                "client_order_id": order["client_order_id"],
                "sequence": order["sequence"],
                "cumulative_filled": order["cumulative_filled"],
                "state": order["state"],
                "send_time": utc_timestamp_string(order["send_wall"]),
                "active": bool(
                    parent["active"]
                    and parent["order"]
                    and parent["order"]["client_order_id"] == order["client_order_id"]
                ),
            }

    def find_order_by_broker_id(self, account_id, broker_order_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT child.id FROM smart_orders child "
                "JOIN smart_parents parent ON parent.id=child.parent_id "
                "WHERE parent.account_id=? "
                "AND json_extract(child.data, '$.broker_order_id')=? LIMIT 2",
                (str(account_id), str(broker_order_id)),
            ).fetchall()
            if len(rows) != 1:
                return None
            return self.get_order_context(rows[0][0])

    def get_parent(self, parent_id):
        with self._lock:
            return self._public(self._load(parent_id))

    def list_parents(self, *, active_only=False, limit=100):
        with self._lock:
            query = (
                "SELECT data FROM smart_parents"
                + (" WHERE active=1" if active_only else "")
                + " ORDER BY rowid DESC LIMIT ?"
            )
            return [
                self._public(json.loads(row[0]))
                for row in self._db.execute(query, (max(1, min(int(limit), 1000)),))
            ]

    def events(self, parent_id, *, limit=100):
        with self._lock:
            return [
                {
                    "id": row["id"],
                    "ts": utc_timestamp_string(row["ts"]),
                    "kind": row["kind"],
                    "data": json.loads(row["data"]),
                }
                for row in self._db.execute(
                    "SELECT * FROM smart_events WHERE parent_id=? ORDER BY id DESC LIMIT ?",
                    (parent_id, max(1, min(int(limit), 1000))),
                )
            ]
