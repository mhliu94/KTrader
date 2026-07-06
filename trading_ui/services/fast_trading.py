import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..models import AccountMeta
from .market_data import BookLevel, MarketDataStore, OrderBookSnapshot
from .orders import build_limit_order_fok_command


LOGGER = logging.getLogger("trading-ui.fast-trading")

FAST_TRADING_MODES = {"E", "F"}
FAST_COMBINE_TARGET_SHARES = 1000
FAST_MIN_EXECUTABLE_SHARES = 100
FAST_MANUAL_END_TIME = datetime(2099, 1, 1, tzinfo=timezone.utc)
FAST_TRADING_WAIT_SECONDS_BY_MEDIUM = {
    "API": 5.0,
    "WEB": 20.0,
    "WINDOWS": 25.0,
    "EMULATOR": 35.0,
}


@dataclass
class FastTradingCycleResult:
    trading_mode: str
    symbol: str
    resting_side: str
    order_side: str
    total_resting_qty: int = 0
    execution_group_id: Optional[int] = None
    limit_price: Optional[float] = None
    wait_seconds: Optional[float] = None
    commands: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""


@dataclass
class _FastGroup:
    group_id: int
    price_limit: float
    accounts: List[Dict[str, Any]]


@dataclass
class _FastStrategy:
    key: str
    command: Dict[str, Any]
    stop_event: threading.Event
    thread: Optional[threading.Thread] = None
    started_at: str = ""
    cycles: int = 0
    published_commands: int = 0
    last_reason: str = ""


