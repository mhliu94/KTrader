import threading
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class QuoteRow:
    symbol: str
    prev_close: Optional[float]
    last: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    short_interest: Optional[float]
    volume: Optional[int]
    asof_epoch: Optional[int]
    error: Optional[str] = None


@dataclass
class BookLevel:
    price: Optional[float]
    quantity: Optional[float]
    order_count: Optional[int] = None


@dataclass
class OrderBookSnapshot:
    symbol: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    asof_epoch: Optional[int] = None
    depth_limit: int = 20
    error: Optional[str] = None


@dataclass
class HistoricalCloseRow:
    trade_date: str
    close: float


def _to_float(v: object) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _first_present(obj: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in obj:
            return obj.get(key)
    return None


def _sort_levels_most_aggressive(levels: list[BookLevel], *, is_bid: bool) -> list[BookLevel]:
    def sort_key(level: BookLevel) -> tuple[int, float]:
        price = level.price
        if price is None:
            return (1, 0.0)
        return (0, -price if is_bid else price)

    return sorted(levels, key=sort_key)


def _top_levels(levels: list[BookLevel], depth_limit: int, *, is_bid: bool) -> list[BookLevel]:
    capped_limit = max(0, depth_limit)
    return _sort_levels_most_aggressive(list(levels), is_bid=is_bid)[:capped_limit]


def _extract_levels(side_obj: object, depth_limit: int, *, is_bid: bool) -> list[BookLevel]:
    if not isinstance(side_obj, dict):
        return []
    levels = side_obj.get("levels")
    if not isinstance(levels, list):
        return []

    out: list[BookLevel] = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        out.append(
            BookLevel(
                price=_to_float(_first_present(level, ("prc", "price", "Price"))),
                quantity=_to_float(_first_present(level, ("qty", "total_qty", "quantity", "TotalSize"))),
                order_count=_to_int(_first_present(level, ("order_count", "num_orders", "TotalOrderCount"))),
            )
        )
    return _top_levels(out, depth_limit, is_bid=is_bid)


def _best_price(side_obj: object, *, is_bid: bool) -> Optional[float]:
    levels = _extract_levels(side_obj, 1, is_bid=is_bid)
    if not levels:
        return None
    return levels[0].price


def quote_row_from_price_book(payload: dict) -> Optional[QuoteRow]:
    symbol = str(payload.get("symbol", "")).strip()
    if not symbol:
        return None

    bid = _best_price(payload.get("bid_side"), is_bid=True)
    ask = _best_price(payload.get("offer_side"), is_bid=False)
    if bid is not None and ask is not None:
        last = (bid + ask) / 2.0
    elif bid is not None:
        last = bid
    elif ask is not None:
        last = ask
    else:
        last = None

    update_time_ns = _to_int(payload.get("update_time"))
    asof_epoch = None
    if update_time_ns is not None:
        asof_epoch = int(update_time_ns / 1_000_000_000)

    volume = _to_int(payload.get("volume"))
    if volume is None:
        volume = _to_int(payload.get("day_volume"))
    if volume is None:
        volume = _to_int(payload.get("total_volume"))

    short_interest = _to_float(payload.get("short_interest"))
    if short_interest is None:
        short_interest = _to_float(payload.get("short_interest_pct"))

    return QuoteRow(
        symbol=symbol,
        prev_close=None,
        last=last,
        change=None,
        change_pct=None,
        short_interest=short_interest,
        volume=volume,
        asof_epoch=asof_epoch,
        error=None,
    )


def order_book_from_price_book(payload: dict, depth_limit: int = 20) -> Optional[OrderBookSnapshot]:
    symbol = str(payload.get("symbol", "")).strip()
    if not symbol:
        return None

    update_time_ns = _to_int(payload.get("update_time"))
    asof_epoch = None
    if update_time_ns is not None:
        asof_epoch = int(update_time_ns / 1_000_000_000)

    capped_limit = max(1, min(int(depth_limit), 20))
    return OrderBookSnapshot(
        symbol=symbol,
        bids=_extract_levels(payload.get("bid_side"), capped_limit, is_bid=True),
        asks=_extract_levels(payload.get("offer_side"), capped_limit, is_bid=False),
        asof_epoch=asof_epoch,
        depth_limit=capped_limit,
        error=None,
    )


class MarketDataStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: Dict[str, QuoteRow] = {}
        self._books: Dict[str, OrderBookSnapshot] = {}

    def upsert(self, row: QuoteRow) -> None:
        with self._lock:
            existing = self._rows.get(row.symbol)
            if existing is None:
                self._rows[row.symbol] = row
                return

            self._rows[row.symbol] = QuoteRow(
                symbol=row.symbol,
                prev_close=row.prev_close if row.prev_close is not None else existing.prev_close,
                last=row.last if row.last is not None else existing.last,
                change=row.change if row.change is not None else existing.change,
                change_pct=row.change_pct if row.change_pct is not None else existing.change_pct,
                short_interest=row.short_interest if row.short_interest is not None else existing.short_interest,
                volume=row.volume if row.volume is not None else existing.volume,
                asof_epoch=row.asof_epoch if row.asof_epoch is not None else existing.asof_epoch,
                error=row.error if row.error is not None else existing.error,
            )

    def upsert_book(self, book: OrderBookSnapshot) -> None:
        with self._lock:
            self._books[book.symbol] = book

    def get_for_symbols(self, symbols: list[str]) -> Dict[str, QuoteRow]:
        with self._lock:
            out: Dict[str, QuoteRow] = {}
            for symbol in symbols:
                existing = self._rows.get(symbol)
                if existing is not None:
                    out[symbol] = existing
                    continue
                out[symbol] = QuoteRow(
                    symbol=symbol,
                    prev_close=None,
                    last=None,
                    change=None,
                    change_pct=None,
                    short_interest=None,
                    volume=None,
                    asof_epoch=None,
                    error="no data yet",
                )
            return out

    def get_book(self, symbol: str, depth_limit: int = 20) -> OrderBookSnapshot:
        capped_limit = max(1, min(int(depth_limit), 20))
        with self._lock:
            existing = self._books.get(symbol)
            if existing is None:
                return OrderBookSnapshot(
                    symbol=symbol,
                    bids=[],
                    asks=[],
                    asof_epoch=None,
                    depth_limit=capped_limit,
                    error="no data yet",
                )

            return OrderBookSnapshot(
                symbol=existing.symbol,
                bids=_top_levels(existing.bids, capped_limit, is_bid=True),
                asks=_top_levels(existing.asks, capped_limit, is_bid=False),
                asof_epoch=existing.asof_epoch,
                depth_limit=capped_limit,
                error=existing.error,
            )


class HistoricalCloseStore:
    """
    Loads latest available close per symbol from historical_prices CSV.
    Automatically reloads when file mtime changes.
    """

    def __init__(self, csv_path: str) -> None:
        self._path = Path(csv_path)
        self._lock = threading.RLock()
        self._mtime_ns: Optional[int] = None
        self._rows: Dict[str, HistoricalCloseRow] = {}

    def get_prev_close(self, symbol: str) -> Optional[float]:
        self._reload_if_needed()
        norm_symbol = str(symbol).strip().upper()
        with self._lock:
            row = self._rows.get(norm_symbol)
            return None if row is None else row.close

    def _reload_if_needed(self) -> None:
        try:
            stat = self._path.stat()
            mtime_ns = stat.st_mtime_ns
        except OSError:
            with self._lock:
                self._mtime_ns = None
                self._rows = {}
            return

        with self._lock:
            if self._mtime_ns == mtime_ns:
                return

        loaded = self._load_latest_rows()
        with self._lock:
            self._rows = loaded
            self._mtime_ns = mtime_ns

    def _load_latest_rows(self) -> Dict[str, HistoricalCloseRow]:
        out: Dict[str, HistoricalCloseRow] = {}
        try:
            with self._path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    symbol = str(row.get("symbol", "")).strip().upper()
                    trade_date = str(row.get("trade_date", "")).strip()
                    close_raw = row.get("close")
                    if not symbol or not trade_date:
                        continue
                    close = _to_float(close_raw)
                    if close is None:
                        continue

                    existing = out.get(symbol)
                    if existing is None or trade_date > existing.trade_date:
                        out[symbol] = HistoricalCloseRow(trade_date=trade_date, close=close)
        except Exception:
            return {}

        return out
