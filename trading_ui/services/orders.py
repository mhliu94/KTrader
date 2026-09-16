import json
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Optional, Tuple, List

from ..models import AccountMeta


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    value = float(s)
    if not math.isfinite(value):
        raise ValueError("number must be finite")
    return value


def safe_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return int(s)


# ---------------------------
# Market order (existing)
# ---------------------------

def build_market_order_command(
    account_id: str,
    symbol: str,
    side: str,
    shares: Optional[int],
    dollar_amount: Optional[float],
    account_metas: Dict[str, AccountMeta],
) -> Dict:
    meta = account_metas.get(account_id)
    cmd_id = f"cmd_{iso_utc_now()}_{int(time.time() * 1_000_000)}"

    cmd: Dict = {
        "type": "MARKET_ORDER",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
    }

    if meta:
        cmd["broker"] = meta.broker
        cmd["broker_id"] = meta.broker_id

    if shares is not None:
        cmd["qty_shares"] = shares
    if dollar_amount is not None:
        cmd["notional_usd"] = float(dollar_amount)

    return cmd


def _add_broker_metadata(cmd: Dict, account_id: str, account_metas: Dict[str, AccountMeta]) -> Dict:
    meta = account_metas.get(account_id)
    if meta:
        cmd["broker"] = meta.broker
        cmd["broker_id"] = meta.broker_id
    return cmd


