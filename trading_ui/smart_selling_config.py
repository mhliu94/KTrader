"""Defaults and validation for the broker-neutral Smart selling control service."""

from decimal import Decimal, InvalidOperation
import math

from .services.smart_selling_policy import (
    PARTICIPATION_PERCENTAGES,
    decimal_string,
    parse_participation_percentage,
)

INSTRUMENT_DEFAULTS = {
    "tick_size": "0.01",
    "quantity_step": "1",
    "min_quantity": "1",
    "min_notional": "0",
}


def canonical_symbol(value):
    symbol = str(value or "").strip().upper().split("/", 1)[0]
    for prefix in ("US.", "HK.", "CN.", "SH.", "SZ."):
        if symbol.startswith(prefix):
            return symbol[len(prefix) :]
    return symbol


def _validate_instrument_rules(rules):
    if not isinstance(rules, dict):
        raise ValueError("Smart selling instrument rules must be objects")
    if "tick_ladder" in rules:
        raise ValueError("Smart selling currently requires a constant tick_size")
    unknown = rules.keys() - INSTRUMENT_DEFAULTS.keys()
    if unknown:
        raise ValueError(f"Unknown Smart selling instrument rules: {', '.join(sorted(unknown))}")
    for key, raw_value in rules.items():
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"invalid Smart selling instrument {key}") from None
        if not value.is_finite() or value < 0 or (key != "min_notional" and value == 0):
            raise ValueError(f"invalid Smart selling instrument {key}")
    return dict(rules)


def normalize_smart_selling_config(raw=None):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("smart_selling must be an object")
    config = {
        "state_db": "./logs/smart_selling.sqlite3",
        "enabled": True,
        "order_status_topic": "order-status",
        "trading_volume_topic": "trading-volume",
        "quote_max_age": 10,
        "trade_max_age": 15,
        "account_max_age": 60,
        "participation_percentages": {},
        "instrument_defaults": {},
        "instruments": {},
        **raw,
    }
    percentages = config["participation_percentages"]
    if not isinstance(percentages, dict):
        raise ValueError("smart_selling.participation_percentages must be an object")
    unknown = percentages.keys() - PARTICIPATION_PERCENTAGES.keys()
    if unknown:
        raise ValueError(
            "Unknown smart_selling.participation_percentages modes: "
            + ", ".join(sorted(map(str, unknown)))
        )
    config["participation_percentages"] = {
        mode: decimal_string(
            parse_participation_percentage(value, f"smart_selling.participation_percentages.{mode}")
        )
        for mode, value in {**PARTICIPATION_PERCENTAGES, **percentages}.items()
    }
    if type(config["enabled"]) is not bool:
        raise ValueError("smart_selling.enabled must be boolean")
    for key in ("quote_max_age", "trade_max_age", "account_max_age"):
        value = config[key]
        try:
            normalized = float(value)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"smart_selling.{key} must be positive and finite") from None
        if isinstance(value, bool) or not math.isfinite(normalized) or normalized <= 0:
            raise ValueError(f"smart_selling.{key} must be positive and finite")
        config[key] = normalized
    for key in ("state_db", "order_status_topic", "trading_volume_topic"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"smart_selling.{key} must be nonempty")
    if not isinstance(config["instruments"], dict):
        raise ValueError("Smart selling instrument settings must be objects")
    config["instrument_defaults"] = {
        **INSTRUMENT_DEFAULTS,
        **_validate_instrument_rules(config["instrument_defaults"]),
    }
    config["instruments"] = {
        canonical_symbol(symbol): _validate_instrument_rules(rules)
        for symbol, rules in config["instruments"].items()
    }
    return config
