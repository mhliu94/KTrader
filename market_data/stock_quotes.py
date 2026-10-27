"""Latest-trade snapshots from TradeStation quotes, not a tick-by-tick tape."""

from decimal import Decimal, InvalidOperation
import json
import math

try:
    from .trading_volume import daily_volume, normalized_symbols, trade_timestamp, utc_iso
except ImportError:  # Direct script execution by price_book_producer.py.
    from trading_volume import daily_volume, normalized_symbols, trade_timestamp, utc_iso


class StockQuotePublisher:
    def __init__(self, producer, symbols, *, topic="stock-quotes", acknowledgement_timeout=5):
        self.producer = producer
        self.symbols = set(normalized_symbols(symbols))
        self.topic = str(topic).strip()
        if not self.topic:
            raise ValueError("stock_quotes_topic must not be empty")
        self.acknowledgement_timeout = acknowledgement_timeout

    def publish_quote(self, symbol, quote, *, request_started_at, observed_at, request_duration_ms):
        symbol = str(symbol).strip().upper()
        if symbol not in self.symbols:
            return None
        event = self._event(symbol, observed_at)
        try:
            if not isinstance(quote, dict) or str(quote.get("Symbol", "")).strip().upper() != symbol:
                raise ValueError("quote symbol mismatch")
            if quote.get("Error"):
                raise ValueError("provider quote error")
            flags = quote.get("MarketFlags")
            if isinstance(flags, dict):
                for target, source in (("is_delayed", "IsDelayed"), ("is_halted", "IsHalted")):
                    if isinstance(flags.get(source), bool):
                        event[target] = flags[source]
            if event["is_delayed"] is not False:
                raise ValueError("quote is delayed or realtime status is unknown")
            price = Decimal(str(quote.get("Last")))
            if not price.is_finite() or price <= 0 or not math.isfinite(float(price)) or float(price) <= 0:
                raise ValueError("last trade price must be finite and positive")
            trade_time = trade_timestamp(quote.get("TradeTime"))
            if trade_time is None or trade_time > observed_at + 1:
                raise ValueError("missing or future last trade timestamp")
            # Old trade times are valid for closed markets and illiquid stocks.
            event["last"] = str(price)
            event["trade_time"] = utc_iso(trade_time)
            event["healthy"] = True
            try:
                event["last_size"] = daily_volume(quote.get("LastSize"))
            except ValueError:
                pass  # An absent size does not invalidate the documented Last.
        except (ValueError, TypeError, InvalidOperation, OverflowError) as error:
            event.update(last=None, trade_time=None, healthy=False)
            event["reason"] = (
                "invalid last trade price" if isinstance(error, InvalidOperation) else str(error)
            )
        self._publish(event)
        return event

    def publish_error(self, symbol, reason, *, request_started_at, observed_at, request_duration_ms):
        symbol = str(symbol).strip().upper()
        if symbol not in self.symbols:
            return None
        event = self._event(symbol, observed_at)
        event["reason"] = str(reason or "provider quote error")[:240]
        self._publish(event)
        return event

    @staticmethod
    def _event(symbol, observed_at):
        return {
            "schema_version": 1,
            "type": "STOCK_QUOTE",
            "symbol": symbol,
            "source": "TradeStation",
            "last": None,
            "last_size": None,
            "trade_time": None,
            "observed_at": utc_iso(observed_at),
            "is_delayed": None,
            "is_halted": None,
            "healthy": False,
        }

    def _publish(self, event):
        self.producer.send(
            topic=self.topic,
            key=event["symbol"],
            value=json.dumps(event, allow_nan=False, separators=(",", ":")),
        ).get(timeout=self.acknowledgement_timeout)