def _parse_iso_datetime(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty timestamp")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_fast_trading_end_time(value: Any, now: Optional[datetime] = None) -> str:
    end_time = _parse_iso_datetime(value)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if end_time <= current_time:
        return _iso_z(FAST_MANUAL_END_TIME)
    return str(value)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _command_json(command: Dict[str, Any]) -> str:
    return json.dumps(command, sort_keys=True, separators=(",", ":"))


def _wait_seconds_for_account(account_id: str, account_metas: Dict[str, AccountMeta]) -> float:
    meta = account_metas.get(account_id)
    medium = str(getattr(meta, "trading_medium", "") or "API").strip().upper()
    return FAST_TRADING_WAIT_SECONDS_BY_MEDIUM.get(medium, FAST_TRADING_WAIT_SECONDS_BY_MEDIUM["API"])


def _normalize_groups(fast_trading_groups: List[Dict[str, Any]]) -> List[_FastGroup]:
    groups: List[_FastGroup] = []
    if not isinstance(fast_trading_groups, list) or not fast_trading_groups:
        raise ValueError("fast_trading_groups must be a non-empty list")

    for idx, group in enumerate(fast_trading_groups, start=1):
        if not isinstance(group, dict):
            raise ValueError("fast_trading_groups entries must be objects")
        price_limit = _as_float(group.get("price_limit"))
        if price_limit is None or price_limit <= 0:
            raise ValueError("fast trading group price_limit must be positive")
        accounts = group.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("fast trading group accounts must be non-empty")
        groups.append(
            _FastGroup(
                group_id=int(group.get("group_id") or idx),
                price_limit=float(price_limit),
                accounts=accounts,
            )
        )

    return sorted(groups, key=lambda group: group.price_limit)


def _level_price(level: BookLevel) -> Optional[float]:
    return _as_float(getattr(level, "price", None))


def _level_quantity(level: BookLevel) -> float:
    qty = _as_float(getattr(level, "quantity", None))
    if qty is None or qty <= 0:
        return 0.0
    return qty


def _level_in_group(price: float, mode: str, groups: List[_FastGroup], group_index: int) -> bool:
    group = groups[group_index]
    if mode == "E":
        previous_limit = groups[group_index - 1].price_limit if group_index > 0 else None
        return price <= group.price_limit and (previous_limit is None or price > previous_limit)

    next_limit = groups[group_index + 1].price_limit if group_index + 1 < len(groups) else None
    return price >= group.price_limit and (next_limit is None or price < next_limit)


def _quantity_by_group(
    levels: List[BookLevel],
    mode: str,
    groups: List[_FastGroup],
) -> Dict[int, int]:
    totals: Dict[int, float] = {idx: 0.0 for idx in range(len(groups))}
    for level in levels:
        price = _level_price(level)
        if price is None or price <= 0:
            continue
        qty = _level_quantity(level)
        if qty <= 0:
            continue
        for idx in range(len(groups)):
            if _level_in_group(price, mode, groups, idx):
                totals[idx] += qty
                break

    return {idx: int(math.floor(qty)) for idx, qty in totals.items()}


def build_fast_trading_cycle_commands(
    trading_mode: str,
    symbol: str,
    book: OrderBookSnapshot,
    fast_trading_groups: List[Dict[str, Any]],
    account_metas: Dict[str, AccountMeta],
    cycle_id: Optional[str] = None,
) -> FastTradingCycleResult:
    mode = str(trading_mode or "").strip().upper()
    clean_symbol = str(symbol or "").strip().upper()
    if mode not in FAST_TRADING_MODES:
        raise ValueError("fast trading mode must be E or F")
    if not clean_symbol:
        raise ValueError("symbol is required")

    resting_side = "ASK" if mode == "E" else "BID"
    order_side = "BUY" if mode == "E" else "SELL"
    result = FastTradingCycleResult(
        trading_mode=mode,
        symbol=clean_symbol,
        resting_side=resting_side,
        order_side=order_side,
    )

    if book.error:
        result.reason = book.error
        return result

    groups = _normalize_groups(fast_trading_groups)
    levels = list(book.asks if mode == "E" else book.bids)
    if not levels:
        result.reason = "no_resting_orders"
        return result

    quantities = _quantity_by_group(levels, mode, groups)
    scan_indices = list(range(len(groups))) if mode == "E" else list(range(len(groups) - 1, -1, -1))

    start_pos: Optional[int] = None
    for pos, group_index in enumerate(scan_indices):
        if quantities[group_index] > 0:
            start_pos = pos
            break

    if start_pos is None:
        result.reason = "no_resting_orders_in_configured_ranges"
        return result

    total_qty = 0
    execution_index = scan_indices[start_pos]
    for group_index in scan_indices[start_pos:]:
        total_qty += quantities[group_index]
        execution_index = group_index
        if total_qty >= FAST_COMBINE_TARGET_SHARES:
            break

    result.total_resting_qty = total_qty
    execution_group = groups[execution_index]
    result.execution_group_id = execution_group.group_id
    result.limit_price = execution_group.price_limit

    if total_qty < FAST_MIN_EXECUTABLE_SHARES:
        result.reason = "total_below_minimum"
        return result

    cycle_id = cycle_id or f"fast_{mode}_{clean_symbol}_{int(time.time() * 1_000_000)}"
    for account in execution_group.accounts:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "").strip()
        allocation_pct = _as_float(account.get("allocation_pct"))
        if not account_id or allocation_pct is None or allocation_pct <= 0:
            continue

        qty = int(math.floor(total_qty * allocation_pct / 100.0))
        if qty <= 0:
            continue

        cmd = build_limit_order_fok_command(
            account_id=account_id,
            symbol=clean_symbol,
            side=order_side,
            shares=qty,
            limit_price=execution_group.price_limit,
            account_metas=account_metas,
        )
        cmd["trading_mode"] = mode
        cmd["algo_cycle_id"] = cycle_id
        cmd["fast_trading_group_id"] = execution_group.group_id
        cmd["fast_trading_resting_side"] = resting_side
        cmd["fast_trading_total_resting_qty"] = total_qty
        result.commands.append(cmd)

    if not result.commands:
        result.reason = "allocations_floor_to_zero"
    else:
        result.wait_seconds = max(
            _wait_seconds_for_account(str(cmd.get("account_id") or ""), account_metas)
            for cmd in result.commands
        )
        for cmd in result.commands:
            cmd["fast_trading_wait_seconds"] = result.wait_seconds
    return result


