"""Read shared accounts/authentication plus independent order_ui settings."""

import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading_ui.config import load_account_metas, load_json_file


def default_config_path() -> Path:
    configured = os.getenv("ORDER_UI_CONFIG") or os.getenv("ACCOUNT_DASHBOARD_CONFIG")
    if configured:
        return Path(configured)
    sample = Path(__file__).resolve().parents[1] / "trading_ui" / "sample"
    local = sample / "config.local.json"
    return local if local.exists() else sample / "config.json"


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    return dict(value)


def _string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value.strip()


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between {minimum} and {maximum}.")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def load_config(path: str | Path) -> dict:
    path = Path(path).resolve()
    shared = _object(load_json_file(str(path)), "Config")
    accounts_file = (shared.get("accounts_file") or shared.get("trading_accounts_file")
                     or os.getenv("TRADING_ACCOUNTS_CONFIG"))
    if accounts_file:
        shared["accounts_file"] = str(path.parent / _string(accounts_file, "accounts_file"))
    elif not isinstance(shared.get("accounts"), list) or not shared["accounts"]:
        raise ValueError("Provide accounts_file or a non-empty accounts list, as in the trading UI.")
    accounts = load_account_metas(shared)

    auth = _object(shared.get("auth", {}), "auth")
    users = _object(auth.get("users", {}), "auth.users")
    for name in ("admin", "trader"):
        if name not in users:
            raise ValueError(f"auth.users.{name}.password is required, as in the trading UI.")
    for name, record in users.items():
        record = _object(record, f"auth.users.{name}")
        password = _string(record.get("password"), f"auth.users.{name}.password")
        # Match the trading UI's normalization for its built-in users.
        users[name] = {"password": password if name in ("admin", "trader") else record["password"]}

    settings = _object(shared.get("order_ui", {}), "order_ui")
    allowed = {"timezone", "refresh_interval_seconds", "server", "kafka"}
    if set(settings) - allowed:
        raise ValueError("Unknown order_ui setting(s): " + ", ".join(sorted(set(settings) - allowed)))
    zone = _string(settings.get("timezone", "America/New_York"), "order_ui.timezone")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"Unknown order_ui.timezone: {zone}") from exc
    refresh = _number(settings.get("refresh_interval_seconds", 2), "refresh_interval_seconds", 0.5, 60)

    server = _object(settings.get("server", {}), "order_ui.server")
    defaults = {"host": "127.0.0.1", "port": 8030, "ssl_certfile": "", "ssl_keyfile": ""}
    if set(server) - set(defaults):
        raise ValueError("Unknown order_ui.server setting(s).")
    server = {**defaults, **server}
    server["host"] = _string(server["host"], "order_ui.server.host")
    _number(server["port"], "order_ui.server.port", 1, 65535)
    if not isinstance(server["port"], int):
        raise ValueError("order_ui.server.port must be an integer.")
    for field in ("ssl_certfile", "ssl_keyfile"):
        if not isinstance(server[field], str):
            raise ValueError(f"order_ui.server.{field} must be a string.")
        if server[field]:
            server[field] = str(path.parent / server[field])
            if not Path(server[field]).is_file():
                raise ValueError(f"order_ui.server.{field} does not point to a file.")
    if bool(server["ssl_certfile"]) != bool(server["ssl_keyfile"]):
        raise ValueError("Provide both order_ui.server.ssl_certfile and ssl_keyfile for HTTPS.")

    shared_kafka = _object(shared.get("kafka", {}), "kafka")
    kafka = _object(settings.get("kafka", {}), "order_ui.kafka")
    defaults = {
        "bootstrap_servers": shared_kafka.get("bootstrap_servers", os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")),
        "topic": "order-status",
        "group_id": "order-dashboard",
        "poll_timeout_sec": 1.0,
        "client_config": {},
    }
    if set(kafka) - set(defaults):
        raise ValueError("Unknown order_ui.kafka setting(s).")
    kafka = {**defaults, **kafka}
    for field in ("bootstrap_servers", "topic", "group_id"):
        kafka[field] = _string(kafka[field], f"order_ui.kafka.{field}")
    _number(kafka["poll_timeout_sec"], "order_ui.kafka.poll_timeout_sec", 0.1, 5)
    client_config = _object(kafka["client_config"], "order_ui.kafka.client_config")
    # Only connection/security settings can be overridden; replay must remain deterministic.
    allowed_client = {"security.protocol", "sasl.mechanism", "sasl.mechanisms", "sasl.username",
                      "sasl.password", "ssl.ca.location", "ssl.certificate.location",
                      "ssl.key.location", "ssl.key.password", "ssl.endpoint.identification.algorithm"}
    if set(client_config) - allowed_client:
        raise ValueError("Unsupported order_ui.kafka.client_config key(s): " + ", ".join(sorted(set(client_config) - allowed_client)))
    for key, value in client_config.items():
        if not isinstance(value, str):
            raise ValueError(f"order_ui.kafka.client_config.{key} must be a string.")
        if key.endswith(".location"):
            client_config[key] = str(path.parent / value)
    kafka["client_config"] = client_config
    return {"accounts": accounts, "users": users, "timezone": zone,
            "refresh_interval_seconds": refresh, "server": server, "kafka": kafka}
