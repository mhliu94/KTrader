import os
import json
import math
from typing import Any, Dict, List

from .models import AccountMeta

TRADING_MEDIA = {"EMULATOR", "WINDOWS", "WEB", "API"}

DEFAULT_FAST_TRADING_AGGRESSION_LEVELS = {
    1: {"book_levels": 2, "cycle_seconds": 30.0},
    2: {"book_levels": 3, "cycle_seconds": 20.0},
    3: {"book_levels": 5, "cycle_seconds": 10.0},
}
DEFAULT_FAST_TRADING_MINIMUM_CYCLE_SECONDS_BY_MEDIUM = {
    "API": 3.0,
    "WEB": 30.0,
    "WINDOWS": 40.0,
    "EMULATOR": 40.0,
}


def _positive_finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Config {field} must be a positive finite number.")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"Config {field} must be a positive finite number.")
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"Config {field} must be a positive finite number.")
    return normalized


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Config {field} must be a positive integer.")
    return value


def _normalize_aggression_level_key(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Config fast_trading.aggression_levels must define exactly levels 1, 2, and 3.")
    if isinstance(value, int):
        level = value
    elif isinstance(value, str) and value in {"1", "2", "3"}:
        level = int(value)
    else:
        raise ValueError("Config fast_trading.aggression_levels must define exactly levels 1, 2, and 3.")
    if level not in {1, 2, 3}:
        raise ValueError("Config fast_trading.aggression_levels must define exactly levels 1, 2, and 3.")
    return level


def _normalize_fast_trading_config(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Config fast_trading must be an object.")

    allowed_keys = {"aggression_levels", "minimum_cycle_seconds_by_medium"}
    unknown_keys = set(raw) - allowed_keys
    if unknown_keys:
        names = ", ".join(sorted(str(key) for key in unknown_keys))
        raise ValueError(f"Config fast_trading contains unknown field(s): {names}")

    raw_levels = raw.get("aggression_levels")
    if raw_levels is None:
        aggression_levels = {
            level: dict(settings)
            for level, settings in DEFAULT_FAST_TRADING_AGGRESSION_LEVELS.items()
        }
    else:
        if not isinstance(raw_levels, dict):
            raise ValueError("Config fast_trading.aggression_levels must be an object.")
        aggression_levels: Dict[int, Dict[str, Any]] = {}
        for raw_level, raw_settings in raw_levels.items():
            level = _normalize_aggression_level_key(raw_level)
            if level in aggression_levels:
                raise ValueError(f"Config fast_trading.aggression_levels defines level {level} more than once.")
            if not isinstance(raw_settings, dict):
                raise ValueError(f"Config fast_trading.aggression_levels.{level} must be an object.")
            expected_fields = {"book_levels", "cycle_seconds"}
            if set(raw_settings) != expected_fields:
                raise ValueError(
                    f"Config fast_trading.aggression_levels.{level} must contain exactly "
                    "book_levels and cycle_seconds."
                )
            book_levels = _positive_integer(
                raw_settings["book_levels"],
                f"fast_trading.aggression_levels.{level}.book_levels",
            )
            if book_levels > 20:
                raise ValueError(
                    f"Config fast_trading.aggression_levels.{level}.book_levels must be at most 20."
                )
            aggression_levels[level] = {
                "book_levels": book_levels,
                "cycle_seconds": _positive_finite_number(
                    raw_settings["cycle_seconds"],
                    f"fast_trading.aggression_levels.{level}.cycle_seconds",
                ),
            }
        if set(aggression_levels) != {1, 2, 3}:
            raise ValueError("Config fast_trading.aggression_levels must define exactly levels 1, 2, and 3.")

    minimum_cycles = dict(DEFAULT_FAST_TRADING_MINIMUM_CYCLE_SECONDS_BY_MEDIUM)
    raw_minimum_cycles = raw.get("minimum_cycle_seconds_by_medium")
    if raw_minimum_cycles is not None:
        if not isinstance(raw_minimum_cycles, dict):
            raise ValueError("Config fast_trading.minimum_cycle_seconds_by_medium must be an object.")
        seen_media = set()
        for raw_medium, value in raw_minimum_cycles.items():
            if not isinstance(raw_medium, str):
                raise ValueError("Config fast_trading minimum-cycle medium names must be strings.")
            medium = raw_medium.strip().upper()
            if medium not in TRADING_MEDIA:
                allowed = ", ".join(sorted(TRADING_MEDIA))
                raise ValueError(
                    "Invalid medium in fast_trading.minimum_cycle_seconds_by_medium: "
                    f"{raw_medium!r}. Allowed: {allowed}"
                )
            if medium in seen_media:
                raise ValueError(
                    "Config fast_trading.minimum_cycle_seconds_by_medium defines "
                    f"{medium} more than once."
                )
            seen_media.add(medium)
            minimum_cycles[medium] = _positive_finite_number(
                value,
                f"fast_trading.minimum_cycle_seconds_by_medium.{medium}",
            )

    return {
        "aggression_levels": aggression_levels,
        "minimum_cycle_seconds_by_medium": minimum_cycles,
    }


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(config_path: str) -> Dict[str, Any]:
    cfg = load_json_file(config_path)
    if not isinstance(cfg, dict):
        raise ValueError("Config root must be an object.")
    config_dir = os.path.dirname(os.path.abspath(config_path))

    def _resolve_path(p: str) -> str:
        if not p:
            return p
        if os.path.isabs(p):
            return p
        return os.path.normpath(os.path.join(config_dir, p))

    accounts_file = str(
        cfg.get("accounts_file")
        or cfg.get("trading_accounts_file")
        or os.getenv("TRADING_ACCOUNTS_CONFIG", "")
    ).strip()
    if accounts_file:
        cfg["accounts_file"] = _resolve_path(accounts_file)
    elif "accounts" not in cfg or not isinstance(cfg["accounts"], list) or not cfg["accounts"]:
        raise ValueError("Config missing accounts_file or non-empty 'accounts' list.")
    if "symbols" not in cfg or not isinstance(cfg["symbols"], list) or not cfg["symbols"]:
        raise ValueError("Config missing non-empty 'symbols' list.")

    cfg.setdefault("fallback", {})
    if "file" not in cfg["fallback"]:
        raise ValueError("Config missing fallback.file (path to JSON fallback snapshots).")
    cfg["fallback"]["file"] = _resolve_path(str(cfg["fallback"]["file"]))

    cfg.setdefault("kafka", {})
    cfg["kafka"].setdefault("bootstrap_servers", os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"))
    cfg["kafka"].setdefault(
        "market_data_bootstrap_servers",
        os.getenv("KAFKA_MARKET_DATA_BOOTSTRAP_SERVERS", cfg["kafka"]["bootstrap_servers"]),
    )
    cfg["kafka"].setdefault("account_details_topic", os.getenv("KAFKA_ACCOUNT_DETAILS_TOPIC", "account-details"))
    cfg["kafka"].setdefault("trading_commands_topic", os.getenv("KAFKA_TRADING_COMMANDS_TOPIC", "trading-commands"))
    cfg["kafka"].setdefault("market_data_topic", os.getenv("KAFKA_MARKET_DATA_TOPIC", "price-books"))
    cfg["kafka"].setdefault("group_id", os.getenv("KAFKA_GROUP_ID", "account-dashboard"))
    cfg["kafka"].setdefault("market_data_group_id", os.getenv("KAFKA_MARKET_DATA_GROUP_ID", "market-data-dashboard"))
    cfg["kafka"].setdefault("auto_offset_reset", os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest"))
    cfg["kafka"].setdefault("market_data_auto_offset_reset", os.getenv("KAFKA_MARKET_DATA_AUTO_OFFSET_RESET", "earliest"))
    cfg["kafka"].setdefault("poll_timeout_sec", 1.0)

    cfg["kafka"].setdefault("producer", {})
    cfg["kafka"]["producer"].setdefault("acks", "all")
    cfg["kafka"]["producer"].setdefault("enable_idempotence", True)
    cfg["kafka"]["producer"].setdefault("linger_ms", 5)

    cfg.setdefault("market_data", {})
    cfg["market_data"].setdefault(
        "historical_prices_csv",
        os.getenv("MARKET_DATA_HISTORICAL_PRICES_CSV", "./market_data/historical_prices.csv"),
    )
    max_levels_raw = os.getenv(
        "MARKET_INSIGHTS_MAX_LEVELS",
        str(cfg["market_data"].get("market_insights_max_levels", 20)),
    )
    try:
        max_levels = int(max_levels_raw)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid MARKET_INSIGHTS_MAX_LEVELS: {max_levels_raw!r}")
    cfg["market_data"]["market_insights_max_levels"] = max(1, min(max_levels, 20))
    cfg["market_data"]["historical_prices_csv"] = _resolve_path(
        str(cfg["market_data"]["historical_prices_csv"])
    )

    cfg["fast_trading"] = _normalize_fast_trading_config(cfg.get("fast_trading"))

    cfg.setdefault("auth", {})
    cfg["auth"].setdefault("users", {})
    users = cfg["auth"]["users"]
    if not isinstance(users, dict):
        raise ValueError("Config auth.users must be an object.")

    admin = users.get("admin", {})
    trader = users.get("trader", {})
    if not isinstance(admin, dict) or not isinstance(trader, dict):
        raise ValueError("Config auth.users.admin/trader must be objects.")

    admin_pw = str(admin.get("password", "")).strip()
    trader_pw = str(trader.get("password", "")).strip()
    if not admin_pw or not trader_pw:
        raise ValueError("Config must provide non-empty auth.users.admin.password and auth.users.trader.password.")

    users["admin"] = {"password": admin_pw, "role": "admin"}
    users["trader"] = {"password": trader_pw, "role": "trader"}

    cfg.setdefault("server", {})
    cfg["server"].setdefault("host", os.getenv("UI_HOST", "0.0.0.0"))
    cfg["server"].setdefault("port", int(os.getenv("UI_PORT", "8000")))
    ssl_enabled_raw = str(os.getenv("UI_SSL_ENABLED", str(cfg["server"].get("ssl_enabled", False)))).strip().lower()
    cfg["server"]["ssl_enabled"] = ssl_enabled_raw in ("1", "true", "yes", "on")
    cfg["server"].setdefault("ssl_certfile", os.getenv("UI_SSL_CERTFILE", ""))
    cfg["server"].setdefault("ssl_keyfile", os.getenv("UI_SSL_KEYFILE", ""))

    certfile = str(cfg["server"].get("ssl_certfile", "")).strip()
    keyfile = str(cfg["server"].get("ssl_keyfile", "")).strip()
    cfg["server"]["ssl_certfile"] = _resolve_path(certfile) if certfile else ""
    cfg["server"]["ssl_keyfile"] = _resolve_path(keyfile) if keyfile else ""
    if cfg["server"]["ssl_enabled"]:
        if not cfg["server"]["ssl_certfile"] or not cfg["server"]["ssl_keyfile"]:
            raise ValueError("SSL is enabled but ssl_certfile/ssl_keyfile are missing.")
        if not os.path.isfile(cfg["server"]["ssl_certfile"]):
            raise ValueError(f"SSL cert file not found: {cfg['server']['ssl_certfile']}")
        if not os.path.isfile(cfg["server"]["ssl_keyfile"]):
            raise ValueError(f"SSL key file not found: {cfg['server']['ssl_keyfile']}")

    return cfg


def _account_field(account: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in account:
            return account.get(name)
    return None


def _required_account_string(account: Dict[str, Any], names: tuple[str, ...], label: str) -> str:
    value = _account_field(account, *names)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Account entry missing required string field '{label}'.")
    return value.strip()


def _optional_account_string(account: Dict[str, Any], names: tuple[str, ...], label: str) -> str | None:
    value = _account_field(account, *names)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Account field '{label}' must be a string when provided.")
    value = value.strip()
    return value or None


def _load_account_entries(cfg: Dict[str, Any]) -> tuple[List[Dict[str, Any]], bool]:
    accounts_file = str(cfg.get("accounts_file") or "").strip()
    if not accounts_file:
        return cfg["accounts"], False

    data = load_json_file(accounts_file)
    accounts = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts, list) or not accounts:
        raise ValueError(f"Trading accounts file must contain a non-empty accounts list: {accounts_file}")
    return accounts, True


def load_account_metas(cfg: Dict[str, Any]) -> Dict[str, AccountMeta]:
    metas: Dict[str, AccountMeta] = {}
    accounts, require_static_fields = _load_account_entries(cfg)

    seen_num_ids: set[int] = set()
    seen_string_ids: set[str] = set()
    for idx, a in enumerate(accounts, start=1):
        if not isinstance(a, dict):
            raise ValueError(f"Account entry #{idx} must be an object.")
        if _account_field(a, "hidden", "Hidden") is True:
            continue

        raw_string_id = _account_field(a, "string_id", "id", "account_id")
        if not isinstance(raw_string_id, str) or not raw_string_id.strip():
            raise ValueError(f"Account entry #{idx} missing required string_id.")
        string_id = raw_string_id.strip()
        if string_id in seen_string_ids:
            raise ValueError(f"Duplicate string_id in accounts config: {string_id}")
        seen_string_ids.add(string_id)

        raw_num_id = _account_field(a, "numeric_id", "num_id", "account_num_id")
        try:
            num_id = int(raw_num_id)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid numeric_id for account '{string_id}': {raw_num_id!r}")
        if num_id < 1:
            raise ValueError(f"numeric_id must be positive for account '{string_id}': {num_id}")
        if num_id in seen_num_ids:
            raise ValueError(f"Duplicate numeric_id in accounts config: {num_id}")
        seen_num_ids.add(num_id)

        broker = _required_account_string(a, ("broker",), "broker")
        raw_medium = _account_field(a, "trading_medium", "medium")
        if raw_medium in (None, "") and not require_static_fields:
            raw_medium = "API"
        if not isinstance(raw_medium, str) or not raw_medium.strip():
            raise ValueError(f"Account '{string_id}' missing required trading_medium.")
        trading_medium = raw_medium.strip().upper()
        if trading_medium not in TRADING_MEDIA:
            allowed = ", ".join(sorted(TRADING_MEDIA))
            raise ValueError(f"Invalid trading_medium for account '{string_id}': {raw_medium!r}. Allowed: {allowed}")

        meta = AccountMeta(
            id=string_id,
            num_id=num_id,
            broker=broker,
            trading_medium=trading_medium,
            broker_id=str(a.get("broker_id", "") or "").strip(),
            ip_address=_optional_account_string(a, ("ip_address", "ip"), "ip_address"),
            machine_alias=_optional_account_string(a, ("machine_alias",), "machine_alias"),
            monitor=a.get("monitor") is True,
        )
        metas[meta.id] = meta
    return metas


def load_symbols(cfg: Dict[str, Any]) -> List[str]:
    symbols: List[str] = []
    seen = set()
    for raw_symbol in cfg["symbols"]:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols
