#!/usr/bin/env python3
"""
Long-running Tiger trading command consumer.

The web UI publishes JSON commands to the Kafka trading-commands topic. This
script consumes those commands, executes Tiger market orders, schedules delayed
market orders in-process, and logs algorithmic-trading start/stop commands.
"""

from __future__ import annotations

import heapq
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer


LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOGGER = logging.getLogger("tiger-trading-server")
TIGER_ACCOUNT_REPORT_INTERVAL_SECONDS = 60
TRADING_MEDIA = {"EMULATOR", "WINDOWS", "WEB", "API"}


@dataclass(frozen=True)
class TradingAccountConfig:
    string_id: str
    numeric_id: int
    broker: str
    trading_medium: str
    broker_id: str = ""
    ip_address: Optional[str] = None
    machine_alias: Optional[str] = None
    tiger_account: str = ""


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_csv(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            LOGGER.warning("Invalid integer env %s=%r; using default %s", name, raw, default)
            value = default
    if minimum is not None and value < minimum:
        LOGGER.warning("Env %s=%s is below minimum %s; using %s", name, value, minimum, minimum)
        value = minimum
    return value


def normalize_tiger_segment(value: str) -> str:
    raw = str(value or "").strip().upper()
    aliases = {
        "": "SEC",
        "S": "SEC",
        "STK": "SEC",
        "STOCK": "SEC",
        "SECURITY": "SEC",
        "SECURITIES": "SEC",
        "F": "FUT",
        "FUTURE": "FUT",
        "FUTURES": "FUT",
    }
    return aliases.get(raw, raw)


def parse_account_map(raw: str) -> Dict[str, str]:
    """
    Parse TIGER_ACCOUNT_MAP as UI_ACCOUNT:TIGER_ACCOUNT,UI_ACCOUNT2:TIGER_ACCOUNT2.
    """
    mapping: Dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            LOGGER.warning("Ignoring malformed TIGER_ACCOUNT_MAP item: %s", item)
            continue
        ui_account, tiger_account = item.split(":", 1)
        ui_account = ui_account.strip()
        tiger_account = tiger_account.strip()
        if ui_account and tiger_account:
            mapping[ui_account] = tiger_account
    return mapping



def parse_account_num_map(raw: str) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            LOGGER.warning("Ignoring malformed TIGER_UI_ACCOUNT_NUM_ID_MAP item: %s", item)
            continue
        ui_account, raw_num_id = item.split(":", 1)
        ui_account = ui_account.strip()
        raw_num_id = raw_num_id.strip()
        if not ui_account or not raw_num_id:
            continue
        try:
            mapping[ui_account] = int(raw_num_id)
        except ValueError:
            LOGGER.warning("Ignoring invalid Tiger UI account numeric ID mapping: %s", item)
    return mapping


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_config_path(path: str, base_dir: Optional[str] = None) -> str:
    raw = str(path or "").strip()
    if not raw:
        return raw
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(base_dir or os.getcwd(), raw))


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _default_ui_config_path() -> str:
    for relative_path in (
        os.path.join("trading_ui", "sample", "config.local.json"),
        os.path.join("trading_ui", "sample", "config.json"),
    ):
        candidate = os.path.join(_repo_root(), relative_path)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _account_field(account: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in account:
            return account.get(name)
    return None


def _required_account_string(account: Dict[str, Any], names: Tuple[str, ...], label: str) -> str:
    value = _account_field(account, *names)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Account entry missing required string field '%s'." % label)
    return value.strip()


def _optional_account_string(account: Dict[str, Any], names: Tuple[str, ...], label: str) -> Optional[str]:
    value = _account_field(account, *names)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Account field '%s' must be a string when provided." % label)
    value = value.strip()
    return value or None


def _parse_trading_account_entries(
    accounts: Any,
    *,
    source: str,
    require_static_fields: bool,
) -> List[TradingAccountConfig]:
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("Trading accounts config must contain a non-empty accounts list: %s" % source)

    parsed: List[TradingAccountConfig] = []
    seen_num_ids: set[int] = set()
    seen_string_ids: set[str] = set()
    for idx, account in enumerate(accounts, start=1):
        if not isinstance(account, dict):
            raise ValueError("Account entry #%d must be an object in %s." % (idx, source))

        raw_string_id = _account_field(account, "string_id", "id", "account_id")
        if not isinstance(raw_string_id, str) or not raw_string_id.strip():
            raise ValueError("Account entry #%d missing required string_id in %s." % (idx, source))
        string_id = raw_string_id.strip()
        if string_id in seen_string_ids:
            raise ValueError("Duplicate string_id in accounts config: %s" % string_id)
        seen_string_ids.add(string_id)

        raw_num_id = _account_field(account, "numeric_id", "num_id", "account_num_id")
        try:
            numeric_id = int(raw_num_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid numeric_id for account '%s': %r" % (string_id, raw_num_id))
        if numeric_id < 1:
            raise ValueError("numeric_id must be positive for account '%s': %s" % (string_id, numeric_id))
        if numeric_id in seen_num_ids:
            raise ValueError("Duplicate numeric_id in accounts config: %s" % numeric_id)
        seen_num_ids.add(numeric_id)

        broker = _required_account_string(account, ("broker",), "broker")
        raw_medium = _account_field(account, "trading_medium", "medium")
        if raw_medium in (None, "") and not require_static_fields:
            raw_medium = "API"
        if not isinstance(raw_medium, str) or not raw_medium.strip():
            raise ValueError("Account '%s' missing required trading_medium." % string_id)
        trading_medium = raw_medium.strip().upper()
        if trading_medium not in TRADING_MEDIA:
            allowed = ", ".join(sorted(TRADING_MEDIA))
            raise ValueError(
                "Invalid trading_medium for account '%s': %r. Allowed: %s"
                % (string_id, raw_medium, allowed)
            )

        parsed.append(
            TradingAccountConfig(
                string_id=string_id,
                numeric_id=numeric_id,
                broker=broker,
                trading_medium=trading_medium,
                broker_id=str(account.get("broker_id", "") or "").strip(),
                ip_address=_optional_account_string(account, ("ip_address", "ip"), "ip_address"),
                machine_alias=_optional_account_string(account, ("machine_alias",), "machine_alias"),
                tiger_account=_optional_account_string(
                    account,
                    ("tiger_account", "tiger_account_id", "real_account"),
                    "tiger_account",
                )
                or "",
            )
        )
    return parsed


def _load_trading_accounts_file(path: str, *, require_static_fields: bool = True) -> List[TradingAccountConfig]:
    data = load_json_file(path)
    accounts = data.get("accounts") if isinstance(data, dict) else data
    return _parse_trading_account_entries(
        accounts,
        source=path,
        require_static_fields=require_static_fields,
    )


def _load_trading_accounts_from_ui_config(config_path: str, *, explicit: bool) -> List[TradingAccountConfig]:
    try:
        cfg = load_json_file(config_path)
    except FileNotFoundError:
        if explicit:
            raise
        return []
    if not isinstance(cfg, dict):
        raise ValueError("UI config must be a JSON object: %s" % config_path)

    config_dir = os.path.dirname(os.path.abspath(config_path))
    accounts_file = str(cfg.get("accounts_file") or cfg.get("trading_accounts_file") or "").strip()
    if accounts_file:
        return _load_trading_accounts_file(
            _resolve_config_path(accounts_file, config_dir),
            require_static_fields=True,
        )

    accounts = cfg.get("accounts")
    if isinstance(accounts, list) and accounts:
        return _parse_trading_account_entries(
            accounts,
            source=config_path,
            require_static_fields=False,
        )
    return []


def load_trading_account_configs() -> List[TradingAccountConfig]:
    direct_path = (
        os.getenv("TIGER_TRADING_ACCOUNTS_CONFIG", "").strip()
        or os.getenv("TRADING_ACCOUNTS_CONFIG", "").strip()
    )
    if direct_path:
        return _load_trading_accounts_file(_resolve_config_path(direct_path), require_static_fields=True)

    ui_config_path = (
        os.getenv("ACCOUNT_DASHBOARD_CONFIG", "").strip()
        or os.getenv("TRADING_UI_CONFIG", "").strip()
    )
    if ui_config_path:
        return _load_trading_accounts_from_ui_config(_resolve_config_path(ui_config_path), explicit=True)

    default_config = _default_ui_config_path()
    if default_config:
        return _load_trading_accounts_from_ui_config(default_config, explicit=False)
    return []


def parse_iso_datetime(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty timestamp")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def command_id(command: Dict[str, Any]) -> str:
    return str(command.get("command_id") or "<missing-command-id>")


def command_summary(command: Dict[str, Any]) -> str:
    interesting = {
        key: command.get(key)
        for key in (
            "type",
            "command_id",
            "account_id",
            "broker",
            "broker_id",
            "symbol",
            "side",
            "qty_shares",
            "notional_usd",
            "execute_at",
            "delay_seconds",
            "trading_mode",
            "source_currency",
            "target_currency",
            "source_amount",
            "trading_enabled",
        )
        if key in command
    }
    return json.dumps(interesting, sort_keys=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pick_first_attr(obj: Any, names: Tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            value = obj[name]
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def normalize_symbol_for_compare(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    for prefix in ("US.", "HK.", "CN.", "SH.", "SZ.", "SG.", "AU."):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def extract_order_symbol(order: Any) -> str:
    symbol = pick_first_attr(order, ("symbol", "stock_symbol", "ticker", "code", "sec_symbol"), None)
    if symbol is None:
        contract = pick_first_attr(order, ("contract",), None)
        if contract is not None:
            symbol = pick_first_attr(
                contract,
                ("symbol", "identifier", "origin_symbol", "local_symbol", "stock_symbol", "ticker", "code", "sec_symbol"),
                None,
            )
    if symbol is None:
        legs = pick_first_attr(order, ("contract_legs", "legs"), None) or []
        first_leg = first_item(legs)
        if first_leg is not None:
            symbol = pick_first_attr(first_leg, ("symbol", "stock_symbol", "ticker", "code", "sec_symbol"), None)
    return normalize_symbol_for_compare(symbol)


def order_symbol_matches(order_symbol: str, target_symbol: str) -> bool:
    return normalize_symbol_for_compare(order_symbol) == normalize_symbol_for_compare(target_symbol)


def positive_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def extract_order_cancel_ref(order: Any) -> Tuple[Optional[str], Optional[int]]:
    global_id = positive_int_or_none(pick_first_attr(order, ("id",), None))
    if global_id is not None:
        return "id", global_id
    order_id = positive_int_or_none(pick_first_attr(order, ("order_id",), None))
    if order_id is not None:
        return "order_id", order_id
    return None, None


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def first_item(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    try:
        return value.iloc[0] if len(value) else None
    except Exception:
        return value


class TigerBroker:
    def __init__(self) -> None:
        self.dry_run = env_bool("TIGER_DRY_RUN", True)
        self.currency = os.getenv("TIGER_CURRENCY", "USD")
        self.cash_currencies = env_csv("TIGER_CASH_CURRENCIES", "USD,HKD")
        self.cancel_order_fetch_limit = env_int("TIGER_CANCEL_ORDER_FETCH_LIMIT", 100, minimum=1)
        self.forex_segment = normalize_tiger_segment(os.getenv("TIGER_FOREX_SEG_TYPE", "SEC"))
        self.default_account = os.getenv("TIGER_ACCOUNT", "").strip()
        self.trading_account_configs = load_trading_account_configs()
        self.trading_accounts_by_id = {account.string_id: account for account in self.trading_account_configs}
        tiger_api_account_configs = [
            account
            for account in self.trading_account_configs
            if account.broker.strip().lower() == "tiger" and account.trading_medium == "API"
        ]
        self.tiger_api_account_ids = {account.string_id for account in tiger_api_account_configs}
        config_ui_accounts = [account.string_id for account in tiger_api_account_configs]
        config_account_map = {
            account.string_id: account.tiger_account
            for account in tiger_api_account_configs
            if account.tiger_account
        }
        env_account_map = parse_account_map(os.getenv("TIGER_ACCOUNT_MAP", ""))
        self.account_map = {**config_account_map, **env_account_map}
        configured_ui_accounts = env_csv("TIGER_UI_ACCOUNT_IDS", "")
        self.ui_account_ids = configured_ui_accounts or config_ui_accounts or sorted(self.account_map) or ["ACC-TIGER"]
        config_account_num_map = {account.string_id: account.numeric_id for account in self.trading_account_configs}
        env_account_num_map = parse_account_num_map(os.getenv("TIGER_UI_ACCOUNT_NUM_ID_MAP", ""))
        self.account_num_map = {**config_account_num_map, **env_account_num_map}
        self._warn_for_static_account_mismatch(configured_ui_accounts)
        if self.trading_account_configs:
            LOGGER.info(
                "Loaded %d trading account config(s); Tiger API account IDs: %s",
                len(self.trading_account_configs),
                ", ".join(config_ui_accounts) or "<none>",
            )
        self._trading_enabled_default = env_bool("TIGER_TRADING_ENABLED_DEFAULT", not self.dry_run)
        self._trading_state_file = os.getenv(
            "TIGER_TRADING_STATE_FILE",
            os.path.join(os.path.dirname(__file__), "tiger_trading_state.json"),
        ).strip()
        self._trading_state_lock = threading.RLock()
        self._trading_enabled_by_account: Dict[str, bool] = {}
        self._load_trading_state()
        self._trade_client = None
        self._stock_contract = None
        self._market_order = None
        self._market_order_by_amount = None
        self._limit_order = None
        self._open_order_statuses = None
        self._validate_account_routing_config()

        if self.dry_run:
            LOGGER.warning("TIGER_DRY_RUN is enabled; orders will be logged only.")
            has_private_key = bool(os.getenv("TIGER_PRIVATE_KEY_PATH", "").strip() or os.getenv("TIGER_PRIVATE_KEY", "").strip())
            license_value = (os.getenv("TIGER_LICENSE", "").strip() or os.getenv("TIGEROPEN_LICENSE", "").strip()).upper()
            needs_token = license_value == "TBHK"
            has_report_config = bool(
                os.getenv("TIGER_ID", "").strip()
                and (os.getenv("TIGER_TOKEN", "").strip() or not needs_token)
                and self.default_account
                and has_private_key
            )
            if not has_report_config:
                LOGGER.warning("Skipping Tiger client init in dry-run because reporting credentials are incomplete.")
                return

        self._init_tiger_client()

    def _warn_for_static_account_mismatch(self, configured_ui_accounts: List[str]) -> None:
        if not self.trading_account_configs or not configured_ui_accounts:
            return

        unknown = [account_id for account_id in configured_ui_accounts if account_id not in self.trading_accounts_by_id]
        if unknown:
            LOGGER.warning(
                "Tiger listener account ID(s) are not in the trading account config: %s",
                ", ".join(unknown),
            )

        non_tiger = [
            account_id
            for account_id in configured_ui_accounts
            if account_id in self.trading_accounts_by_id and account_id not in self.tiger_api_account_ids
        ]
        if non_tiger:
            LOGGER.warning(
                "Tiger listener account ID(s) are configured but are not Tiger/API accounts: %s",
                ", ".join(non_tiger),
            )

    def _validate_account_routing_config(self) -> None:
        if len(self.ui_account_ids) <= 1:
            return

        missing = [account_id for account_id in self.ui_account_ids if account_id not in self.account_map]
        if missing:
            raise RuntimeError(
                "TIGER_ACCOUNT_MAP or static tiger_account entries must map every TIGER_UI_ACCOUNT_IDS "
                "entry when one Tiger listener handles multiple UI accounts. For one-account listeners, "
                "set TIGER_UI_ACCOUNT_IDS to that listener's static account ID. Missing mappings: %s"
                % ", ".join(missing)
            )

    def _init_tiger_client(self) -> None:
        try:
            from tigeropen.common.consts import Language
            from tigeropen.common.util.contract_utils import stock_contract
            from tigeropen.common.util.order_utils import OrderStatus, limit_order, market_order, market_order_by_amount
            from tigeropen.common.util.signature_utils import read_private_key
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError(
                "tigeropen is required when TIGER_DRY_RUN=false. Install the Tiger OpenAPI SDK."
            ) from exc

        private_key_path = os.getenv("TIGER_PRIVATE_KEY_PATH", "").strip()
        private_key = (os.getenv("TIGER_PRIVATE_KEY", "").strip() or os.getenv("TIGEROPEN_PRIVATE_KEY", "").strip())
        tiger_id = (os.getenv("TIGER_ID", "").strip() or os.getenv("TIGEROPEN_TIGER_ID", "").strip())
        token = (os.getenv("TIGER_TOKEN", "").strip() or os.getenv("TIGEROPEN_TOKEN", "").strip())
        secret_key = (os.getenv("TIGER_SECRET_KEY", "").strip() or os.getenv("TIGEROPEN_SECRET_KEY", "").strip())
        license_value = (os.getenv("TIGER_LICENSE", "").strip() or os.getenv("TIGEROPEN_LICENSE", "").strip())
        props_path = os.getenv("TIGEROPEN_PROPS_PATH", "").strip()

        client_config = TigerOpenClientConfig(
            sandbox_debug=env_bool("TIGER_SANDBOX_DEBUG", False),
            props_path=props_path or None,
        )
        if tiger_id:
            client_config.tiger_id = tiger_id
        if self.default_account:
            client_config.account = self.default_account
        if token:
            client_config.token = token
        if secret_key:
            client_config.secret_key = secret_key
        if license_value:
            client_config.license = license_value
        if private_key_path:
            client_config.private_key = read_private_key(private_key_path)
        elif private_key:
            client_config.private_key = private_key
        client_config.language = Language.en_US

        account = str(client_config.account or "").strip()
        effective_license = str(client_config.license or "").strip().upper()
        self.default_account = account

        missing = []
        if not str(client_config.tiger_id or "").strip():
            missing.append("TIGER_ID, TIGEROPEN_TIGER_ID, or tiger_id")
        if not account:
            missing.append("TIGER_ACCOUNT, TIGEROPEN_ACCOUNT, or account")
        if effective_license == "TBHK" and not str(client_config.token or "").strip():
            missing.append("TIGER_TOKEN, TIGEROPEN_TOKEN, or tiger_openapi_token.properties token")
        if not str(client_config.private_key or "").strip():
            missing.append("TIGER_PRIVATE_KEY_PATH, TIGER_PRIVATE_KEY, TIGEROPEN_PRIVATE_KEY, or private_key_pk8/private_key_pk1")
        if missing:
            raise RuntimeError("Missing Tiger configuration: " + ", ".join(missing))

        self._trade_client = TradeClient(client_config)
        self._stock_contract = stock_contract
        self._market_order = market_order
        self._market_order_by_amount = market_order_by_amount
        self._limit_order = limit_order
        self._open_order_statuses = [
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_NEW,
            OrderStatus.HELD,
        ]
        LOGGER.info("Tiger TradeClient initialized for account=%s", account)

    def resolve_account(self, command: Dict[str, Any]) -> str:
        ui_account = str(command.get("account_id") or "").strip()
        mapped = self.account_map.get(ui_account)
        if mapped:
            return mapped
        if ui_account:
            if self.account_map:
                raise ValueError(
                    "No Tiger account mapping configured for command account_id=%r; update TIGER_ACCOUNT_MAP "
                    "or add a tiger_account entry to the trading account config"
                    % ui_account
                )
            if self.ui_account_ids and ui_account not in self.ui_account_ids:
                raise ValueError("Tiger listener is not configured for command account_id=%r" % ui_account)
            if len(self.ui_account_ids) > 1:
                raise ValueError(
                    "Cannot resolve command account_id=%r because this multi-account listener has no "
                    "TIGER_ACCOUNT_MAP or static tiger_account routing"
                    % ui_account
                )
            if self.default_account:
                return self.default_account
        broker_id = str(command.get("broker_id") or "").strip()
        if broker_id:
            return broker_id
        if self.default_account:
            return self.default_account
        raise ValueError("No Tiger account configured for command account_id=%r" % ui_account)

    def _load_trading_state(self) -> None:
        if not self._trading_state_file:
            return
        try:
            with open(self._trading_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            LOGGER.exception("Failed loading Tiger trading state file=%s", self._trading_state_file)
            return

        accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
        if not isinstance(accounts, dict):
            return
        with self._trading_state_lock:
            self._trading_enabled_by_account = {
                str(account_id): bool(enabled)
                for account_id, enabled in accounts.items()
                if str(account_id).strip()
            }

    def _save_trading_state(self) -> None:
        if not self._trading_state_file:
            return
        payload = {
            "updated_at": utc_now_iso(),
            "default_enabled": self._trading_enabled_default,
            "accounts": dict(sorted(self._trading_enabled_by_account.items())),
        }
        tmp_path = self._trading_state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._trading_state_file)), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, self._trading_state_file)
        except Exception:
            LOGGER.exception("Failed saving Tiger trading state file=%s", self._trading_state_file)

    def _command_account_id(self, command: Dict[str, Any]) -> str:
        account_id = str(command.get("account_id") or "").strip()
        if account_id:
            return account_id
        return self.default_account

    def is_trading_enabled(self, account_id: str) -> bool:
        account_id = str(account_id or "").strip()
        if not account_id:
            return False
        with self._trading_state_lock:
            return bool(self._trading_enabled_by_account.get(account_id, self._trading_enabled_default))

    def is_trading_enabled_for_command(self, command: Dict[str, Any]) -> bool:
        return self.is_trading_enabled(self._command_account_id(command))

    def set_trading_enabled(self, command: Dict[str, Any]) -> None:
        account_id = self._command_account_id(command)
        if not account_id:
            raise ValueError("Trading status command missing account_id")
        # Validate the account can resolve to a Tiger account before mutating local state.
        self.resolve_account({**command, "account_id": account_id})
        enabled = bool(command.get("trading_enabled"))
        with self._trading_state_lock:
            self._trading_enabled_by_account[account_id] = enabled
            self._save_trading_state()
        LOGGER.info(
            "Set Tiger trading status command_id=%s account_id=%s trading_enabled=%s",
            command_id(command),
            account_id,
            enabled,
        )

    def _ensure_trading_enabled(self, command: Dict[str, Any], action_name: str) -> None:
        if self.is_trading_enabled_for_command(command):
            return
        account_id = self._command_account_id(command)
        raise RuntimeError("Tiger trading is disabled for account_id=%s; blocked %s" % (account_id, action_name))

    def account_reporting_targets(self, ui_account_ids: List[str]) -> List[Tuple[str, str]]:
        targets: List[Tuple[str, str]] = []
        seen: set[str] = set()

        for ui_account in ui_account_ids:
            real_account = self.account_map.get(ui_account) or self.default_account
            if ui_account and real_account:
                targets.append((ui_account, real_account))
                seen.add(ui_account)

        for ui_account, real_account in self.account_map.items():
            if ui_account not in seen and real_account:
                targets.append((ui_account, real_account))
                seen.add(ui_account)

        if not targets and self.default_account:
            targets.append((self.default_account, self.default_account))

        return targets

    def account_snapshot(self, ui_account: str, real_account: str) -> Dict[str, Any]:
        if self._trade_client is None:
            raise RuntimeError("Tiger account reporting requires Tiger client credentials")

        cash_by_currency = self._query_cash_by_currency(real_account)
        usd_cash = cash_by_currency.get("USD", 0.0)
        return {
            "account_id": ui_account,
            "account_num_id": self.account_num_map.get(ui_account),
            "cash": usd_cash,
            "cash_by_currency": cash_by_currency,
            "positions": self._query_positions(real_account),
            "ts": utc_now_iso(),
            "trading_enabled": self.is_trading_enabled(ui_account),
        }

    def _query_usd_cash(self, account: str) -> float:
        return self._query_cash(account, self.currency)

    def _query_cash_by_currency(self, account: str) -> Dict[str, float]:
        assert self._trade_client is not None

        cash_by_currency: Dict[str, float] = {}
        try:
            assets = self._trade_client.get_prime_assets(account=account, base_currency="USD")
            cash_by_currency.update(self._cash_by_currency_from_prime_assets(first_item(assets)))
        except Exception:
            LOGGER.debug("Tiger get_prime_assets failed while querying cash map account=%s", account, exc_info=True)

        for currency in self.cash_currencies:
            currency = str(currency).strip().upper()
            if not currency or currency in cash_by_currency:
                continue
            try:
                cash_by_currency[currency] = self._query_cash(account, currency)
            except Exception:
                LOGGER.debug("Tiger cash query failed account=%s currency=%s", account, currency, exc_info=True)

        if not cash_by_currency:
            raise RuntimeError("Unable to find cash balances in Tiger assets for account=%s" % account)

        return dict(sorted(cash_by_currency.items()))

    def _query_cash(self, account: str, currency: str) -> float:
        assert self._trade_client is not None
        currency = str(currency or "").strip().upper()
        if not currency:
            raise RuntimeError("Currency is required for Tiger cash query")

        try:
            assets = self._trade_client.get_prime_assets(account=account, base_currency=currency)
            cash = self._cash_from_prime_assets(first_item(assets), currency)
            if cash is not None:
                return cash
        except Exception:
            LOGGER.debug("Tiger get_prime_assets failed for account=%s currency=%s", account, currency, exc_info=True)

        assets = self._trade_client.get_assets(account=account, market_value=True)
        cash = self._cash_from_global_assets(first_item(assets), currency)
        if cash is not None:
            return cash
        raise RuntimeError("Unable to find %s cash in Tiger assets for account=%s" % (currency, account))

    def _cash_by_currency_from_prime_assets(self, portfolio: Any) -> Dict[str, float]:
        if portfolio is None:
            return {}

        out: Dict[str, float] = {}
        segments = pick_first_attr(portfolio, ("segments",), {}) or {}
        security_segment = segments.get("S") if isinstance(segments, dict) else None
        if security_segment is None:
            security_segment = pick_first_attr(portfolio, ("summary",), None)

        currency_assets = pick_first_attr(security_segment, ("currency_assets", "_currency_assets"), {}) or {}
        if isinstance(currency_assets, dict):
            for raw_currency, currency_asset in currency_assets.items():
                currency = str(raw_currency or "").strip().upper()
                if not currency:
                    continue
                cash = pick_first_attr(currency_asset, ("cash_balance", "cash_available_for_trade"), None)
                if cash is not None:
                    out[currency] = as_float(cash)
        return out

    def _cash_from_prime_assets(self, portfolio: Any, currency: str) -> Optional[float]:
        if portfolio is None:
            return None

        currency = str(currency or "").strip().upper()
        cash_by_currency = self._cash_by_currency_from_prime_assets(portfolio)
        if currency in cash_by_currency:
            return cash_by_currency[currency]

        segments = pick_first_attr(portfolio, ("segments",), {}) or {}
        security_segment = segments.get("S") if isinstance(segments, dict) else None
        if security_segment is None:
            security_segment = pick_first_attr(portfolio, ("summary",), None)

        cash = pick_first_attr(security_segment, ("cash_balance", "cash_available_for_trade"), None)
        return as_float(cash) if cash is not None else None

    def _cash_from_global_assets(self, portfolio: Any, currency: str) -> Optional[float]:
        if portfolio is None:
            return None

        currency = str(currency or "").strip().upper()
        market_values = pick_first_attr(portfolio, ("market_values", "market_value"), {}) or {}
        market_value = market_values.get(currency) if isinstance(market_values, dict) else None
        if market_value is not None:
            cash = pick_first_attr(market_value, ("cash_balance",), None)
            if cash is not None:
                return as_float(cash)

        summary = pick_first_attr(portfolio, ("summary",), portfolio)
        cash = pick_first_attr(summary, ("cash", "cash_balance", "available_funds"), None)
        return as_float(cash) if cash is not None else None

    def _query_positions(self, account: str) -> List[Dict[str, Any]]:
        assert self._trade_client is not None

        positions: List[Dict[str, Any]] = []
        for position in self._trade_client.get_positions(account=account):
            contract = pick_first_attr(position, ("contract",), None)
            symbol = pick_first_attr(contract, ("symbol",), None) or pick_first_attr(position, ("symbol",), None)
            if not symbol:
                continue

            qty = as_float(pick_first_attr(position, ("position_qty", "quantity", "qty"), 0.0))
            if qty == 0.0:
                continue

            avg_price = pick_first_attr(position, ("average_cost", "average_cost_by_average", "avg_price"), None)
            positions.append(
                {
                    "symbol": str(symbol).strip().upper(),
                    "qty": qty,
                    "avg_price": as_float(avg_price, None) if avg_price is not None else None,
                }
            )

        return positions

    def place_market_order(self, command: Dict[str, Any]) -> None:
        symbol = str(command.get("symbol") or "").strip().upper()
        side = str(command.get("side") or "").strip().upper()
        if not symbol:
            raise ValueError("Market order missing symbol")
        if side not in ("BUY", "SELL"):
            raise ValueError("Market order has invalid side: %r" % side)

        account = self.resolve_account(command)
        self._ensure_trading_enabled(command, "market order")
        qty = command.get("qty_shares")
        notional = command.get("notional_usd")

        if qty is not None and notional is not None:
            raise ValueError("Market order cannot contain both qty_shares and notional_usd")
        if qty is None and notional is None:
            raise ValueError("Market order requires qty_shares or notional_usd")

        if qty is not None:
            quantity = int(qty)
            if quantity <= 0:
                raise ValueError("qty_shares must be positive")
            order_kind = "shares"
            order_size = quantity
        else:
            amount = int(round(float(notional)))
            if amount <= 0:
                raise ValueError("notional_usd must round to a positive integer amount")
            order_kind = "amount"
            order_size = amount

        if self.dry_run:
            LOGGER.info(
                "DRY RUN Tiger market order command_id=%s account=%s symbol=%s side=%s %s=%s",
                command_id(command),
                account,
                symbol,
                side,
                order_kind,
                order_size,
            )
            return

        assert self._trade_client is not None
        assert self._stock_contract is not None
        assert self._market_order is not None
        assert self._market_order_by_amount is not None

        contract = self._stock_contract(symbol=symbol, currency=self.currency)
        if qty is not None:
            order = self._market_order(account=account, contract=contract, action=side, quantity=order_size)
        else:
            order = self._market_order_by_amount(account=account, contract=contract, action=side, amount=order_size)

        tiger_order_id = self._trade_client.place_order(order)
        if tiger_order_id is None:
            raise RuntimeError(
                "Tiger market order command_id=%s returned no order id; order may not have been accepted: %s"
                % (command_id(command), order)
            )
        LOGGER.info(
            "Placed Tiger market order command_id=%s tiger_order_id=%s order=%s",
            command_id(command),
            tiger_order_id,
            order,
        )

    def place_limit_order(self, command: Dict[str, Any]) -> None:
        symbol = str(command.get("symbol") or "").strip().upper()
        side = str(command.get("side") or "").strip().upper()
        if not symbol:
            raise ValueError("Limit order missing symbol")
        if side not in ("BUY", "SELL"):
            raise ValueError("Limit order has invalid side: %r" % side)

        qty = command.get("qty_shares")
        price = command.get("limit_price")
        if qty is None:
            raise ValueError("Limit order requires qty_shares")
        if price is None:
            raise ValueError("Limit order requires limit_price")

        quantity = int(qty)
        limit_price = float(price)
        if quantity <= 0:
            raise ValueError("qty_shares must be positive")
        if limit_price <= 0:
            raise ValueError("limit_price must be positive")

        account = self.resolve_account(command)
        self._ensure_trading_enabled(command, "limit order")
        if self.dry_run:
            LOGGER.info(
                "DRY RUN Tiger limit order command_id=%s account=%s symbol=%s side=%s shares=%s limit_price=%s",
                command_id(command),
                account,
                symbol,
                side,
                quantity,
                limit_price,
            )
            return

        assert self._trade_client is not None
        assert self._stock_contract is not None
        assert self._limit_order is not None

        contract = self._stock_contract(symbol=symbol, currency=self.currency)
        order = self._limit_order(account=account, contract=contract, action=side, quantity=quantity, limit_price=limit_price)
        tiger_order_id = self._trade_client.place_order(order)
        if tiger_order_id is None:
            raise RuntimeError(
                "Tiger limit order command_id=%s returned no order id; order may not have been accepted: %s"
                % (command_id(command), order)
            )
        LOGGER.info(
            "Placed Tiger limit order command_id=%s tiger_order_id=%s order=%s",
            command_id(command),
            tiger_order_id,
            order,
        )

    def convert_currency(self, command: Dict[str, Any]) -> None:
        source_currency = str(command.get("source_currency") or "").strip().upper()
        target_currency = str(command.get("target_currency") or "").strip().upper()
        if source_currency not in ("HKD", "USD"):
            raise ValueError("Currency conversion has invalid source_currency: %r" % source_currency)
        if target_currency not in ("HKD", "USD"):
            raise ValueError("Currency conversion has invalid target_currency: %r" % target_currency)
        if source_currency == target_currency:
            raise ValueError("Currency conversion requires different source and target currencies")

        amount = as_float(command.get("source_amount"), 0.0)
        if amount <= 0:
            raise ValueError("Currency conversion requires positive source_amount")

        account = self.resolve_account(command)
        self._ensure_trading_enabled(command, "currency conversion")
        if self.dry_run:
            LOGGER.info(
                "DRY RUN Tiger currency conversion command_id=%s account=%s source_currency=%s target_currency=%s source_amount=%s",
                command_id(command),
                account,
                source_currency,
                target_currency,
                amount,
            )
            return

        assert self._trade_client is not None
        result = self._trade_client.place_forex_order(
            self.forex_segment,
            source_currency,
            target_currency,
            amount,
        )
        LOGGER.info("Placed Tiger currency conversion command_id=%s result=%s", command_id(command), result)

    def _get_open_orders_for_cancel(self, account: str, target_symbol: str) -> List[Any]:
        assert self._trade_client is not None
        assert self._open_order_statuses is not None

        all_orders: List[Any] = []
        page_token: Optional[str] = ""
        seen_tokens: set[str] = set()
        while True:
            result = self._trade_client.get_orders(
                account=account,
                states=self._open_order_statuses,
                symbol=target_symbol or None,
                limit=self.cancel_order_fetch_limit,
                is_brief=not bool(target_symbol),
                page_token=page_token,
            )
            if hasattr(result, "result"):
                current_orders = list(getattr(result, "result", None) or [])
                next_page_token = str(getattr(result, "next_page_token", "") or "")
            else:
                current_orders = list(result or [])
                next_page_token = ""

            all_orders.extend(current_orders)
            if not next_page_token:
                break
            if next_page_token in seen_tokens:
                LOGGER.warning(
                    "Stopping Tiger open-order pagination because next_page_token repeated account=%s symbol=%s token=%s",
                    account,
                    target_symbol or "<all>",
                    next_page_token,
                )
                break
            seen_tokens.add(next_page_token)
            page_token = next_page_token

        return all_orders

    def cancel_open_orders(self, command: Dict[str, Any]) -> None:
        account = self.resolve_account(command)
        target_symbol = normalize_symbol_for_compare(command.get("symbol"))
        if self.dry_run:
            LOGGER.info(
                "DRY RUN Tiger cancel open orders command_id=%s account=%s symbol=%s",
                command_id(command),
                account,
                target_symbol or "<all>",
            )
            return

        assert self._trade_client is not None
        assert self._open_order_statuses is not None

        orders = self._get_open_orders_for_cancel(account=account, target_symbol=target_symbol)
        cancelled = 0
        failed = 0
        skipped_symbol = 0
        skipped_unknown_symbol = 0
        skipped_missing_id = 0
        for order in orders:
            if target_symbol:
                order_symbol = extract_order_symbol(order)
                if not order_symbol:
                    skipped_unknown_symbol += 1
                    LOGGER.warning(
                        "Skipping Tiger open order with unknown symbol during symbol-scoped cancel command_id=%s account=%s target_symbol=%s order=%s",
                        command_id(command),
                        account,
                        target_symbol,
                        order,
                    )
                    continue
                if not order_symbol_matches(order_symbol, target_symbol):
                    skipped_symbol += 1
                    continue

            cancel_field, cancel_value = extract_order_cancel_ref(order)
            if cancel_field is None or cancel_value is None:
                skipped_missing_id += 1
                LOGGER.warning("Skipping Tiger open order without usable cancel id: %s", order)
                continue

            try:
                if cancel_field == "id":
                    self._trade_client.cancel_order(account=account, id=cancel_value)
                else:
                    self._trade_client.cancel_order(account=account, order_id=cancel_value)
                cancelled += 1
            except Exception:
                failed += 1
                LOGGER.exception(
                    "Failed cancelling Tiger open order command_id=%s account=%s symbol=%s %s=%s order=%s",
                    command_id(command),
                    account,
                    target_symbol or "<all>",
                    cancel_field,
                    cancel_value,
                    order,
                )

        LOGGER.info(
            "Cancelled Tiger open orders command_id=%s account=%s symbol=%s fetched=%d cancelled=%d failed=%d skipped_symbol=%d skipped_unknown_symbol=%d skipped_missing_id=%d",
            command_id(command),
            account,
            target_symbol or "<all>",
            len(orders),
            cancelled,
            failed,
            skipped_symbol,
            skipped_unknown_symbol,
            skipped_missing_id,
        )


@dataclass(order=True)
class ScheduledCommand:
    execute_at: datetime
    sequence: int
    command: Dict[str, Any] = field(compare=False)


class DelayedOrderScheduler:
    def __init__(
        self,
        broker: TigerBroker,
        stop_event: threading.Event,
        after_execute: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._broker = broker
        self._stop_event = stop_event
        self._after_execute = after_execute
        self._condition = threading.Condition()
        self._queue: List[ScheduledCommand] = []
        self._sequence = 0
        self._thread = threading.Thread(target=self._run, name="tiger-delayed-order-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=5)

    def schedule(self, command: Dict[str, Any], execute_at: datetime) -> None:
        with self._condition:
            self._sequence += 1
            heapq.heappush(self._queue, ScheduledCommand(execute_at, self._sequence, dict(command)))
            self._condition.notify_all()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            due: Optional[ScheduledCommand] = None
            with self._condition:
                while not self._stop_event.is_set():
                    if not self._queue:
                        self._condition.wait(timeout=1.0)
                        continue
                    now = datetime.now(timezone.utc)
                    next_cmd = self._queue[0]
                    wait_seconds = (next_cmd.execute_at - now).total_seconds()
                    if wait_seconds <= 0:
                        due = heapq.heappop(self._queue)
                        break
                    self._condition.wait(timeout=min(wait_seconds, 1.0))

            if due is None:
                continue

            try:
                LOGGER.info("Executing delayed Tiger order: %s", command_summary(due.command))
                if not self._broker.is_trading_enabled_for_command(due.command):
                    LOGGER.warning("Skipping delayed Tiger order because trading is disabled: %s", command_summary(due.command))
                    if self._after_execute is not None:
                        self._after_execute(due.command)
                    continue
                self._broker.place_market_order(due.command)
                if self._after_execute is not None:
                    self._after_execute(due.command)
            except Exception:
                LOGGER.exception("Failed delayed Tiger order command_id=%s", command_id(due.command))


class AccountReporter:
    def __init__(self, broker: TigerBroker, stop_event: threading.Event) -> None:
        self._broker = broker
        self._stop_event = stop_event
        self._topic = os.getenv("KAFKA_ACCOUNT_DETAILS_TOPIC", "account-details")
        self._interval_seconds = TIGER_ACCOUNT_REPORT_INTERVAL_SECONDS
        configured_ui_accounts = env_csv("TIGER_UI_ACCOUNT_IDS", "")
        self._ui_account_ids = configured_ui_accounts or broker.ui_account_ids
        self._targets = broker.account_reporting_targets(self._ui_account_ids)
        self._producer: Optional[Producer] = None
        self._thread: Optional[threading.Thread] = None
        self._publish_lock = threading.RLock()

    def start(self) -> None:
        if self._broker._trade_client is None:
            LOGGER.warning("Skipping Tiger account reporting because Tiger client is not initialized.")
            return
        if not self._targets:
            LOGGER.warning("Skipping Tiger account reporting because no Tiger account target is configured.")
            return

        self._producer = Producer(
            {
                "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"),
                "acks": "all",
                "enable.idempotence": True,
                "linger.ms": 5,
            }
        )
        self._thread = threading.Thread(target=self._run, name="tiger-account-reporter", daemon=True)
        self._thread.start()
        LOGGER.info(
            "Reporting Tiger account snapshots every %s seconds to topic=%s",
            self._interval_seconds,
            self._topic,
        )

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._producer is not None:
            self._producer.flush(2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._publish_all("periodic")
            if self._stop_event.wait(self._interval_seconds):
                break

    def publish_all_now(self, reason: str = "manual") -> None:
        if self._producer is None:
            LOGGER.debug("Skipping immediate Tiger account snapshot because account reporter is not running.")
            return
        self._publish_all(reason)

    def _publish_all(self, reason: str) -> None:
        assert self._producer is not None

        with self._publish_lock:
            for ui_account, real_account in self._targets:
                try:
                    snapshot = self._broker.account_snapshot(ui_account, real_account)
                    payload = json.dumps(snapshot).encode("utf-8")
                    self._producer.produce(
                        topic=self._topic,
                        key=ui_account.encode("utf-8"),
                        value=payload,
                    )
                    self._producer.poll(0)
                    LOGGER.info(
                        "Published Tiger account snapshot account_id=%s positions=%d reason=%s",
                        ui_account,
                        len(snapshot.get("positions", [])),
                        reason,
                    )
                except Exception:
                    LOGGER.exception(
                        "Failed publishing Tiger account snapshot account_id=%s tiger_account=%s reason=%s",
                        ui_account,
                        real_account,
                        reason,
                    )


class TradingCommandConsumer:
    def __init__(
        self,
        broker: TigerBroker,
        scheduler: DelayedOrderScheduler,
        stop_event: threading.Event,
        reporter: Optional[AccountReporter] = None,
    ) -> None:
        self._broker = broker
        self._scheduler = scheduler
        self._stop_event = stop_event
        self._reporter = reporter
        self._topic = os.getenv("KAFKA_TRADING_COMMANDS_TOPIC", "trading-commands")
        self._poll_timeout = float(os.getenv("KAFKA_POLL_TIMEOUT_SEC", "1.0"))
        self._max_command_age_seconds = float(os.getenv("TIGER_MAX_COMMAND_AGE_SECONDS", "300"))
        configured_ui_accounts = env_csv("TIGER_UI_ACCOUNT_IDS", "")
        self._tiger_account_ids = set(configured_ui_accounts or broker.ui_account_ids)
        self._allow_broker_fallback = env_bool("TIGER_ALLOW_BROKER_FALLBACK", False)
        if not self._tiger_account_ids and not self._allow_broker_fallback:
            LOGGER.warning("Tiger command listener has no UI account IDs configured; Tiger commands will be ignored.")
        self._consumer = Consumer(
            {
                "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"),
                "group.id": os.getenv("TIGER_KAFKA_GROUP_ID", "tiger-trading-server"),
                "enable.auto.commit": False,
                "auto.offset.reset": os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest"),
                "session.timeout.ms": 10000,
                "max.poll.interval.ms": 300000,
            }
        )

    def run_forever(self) -> None:
        self._consumer.subscribe([self._topic])
        LOGGER.info("Consuming Tiger trading commands topic=%s", self._topic)
        try:
            while not self._stop_event.is_set():
                msg = self._consumer.poll(self._poll_timeout)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() != KafkaError._PARTITION_EOF:
                        LOGGER.warning("Kafka consumer error: %s", msg.error())
                        time.sleep(0.2)
                    continue

                try:
                    command = self._parse_message(msg.value())
                    self._handle_command(command)
                    self._consumer.commit(message=msg, asynchronous=False)
                except Exception:
                    LOGGER.exception("Failed processing Kafka message; leaving offset uncommitted")
        finally:
            self._consumer.close()

    def _parse_message(self, raw: Optional[bytes]) -> Dict[str, Any]:
        if not raw:
            raise ValueError("empty Kafka message")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("command payload must be a JSON object")
        return payload

    def _handle_command(self, command: Dict[str, Any]) -> None:
        cmd_type = str(command.get("type") or "").strip().upper()
        if self._is_stale_command(command):
            LOGGER.warning(
                "Ignoring stale trading command older than %.0f seconds: %s",
                self._max_command_age_seconds,
                command_summary(command),
            )
            return

        if cmd_type in ("START_ALGO_TRADING", "STOP_ALGO_TRADING"):
            LOGGER.info("Algorithmic trading command received: %s", command_summary(command))
            return

        if cmd_type not in (
            "MARKET_ORDER",
            "DELAYED_MARKET_ORDER",
            "LIMIT_ORDER",
            "CANCEL_OPEN_ORDERS",
            "CURRENCY_CONVERSION",
            "SET_TRADING_ENABLED",
        ):
            LOGGER.info("Ignoring unsupported command type=%s payload=%s", cmd_type, command_summary(command))
            return

        if not self._is_tiger_order(command):
            LOGGER.debug("Ignoring non-Tiger order: %s", command_summary(command))
            return

        if cmd_type == "SET_TRADING_ENABLED":
            LOGGER.info("Executing Tiger trading status command: %s", command_summary(command))
            self._broker.set_trading_enabled(command)
            self._publish_snapshot_after_command(command)
            return

        if cmd_type != "CANCEL_OPEN_ORDERS" and not self._broker.is_trading_enabled_for_command(command):
            LOGGER.warning("Ignoring Tiger command because trading is disabled: %s", command_summary(command))
            self._publish_snapshot_after_command(command)
            return

        if cmd_type == "MARKET_ORDER":
            LOGGER.info("Executing Tiger market order: %s", command_summary(command))
            self._broker.place_market_order(command)
            self._publish_snapshot_after_command(command)
            return

        if cmd_type == "LIMIT_ORDER":
            LOGGER.info("Executing Tiger limit order: %s", command_summary(command))
            self._broker.place_limit_order(command)
            self._publish_snapshot_after_command(command)
            return

        if cmd_type == "CANCEL_OPEN_ORDERS":
            LOGGER.info("Executing Tiger cancel open orders: %s", command_summary(command))
            self._broker.cancel_open_orders(command)
            self._publish_snapshot_after_command(command)
            return

        if cmd_type == "CURRENCY_CONVERSION":
            LOGGER.info("Executing Tiger currency conversion: %s", command_summary(command))
            self._broker.convert_currency(command)
            self._publish_snapshot_after_command(command)
            return

        execute_at = self._resolve_execute_at(command)
        if execute_at <= datetime.now(timezone.utc):
            LOGGER.info("Delayed order is due now; executing: %s", command_summary(command))
            self._broker.place_market_order(command)
            self._publish_snapshot_after_command(command)
            return

        self._scheduler.schedule(command, execute_at)
        LOGGER.info(
            "Scheduled Tiger delayed market order command_id=%s execute_at=%s",
            command_id(command),
            execute_at.isoformat(),
        )

    def _publish_snapshot_after_command(self, command: Dict[str, Any]) -> None:
        if self._reporter is None:
            return
        try:
            self._reporter.publish_all_now("command_id=%s" % command_id(command))
        except Exception:
            LOGGER.exception("Failed immediate Tiger account snapshot command_id=%s", command_id(command))

    def _is_tiger_order(self, command: Dict[str, Any]) -> bool:
        account_id = str(command.get("account_id") or "").strip()
        if self._tiger_account_ids:
            if account_id and account_id in self._tiger_account_ids:
                return True
            log_method = LOGGER.info if self._looks_like_tiger_command(command) else LOGGER.debug
            log_method(
                "Ignoring command for account_id=%s; Tiger listener account_ids=%s payload=%s",
                account_id or "<missing>",
                ",".join(sorted(self._tiger_account_ids)),
                command_summary(command),
            )
            return False

        if not self._allow_broker_fallback:
            LOGGER.warning("Ignoring Tiger command without explicit UI account routing: %s", command_summary(command))
            return False

        broker = str(command.get("broker") or "").strip().lower()
        if broker:
            return broker == "tiger"

        broker_id = str(command.get("broker_id") or "").strip().upper()
        return broker_id.startswith("TG-")

    def _looks_like_tiger_command(self, command: Dict[str, Any]) -> bool:
        broker = str(command.get("broker") or "").strip().lower()
        if broker:
            return broker == "tiger"

        broker_id = str(command.get("broker_id") or "").strip().upper()
        if broker_id.startswith("TG-"):
            return True

        account_id = str(command.get("account_id") or "").strip().upper()
        return account_id.startswith("TIGER")

    def _resolve_execute_at(self, command: Dict[str, Any]) -> datetime:
        execute_at = command.get("execute_at")
        if execute_at:
            return parse_iso_datetime(str(execute_at))

        delay_seconds = int(command.get("delay_seconds") or 0)
        if delay_seconds <= 0:
            raise ValueError("DELAYED_MARKET_ORDER requires future execute_at or positive delay_seconds")
        return datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

    def _is_stale_command(self, command: Dict[str, Any]) -> bool:
        if self._max_command_age_seconds <= 0:
            return False

        raw_ts = command.get("ts")
        if not raw_ts:
            LOGGER.warning("Ignoring trading command without ts: %s", command_summary(command))
            return True

        try:
            issued_at = parse_iso_datetime(str(raw_ts))
        except Exception:
            LOGGER.warning("Ignoring trading command with invalid ts=%r: %s", raw_ts, command_summary(command))
            return True

        age_seconds = (datetime.now(timezone.utc) - issued_at).total_seconds()
        return age_seconds > self._max_command_age_seconds


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def run_precheck(broker: TigerBroker) -> int:
    targets = broker.account_reporting_targets(env_csv("TIGER_UI_ACCOUNT_IDS", "") or broker.ui_account_ids)
    if not targets:
        raise RuntimeError("No Tiger account target is configured")

    for ui_account, real_account in targets:
        snapshot = broker.account_snapshot(ui_account, real_account)
        LOGGER.info(
            "Tiger precheck succeeded account_id=%s tiger_account=%s trading_enabled=%s cash_ok=%s positions=%d",
            ui_account,
            real_account,
            snapshot.get("trading_enabled"),
            isinstance(snapshot.get("cash"), (int, float)),
            len(snapshot.get("positions", [])),
        )
    return 0


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    scheduler: Optional[DelayedOrderScheduler] = None
    reporter: Optional[AccountReporter] = None
    try:
        broker = TigerBroker()
        if env_bool("TIGER_PRECHECK_ONLY", False):
            return run_precheck(broker)
        reporter = AccountReporter(broker, stop_event)
        reporter.start()
        scheduler = DelayedOrderScheduler(
            broker,
            stop_event,
            after_execute=lambda command: reporter.publish_all_now("delayed_command_id=%s" % command_id(command)),
        )
        scheduler.start()
        consumer = TradingCommandConsumer(broker, scheduler, stop_event, reporter)
        consumer.run_forever()
    except KafkaException:
        LOGGER.exception("Kafka error while running Tiger trading server")
        return 1
    except Exception:
        LOGGER.exception("Tiger trading server stopped with an error")
        return 1
    finally:
        stop_event.set()
        if scheduler is not None:
            scheduler.stop()
        if reporter is not None:
            reporter.stop()

    LOGGER.info("Tiger trading server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
