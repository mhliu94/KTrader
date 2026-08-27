import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..models import AccountMeta, AccountSnapshot
from .market_data import BookLevel, MarketDataStore, OrderBookSnapshot
from .operations_log import OperationsLog
from .orders import build_limit_order_fok_command


LOGGER = logging.getLogger("trading-ui.fast-trading")

FAST_TRADING_MODES = {"E", "F"}
FAST_MIN_EXECUTABLE_SHARES = 100
FAST_MAX_BOOK_LEVELS = 20
FAST_MAX_CONSECUTIVE_CAPACITY_NOOPS = 10
FAST_TRADING_DEFAULT_SETTINGS: Dict[str, Any] = {
    "aggression_levels": {
        1: {"book_levels": 2, "cycle_seconds": 30.0},
        2: {"book_levels": 3, "cycle_seconds": 20.0},
        3: {"book_levels": 5, "cycle_seconds": 10.0},
    },
    "minimum_cycle_seconds_by_medium": {
        "API": 3.0,
        "WEB": 30.0,
        "WINDOWS": 40.0,
        "EMULATOR": 40.0,
    },
}

STOP_REASON_MANUAL = "Manual stop"
STOP_REASON_CAPACITY_NOOPS = "10 consecutive purchasing-power no-ops"


@dataclass(frozen=True)
class FastTradingAggressionSettings:
    book_levels: int
    cycle_seconds: float


@dataclass(frozen=True)
class FastTradingRuntimeSettings:
    aggression_levels: Mapping[int, FastTradingAggressionSettings]
    minimum_cycle_seconds_by_medium: Mapping[str, float]


@dataclass
class FastTradingCycleResult:
    trading_mode: str
    symbol: str
    resting_side: str
    order_side: str
    book_levels: int
    total_resting_qty: int = 0
    limit_price: Optional[float] = None
    per_account_qty: int = 0
    allocated_quantity: int = 0
    cooldown_eligible_account_ids: List[str] = field(default_factory=list)
    capacity_eligible_account_ids: List[str] = field(default_factory=list)
    commands: List[Dict[str, Any]] = field(default_factory=list)
    capacity_noop: bool = False
    reason: str = ""


