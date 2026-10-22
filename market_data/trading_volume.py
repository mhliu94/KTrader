"""TradeStation quote-snapshot volume messages for the trading-volume topic.

Official v3 specification: https://api.tradestation.com/docs/specification/
GetQuoteSnapshots /v3/marketdata/quotes/{symbols} supports <=100 symbols. Its
Quote.Volume is DAILY shares/contracts; TradeTime is the LAST TRADE timestamp.
TotalVolume belongs to bars, and LastSize is one trade, so neither is used here.
MarketFlags supplies IsDelayed and IsHalted booleans.

The application uses America/New_York calendar dates for US-equity session
identity. The provider specification does not promise an exact reset boundary.
Consumers must baseline on a session/feed change, a gap, and a counter reset;
this producer publishes snapshots, never synthesizes eligible trade increments.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import threading
import uuid
from zoneinfo import ZoneInfo

US_MARKET_ZONE = ZoneInfo("America/New_York")
DEFAULT_TRADING_VOLUME_TOPIC = "trading-volume"


def normalized_symbols(symbols):
    if not isinstance(symbols, (list, tuple)):
        raise ValueError("trading volume symbols must be a list")
    return list(
        dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    )


def daily_volume(value):
    """Parse the documented Quote.Volume without truncating fractional values."""
    if isinstance(value, bool):
        raise ValueError("invalid daily volume")
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError("invalid daily volume") from None
    if (
        not number.is_finite()
        or number < 0
        or number != number.to_integral_value()
        or number > 2**63 - 1
    ):
        raise ValueError("invalid daily volume")
    return int(number)


def utc_iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def trade_timestamp(value):
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid last trade timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("last trade timestamp requires a UTC offset")
    return parsed.timestamp()


def select_quote(payload, symbol):
    """Require one exact requested symbol; errors cannot borrow another quote."""
    if not isinstance(payload, dict):
        raise ValueError("invalid quote response")
    errors = payload.get("Errors", [])
    if not isinstance(errors, list):
        raise ValueError("invalid quote error response")
    for error in errors:
        if (
            not isinstance(error, dict)
            or not error.get("Symbol")
            or str(error.get("Symbol", "")).upper() == symbol
        ):
            raise ValueError("provider rejected quote symbol")
    quotes = payload.get("Quotes")
    if not isinstance(quotes, list):
        raise ValueError("quote response is missing Quotes")
    matches = [
        quote
        for quote in quotes
        if isinstance(quote, dict) and str(quote.get("Symbol", "")).strip().upper() == symbol
    ]
    if len(matches) != 1:
        raise ValueError("missing or duplicate requested quote symbol")
    return matches[0]


def validated_quote(quote, symbol, observed_at):
    if not isinstance(quote, dict) or str(quote.get("Symbol", "")).strip().upper() != symbol:
        raise ValueError("quote symbol mismatch")
    if quote.get("Error"):
        raise ValueError("provider quote error")
    flags = quote.get("MarketFlags")
    if not isinstance(flags, dict) or flags.get("IsDelayed") is not False:
        raise ValueError("quote is delayed or realtime status is unknown")
    if flags.get("IsHalted") is not False:
        raise ValueError("instrument is halted or halt status is unknown")
    volume = daily_volume(quote.get("Volume"))
    trade_time = trade_timestamp(quote.get("TradeTime"))
    if trade_time is not None and trade_time > observed_at + 1:
        raise ValueError("last trade timestamp is in the future")
    return volume, trade_time


class TradingVolumePublisher:
    """A bounded per-symbol publisher, independent of market-depth callbacks."""

    def __init__(
        self,
        producer,
        symbols,
        *,
        topic=DEFAULT_TRADING_VOLUME_TOPIC,
        feed_id=None,
        max_request_seconds=10,
        acknowledgement_timeout=5
    ):
        self.producer = producer
        self.symbols = normalized_symbols(symbols)
        self._symbols = set(self.symbols)
        self.topic = str(topic).strip()
        if not self.topic:
            raise ValueError("trading_volume_topic must not be empty")
        self.feed_id = str(feed_id or uuid.uuid4().hex)
        self.max_request_seconds = float(max_request_seconds)
        if not math.isfinite(self.max_request_seconds) or self.max_request_seconds <= 0:
            raise ValueError("max_request_seconds must be finite and positive")
        self.acknowledgement_timeout = acknowledgement_timeout
        self._sequence = {}
        self._previous = {}
        self._lock = threading.Lock()

    def publish_quote(self, symbol, quote, *, request_started_at, observed_at, request_duration_ms):
        symbol = str(symbol).strip().upper()
        if symbol not in self._symbols:
            return None
        reason, volume, trade_time = "", None, None
        try:
            duration = float(request_duration_ms)
            if (
                not math.isfinite(duration)
                or duration < 0
                or duration > self.max_request_seconds * 1000
            ):
                raise ValueError("quote request exceeded freshness limit")
            if observed_at < request_started_at:
                raise ValueError("quote request clock moved backwards")
            volume, trade_time = validated_quote(quote, symbol, observed_at)
        except (ValueError, TypeError) as error:
            reason = str(error)
        with self._lock:
            previous = self._previous.get(symbol)
            if not reason and previous and previous["session"] == self._session(observed_at):
                if observed_at <= previous["observed_at"]:
                    reason = "quote observations are not time ordered"
                elif volume > previous["volume"] and trade_time is None:
                    reason = "volume increased without a last trade timestamp"
                elif (
                    volume > previous["volume"]
                    and trade_time is not None
                    and trade_time < previous["observed_at"] - 1
                ):
                    # An unchanged old last trade is normal for an illiquid
                    # instrument. An increased counter with that old timestamp
                    # cannot establish timely newly eligible volume.
                    reason = "volume increased with an old last trade timestamp"
            event = self._event(
                symbol,
                request_started_at,
                observed_at,
                request_duration_ms,
                volume if not reason else None,
                trade_time,
                reason,
                quote.get("MarketFlags") if isinstance(quote, dict) else None,
            )
            self._publish(event)
            if reason:
                self._previous.pop(symbol, None)
            else:
                self._previous[symbol] = {
                    "volume": volume,
                    "observed_at": observed_at,
                    "session": event["session"],
                }
            return event

    def publish_error(
        self, symbol, reason, *, request_started_at, observed_at, request_duration_ms
    ):
        symbol = str(symbol).strip().upper()
        if symbol not in self._symbols:
            return None
        with self._lock:
            event = self._event(
                symbol,
                request_started_at,
                observed_at,
                request_duration_ms,
                None,
                None,
                str(reason or "provider quote error")[:240],
            )
            self._publish(event)
            self._previous.pop(symbol, None)
            return event

    @staticmethod
    def _session(observed_at):
        return datetime.fromtimestamp(observed_at, US_MARKET_ZONE).date().isoformat()

    def _event(
        self,
        symbol,
        request_started_at,
        observed_at,
        duration,
        volume,
        trade_time,
        reason,
        flags=None,
    ):
        previous = self._previous.get(symbol)
        session = self._session(observed_at)
        interval_start = (
            previous["observed_at"]
            if previous and previous["session"] == session
            else request_started_at
        )
        # Allocate before enqueue: an ambiguous delivery must never reuse a
        # sequence number for different content. Gaps explicitly force a reset.
        sequence = self._sequence.get(symbol, 0) + 1
        self._sequence[symbol] = sequence
        try:
            duration = float(duration)
            duration = duration if math.isfinite(duration) else None
        except (TypeError, ValueError):
            duration = None
        event = {
            "schema_version": 1,
            "type": "TRADING_VOLUME",
            "symbol": symbol,
            "source": "TradeStation",
            "feed_id": self.feed_id,
            "sequence": sequence,
            "total_volume": volume,
            "observed_at": utc_iso(observed_at),
            "session": session,
            "healthy": not bool(reason),
            "is_halted": (
                flags.get("IsHalted")
                if isinstance(flags, dict) and isinstance(flags.get("IsHalted"), bool)
                else None
            ),
            "is_delayed": (
                flags.get("IsDelayed")
                if isinstance(flags, dict) and isinstance(flags.get("IsDelayed"), bool)
                else None
            ),
            "interval_start": utc_iso(interval_start),
            "request_duration_ms": duration,
        }
        if trade_time is not None:
            event["trade_time"] = utc_iso(trade_time)
        if reason:
            event["reason"] = reason
        return event

    def _publish(self, event):
        try:
            future = self.producer.send(
                topic=self.topic,
                key=event["symbol"],
                value=json.dumps(event, allow_nan=False, separators=(",", ":")),
            )
            future.get(timeout=self.acknowledgement_timeout)
        except Exception:
            # A lost Kafka ACK can still mean delivery. Keep the allocated
            # sequence, discard the local interval, and let consumers baseline.
            self._previous.pop(event["symbol"], None)
            raise
