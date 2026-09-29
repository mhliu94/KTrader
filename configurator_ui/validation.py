"""Validate editable file contents without environment overrides or live services."""

import math
from typing import Any

from trading_ui.config import _normalize_fast_trading_config, parse_account_metas


def object_value(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def string(value: Any, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be {'a' if empty else 'a non-empty'} string.")


def number(value: Any, name: str, *, minimum: float = 0, maximum: float = math.inf, integer: bool = False) -> None:
    try:
        finite = isinstance(value, (float, int)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if (isinstance(value, bool) or not finite or value < minimum or value > maximum
            or (integer and not isinstance(value, int))):
        raise ValueError(f"{name} must be {'an integer' if integer else 'a finite number'} between {minimum} and {maximum}.")


def boolean(value: Any, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false.")


def validate_accounts(document: Any) -> None:
    entries = document.get("accounts") if isinstance(document, dict) else document
    if not isinstance(entries, list) or not entries:
        raise ValueError("Trading accounts must contain a non-empty accounts list.")
    for index, entry in enumerate(entries, 1):
        account = object_value(entry, f"Account {index}")
        for key in ("numeric_id", "num_id", "account_num_id"):
            if key in account:
                number(account[key], f"Account {index}.{key}", minimum=1, integer=True)
        for key in ("hidden", "Hidden", "monitor"):
            if key in account:
                boolean(account[key], f"Account {index}.{key}")
        for key in ("broker_id", "tiger_account", "owner_type", "operating_system"):
            if key in account:
                string(account[key], f"Account {index}.{key}", empty=True)
    # Validate hidden accounts too, including duplicate identifiers, so unhiding is safe.
    parse_account_metas(entries, include_hidden=True)


def validate_config(document: Any) -> None:
    cfg = object_value(document, "Configuration")
    path = cfg.get("accounts_file") or cfg.get("trading_accounts_file")
    if path:
        string(path, "accounts_file")
    else:
        raise ValueError("accounts_file must reference the trading accounts JSON file.")
    symbols = cfg.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("symbols must be a non-empty array.")
    for symbol in symbols:
        string(symbol, "Each symbol")
    auth = object_value(cfg.get("auth"), "auth")
    users = object_value(auth.get("users"), "auth.users")
    if not {"admin", "trader"}.issubset(users):
        raise ValueError("auth.users must include admin and trader.")
    for username, details in users.items():
        string(username, "Username")
        user = object_value(details, f"auth.users.{username}")
        string(user.get("password"), f"auth.users.{username}.password")
    server = object_value(cfg.get("server", {}), "server")
    for key in ("host", "ssl_certfile", "ssl_keyfile"):
        if key in server:
            string(server[key], f"server.{key}", empty=key != "host")
    if "port" in server:
        number(server["port"], "server.port", minimum=1, maximum=65535, integer=True)
    if "ssl_enabled" in server:
        boolean(server["ssl_enabled"], "server.ssl_enabled")
    if server.get("ssl_enabled"):
        for key in ("ssl_certfile", "ssl_keyfile"):
            string(server.get(key), f"server.{key}")
    kafka = object_value(cfg.get("kafka", {}), "kafka")
    for key in ("bootstrap_servers", "market_data_bootstrap_servers", "account_details_topic",
                "trading_commands_topic", "market_data_topic", "group_id", "market_data_group_id"):
        if key in kafka:
            string(kafka[key], f"kafka.{key}")
    for key in ("auto_offset_reset", "market_data_auto_offset_reset"):
        if key in kafka and kafka[key] not in ("earliest", "latest", "error"):
            raise ValueError(f"kafka.{key} must be earliest, latest, or error.")
    if "poll_timeout_sec" in kafka:
        number(kafka["poll_timeout_sec"], "kafka.poll_timeout_sec", minimum=0.001)
    producer = object_value(kafka.get("producer", {}), "kafka.producer")
    if "enable_idempotence" in producer:
        boolean(producer["enable_idempotence"], "kafka.producer.enable_idempotence")
    if "linger_ms" in producer:
        number(producer["linger_ms"], "kafka.producer.linger_ms", integer=True)
    if "acks" in producer and (isinstance(producer["acks"], bool) or producer["acks"] not in ("all", "-1", "0", "1", -1, 0, 1)):
        raise ValueError("kafka.producer.acks must be all, -1, 0, or 1.")
    market = object_value(cfg.get("market_data", {}), "market_data")
    if "historical_prices_csv" in market:
        string(market["historical_prices_csv"], "market_data.historical_prices_csv")
    if "market_insights_max_levels" in market:
        number(market["market_insights_max_levels"], "market_data.market_insights_max_levels", minimum=1, maximum=20, integer=True)
    _normalize_fast_trading_config(cfg.get("fast_trading"))
