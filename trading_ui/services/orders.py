import json
import time
from datetime import datetime, timedelta, timezone
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
    return float(s)


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

    shares = safe_int(shares_raw)
    dollars = safe_float(dollars_raw)

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

    shares = safe_int(shares_raw)
    if shares is None or shares <= 0:
        return None, shares_positive

    limit_price = safe_float(limit_price_raw)
    if limit_price is not None:
        if limit_price <= 0:
            return None, price_positive
        return build_limit_order_command(account_id, symbol, side, shares, limit_price, account_metas), None

    through_market_pct = safe_float(through_market_pct_raw)
    if through_market_pct is None or through_market_pct <= 0:
        return None, price_positive
    if market_last is None or market_last <= 0:
        return None, no_last_price

    if side == "BUY":
        limit_price = market_last * (1.0 + through_market_pct / 100.0)
    else:
        limit_price = market_last * (1.0 - through_market_pct / 100.0)

    if limit_price <= 0:
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


def _parse_datetime_local_to_us_eastern(dt_local: str) -> Optional[datetime]:
    s = (dt_local or "").strip()
    if not s:
        return None
    try:
        if len(s) == 16:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
        else:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=_US_EASTERN)
    except Exception:
        return None


def build_delayed_market_order_command(
    account_id: str,
    symbol: str,
    side: str,
    shares: Optional[int],
    dollar_amount: Optional[float],
    account_metas: Dict[str, AccountMeta],
    execute_at_iso: str,
    delay_seconds: int,
) -> Dict:
    cmd = build_market_order_command(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares=shares,
        dollar_amount=dollar_amount,
        account_metas=account_metas,
    )
    cmd["type"] = "DELAYED_MARKET_ORDER"
    cmd["execute_at"] = execute_at_iso
    cmd["delay_seconds"] = delay_seconds
    return cmd


