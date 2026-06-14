import json
from typing import Any, Dict, Optional

from ..models import AccountSnapshot, Position


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_snapshot_obj(obj: Dict[str, Any]) -> Optional[AccountSnapshot]:
    try:
        account_id = str(obj["account_id"])
        account_num_id = obj.get("account_num_id")
        account_num_id = int(account_num_id) if account_num_id not in (None, "") else None
        cash = float(obj.get("cash", 0.0))
        raw_cash_by_currency = obj.get("cash_by_currency", {}) or {}
        cash_by_currency: Dict[str, float] = {}
        if isinstance(raw_cash_by_currency, dict):
            for raw_currency, raw_amount in raw_cash_by_currency.items():
                currency = str(raw_currency or "").strip().upper()
                if not currency:
                    continue
                cash_by_currency[currency] = float(raw_amount or 0.0)
        if "USD" not in cash_by_currency:
            cash_by_currency["USD"] = cash
        ts = obj.get("ts")
        trading_enabled = parse_bool(obj.get("trading_enabled", False))

        positions_in = obj.get("positions", []) or []
        positions = []
        for p in positions_in:
            symbol = str(p["symbol"])
            qty = float(p.get("qty", 0.0))
            avg_price = p.get("avg_price", None)
            avg_price = float(avg_price) if avg_price is not None else None
            positions.append(Position(symbol=symbol, qty=qty, avg_price=avg_price))

        return AccountSnapshot(
            account_id=account_id,
            cash=cash,
            account_num_id=account_num_id,
            cash_by_currency=cash_by_currency,
            positions=positions,
            ts=ts,
            trading_enabled=trading_enabled,
        )
    except Exception:
        return None


def load_fallback_snapshots(fallback_path: str) -> Dict[str, AccountSnapshot]:
    """
    Fallback file format:
    {
      "asof": "...",
      "accounts": [
        { "account_id": "...", "cash": ..., "positions": [...] },
        ...
      ]
    }
    """
    try:
        data = load_json_file(fallback_path)
        accounts = data.get("accounts", []) or []
        out: Dict[str, AccountSnapshot] = {}
        for a in accounts:
            snap = parse_snapshot_obj(a)
            if snap is not None:
                snap.trading_enabled = False
                out[snap.account_id] = snap
        return out
    except Exception:
        return {}