def validate_order_inputs(
    account_id: str,
    symbol: str,
    side: str,
    shares_raw: Optional[str],
    dollars_raw: Optional[str],
    account_metas: Dict[str, AccountMeta],
    symbols: List[str],
    # localized strings injected
    invalid_account: str,
    invalid_symbol: str,
    invalid_side: str,
    both_shares_and_dollars: str,
    neither_shares_nor_dollars: str,
    shares_positive: str,
    dollars_positive: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if account_id not in account_metas:
        return None, invalid_account
    if symbol not in symbols:
        return None, invalid_symbol
    if side not in ("BUY", "SELL"):
        return None, invalid_side

    try:
        shares = safe_int(shares_raw)
    except (TypeError, ValueError, OverflowError):
        return None, shares_positive
    try:
        dollars = safe_float(dollars_raw)
    except (TypeError, ValueError, OverflowError):
        return None, dollars_positive

    if shares is not None and dollars is not None:
        return None, both_shares_and_dollars
    if shares is None and dollars is None:
        return None, neither_shares_nor_dollars

    if shares is not None and shares <= 0:
        return None, shares_positive
    if dollars is not None and dollars <= 0:
        return None, dollars_positive

    cmd = build_market_order_command(account_id, symbol, side, shares, dollars, account_metas)
    return cmd, None


def validate_quick_order_inputs(
    account_id: str,
    symbol: str,
    side: str,
    dollars_raw: Optional[str],
    market_last: Optional[float],
    account_metas: Dict[str, AccountMeta],
    symbols: List[str],
    invalid_account: str,
    invalid_symbol: str,
    invalid_side: str,
    dollars_positive: str,
    no_last_price: str,
    dollars_too_low: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if account_id not in account_metas:
        return None, invalid_account
    if symbol not in symbols:
        return None, invalid_symbol
    if side not in ("BUY", "SELL"):
        return None, invalid_side

    try:
        dollars = safe_float(dollars_raw)
    except (TypeError, ValueError, OverflowError):
        return None, dollars_positive
    if dollars is None or dollars <= 0:
        return None, dollars_positive
    try:
        market_price = float(market_last) if market_last is not None else None
    except (TypeError, ValueError, OverflowError):
        market_price = None
    if market_price is None or not math.isfinite(market_price) or market_price <= 0:
        return None, no_last_price

    share_estimate = dollars / market_price
    if not math.isfinite(share_estimate):
        return None, no_last_price
    shares = int(math.floor(share_estimate))
    if shares <= 0:
        return None, dollars_too_low

    return build_market_order_command(account_id, symbol, side, shares, None, account_metas), None


def build_limit_order_command(
    account_id: str,
    symbol: str,
    side: str,
    shares: int,
    limit_price: float,
    account_metas: Dict[str, AccountMeta],
) -> Dict:
    cmd_id = f"limit_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "LIMIT_ORDER",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "qty_shares": shares,
        "limit_price": float(limit_price),
    }
    return _add_broker_metadata(cmd, account_id, account_metas)


def build_limit_order_fok_command(
    account_id: str,
    symbol: str,
    side: str,
    shares: int,
    limit_price: float,
    account_metas: Dict[str, AccountMeta],
) -> Dict:
    cmd = build_limit_order_command(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares=shares,
        limit_price=limit_price,
        account_metas=account_metas,
    )
    cmd["type"] = "LIMIT_ORDER_FOK"
    cmd["time_in_force"] = "FOK"
    cmd["cancel_unfilled"] = True
    return cmd


def validate_limit_order_inputs(
    account_id: str,
    symbol: str,
    side: str,
    shares_raw: Optional[str],
    limit_price_raw: Optional[str],
    through_market_pct_raw: Optional[str],
    market_last: Optional[float],
    account_metas: Dict[str, AccountMeta],
    symbols: List[str],
    invalid_account: str,
    invalid_symbol: str,
    invalid_side: str,
    shares_positive: str,
    price_positive: str,
    no_last_price: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if account_id not in account_metas:
        return None, invalid_account
    if symbol not in symbols:
        return None, invalid_symbol
    if side not in ("BUY", "SELL"):
        return None, invalid_side

    try:
        shares = safe_int(shares_raw)
    except (TypeError, ValueError, OverflowError):
        return None, shares_positive
    if shares is None or shares <= 0:
        return None, shares_positive

    try:
        limit_price = safe_float(limit_price_raw)
    except (TypeError, ValueError, OverflowError):
        return None, price_positive
    if limit_price is not None:
        if limit_price <= 0:
            return None, price_positive
        return build_limit_order_command(account_id, symbol, side, shares, limit_price, account_metas), None

    try:
        through_market_pct = safe_float(through_market_pct_raw)
    except (TypeError, ValueError, OverflowError):
        return None, price_positive
    if through_market_pct is None or through_market_pct <= 0:
        return None, price_positive
    try:
        market_price = float(market_last) if market_last is not None else None
    except (TypeError, ValueError, OverflowError):
        market_price = None
    if market_price is None or not math.isfinite(market_price) or market_price <= 0:
        return None, no_last_price

    if side == "BUY":
        limit_price = market_price * (1.0 + through_market_pct / 100.0)
    else:
        limit_price = market_price * (1.0 - through_market_pct / 100.0)

    if not math.isfinite(limit_price) or limit_price <= 0:
        return None, price_positive

    return build_limit_order_command(account_id, symbol, side, shares, round(limit_price, 4), account_metas), None


def build_cancel_open_orders_command(
    account_id: str,
    account_metas: Dict[str, AccountMeta],
    symbol: Optional[str] = None,
) -> Dict:
    cmd_id = f"cancel_open_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "CANCEL_OPEN_ORDERS",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "account_id": account_id,
    }
    clean_symbol = str(symbol or "").strip().upper()
    if clean_symbol:
        cmd["symbol"] = clean_symbol
    return _add_broker_metadata(cmd, account_id, account_metas)


def validate_cancel_open_orders_inputs(
    account_id: str,
    symbol: Optional[str],
    account_metas: Dict[str, AccountMeta],
    symbols: List[str],
    invalid_account: str,
    invalid_symbol: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if account_id not in account_metas:
        return None, invalid_account

    clean_symbol = str(symbol or "").strip().upper()
    known_symbols = {str(s).strip().upper() for s in symbols}
    if clean_symbol and clean_symbol not in known_symbols:
        return None, invalid_symbol

    return build_cancel_open_orders_command(account_id, account_metas, clean_symbol or None), None


def build_currency_conversion_command(
    account_id: str,
    source_currency: str,
    target_currency: str,
    source_amount: float,
    account_metas: Dict[str, AccountMeta],
) -> Dict:
    cmd_id = f"fx_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "CURRENCY_CONVERSION",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "account_id": account_id,
        "source_currency": source_currency,
        "target_currency": target_currency,
        "source_amount": float(source_amount),
    }
    return _add_broker_metadata(cmd, account_id, account_metas)


def validate_currency_conversion_inputs(
    account_id: str,
    source_currency_raw: Optional[str],
    target_currency_raw: Optional[str],
    source_amount_raw: Optional[str],
    account_metas: Dict[str, AccountMeta],
    invalid_account: str,
    invalid_currency: str,
    same_currency: str,
    amount_positive: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if account_id not in account_metas:
        return None, invalid_account

    meta = account_metas.get(account_id)
    if meta and str(meta.broker).strip().lower() != "tiger":
        return None, invalid_account

    source_currency = str(source_currency_raw or "").strip().upper()
    target_currency = str(target_currency_raw or "").strip().upper()
    if source_currency not in ("HKD", "USD") or target_currency not in ("HKD", "USD"):
        return None, invalid_currency
    if source_currency == target_currency:
        return None, same_currency

    source_amount = safe_float(source_amount_raw)
    if source_amount is None or source_amount <= 0:
        return None, amount_positive

    return build_currency_conversion_command(
        account_id=account_id,
        source_currency=source_currency,
        target_currency=target_currency,
        source_amount=source_amount,
        account_metas=account_metas,
    ), None


def build_trading_status_command(
    account_id: str,
    trading_enabled: bool,
    account_metas: Dict[str, AccountMeta],
) -> Dict:
    cmd_id = f"trading_status_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "SET_TRADING_ENABLED",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "account_id": account_id,
        "trading_enabled": bool(trading_enabled),
    }
    return _add_broker_metadata(cmd, account_id, account_metas)


def validate_trading_status_inputs(
    account_id: str,
    trading_enabled_raw: str,
    account_metas: Dict[str, AccountMeta],
    invalid_account: str,
    invalid_status: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    clean_account_id = str(account_id or "").strip()
    if clean_account_id not in account_metas:
        return None, invalid_account

    raw_status = str(trading_enabled_raw or "").strip().lower()
    if raw_status in ("1", "true", "yes", "on", "enabled"):
        trading_enabled = True
    elif raw_status in ("0", "false", "no", "off", "disabled"):
        trading_enabled = False
    else:
        return None, invalid_status

    return build_trading_status_command(clean_account_id, trading_enabled, account_metas), None


# ---------------------------
# Algo trading (new)
# ---------------------------

_ALLOWED_MODES = ("A", "B", "C", "D", "E", "F")
_FAST_MODES = ("E", "F")
_US_EASTERN = ZoneInfo("America/New_York")


def _parse_datetime_local_to_us_eastern_iso(dt_local: str) -> Optional[str]:
    """
    Input from <input type="datetime-local"> like: "2099-12-31T00:00"
    Interpret as US Eastern Time and emit ISO8601 with offset.
    """
    s = (dt_local or "").strip()
    if not s:
        return None
    # Accept both "YYYY-MM-DDTHH:MM" and "YYYY-MM-DDTHH:MM:SS"
    try:
        if len(s) == 16:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
        else:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=_US_EASTERN)
        return dt.isoformat()
    except Exception:
        return None


def build_algo_start_command(
    trading_mode: str,
    symbol: str,
    max_volume: Optional[float],
    market_volume_target: Optional[float],
    end_time_et_iso: Optional[str],
    abs_pos_change_limit: Optional[float],
    price_target: Optional[float],
    single_order_notional_limit: Optional[float],
    order_rate_limit_per_minute: Optional[float],
    fast_trading_price_limit: Optional[float] = None,
    fast_trading_account_ids: Optional[List[str]] = None,
    fast_trading_aggression_level: Optional[int] = None,
    fast_trading_test_mode: Optional[bool] = None,
) -> Dict:
    cmd_id = f"algo_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "START_ALGO_TRADING",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "trading_mode": trading_mode,
        "symbol": symbol,
    }
    if trading_mode in _FAST_MODES:
        cmd.update(
            {
                "fast_trading_price_limit": fast_trading_price_limit,
                "fast_trading_account_ids": list(fast_trading_account_ids or []),
                "fast_trading_aggression_level": fast_trading_aggression_level,
                "fast_trading_test_mode": bool(fast_trading_test_mode),
            }
        )
    else:
        cmd.update(
            {
                "max_volume": max_volume,
                "market_volume_target": market_volume_target,
                "end_time_et": end_time_et_iso,
                "abs_pos_change_limit": abs_pos_change_limit,
                "price_target": price_target,
                "single_order_notional_limit": single_order_notional_limit,
                "order_rate_limit_per_minute": order_rate_limit_per_minute,
            }
        )
    return cmd


