import json
import os
from typing import Any, Dict


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

    cfg.setdefault("kafka", {})
    cfg["kafka"].setdefault(
        "bootstrap_servers",
        os.getenv("ADMIN_KAFKA_BOOTSTRAP_SERVERS", os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")),
    )
    cfg["kafka"].setdefault(
        "live_trading_updates_topic",
        os.getenv("ADMIN_KAFKA_LIVE_TRADING_UPDATES_TOPIC", "live-trading-updates"),
    )
    cfg["kafka"].setdefault(
        "group_id",
        os.getenv("ADMIN_KAFKA_GROUP_ID", "admin-strategy-monitor"),
    )
    cfg["kafka"].setdefault(
        "auto_offset_reset",
        os.getenv("ADMIN_KAFKA_AUTO_OFFSET_RESET", "latest"),
    )
    cfg["kafka"].setdefault("poll_timeout_sec", 1.0)

    cfg.setdefault("auth", {})
    cfg["auth"].setdefault("users", {})
    users = cfg["auth"]["users"]
    if not isinstance(users, dict):
        raise ValueError("Config auth.users must be an object.")
    if "admin" not in users or not isinstance(users["admin"], dict):
        raise ValueError("Config must provide auth.users.admin.")
    admin_pw = str(users["admin"].get("password", "")).strip()
    if not admin_pw:
        raise ValueError("Config must provide non-empty auth.users.admin.password.")
    users["admin"] = {"password": admin_pw, "role": "admin"}

    cfg.setdefault("server", {})
    cfg["server"].setdefault("host", os.getenv("ADMIN_UI_HOST", os.getenv("UI_HOST", "0.0.0.0")))
    cfg["server"].setdefault("port", int(os.getenv("ADMIN_UI_PORT", "8010")))
    ssl_enabled_raw = str(
        os.getenv("ADMIN_UI_SSL_ENABLED", str(cfg["server"].get("ssl_enabled", False)))
    ).strip().lower()
    cfg["server"]["ssl_enabled"] = ssl_enabled_raw in ("1", "true", "yes", "on")
    cfg["server"].setdefault("ssl_certfile", os.getenv("ADMIN_UI_SSL_CERTFILE", ""))
    cfg["server"].setdefault("ssl_keyfile", os.getenv("ADMIN_UI_SSL_KEYFILE", ""))

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