class FastTradingStrategyManager:
    def __init__(
        self,
        market_data_store: MarketDataStore,
        account_metas_provider: Callable[[], Dict[str, AccountMeta]],
        publish_command: Callable[[Dict[str, Any], str], None],
        cycle_seconds: Optional[float] = None,
        idle_cycle_seconds: Optional[float] = None,
    ) -> None:
        self._market_data_store = market_data_store
        self._account_metas_provider = account_metas_provider
        self._publish_command = publish_command
        self._cycle_seconds = self._positive_float(
            cycle_seconds,
            os.getenv("FAST_TRADING_CYCLE_SECONDS", "1.0"),
            1.0,
        )
        self._idle_cycle_seconds = self._positive_float(
            idle_cycle_seconds,
            os.getenv("FAST_TRADING_IDLE_CYCLE_SECONDS", str(self._cycle_seconds)),
            self._cycle_seconds,
        )
        self._lock = threading.RLock()
        self._strategies: Dict[str, _FastStrategy] = {}

    @staticmethod
    def _positive_float(value: Optional[float], raw_default: str, fallback: float) -> float:
        if value is not None:
            parsed = float(value)
        else:
            try:
                parsed = float(raw_default)
            except (TypeError, ValueError):
                parsed = fallback
        return max(0.1, parsed)

    @staticmethod
    def _strategy_key(trading_mode: str, symbol: str) -> str:
        return f"{str(trading_mode).strip().upper()}:{str(symbol).strip().upper()}"

    @staticmethod
    def _strategy_accounts(command: Dict[str, Any]) -> set[str]:
        accounts: set[str] = set()
        groups = command.get("fast_trading_groups")
        if not isinstance(groups, list):
            return accounts
        for group in groups:
            if not isinstance(group, dict):
                continue
            for account in group.get("accounts") or []:
                if isinstance(account, dict):
                    account_id = str(account.get("account_id") or "").strip()
                    if account_id:
                        accounts.add(account_id)
        return accounts

    def start_strategy(self, start_command: Dict[str, Any]) -> str:
        mode = str(start_command.get("trading_mode") or "").strip().upper()
        symbol = str(start_command.get("symbol") or "").strip().upper()
        if mode not in FAST_TRADING_MODES:
            raise ValueError("fast trading strategy mode must be E or F")
        if not symbol:
            raise ValueError("fast trading strategy requires a symbol")

        strategy_command = dict(start_command)
        strategy_command["end_time_et"] = normalize_fast_trading_end_time(strategy_command.get("end_time_et"))
        test_mode = _truthy_bool(strategy_command.get("fast_trading_test_mode"))
        key = self._strategy_key(mode, symbol)
        self.stop_strategy(mode, symbol)

        strategy = _FastStrategy(
            key=key,
            command=strategy_command,
            stop_event=threading.Event(),
            started_at=_utc_now_iso(),
        )
        thread = threading.Thread(
            target=self._run_strategy,
            args=(strategy,),
            name=f"fast-trading-{key}",
            daemon=True,
        )
        strategy.thread = thread
        with self._lock:
            self._strategies[key] = strategy
        thread.start()
        LOGGER.info("Started fast trading strategy key=%s test_mode=%s", key, test_mode)
        return key

    def stop_strategy(self, trading_mode: str, symbol: str) -> int:
        key = self._strategy_key(trading_mode, symbol)
        return self._stop_keys([key])

    def stop_matching(self, trading_mode: str, account_ids: Optional[List[str]] = None) -> int:
        mode = str(trading_mode or "").strip().upper()
        account_filter = {str(account_id).strip() for account_id in (account_ids or []) if str(account_id).strip()}
        keys: List[str] = []
        with self._lock:
            for key, strategy in self._strategies.items():
                strategy_mode = str(strategy.command.get("trading_mode") or "").strip().upper()
                if strategy_mode != mode:
                    continue
                if account_filter and not (self._strategy_accounts(strategy.command) & account_filter):
                    continue
                keys.append(key)
        return self._stop_keys(keys)

    def stop_all(self) -> int:
        with self._lock:
            keys = list(self._strategies.keys())
        return self._stop_keys(keys)

    def _stop_keys(self, keys: List[str]) -> int:
        strategies: List[_FastStrategy] = []
        with self._lock:
            for key in keys:
                strategy = self._strategies.pop(key, None)
                if strategy is not None:
                    strategies.append(strategy)

        for strategy in strategies:
            strategy.stop_event.set()
        for strategy in strategies:
            if strategy.thread is not None and strategy.thread is not threading.current_thread():
                strategy.thread.join(timeout=2.0)
            LOGGER.info("Stopped fast trading strategy key=%s", strategy.key)
        return len(strategies)

    def _run_strategy(self, strategy: _FastStrategy) -> None:
        try:
            end_time = _parse_iso_datetime(strategy.command.get("end_time_et"))
            while not strategy.stop_event.is_set():
                if datetime.now(timezone.utc) >= end_time:
                    strategy.last_reason = "end_time_reached"
                    break

                cycle_start = time.monotonic()
                result = self.run_cycle(strategy.command)
                published_count = 0 if _truthy_bool(strategy.command.get("fast_trading_test_mode")) else len(result.commands)
                with self._lock:
                    strategy.cycles += 1
                    strategy.published_commands += published_count
                    strategy.last_reason = result.reason

                wait_seconds = self._wait_seconds_after_cycle(result)
                elapsed = time.monotonic() - cycle_start
                if strategy.stop_event.wait(max(0.0, wait_seconds - elapsed)):
                    break
        except Exception:
            LOGGER.exception("Fast trading strategy failed key=%s", strategy.key)
        finally:
            with self._lock:
                existing = self._strategies.get(strategy.key)
                if existing is strategy:
                    self._strategies.pop(strategy.key, None)

    def _wait_seconds_after_cycle(self, result: FastTradingCycleResult) -> float:
        if result.commands:
            return result.wait_seconds or self._cycle_seconds
        return self._idle_cycle_seconds

    def run_cycle(self, command: Dict[str, Any]) -> FastTradingCycleResult:
        mode = str(command.get("trading_mode") or "").strip().upper()
        symbol = str(command.get("symbol") or "").strip().upper()
        groups = command.get("fast_trading_groups")
        if not isinstance(groups, list):
            raise ValueError("fast trading command missing fast_trading_groups")

        book = self._market_data_store.get_book(symbol, depth_limit=20)
        result = build_fast_trading_cycle_commands(
            trading_mode=mode,
            symbol=symbol,
            book=book,
            fast_trading_groups=groups,
            account_metas=self._account_metas_provider(),
        )
        test_mode = _truthy_bool(command.get("fast_trading_test_mode"))
        for cmd in result.commands:
            key = str(cmd.get("account_id") or "LIMIT_ORDER_FOK")
            if test_mode:
                cmd["fast_trading_test_mode"] = True
                LOGGER.info(
                    "Fast trading TEST MODE logged command without Kafka publish key=%s command=%s",
                    key,
                    _command_json(cmd),
                )
                continue

            LOGGER.info("Publishing fast trading command key=%s command=%s", key, _command_json(cmd))
            self._publish_command(cmd, key)
        LOGGER.info(
            "Fast trading cycle mode=%s symbol=%s resting_side=%s total_qty=%s group=%s price=%s commands=%d wait_seconds=%s test_mode=%s reason=%s",
            result.trading_mode,
            result.symbol,
            result.resting_side,
            result.total_resting_qty,
            result.execution_group_id,
            result.limit_price,
            len(result.commands),
            result.wait_seconds,
            test_mode,
            result.reason,
        )
        return result
