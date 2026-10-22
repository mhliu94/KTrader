"""Pure arithmetic and fixed execution policy for Smart selling.

This module does not mutate ledgers, read clocks, access SQLite, or contact a
broker. The engine supplies an explicit decision time and a quote snapshot;
policy returns a fixed child proposal or a reason to wait.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

ZERO = Decimal("0")
PARTICIPATION_RATES = {
    "patient": Decimal(".03"),
    "normal": Decimal(".06"),
    "urgent": Decimal(".10"),
}
SMART_SELLING_DEFAULTS = {
    "horizon_seconds": 7200,
    "mode": "normal",
    "quote_max_age": 5,
    "trade_max_age": 10,
    "time_in_force": "DAY",
}


def parse_decimal(value, label="quantity", *, positive=False, nonnegative=False):
    """Reject booleans, nonfinite values, and prohibited quantity signs."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError(f"{label} must be a finite number") from None
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"invalid {label}")
    return result


def parse_input_decimal(value, label="quantity", *, positive=False, nonnegative=False):
    """Bound external precision without restricting derived internal credit.

    Participation arithmetic can add digits and decimal places to a valid input.
    Internal ledger reads therefore use parse_decimal inside the engine's
    80-digit transaction context; external values are limited here.
    """
    number = parse_decimal(value, label, positive=positive, nonnegative=nonnegative)
    digits = number.as_tuple()
    if len(digits.digits) > 30 or number.adjusted() > 24 or digits.exponent < -24:
        raise ValueError(f"{label} exceeds supported decimal precision")
    return number


def decimal_string(value):
    """Keep Decimal values exact when writing JSON or SQLite payloads."""
    return format(value, "f") if isinstance(value, Decimal) else value


def round_down_to_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up_to_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def parse_timestamp(value, default=None):
    """Accept epoch seconds or an ISO datetime carrying an explicit offset."""
    if value is None:
        return default
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return float(parse_input_decimal(value, "timestamp"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return parsed.timestamp()


def utc_timestamp_string(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def meets_order_minimum(parent, quantity, price):
    """Both quantity and notional rules must hold after downward sizing."""
    return quantity >= parse_decimal(parent["min_quantity"]) and quantity * price >= parse_decimal(
        parent["min_notional"]
    )


@dataclass(frozen=True)
class ChildPlan:
    """A quote-specific proposal; ``quantity=None`` means wait without sending."""

    quantity: Decimal | None
    limit_price: Decimal
    aggressive: bool
    depth_limited: bool
    reason: str = ""


def plan_child_order(parent: Mapping[str, Any], quote: Mapping[str, Any], now: float) -> ChildPlan:
    """Apply fixed participation funding, depth, floor, and partial-flush rules.

    Parent health, lifecycle, and five-second submission spacing are the
    engine's responsibility. This function only proposes a legal child from
    existing free credit; it never earns credit or changes participation.
    """
    parent_quantity = parse_decimal(parent["quantity"])
    filled = parse_decimal(parent["F"])
    reserved = parse_decimal(parent["R"])
    credit = parse_decimal(parent["C"])
    child_cap = parse_decimal(parent["child_cap"])
    quantity_step = parse_decimal(parent["quantity_step"])
    remaining = parent_quantity - filled - reserved
    depth_budget = max(ZERO, quote["bid_size"] - quote["consumed"]) * Decimal(".10")
    depth_limited = depth_budget < min(child_cap, remaining)
    ready_quantity = round_down_to_step(min(child_cap, remaining, depth_budget), quantity_step)

    horizon = parent["deadline_mono"] - parent["start_mono"]
    elapsed_fraction = max(0, min(1, (now - parent["start_mono"]) / horizon))
    progress_reference = parent_quantity * parse_decimal(elapsed_fraction)
    behind_schedule = filled < progress_reference - child_cap
    aggressive = bool(
        parent["mode"] == "urgent"
        or parent["fallback"]
        or behind_schedule
        or parent["imminent_event"]
    )
    if aggressive:
        selected_price = quote["bid"]
    elif parent["mode"] == "patient":
        selected_price = quote["ask"]
    else:
        selected_price = (quote["bid"] + quote["ask"]) / 2
    if selected_price <= quote["bid"]:
        aggressive = True
        selected_price = quote["bid"]
    minimum_price = parse_decimal(parent["minimum_price"]) if parent["minimum_price"] else ZERO
    price = round_up_to_step(
        max(selected_price, quote["bid"], minimum_price), parse_decimal(parent["tick_size"])
    )

    if not meets_order_minimum(parent, ready_quantity, price):
        return ChildPlan(
            None,
            price,
            aggressive,
            depth_limited,
            "depth or residual quantity below broker minimum",
        )
    flush_since = parent["last_send"] if parent["last_send"] is not None else parent["start_mono"]
    if credit < ready_quantity and now - flush_since < 30:
        return ChildPlan(None, price, aggressive, depth_limited, "earning volume credit")
    quantity = round_down_to_step(min(ready_quantity, max(ZERO, credit)), quantity_step)
    if not meets_order_minimum(parent, quantity, price):
        return ChildPlan(
            None, price, aggressive, depth_limited, "earned credit below broker minimum"
        )
    return ChildPlan(quantity, price, aggressive, depth_limited)
