import os
import json
from typing import Any, Dict, List

from .models import AccountMeta

TRADING_MEDIA = {"EMULATOR", "WINDOWS", "WEB", "API"}


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(config_path: str) -> Dict[str, Any]:
    cfg = load_json_file(config_path)
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
        )
        metas[meta.id] = meta
    return metas


def load_symbols(cfg: Dict[str, Any]) -> List[str]:
    return [str(s) for s in cfg["symbols"]]
