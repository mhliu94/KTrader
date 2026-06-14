import os
import json
from typing import Any, Dict, List

from .models import AccountMeta


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

    if "accounts" not in cfg or not isinstance(cfg["accounts"], list) or not cfg["accounts"]:
        raise ValueError("Config missing non-empty 'accounts' list.")
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


def load_account_metas(cfg: Dict[str, Any]) -> Dict[str, AccountMeta]:
    metas: Dict[str, AccountMeta] = {}
    accounts = cfg["accounts"]
    if len(accounts) > 10:
        raise ValueError("Config supports at most 10 accounts.")

    seen_num_ids: set[int] = set()
    for idx, a in enumerate(accounts, start=1):
        raw_num_id = a.get("num_id", idx)
        try:
            num_id = int(raw_num_id)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid num_id for account '{a.get('id', '')}': {raw_num_id!r}")
        if num_id < 1 or num_id > 10:
            raise ValueError(f"num_id out of range [1,10] for account '{a.get('id', '')}': {num_id}")
        if num_id in seen_num_ids:
            raise ValueError(f"Duplicate num_id in accounts config: {num_id}")
        seen_num_ids.add(num_id)

        meta = AccountMeta(
            id=str(a["id"]),
            num_id=num_id,
            broker_id=str(a.get("broker_id", "")),
            broker=str(a.get("broker", "")),
        )
        metas[meta.id] = meta
    return metas


def load_symbols(cfg: Dict[str, Any]) -> List[str]:
    return [str(s) for s in cfg["symbols"]]