def _truthy_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def parse_fast_trading_config_payload(
    fast_trading_config_raw: object,
    fast_config_required: str,
) -> Tuple[Optional[object], Optional[str]]:
    if fast_trading_config_raw is None:
        return None, fast_config_required

    if isinstance(fast_trading_config_raw, str):
        raw = fast_trading_config_raw.strip()
        if not raw:
            return None, fast_config_required
        try:
            return json.loads(raw), None
        except json.JSONDecodeError:
            return None, fast_config_required

    return fast_trading_config_raw, None


def parse_fast_trading_settings(
    fast_trading_config_raw: object,
    account_metas: Dict[str, AccountMeta],
    invalid_account: str,
    fast_config_required: str,
    fast_price_limit_positive: str,
    fast_accounts_required: str,
    fast_account_duplicate: str,
    fast_aggression_level_invalid: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    payload, err = parse_fast_trading_config_payload(fast_trading_config_raw, fast_config_required)
    if err:
        return None, err

    if not isinstance(payload, dict):
        return None, fast_config_required

    try:
        price_limit = safe_float(payload.get("price_limit"))
    except (TypeError, ValueError, OverflowError):
        price_limit = None
    if price_limit is None or price_limit <= 0:
        return None, fast_price_limit_positive

    raw_account_ids = payload.get("account_ids")
    if not isinstance(raw_account_ids, list) or not raw_account_ids:
        return None, fast_accounts_required

    account_ids: List[str] = []
    seen_accounts: set[str] = set()
    for raw_account_id in raw_account_ids:
        account_id = str(raw_account_id or "").strip()
        if account_id not in account_metas:
            return None, invalid_account
        if account_id in seen_accounts:
            return None, fast_account_duplicate.format(account=account_id)
        seen_accounts.add(account_id)
        account_ids.append(account_id)

    try:
        aggression_level = safe_int(payload.get("aggression_level"))
    except (TypeError, ValueError, OverflowError):
        aggression_level = None
    if aggression_level not in (1, 2, 3):
        return None, fast_aggression_level_invalid

    return {
        "price_limit": float(price_limit),
        "account_ids": account_ids,
        "aggression_level": aggression_level,
        "test_mode": _truthy_bool(payload.get("test_mode")),
    }, None


def parse_fast_trading_test_mode(
    fast_trading_config_raw: object,
    fast_config_required: str,
) -> Tuple[bool, Optional[str]]:
    payload, err = parse_fast_trading_config_payload(fast_trading_config_raw, fast_config_required)
    if err:
        return False, err
    if not isinstance(payload, dict):
        return False, None
    return _truthy_bool(payload.get("test_mode")), None


def validate_algo_start_inputs(
    trading_mode: str,
    symbol: str,
    max_volume_raw: Optional[str],
    market_volume_target_raw: Optional[str],
    end_time_et_raw: Optional[str],
    abs_pos_change_limit_raw: Optional[str],
    price_target_raw: Optional[str],
    single_order_notional_limit_raw: Optional[str],
    order_rate_limit_per_minute_raw: Optional[str],
    fast_trading_config_raw: object,
    symbols: List[str],
    account_metas: Dict[str, AccountMeta],
    # localized strings injected
    invalid_mode: str,
    invalid_symbol: str,
    invalid_account: str,
    number_required: str,
    end_time_required: str,
    fast_config_required: str,
    fast_price_limit_positive: str,
    fast_accounts_required: str,
    fast_account_duplicate: str,
    fast_aggression_level_invalid: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if trading_mode not in _ALLOWED_MODES:
        return None, invalid_mode
    if symbol not in symbols:
        return None, invalid_symbol

    if trading_mode in _FAST_MODES:
        fast_settings, err = parse_fast_trading_settings(
            fast_trading_config_raw=fast_trading_config_raw,
            account_metas=account_metas,
            invalid_account=invalid_account,
            fast_config_required=fast_config_required,
            fast_price_limit_positive=fast_price_limit_positive,
            fast_accounts_required=fast_accounts_required,
            fast_account_duplicate=fast_account_duplicate,
            fast_aggression_level_invalid=fast_aggression_level_invalid,
        )
        if err:
            return None, err
        assert fast_settings is not None
        return build_algo_start_command(
            trading_mode=trading_mode,
            symbol=symbol,
            max_volume=None,
            market_volume_target=None,
            end_time_et_iso=None,
            abs_pos_change_limit=None,
            price_target=None,
            single_order_notional_limit=None,
            order_rate_limit_per_minute=None,
            fast_trading_price_limit=fast_settings["price_limit"],
            fast_trading_account_ids=fast_settings["account_ids"],
            fast_trading_aggression_level=fast_settings["aggression_level"],
            fast_trading_test_mode=fast_settings["test_mode"],
        ), None

    try:
        max_volume = safe_float(max_volume_raw)
        mvt = safe_float(market_volume_target_raw)
        abs_lim = safe_float(abs_pos_change_limit_raw)
        price_target = safe_float(price_target_raw)
        single_order_notional_limit = safe_float(single_order_notional_limit_raw)
        order_rate_limit_per_minute = safe_float(order_rate_limit_per_minute_raw)
    except (TypeError, ValueError, OverflowError):
        return None, number_required

    if (
        max_volume is None
        or mvt is None
        or abs_lim is None
        or price_target is None
        or single_order_notional_limit is None
        or order_rate_limit_per_minute is None
    ):
        return None, number_required

    end_iso = _parse_datetime_local_to_us_eastern_iso(end_time_et_raw or "")
    if end_iso is None:
        return None, end_time_required

    cmd = build_algo_start_command(
        trading_mode=trading_mode,
        symbol=symbol,
        max_volume=max_volume,
        market_volume_target=mvt,
        end_time_et_iso=end_iso,
        abs_pos_change_limit=abs_lim,
        price_target=price_target,
        single_order_notional_limit=single_order_notional_limit,
        order_rate_limit_per_minute=order_rate_limit_per_minute,
    )
    return cmd, None


def build_algo_stop_command(
    trading_mode: str,
    account_ids: List[str],
    reason: str | None,
) -> Dict:
    cmd_id = f"algo_stop_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "STOP_ALGO_TRADING",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "trading_mode": trading_mode,
    }
    if account_ids:
        cmd["account_ids"] = account_ids
    if reason:
        cmd["reason"] = reason
    return cmd


def validate_algo_stop_inputs(
    trading_mode: str,
    account_ids: List[str],
    reason_raw: Optional[str],
    account_metas: Dict[str, AccountMeta],
    invalid_mode: str,
    invalid_account: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if trading_mode not in _ALLOWED_MODES:
        return None, invalid_mode

    clean_account_ids: List[str] = []
    seen: set[str] = set()
    for account_id in account_ids:
        clean_id = str(account_id).strip()
        if not clean_id:
            continue
        if clean_id not in account_metas:
            return None, invalid_account
        if clean_id in seen:
            continue
        seen.add(clean_id)
        clean_account_ids.append(clean_id)

    reason = (reason_raw or "").strip()
    cmd = build_algo_stop_command(trading_mode=trading_mode, account_ids=clean_account_ids, reason=reason or None)
    return cmd, None