def validate_delayed_order_inputs(
    account_id: str,
    symbol: str,
    side: str,
    shares_raw: Optional[str],
    dollars_raw: Optional[str],
    delay_choice_raw: Optional[str],
    execute_at_raw: Optional[str],
    account_metas: Dict[str, AccountMeta],
    symbols: List[str],
    invalid_account: str,
    invalid_symbol: str,
    invalid_side: str,
    both_shares_and_dollars: str,
    neither_shares_nor_dollars: str,
    shares_positive: str,
    dollars_positive: str,
    invalid_delay_choice: str,
    future_time_required: str,
    future_time_must_be_future: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    base_cmd, err = validate_order_inputs(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares_raw=shares_raw,
        dollars_raw=dollars_raw,
        account_metas=account_metas,
        symbols=symbols,
        invalid_account=invalid_account,
        invalid_symbol=invalid_symbol,
        invalid_side=invalid_side,
        both_shares_and_dollars=both_shares_and_dollars,
        neither_shares_nor_dollars=neither_shares_nor_dollars,
        shares_positive=shares_positive,
        dollars_positive=dollars_positive,
    )
    if err:
        return None, err

    delay_choice = str(delay_choice_raw or "").strip().lower()
    preset_delays = {
        "1": 60,
        "2": 120,
        "5": 300,
        "10": 600,
    }

    execute_at: Optional[datetime] = None
    delay_seconds: Optional[int] = None
    now_utc = datetime.now(timezone.utc)

    if delay_choice in preset_delays:
        delay_seconds = preset_delays[delay_choice]
        execute_at = now_utc + timedelta(seconds=delay_seconds)
    elif delay_choice == "custom":
        execute_at_local = _parse_datetime_local_to_us_eastern(execute_at_raw or "")
        if execute_at_local is None:
            return None, future_time_required
        execute_at = execute_at_local.astimezone(timezone.utc)
        delay_seconds = int((execute_at - now_utc).total_seconds())
        if delay_seconds <= 0:
            return None, future_time_must_be_future
    else:
        return None, invalid_delay_choice

    assert base_cmd is not None
    cmd = build_delayed_market_order_command(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares=base_cmd.get("qty_shares"),
        dollar_amount=base_cmd.get("notional_usd"),
        account_metas=account_metas,
        execute_at_iso=execute_at.isoformat().replace("+00:00", "Z"),
        delay_seconds=delay_seconds,
    )
    return cmd, None


def build_algo_start_command(
    trading_mode: str,
    symbol: str,
    max_volume: float,
    market_volume_target: float,
    end_time_et_iso: str,
    abs_pos_change_limit: float,
    price_target: float,
    single_order_notional_limit: float,
    order_rate_limit_per_minute: float,
    fast_trading_groups: Optional[List[Dict]] = None,
) -> Dict:
    cmd_id = f"algo_{iso_utc_now()}_{int(time.time() * 1_000_000)}"
    cmd: Dict = {
        "type": "START_ALGO_TRADING",
        "command_id": cmd_id,
        "ts": iso_utc_now(),
        "trading_mode": trading_mode,
        "symbol": symbol,
        "max_volume": max_volume,
        "market_volume_target": market_volume_target,
        "end_time_et": end_time_et_iso,
        "abs_pos_change_limit": abs_pos_change_limit,
        "price_target": price_target,
        "single_order_notional_limit": single_order_notional_limit,
        "order_rate_limit_per_minute": order_rate_limit_per_minute,
    }
    if fast_trading_groups is not None:
        cmd["fast_trading_groups"] = fast_trading_groups
    return cmd


def parse_fast_trading_groups(
    fast_trading_config_raw: object,
    account_metas: Dict[str, AccountMeta],
    invalid_account: str,
    fast_config_required: str,
    fast_group_required: str,
    fast_price_limit_positive: str,
    fast_group_accounts_required: str,
    fast_account_duplicate: str,
    fast_allocation_positive: str,
    fast_allocation_total: str,
) -> Tuple[Optional[List[Dict]], Optional[str]]:
    if fast_trading_config_raw is None:
        return None, fast_config_required

    if isinstance(fast_trading_config_raw, str):
        raw = fast_trading_config_raw.strip()
        if not raw:
            return None, fast_config_required
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None, fast_config_required
    else:
        payload = fast_trading_config_raw

    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list) or not groups:
        return None, fast_group_required

    normalized_groups: List[Dict] = []
    used_accounts: set[str] = set()

    for idx, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            return None, fast_group_required

        price_limit = safe_float(group.get("price_limit"))
        if price_limit is None or price_limit <= 0:
            return None, fast_price_limit_positive.format(group=idx)

        accounts = group.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            return None, fast_group_accounts_required.format(group=idx)

        total_allocation = 0.0
        normalized_accounts: List[Dict] = []
        seen_in_group: set[str] = set()
        for account in accounts:
            if not isinstance(account, dict):
                return None, fast_group_accounts_required.format(group=idx)

            account_id = str(account.get("account_id") or "").strip()
            if account_id not in account_metas:
                return None, invalid_account
            if account_id in used_accounts or account_id in seen_in_group:
                return None, fast_account_duplicate.format(account=account_id)

            allocation_pct = safe_float(account.get("allocation_pct"))
            if allocation_pct is None or allocation_pct <= 0:
                return None, fast_allocation_positive.format(group=idx)

            seen_in_group.add(account_id)
            total_allocation += allocation_pct
            normalized_accounts.append(
                {
                    "account_id": account_id,
                    "allocation_pct": round(float(allocation_pct), 6),
                }
            )

        if total_allocation > 100.0 + 1e-9:
            return None, fast_allocation_total.format(group=idx)

        used_accounts.update(seen_in_group)
        normalized_groups.append(
            {
                "group_id": idx,
                "price_limit": float(price_limit),
                "accounts": normalized_accounts,
            }
        )

    return normalized_groups, None


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
    fast_group_required: str,
    fast_price_limit_positive: str,
    fast_group_accounts_required: str,
    fast_account_duplicate: str,
    fast_allocation_positive: str,
    fast_allocation_total: str,
) -> Tuple[Optional[Dict], Optional[str]]:
    if trading_mode not in _ALLOWED_MODES:
        return None, invalid_mode
    if symbol not in symbols:
        return None, invalid_symbol

    max_volume = safe_float(max_volume_raw)
    mvt = safe_float(market_volume_target_raw)
    abs_lim = safe_float(abs_pos_change_limit_raw)
    price_target = safe_float(price_target_raw)
    single_order_notional_limit = safe_float(single_order_notional_limit_raw)
    order_rate_limit_per_minute = safe_float(order_rate_limit_per_minute_raw)

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

    fast_trading_groups = None
    if trading_mode in _FAST_MODES:
        fast_trading_groups, err = parse_fast_trading_groups(
            fast_trading_config_raw=fast_trading_config_raw,
            account_metas=account_metas,
            invalid_account=invalid_account,
            fast_config_required=fast_config_required,
            fast_group_required=fast_group_required,
            fast_price_limit_positive=fast_price_limit_positive,
            fast_group_accounts_required=fast_group_accounts_required,
            fast_account_duplicate=fast_account_duplicate,
            fast_allocation_positive=fast_allocation_positive,
            fast_allocation_total=fast_allocation_total,
        )
        if err:
            return None, err

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
        fast_trading_groups=fast_trading_groups,
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
