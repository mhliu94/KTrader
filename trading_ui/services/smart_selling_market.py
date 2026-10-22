"""Conservative quote and independent trading-volume adapters for POV execution.

The configured price-book feed is assumed to describe accessible displayed depth.
Prices and quantities stay decimal; malformed/absent depth never becomes tradable.
Daily cumulative volume is admitted only as fresh, ordered differences after a
baseline. Reconnects, source/session changes and gaps discard uncertain volume.
Price-book messages never earn tape credit. The ``trading-volume`` topic supplies
actual cumulative observations with their own feed/session identity and sequence.
Producer observation timestamps, never consumer arrival time, establish freshness.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import threading
import time

ZERO = Decimal("0")


def _decimal(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _first(mapping, names):
    return next((mapping[name] for name in names if mapping.get(name) is not None), None)


def _timestamp(value, *, nanoseconds=False):
    number = _decimal(value)
    if number is not None:
        return float(number / Decimal(1_000_000_000) if nanoseconds else number)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError):
            pass
    return None


def _sequence(value):
    number = _decimal(value)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _best(side, *, bid):
    if not isinstance(side, dict) or not isinstance(side.get("levels"), list):
        return None, ZERO
    levels = []
    for level in side["levels"]:
        if not isinstance(level, dict):
            continue
        price = _decimal(_first(level, ("prc", "price", "Price")))
        quantity = _decimal(_first(level, ("qty", "total_qty", "quantity", "TotalSize")))
        if price is not None and price > 0:
            levels.append((price, quantity))
    if not levels:
        return None, ZERO
    price = (max if bid else min)(entry[0] for entry in levels)
    quantities = [quantity for level_price, quantity in levels if level_price == price]
    if any(quantity is None or quantity < 0 for quantity in quantities):
        return price, ZERO
    return price, sum(quantities, ZERO)


@dataclass
class _SymbolState:
    # Quotes: provider timestamps are wall-clock epochs; raw sequences may skip.
    book_feed_id: str = ""
    book_time: float | None = None
    book_snapshot_time: float | None = None
    book_sequence: int | None = None
    raw_sequence: int | None = None
    quote_gap: bool = False
    reset_quote: bool = True
    quote_healthy: bool = False

    # Tape: sequence numbers are contiguous within one producer feed and symbol.
    volume_feed_id: str = ""
    session: str = ""
    volume_healthy: bool = False
    volume: Decimal | None = None
    volume_time: float | None = None
    volume_sequence: int | None = None
    volume_mode: str = ""

    # Halt policy: cooldown uses monotonic time; source comparisons use UTC epochs.
    market_state: str = "UNKNOWN"
    market_reason: str = "halt and real-time status not yet confirmed"
    halt_latched: bool = False
    resume_started: float | None = None
    resume_source: float | None = None
    resume_count: int = 0
    status_feed_id: str = ""
    status_sequence: int | None = None
    status_time: float | None = None

    # Durable safeguards survive transport resets and process restarts.
    credited_session: str = ""
    credited_cumulative: Decimal | None = None
    credited_at: float | None = None
    counter_conflict: bool = False

    # Bounded forecasting/dedup caches; the engine owns durable event deduplication.
    observations: deque = field(default_factory=deque)
    seen_trades: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _VolumeEnvelope:
    symbol: str
    feed_id: str
    sequence: int
    observed_at: float
    session: str
    total_volume: Decimal | None


class SmartSellingMarketAdapter:
    """Normalize feed messages; the engine owns orders, reservations and credit."""

    def __init__(
        self,
        engine,
        *,
        clock=time.monotonic,
        wall_clock=time.time,
        max_source_age=10,
        rate_window_seconds=300,
        max_observations=256,
        resume_cooldown_seconds=10,
    ):
        self.engine = engine
        self.clock, self.wall_clock = clock, wall_clock
        self.max_source_age = float(max_source_age)
        self.rate_window_seconds = float(rate_window_seconds)
        self.max_observations = max(2, int(max_observations))
        self.resume_cooldown_seconds = max(0, float(resume_cooldown_seconds))
        self._lock = threading.RLock()
        self._symbols = {}
        self._offsets = {}
        self._connected = True
        self._volume_connected = True
        self._volume_connected_since = None

    def _fresh(self, timestamp):
        return timestamp is not None and -1 <= self.wall_clock() - timestamp <= self.max_source_age

    def _symbol_state(self, symbol):
        """Load only durable safeguards; a cumulative baseline is never restored."""
        if symbol not in self._symbols:
            state = _SymbolState()
            checkpoint = self.engine.get_market_checkpoint(symbol) or {}
            state.credited_session = str(checkpoint.get("session") or "")
            state.credited_cumulative = _decimal(checkpoint.get("cumulative"))
            state.credited_at = _timestamp(checkpoint.get("observed_at"))
            state.counter_conflict = checkpoint.get("counter_conflict") is True
            if checkpoint.get("halted") is True:
                state.halt_latched = True
                state.market_state, state.market_reason = (
                    "HALTED",
                    "persisted halt requires fresh reopening checks",
                )
            self._symbols[symbol] = state
        return self._symbols[symbol]

    def _save_checkpoint(self, symbol, state):
        """Persist the halt latch and cumulative watermark before credit admission."""
        self.engine.set_market_checkpoint(
            symbol,
            {
                "session": state.credited_session,
                "cumulative": (
                    str(state.credited_cumulative)
                    if state.credited_cumulative is not None
                    else None
                ),
                "observed_at": state.credited_at,
                "halted": state.halt_latched,
                "counter_conflict": state.counter_conflict,
            },
        )

    @staticmethod
    def _reset_volume(state):
        state.volume = None
        state.volume_time = None
        state.volume_sequence = None
        state.observations.clear()

    def set_connected(self, connected):
        """Book reconnects require a new snapshot, independently of tape health."""
        with self._lock:
            connected = bool(connected)
            if connected == self._connected:
                return
            self._connected = connected
            reason = "waiting for fresh feed snapshot" if connected else "feed disconnected"
            for symbol, state in self._symbols.items():
                state.reset_quote = True
                state.quote_healthy = False
                self.engine.set_feed_health(symbol, False, reason)

    def set_volume_connected(self, connected):
        """Discard tape continuity while retaining durable halt/correction safeguards."""
        with self._lock:
            connected = bool(connected)
            if connected == self._volume_connected:
                return
            self._volume_connected = connected
            self._volume_connected_since = self.wall_clock() if connected else None
            reason = (
                "waiting for fresh trading-volume baseline"
                if connected
                else "trading-volume feed disconnected"
            )
            for symbol, state in self._symbols.items():
                self._reset_volume(state)
                state.volume_healthy = False
                state.resume_count = 0
                if state.halt_latched:
                    state.market_state, state.market_reason = (
                        "HALTED",
                        "halt status requires fresh confirmation",
                    )
                    state.resume_started, state.resume_source = None, None
                else:
                    state.market_state, state.market_reason = (
                        "UNKNOWN",
                        "trading-volume feed disconnected",
                    )
                self.engine.set_feed_health(symbol, False, reason)

    def _volume_is_healthy(self, state):
        return (
            self._volume_connected
            and state.volume_healthy
            and self._fresh(state.volume_time)
            and self._fresh(state.status_time)
            and state.market_state == "TRADING"
            and not state.counter_conflict
        )

    def _refresh_health(self, symbol, state):
        healthy = (
            self._connected
            and state.quote_healthy
            and self._fresh(state.book_time)
            and self._volume_is_healthy(state)
        )
        reason = (
            state.market_reason
            if state.market_state != "TRADING"
            else "trading-volume or quote feed unavailable"
        )
        self.engine.set_feed_health(symbol, healthy, reason if not healthy else "")

    def market_status(self, symbol):
        with self._lock:
            state = self._symbol_state(str(symbol).strip().upper())
            if state.counter_conflict and state.market_state != "HALTED":
                return {
                    "state": "UNKNOWN",
                    "reason": "cumulative volume correction requires reconciliation",
                }
            if state.market_state == "TRADING" and not self._volume_is_healthy(state):
                return {
                    "state": "UNKNOWN",
                    "reason": "trading-volume status is stale or unavailable",
                }
            return {"state": state.market_state, "reason": state.market_reason}

    def _halt_gate(self, symbol, state, payload, source):
        """Return whether this batch can earn credit; reopening batches cannot."""
        halted = payload.get("is_halted")
        delayed = payload.get("is_delayed")
        if halted is True:
            state.halt_latched = True
            state.market_state, state.market_reason = "HALTED", "exchange halt reported"
            state.resume_started = None
            state.resume_source = None
            state.resume_count = 0
            self._save_checkpoint(symbol, state)
            return False
        if halted is not False or delayed is not False or payload.get("healthy", True) is not True:
            state.resume_count = 0
            if not state.halt_latched:
                state.market_state = "UNKNOWN"
            state.market_reason = "awaiting explicit nonhalted, real-time healthy status"
            return False
        if state.halt_latched:
            if state.resume_started is None:
                state.resume_started, state.resume_source = self.clock(), source
                state.resume_count = 0
            state.resume_count += 1
            state.market_state, state.market_reason = (
                "RESUMING",
                "discarding reopening volume; awaiting stable trading and a fresh book",
            )
            refreshed_book = (
                state.quote_healthy
                and self._fresh(state.book_time)
                and state.book_snapshot_time is not None
                and state.book_snapshot_time > state.resume_source
                and self._fresh(state.book_snapshot_time)
            )
            cooldown_complete = self.clock() - state.resume_started >= self.resume_cooldown_seconds
            if state.resume_count >= 2 and cooldown_complete and refreshed_book and self._connected:
                state.halt_latched = False
                state.market_state, state.market_reason = (
                    "TRADING",
                    "reopening checks complete; new volume baseline established",
                )
                self._save_checkpoint(symbol, state)
            return False
        if state.market_state != "TRADING":
            self._reset_volume(state)
        state.market_state, state.market_reason = (
            "TRADING",
            "explicit nonhalted, real-time status confirmed",
        )
        return True

    def _accept_offset(self, topic, partition, offset):
        """Kafka ordering is transport deduplication, independent of provider sequences."""
        if topic is None or partition is None or offset is None:
            return True
        key = (str(topic), int(partition))
        position = int(offset)
        if key in self._offsets and position <= self._offsets[key]:
            return False
        self._offsets[key] = position
        return True

    def _observation(self, state, start, end, quantity):
        if end > start:
            state.observations.append((start, end, quantity))
        while state.observations and (
            len(state.observations) > self.max_observations
            or state.observations[0][1] < self.wall_clock() - self.rate_window_seconds
        ):
            state.observations.popleft()

    def volume_rate(self, symbol):
        """Recent observed shares/second, or None without fresh continuous history."""
        with self._lock:
            state = self._symbols.get(str(symbol).strip().upper())
            if state is None or not self._volume_is_healthy(state) or not state.observations:
                return None
            now = self.wall_clock()
            if not self._fresh(state.observations[-1][1]):
                return None
            cutoff = now - self.rate_window_seconds
            while state.observations and state.observations[0][1] <= cutoff:
                state.observations.popleft()
            if not state.observations:
                return None
            start = max(state.observations[0][0], cutoff)
            elapsed = Decimal(str(now - start))
            if elapsed <= 0:
                return None
            volume = ZERO
            for interval_start, interval_end, quantity in state.observations:
                covered = Decimal(str(interval_end - max(interval_start, cutoff)))
                duration = Decimal(str(interval_end - interval_start))
                fraction = covered / duration
                volume += quantity * fraction
            return volume / elapsed

    def apply_price_book(self, payload, *, topic=None, partition=None, offset=None):
        """Refresh depth only; producer heartbeats preserve known consumed depth."""
        if not isinstance(payload, dict):
            return False
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return False
        with self._lock:
            if not self._connected:
                return False
            if not self._accept_offset(topic, partition, offset):
                return False
            state = self._symbol_state(symbol)
            book_time = _timestamp(payload.get("book_observed_at"))
            if book_time is None:
                book_time = _timestamp(payload.get("update_time"), nanoseconds=True)
            feed_id = str(payload.get("feed_id") or "legacy")
            new_feed = bool(state.book_feed_id and state.book_feed_id != feed_id)
            state.book_feed_id = feed_id
            strict_sequence = _sequence(_first(payload, ("book_sequence", "provider_sequence")))
            raw_sequence = _sequence(payload.get("sequence_num"))
            if new_feed:
                state.book_time, state.book_sequence, state.raw_sequence = None, None, None
                state.reset_quote = True
            if (
                book_time is not None
                and state.book_time is not None
                and book_time < state.book_time
            ):
                return False
            if (
                strict_sequence is not None
                and state.book_sequence is not None
                and not state.reset_quote
            ):
                if strict_sequence <= state.book_sequence:
                    return False
            if (
                strict_sequence is None
                and raw_sequence is not None
                and state.raw_sequence is not None
            ):
                if raw_sequence == state.raw_sequence:
                    return False
                if raw_sequence < state.raw_sequence:
                    if book_time is None or state.book_time is None or book_time <= state.book_time:
                        return False
                    # A legacy producer restart has no feed ID. Its next full
                    # snapshot establishes a new quote sequence baseline.
                    state.reset_quote = True
            gap = (
                strict_sequence is not None
                and state.book_sequence is not None
                and not state.reset_quote
                and strict_sequence != state.book_sequence + 1
            )
            recovering = state.reset_quote or state.quote_gap
            bid, bid_size = _best(payload.get("bid_side"), bid=True)
            ask, _ = _best(payload.get("offer_side"), bid=False)
            quote_healthy = (
                self._fresh(book_time)
                and not gap
                and payload.get("healthy", True) is not False
                and bid is not None
                and ask is not None
                and bid_size > 0
                and ask >= bid
            )
            accessible = payload.get("accessible", True) is not False
            state.quote_healthy = quote_healthy and accessible
            self.engine.on_quote(
                symbol,
                bid,
                ask,
                bid_size,
                source_time=book_time,
                sequence=strict_sequence,
                healthy=state.quote_healthy and self._volume_is_healthy(state),
                accessible=accessible,
                received_at=self.clock(),
                reset_sequence=recovering and not gap,
                preserve_consumption=payload.get("kind") == "PRICE_BOOK_HEARTBEAT",
            )
            state.book_time = book_time if book_time is not None else state.book_time
            if quote_healthy and payload.get("kind") != "PRICE_BOOK_HEARTBEAT":
                state.book_snapshot_time = book_time
            state.book_sequence, state.raw_sequence = strict_sequence, raw_sequence
            state.quote_gap, state.reset_quote = gap, False
            return True

    def apply_volume(self, payload, *, topic=None, partition=None, offset=None):
        """Validate one tape observation, apply market policy, then admit volume."""
        if not isinstance(payload, dict):
            return False
        if (
            payload.get("type", "TRADING_VOLUME") != "TRADING_VOLUME"
            or payload.get("schema_version", 1) != 1
        ):
            return False
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return False
        with self._lock:
            if not self._volume_connected or not self._accept_offset(topic, partition, offset):
                return False
            state = self._symbol_state(symbol)
            envelope = self._parse_volume_envelope(symbol, payload)
            if envelope is None:
                self._reset_volume(state)
                state.volume_healthy = False
                state.resume_count = 0
                self._refresh_health(symbol, state)
                return False
            if not self._accept_volume_status(state, envelope, payload):
                return False
            self._check_credited_watermark(state, envelope)
            if not self._prepare_volume_baseline(state, envelope, payload):
                return False
            admit_volume = self._halt_gate(symbol, state, payload, envelope.observed_at)
            if admit_volume and not state.counter_conflict:
                self._admit_volume(state, envelope, payload)
                accepted = True
            else:
                self._update_blocked_baseline(state, envelope, payload)
                accepted = payload.get("healthy", True) is True
            state.volume_healthy = state.volume_time is not None
            self._refresh_health(symbol, state)
            return accepted

    def _parse_volume_envelope(self, symbol, payload):
        """Freshness uses the producer's UTC observation time, never arrival time."""
        source = _timestamp(payload.get("observed_at"))
        sequence = _sequence(payload.get("sequence"))
        feed_id = str(payload.get("feed_id") or "")
        if not feed_id or sequence is None or not self._fresh(source):
            return None
        session = (
            payload.get("session")
            or datetime.fromtimestamp(source, timezone.utc).date().isoformat()
        )
        return _VolumeEnvelope(
            symbol, feed_id, sequence, source, str(session), _decimal(payload.get("total_volume"))
        )

    def _accept_volume_status(self, state, envelope, payload):
        """Reject replayed status; a newer halt may share the prior timestamp."""
        source = envelope.observed_at
        fresh_halt = payload.get("is_halted") is True
        if self._volume_connected_since is not None and source < self._volume_connected_since:
            return False
        if state.status_time is not None:
            if source < state.status_time or (source == state.status_time and not fresh_halt):
                return False
        if state.status_feed_id == envelope.feed_id and state.status_sequence is not None:
            if envelope.sequence <= state.status_sequence:
                return False
        state.status_feed_id = envelope.feed_id
        state.status_sequence = envelope.sequence
        state.status_time = source
        if fresh_halt:
            self._halt_gate(envelope.symbol, state, payload, source)
            self._refresh_health(envelope.symbol, state)
        return True

    def _check_credited_watermark(self, state, envelope):
        """A transport reset must not hide corrections to previously earned credit."""
        if envelope.session != state.credited_session or envelope.total_volume is None:
            return
        if state.credited_cumulative is None or envelope.total_volume >= state.credited_cumulative:
            return
        state.counter_conflict = True
        self._save_checkpoint(envelope.symbol, state)
        state.volume_feed_id = envelope.feed_id
        self._unknown_correction(
            envelope.symbol,
            state,
            envelope.total_volume - state.credited_cumulative,
            envelope.observed_at,
            envelope.sequence,
        )

    def _prepare_volume_baseline(self, state, envelope, payload):
        """Discard unobserved intervals while preserving unknown correction holds."""
        if state.volume_feed_id != envelope.feed_id or state.session != envelope.session:
            self._reset_volume(state)
            state.resume_count = 0
        state.volume_feed_id = envelope.feed_id
        state.session = envelope.session
        if state.volume_sequence is not None and envelope.sequence <= state.volume_sequence:
            return False
        if state.volume_time is not None and envelope.observed_at <= state.volume_time:
            return False
        sequence_gap = (
            state.volume_sequence is not None and envelope.sequence != state.volume_sequence + 1
        )
        time_gap = (
            state.volume_time is not None
            and envelope.observed_at - state.volume_time > self.max_source_age
        )
        if sequence_gap or time_gap:
            if (
                not isinstance(payload.get("trades"), list)
                and envelope.total_volume is not None
                and state.volume is not None
                and envelope.total_volume < state.volume
            ):
                self._unknown_correction(
                    envelope.symbol,
                    state,
                    envelope.total_volume - state.volume,
                    envelope.observed_at,
                    envelope.sequence,
                )
            self._reset_volume(state)
            state.resume_count = 0
        return True

    def _update_blocked_baseline(self, state, envelope, payload):
        """Ignore new volume during holds/reopening; still apply trade corrections."""
        current = envelope.total_volume
        if state.volume is not None and current is not None and current < state.volume:
            self._unknown_correction(
                envelope.symbol,
                state,
                current - state.volume,
                envelope.observed_at,
                envelope.sequence,
            )
        self._reset_volume(state)
        if isinstance(payload.get("trades"), list):
            state.volume_mode = "trades"
            self._trades(envelope.symbol, state, payload["trades"], corrections_only=True)
        else:
            state.volume_mode = "cumulative"
        if payload.get("healthy", True) is True and current is not None and current >= 0:
            state.volume = current
            state.volume_time = envelope.observed_at
            state.volume_sequence = envelope.sequence

    def _admit_volume(self, state, envelope, payload):
        """Use exactly one tape representation; switching representations rebaselines."""
        mode = "trades" if isinstance(payload.get("trades"), list) else "cumulative"
        if state.volume_mode != mode:
            self._reset_volume(state)
        state.volume_mode = mode
        if mode == "trades":
            self._trades(envelope.symbol, state, payload["trades"])
            state.volume_time = envelope.observed_at
            state.volume_sequence = envelope.sequence
        else:
            self._cumulative(
                envelope.symbol, state, payload, envelope.observed_at, envelope.sequence
            )

    def _cumulative(self, symbol, state, payload, source, sequence):
        """Admit only the observed interval's delta, after persisting its watermark."""
        volume = _decimal(payload.get("total_volume"))
        if volume is None or volume < 0:
            self._reset_volume(state)
            return
        previous, previous_time = state.volume, state.volume_time
        state.volume, state.volume_time, state.volume_sequence = volume, source, sequence
        if previous is None:
            return
        delta = volume - previous
        if delta < 0:
            self._unknown_correction(symbol, state, delta, source, sequence)
            state.observations.clear()
            return
        self._observation(state, previous_time, source, delta)
        if delta == 0:
            return
        interval_start = _timestamp(payload.get("interval_start"))
        interval_start = (
            min(previous_time, interval_start) if interval_start is not None else previous_time
        )
        # Persist before credit: a crash can cause a conservative extra hold,
        # but never erase the watermark protecting earned credit.
        state.credited_session = state.session
        state.credited_cumulative = volume
        state.credited_at = source
        self._save_checkpoint(symbol, state)
        self.engine.on_trade(
            symbol,
            delta,
            self._event_id(symbol, state, sequence),
            source_time=source,
            interval_start=interval_start,
            received_at=self.clock(),
        )

    @staticmethod
    def _event_id(symbol, state, sequence):
        return f"{state.volume_feed_id}:{symbol}:{sequence}"

    def _unknown_correction(self, symbol, state, delta, source, sequence):
        event_id = self._event_id(symbol, state, sequence)
        self.engine.on_trade(
            symbol,
            delta,
            event_id,
            source_time=source,
            correction_of="unreconstructable:" + event_id,
            received_at=self.clock(),
        )

    def _trades(self, symbol, state, trades, *, corrections_only=False):
        """Forward stable trade IDs; malformed corrections require reconciliation."""
        for index, trade in enumerate(trades):
            if not isinstance(trade, dict):
                continue
            event_id = str(trade.get("event_id") or trade.get("trade_id") or "")
            quantity = _decimal(trade.get("quantity"))
            source = _timestamp(trade.get("source_time"))
            correction = trade.get("correction_of")
            incomplete_reference = correction is not None and (
                not event_id or quantity is None or not str(correction)
            )
            unreferenced_negative = quantity is not None and quantity < 0 and correction is None
            malformed_correction = incomplete_reference or unreferenced_negative
            if malformed_correction:
                identifier = (
                    self._event_id(symbol, state, state.status_sequence) + ":invalid:" + str(index)
                )
                self.engine.on_trade(
                    symbol,
                    ZERO,
                    identifier,
                    source_time=source,
                    correction_of="unreconstructable:" + identifier,
                    received_at=self.clock(),
                )
                continue
            if not event_id or quantity is None or (correction is None and quantity <= 0):
                continue
            if corrections_only and correction is None:
                continue
            if correction is None and (
                not self._fresh(source)
                or (
                    self._volume_connected_since is not None
                    and source < self._volume_connected_since
                )
            ):
                continue
            if event_id in state.seen_trades:
                continue
            self.engine.on_trade(
                symbol,
                quantity,
                event_id,
                source_time=source,
                sequence=_sequence(trade.get("sequence")),
                correction_of=correction,
                received_at=self.clock(),
            )
            state.seen_trades[event_id] = None
            while len(state.seen_trades) > self.max_observations * 4:
                del state.seen_trades[next(iter(state.seen_trades))]
            # Forecast only: the durable engine remains the authority for trade
            # deduplication and correction rates, including historical prints.
            if correction is not None:
                state.observations.clear()
            elif state.volume_time is not None and source >= state.volume_time:
                self._observation(state, state.volume_time, source, quantity)
            state.volume_time = source if source is not None else state.volume_time