@dataclass
class _FastStrategy:
    key: str
    command: Dict[str, Any]
    stop_event: threading.Event
    thread: Optional[threading.Thread] = None
    started_at: str = ""
    cycles: int = 0
    published_commands: int = 0
    consecutive_capacity_noops: int = 0
    last_reason: str = ""


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_finite_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_float(value: Any, label: str) -> float:
    parsed = _as_finite_float(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return parsed


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a positive integer") from None
    if parsed <= 0 or str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


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


def _runtime_settings(raw: Optional[Mapping[str, Any]]) -> FastTradingRuntimeSettings:
    source = raw or FAST_TRADING_DEFAULT_SETTINGS
    raw_levels = source.get("aggression_levels")
    raw_media = source.get("minimum_cycle_seconds_by_medium")
    if not isinstance(raw_levels, Mapping) or not isinstance(raw_media, Mapping):
        raise ValueError("fast trading settings require aggression_levels and minimum_cycle_seconds_by_medium")

    levels: Dict[int, FastTradingAggressionSettings] = {}
    for level in (1, 2, 3):
        level_raw = raw_levels.get(level, raw_levels.get(str(level)))
        if not isinstance(level_raw, Mapping):
            raise ValueError(f"fast trading aggression level {level} is missing")
        book_levels = _positive_int(level_raw.get("book_levels"), f"aggression level {level} book_levels")
        if book_levels > FAST_MAX_BOOK_LEVELS:
            raise ValueError(f"aggression level {level} book_levels must be at most {FAST_MAX_BOOK_LEVELS}")
        levels[level] = FastTradingAggressionSettings(
            book_levels=book_levels,
            cycle_seconds=_positive_float(
                level_raw.get("cycle_seconds"),
                f"aggression level {level} cycle_seconds",
            ),
        )

    media: Dict[str, float] = {}
    for medium in ("API", "WEB", "WINDOWS", "EMULATOR"):
        value = raw_media.get(medium, raw_media.get(medium.lower()))
        media[medium] = _positive_float(value, f"{medium} minimum cycle time")
    return FastTradingRuntimeSettings(
        aggression_levels=levels,
        minimum_cycle_seconds_by_medium=media,
    )


def _normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    for prefix in ("US.", "HK.", "CN.", "SH.", "SZ."):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _usd_cash(snapshot: AccountSnapshot) -> float:
    available_cash = getattr(snapshot, "available_cash_by_currency", {}) or {}
    if "USD" in available_cash:
        value = _as_finite_float(available_cash.get("USD"))
    elif "USD" in snapshot.cash_by_currency:
        value = _as_finite_float(snapshot.cash_by_currency.get("USD"))
    else:
        value = _as_finite_float(snapshot.cash)
    return max(0.0, value or 0.0)


def _symbol_position_qty(snapshot: AccountSnapshot, symbol: str) -> int:
    target = _normalize_symbol(symbol)
    qty = 0.0
    for position in snapshot.positions:
        if _normalize_symbol(getattr(position, "symbol", "")) != target:
            continue
        available_qty = getattr(position, "available_qty", None)
        position_qty = _as_finite_float(
            getattr(position, "qty", 0.0) if available_qty is None else available_qty
        )
        if position_qty is not None:
            qty += position_qty
    return int(math.floor(max(0.0, qty)))


def _account_capacity(
    account_id: str,
    mode: str,
    symbol: str,
    limit_price: float,
    account_snapshots: Optional[Mapping[str, AccountSnapshot]],
) -> int:
    # Production supplies account snapshots. Keeping the provider optional
    # makes the pure command builder usable independently in focused tests.
    if account_snapshots is None:
        return 10**15
    snapshot = account_snapshots.get(account_id)
    if snapshot is None:
        return 0
    if mode == "E":
        return int(math.floor(_usd_cash(snapshot) / limit_price))
    return _symbol_position_qty(snapshot, symbol)


def _deduplicated_account_ids(account_ids: Sequence[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in account_ids:
        account_id = str(value or "").strip()
        if account_id and account_id not in seen:
            out.append(account_id)
            seen.add(account_id)
    return out


def _ordered_top_levels(levels: Sequence[BookLevel], mode: str, book_levels: int) -> List[BookLevel]:
    valid: List[BookLevel] = []
    for level in levels:
        price = _as_finite_float(getattr(level, "price", None))
        quantity = _as_finite_float(getattr(level, "quantity", None))
        if price is None or price <= 0 or quantity is None or quantity <= 0:
            continue
        valid.append(level)
    valid.sort(
        key=lambda level: float(getattr(level, "price")),
        reverse=(mode == "F"),
    )
    return valid[:book_levels]


def _book_quantity_and_limit(
    *,
    mode: str,
    levels: Sequence[BookLevel],
    price_limit: float,
    book_levels: int,
) -> tuple[int, Optional[float], str]:
    top_levels = _ordered_top_levels(levels, mode, book_levels)
    if not top_levels:
        return 0, None, "no_resting_orders"

    prices = [float(getattr(level, "price")) for level in top_levels]
    if mode == "E":
        execution_limit = min(max(prices), price_limit)
        in_range = [level for level in top_levels if float(getattr(level, "price")) <= price_limit]
    else:
        execution_limit = max(min(prices), price_limit)
        in_range = [level for level in top_levels if float(getattr(level, "price")) >= price_limit]

    total = 0.0
    for level in in_range:
        qty = _as_finite_float(getattr(level, "quantity", None))
        if qty is not None and qty > 0:
            total += qty
    total_qty = int(math.floor(total))
    if total_qty <= 0:
        return 0, float(execution_limit), "no_resting_orders_within_price_limit"
    return total_qty, float(execution_limit), ""


def _cooldown_eligible_accounts(
    *,
    account_ids: Sequence[str],
    account_metas: Mapping[str, AccountMeta],
    minimum_cycle_seconds_by_medium: Mapping[str, float],
    last_action_monotonic_by_account: Optional[Mapping[str, float]],
    now_monotonic: float,
) -> List[str]:
    last_actions = last_action_monotonic_by_account or {}
    eligible: List[str] = []
    for account_id in account_ids:
        meta = account_metas.get(account_id)
        if meta is None:
            continue
        medium = str(getattr(meta, "trading_medium", "") or "API").strip().upper()
        minimum_cycle = minimum_cycle_seconds_by_medium.get(
            medium,
            minimum_cycle_seconds_by_medium["API"],
        )
        last_action = _as_finite_float(last_actions.get(account_id))
        if last_action is None or now_monotonic - last_action >= minimum_cycle:
            eligible.append(account_id)
    return eligible


def _even_capacity_allocation(
    *,
    total_qty: int,
    account_ids: Sequence[str],
    mode: str,
    symbol: str,
    limit_price: float,
    account_snapshots: Optional[Mapping[str, AccountSnapshot]],
) -> tuple[Dict[str, int], bool, str]:
    candidates = list(account_ids)
    if not candidates:
        return {}, False, "no_accounts_off_cooldown"

    while candidates:
        per_account_qty = total_qty // len(candidates)
        underfunded = {
            account_id
            for account_id in candidates
            if _account_capacity(
                account_id,
                mode,
                symbol,
                limit_price,
                account_snapshots,
            ) < per_account_qty
        }
        if not underfunded:
            if per_account_qty < FAST_MIN_EXECUTABLE_SHARES:
                return {}, False, "per_account_quantity_below_minimum"
            return {account_id: per_account_qty for account_id in candidates}, False, ""
        remaining = [account_id for account_id in candidates if account_id not in underfunded]
        if not remaining and per_account_qty < FAST_MIN_EXECUTABLE_SHARES:
            return {}, False, "per_account_quantity_below_minimum"
        candidates = remaining

    return {}, True, "no_account_capacity"


def build_fast_trading_cycle_commands(
    trading_mode: str,
    symbol: str,
    book: OrderBookSnapshot,
    price_limit: float,
    account_ids: Sequence[str],
    book_levels: int,
    account_metas: Mapping[str, AccountMeta],
    account_snapshots: Optional[Mapping[str, AccountSnapshot]] = None,
    minimum_cycle_seconds_by_medium: Optional[Mapping[str, float]] = None,
    last_action_monotonic_by_account: Optional[Mapping[str, float]] = None,
    now_monotonic: Optional[float] = None,
    cycle_id: Optional[str] = None,
) -> FastTradingCycleResult:
    mode = str(trading_mode or "").strip().upper()
    clean_symbol = str(symbol or "").strip().upper()
    if mode not in FAST_TRADING_MODES:
        raise ValueError("fast trading mode must be E or F")
    if not clean_symbol:
        raise ValueError("symbol is required")
    clean_price_limit = _positive_float(price_limit, "fast trading price limit")
    clean_book_levels = _positive_int(book_levels, "fast trading book levels")
    if clean_book_levels > FAST_MAX_BOOK_LEVELS:
        raise ValueError(f"fast trading book levels must be at most {FAST_MAX_BOOK_LEVELS}")
    clean_account_ids = _deduplicated_account_ids(account_ids)
    if not clean_account_ids:
        raise ValueError("fast trading account_ids must be non-empty")
    missing_accounts = [account_id for account_id in clean_account_ids if account_id not in account_metas]
    if missing_accounts:
        raise ValueError(f"unknown fast trading account: {missing_accounts[0]}")

    resting_side = "ASK" if mode == "E" else "BID"
    order_side = "BUY" if mode == "E" else "SELL"
    result = FastTradingCycleResult(
        trading_mode=mode,
        symbol=clean_symbol,
        resting_side=resting_side,
        order_side=order_side,
        book_levels=clean_book_levels,
    )
    if book.error:
        result.reason = str(book.error)
        return result

    levels = list(book.asks if mode == "E" else book.bids)
    total_qty, execution_limit, book_reason = _book_quantity_and_limit(
        mode=mode,
        levels=levels,
        price_limit=clean_price_limit,
        book_levels=clean_book_levels,
    )
    result.total_resting_qty = total_qty
    result.limit_price = execution_limit
    if book_reason:
        result.reason = book_reason
        return result
    assert execution_limit is not None

    minimum_cycles = dict(FAST_TRADING_DEFAULT_SETTINGS["minimum_cycle_seconds_by_medium"])
    if minimum_cycle_seconds_by_medium is not None:
        for raw_medium, raw_seconds in minimum_cycle_seconds_by_medium.items():
            medium = str(raw_medium).strip().upper()
            minimum_cycles[medium] = _positive_float(raw_seconds, f"{medium} minimum cycle time")
    current_monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
    result.cooldown_eligible_account_ids = _cooldown_eligible_accounts(
        account_ids=clean_account_ids,
        account_metas=account_metas,
        minimum_cycle_seconds_by_medium=minimum_cycles,
        last_action_monotonic_by_account=last_action_monotonic_by_account,
        now_monotonic=current_monotonic,
    )
    if not result.cooldown_eligible_account_ids:
        result.reason = "no_accounts_off_cooldown"
        return result

    allocation_account_ids = result.cooldown_eligible_account_ids
    missing_snapshot_ids: List[str] = []
    if account_snapshots is not None:
        allocation_account_ids = [
            account_id
            for account_id in result.cooldown_eligible_account_ids
            if account_id in account_snapshots and account_snapshots.get(account_id) is not None
        ]
        missing_snapshot_ids = [
            account_id
            for account_id in result.cooldown_eligible_account_ids
            if account_id not in allocation_account_ids
        ]
        if not allocation_account_ids:
            result.reason = "account_snapshots_unavailable"
            return result

    allocations, capacity_noop, allocation_reason = _even_capacity_allocation(
        total_qty=total_qty,
        account_ids=allocation_account_ids,
        mode=mode,
        symbol=clean_symbol,
        limit_price=execution_limit,
        account_snapshots=account_snapshots,
    )
    if capacity_noop and missing_snapshot_ids:
        # Capacity exhaustion is only confirmed when every otherwise-eligible
        # account has a snapshot. Missing data must never drive auto-stop.
        result.reason = "account_snapshots_incomplete"
        return result
    result.capacity_noop = capacity_noop
    if allocation_reason:
        result.reason = allocation_reason
        return result

    result.capacity_eligible_account_ids = list(allocations)
    result.per_account_qty = next(iter(allocations.values()))
    result.allocated_quantity = sum(allocations.values())
    cycle_id = cycle_id or f"fast_{mode}_{clean_symbol}_{int(time.time() * 1_000_000)}"
    for account_id, qty in allocations.items():
        cmd = build_limit_order_fok_command(
            account_id=account_id,
            symbol=clean_symbol,
            side=order_side,
            shares=qty,
            limit_price=execution_limit,
            account_metas=dict(account_metas),
        )
        cmd.update(
            {
                "trading_mode": mode,
                "algo_cycle_id": cycle_id,
                "fast_trading_resting_side": resting_side,
                "fast_trading_total_resting_qty": total_qty,
                "fast_trading_book_levels": clean_book_levels,
                "fast_trading_price_limit": clean_price_limit,
            }
        )
        result.commands.append(cmd)
    return result


class FastTradingStrategyManager:
    def __init__(
        self,
        market_data_store: MarketDataStore,
        account_metas_provider: Callable[[], Dict[str, AccountMeta]],
        publish_command: Callable[[Dict[str, Any], str], None],
        account_snapshots_provider: Optional[Callable[[], Dict[str, AccountSnapshot]]] = None,
        operations_log: Optional[OperationsLog] = None,
        fast_trading_settings: Optional[Mapping[str, Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._market_data_store = market_data_store
        self._account_metas_provider = account_metas_provider
        self._account_snapshots_provider = account_snapshots_provider
        self._operations_log = operations_log
        self._publish_command = publish_command
        self._settings = _runtime_settings(fast_trading_settings)
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._strategies: Dict[str, _FastStrategy] = {}
        # Shared by all E/F strategies and retained across session stops for
        # the lifetime of this control-server process.
        self._last_action_monotonic_by_account: Dict[str, float] = {}
        # A publisher may block. Keep accounts reserved while their command is
        # being handed off so a concurrent strategy cannot use them too.
        self._inflight_account_ids: set[str] = set()

    @staticmethod
    def _strategy_key(trading_mode: str, symbol: str) -> str:
        return f"{str(trading_mode).strip().upper()}:{str(symbol).strip().upper()}"

    @staticmethod
    def _strategy_accounts(command: Mapping[str, Any]) -> set[str]:
        raw_accounts = command.get("fast_trading_account_ids")
        if not isinstance(raw_accounts, list):
            return set()
        return set(_deduplicated_account_ids(raw_accounts))

    def _validated_strategy_values(
        self,
        command: Mapping[str, Any],
    ) -> tuple[str, str, float, List[str], int, FastTradingAggressionSettings]:
        mode = str(command.get("trading_mode") or "").strip().upper()
        symbol = str(command.get("symbol") or "").strip().upper()
        if mode not in FAST_TRADING_MODES:
            raise ValueError("fast trading strategy mode must be E or F")
        if not symbol:
            raise ValueError("fast trading strategy requires a symbol")
        price_limit = _positive_float(command.get("fast_trading_price_limit"), "fast trading price limit")
        raw_accounts = command.get("fast_trading_account_ids")
        if not isinstance(raw_accounts, list):
            raise ValueError("fast trading strategy requires account ids")
        account_ids = _deduplicated_account_ids(raw_accounts)
        if not account_ids:
            raise ValueError("fast trading strategy requires account ids")
        aggression_level = _positive_int(
            command.get("fast_trading_aggression_level"),
            "fast trading aggression level",
        )
        aggression = self._settings.aggression_levels.get(aggression_level)
        if aggression is None:
            raise ValueError("fast trading aggression level must be 1, 2, or 3")
        return mode, symbol, price_limit, account_ids, aggression_level, aggression

    def start_strategy(self, start_command: Dict[str, Any]) -> str:
        mode, symbol, _, _, aggression_level, aggression = self._validated_strategy_values(start_command)
        strategy_command = dict(start_command)
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
        self._record_strategy_started(strategy)
        thread.start()
        LOGGER.info(
            "Algo trading started successfully mode=%s symbol=%s key=%s command_id=%s aggression=%s "
            "book_levels=%s cycle_seconds=%s test_mode=%s",
            mode,
            symbol,
            key,
            strategy_command.get("command_id"),
            aggression_level,
            aggression.book_levels,
            aggression.cycle_seconds,
            _truthy_bool(strategy_command.get("fast_trading_test_mode")),
        )
        return key

    def stop_strategy(self, trading_mode: str, symbol: str) -> int:
        return self._stop_keys(
            [self._strategy_key(trading_mode, symbol)],
            reason=STOP_REASON_MANUAL,
        )

    def stop_matching(self, trading_mode: str, account_ids: Optional[List[str]] = None) -> int:
        mode = str(trading_mode or "").strip().upper()
        account_filter = set(_deduplicated_account_ids(account_ids or []))
        keys: List[str] = []
        with self._lock:
            for key, strategy in self._strategies.items():
                strategy_mode = str(strategy.command.get("trading_mode") or "").strip().upper()
                if strategy_mode != mode:
                    continue
                if account_filter and not (self._strategy_accounts(strategy.command) & account_filter):
                    continue
                keys.append(key)
        return self._stop_keys(keys, reason=STOP_REASON_MANUAL)

    def stop_all(self) -> int:
        with self._lock:
            keys = list(self._strategies)
        return self._stop_keys(keys, reason=STOP_REASON_MANUAL)

    def _stop_keys(self, keys: List[str], reason: str) -> int:
        strategies: List[_FastStrategy] = []
        with self._lock:
            for key in keys:
                strategy = self._strategies.pop(key, None)
                if strategy is not None:
                    strategy.last_reason = reason
                    strategies.append(strategy)

        for strategy in strategies:
            strategy.stop_event.set()
        for strategy in strategies:
            if strategy.thread is not None and strategy.thread is not threading.current_thread():
                strategy.thread.join(timeout=2.0)
            LOGGER.info(
                "Algo trading stopped mode=%s symbol=%s key=%s command_id=%s reason=%s",
                strategy.command.get("trading_mode"),
                strategy.command.get("symbol"),
                strategy.key,
                strategy.command.get("command_id"),
                reason,
            )
        return len(strategies)

    def _record_strategy_started(self, strategy: _FastStrategy) -> None:
        if self._operations_log is None:
            return
        try:
            self._operations_log.record_algo_started(
                strategy.command,
                strategy_key=strategy.key,
                session_id=strategy.command.get("command_id"),
            )
        except Exception:
            LOGGER.exception("Failed recording algo start operations event key=%s", strategy.key)

    def _record_strategy_ended(
        self,
        strategy: _FastStrategy,
        *,
        reason: str,
        cycles: int,
        published_commands: int,
    ) -> None:
        if self._operations_log is None:
            return
        try:
            self._operations_log.record_algo_ended(
                strategy.command,
                strategy_key=strategy.key,
                session_id=strategy.command.get("command_id"),
                reason=reason,
                cycles=cycles,
                published_commands=published_commands,
            )
        except Exception:
            LOGGER.exception("Failed recording algo end operations event key=%s", strategy.key)

    def _run_strategy(self, strategy: _FastStrategy) -> None:
        try:
            _, _, _, _, _, aggression = self._validated_strategy_values(strategy.command)
            while not strategy.stop_event.is_set():
                cycle_start = self._monotonic()
                result = self.run_cycle(strategy.command)
                test_mode = _truthy_bool(strategy.command.get("fast_trading_test_mode"))
                published_count = 0 if test_mode else len(result.commands)

                reached_noop_limit = False
                with self._lock:
                    strategy.cycles += 1
                    strategy.published_commands += published_count
                    if result.commands:
                        strategy.consecutive_capacity_noops = 0
                    elif result.capacity_noop:
                        strategy.consecutive_capacity_noops += 1
                    if strategy.consecutive_capacity_noops >= FAST_MAX_CONSECUTIVE_CAPACITY_NOOPS:
                        strategy.last_reason = STOP_REASON_CAPACITY_NOOPS
                        reached_noop_limit = True
                    else:
                        strategy.last_reason = result.reason

                if reached_noop_limit:
                    LOGGER.info(
                        "Algo trading automatically stopped mode=%s symbol=%s key=%s command_id=%s reason=%s",
                        strategy.command.get("trading_mode"),
                        strategy.command.get("symbol"),
                        strategy.key,
                        strategy.command.get("command_id"),
                        STOP_REASON_CAPACITY_NOOPS,
                    )
                    break

                elapsed = self._monotonic() - cycle_start
                if strategy.stop_event.wait(max(0.0, aggression.cycle_seconds - elapsed)):
                    break
        except Exception:
            strategy.last_reason = "Strategy error"
            LOGGER.exception("Fast trading strategy failed key=%s", strategy.key)
        finally:
            with self._lock:
                existing = self._strategies.get(strategy.key)
                if existing is strategy:
                    self._strategies.pop(strategy.key, None)
                reason = strategy.last_reason or "Strategy stopped"
                cycles = strategy.cycles
                published_commands = strategy.published_commands
            self._record_strategy_ended(
                strategy,
                reason=reason,
                cycles=cycles,
                published_commands=published_commands,
            )

    def run_cycle(self, command: Dict[str, Any]) -> FastTradingCycleResult:
        mode, symbol, price_limit, account_ids, aggression_level, aggression = self._validated_strategy_values(command)
        book = self._market_data_store.get_book(symbol, depth_limit=aggression.book_levels)
        account_metas = self._account_metas_provider()
        account_snapshots = (
            self._account_snapshots_provider()
            if self._account_snapshots_provider is not None
            else None
        )
        now_monotonic = self._monotonic()

        # Build and reserve accounts atomically so simultaneous E/F or
        # multi-symbol sessions cannot both use the same account while a
        # command is still being handed to the publisher.
        with self._lock:
            effective_last_actions = dict(self._last_action_monotonic_by_account)
            for account_id in self._inflight_account_ids:
                effective_last_actions[account_id] = now_monotonic
            result = build_fast_trading_cycle_commands(
                trading_mode=mode,
                symbol=symbol,
                book=book,
                price_limit=price_limit,
                account_ids=account_ids,
                book_levels=aggression.book_levels,
                account_metas=account_metas,
                account_snapshots=account_snapshots,
                minimum_cycle_seconds_by_medium=self._settings.minimum_cycle_seconds_by_medium,
                last_action_monotonic_by_account=effective_last_actions,
                now_monotonic=now_monotonic,
            )
            reserved_account_ids = {
                str(cmd.get("account_id") or "").strip()
                for cmd in result.commands
                if str(cmd.get("account_id") or "").strip()
            }
            self._inflight_account_ids.update(reserved_account_ids)

        test_mode = _truthy_bool(command.get("fast_trading_test_mode"))
        try:
            for cmd in result.commands:
                cmd["fast_trading_aggression_level"] = aggression_level
                cmd["fast_trading_cycle_seconds"] = aggression.cycle_seconds
                account_id = str(cmd.get("account_id") or "").strip()
                key = account_id or "LIMIT_ORDER_FOK"
                if test_mode:
                    cmd["fast_trading_test_mode"] = True
                    LOGGER.info(
                        "Fast trading TEST MODE logged command without Kafka publish key=%s command=%s",
                        key,
                        _command_json(cmd),
                    )
                else:
                    LOGGER.info("Publishing fast trading command key=%s command=%s", key, _command_json(cmd))
                    self._publish_command(cmd, key)

                if account_id:
                    with self._lock:
                        self._last_action_monotonic_by_account[account_id] = self._monotonic()
                        self._inflight_account_ids.discard(account_id)
        finally:
            # A failed publish is not an account action. Release it (and any
            # later commands that were never attempted) without a cooldown.
            with self._lock:
                self._inflight_account_ids.difference_update(reserved_account_ids)

        LOGGER.info(
            "Fast trading cycle mode=%s symbol=%s resting_side=%s aggression=%s book_levels=%s "
            "total_qty=%s allocated_qty=%s price=%s commands=%s capacity_noop=%s reason=%s",
            result.trading_mode,
            result.symbol,
            result.resting_side,
            aggression_level,
            aggression.book_levels,
            result.total_resting_qty,
            result.allocated_quantity,
            result.limit_price,
            len(result.commands),
            result.capacity_noop,
            result.reason,
        )
        return result
