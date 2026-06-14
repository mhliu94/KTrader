import json
import threading
import time
from typing import Any, Dict, Optional

from confluent_kafka import Consumer, KafkaError

from ..models import StrategySnapshot
from ..store import StrategyStore


def _pick(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_status(payload: Dict[str, Any]) -> str:
    raw = _pick(payload, "status", "state", "strategy_status", "lifecycle_state")
    if not raw:
        running_flag = payload.get("running")
        if isinstance(running_flag, bool):
            return "RUNNING" if running_flag else "STOPPED"
        return "UNKNOWN"
    norm = raw.strip().upper().replace(" ", "_").replace("-", "_")
    if norm in {"ACTIVE", "STARTED", "IN_PROGRESS"}:
        return "RUNNING"
    if norm in {"DONE", "FINISHED"}:
        return "COMPLETED"
    return norm


def parse_strategy_message(raw: bytes) -> Optional[StrategySnapshot]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    trading_mode = _pick(payload, "trading_mode", "mode")
    symbol = _pick(payload, "symbol", "ticker")
    command_id = _pick(payload, "command_id", "strategy_id", "id")

    account_ids_raw = payload.get("account_ids")
    if isinstance(account_ids_raw, list):
        account_ids = [str(v).strip() for v in account_ids_raw if str(v).strip()]
    else:
        single_account = _pick(payload, "account_id")
        account_ids = [single_account] if single_account else []

    detail = _pick(payload, "detail", "message", "reason", "note")
    started_at = _pick(payload, "started_at", "start_time", "ts", "timestamp")
    updated_at = _pick(payload, "updated_at", "ts", "timestamp", "event_time")
    status = _normalize_status(payload)

    strategy_key = _pick(payload, "strategy_key")
    if not strategy_key:
        parts = [part for part in [command_id, trading_mode, symbol, ",".join(account_ids)] if part]
        strategy_key = "|".join(parts) if parts else f"event-{updated_at or int(time.time())}"

    return StrategySnapshot(
        strategy_key=strategy_key,
        status=status,
        trading_mode=trading_mode,
        symbol=symbol,
        account_ids=account_ids,
        command_id=command_id,
        started_at=started_at,
        updated_at=updated_at or started_at,
        detail=detail,
        raw=payload,
    )


class LiveTradingUpdatesConsumer:
    def __init__(self, kafka_cfg: Dict[str, Any], store: StrategyStore) -> None:
        self._kafka_cfg = kafka_cfg
        self._store = store
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._consumer: Optional[Consumer] = None

    def start(self) -> None:
        conf = {
            "bootstrap.servers": self._kafka_cfg["bootstrap_servers"],
            "group.id": self._kafka_cfg["group_id"],
            "enable.auto.commit": True,
            "auto.offset.reset": self._kafka_cfg["auto_offset_reset"],
            "session.timeout.ms": 10000,
            "max.poll.interval.ms": 300000,
        }
        topic = self._kafka_cfg["live_trading_updates_topic"]
        self._consumer = Consumer(conf)
        self._consumer.subscribe([topic])
        self._thread = threading.Thread(target=self._run, name="live-trading-updates-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._consumer:
            try:
                self._consumer.close()
            except Exception:
                pass

    def _run(self) -> None:
        assert self._consumer is not None
        timeout = float(self._kafka_cfg.get("poll_timeout_sec", 1.0))
        while not self._stop.is_set():
            msg = self._consumer.poll(timeout)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    time.sleep(0.2)
                continue
            val = msg.value()
            if not val:
                continue
            snap = parse_strategy_message(val)
            if snap is not None:
                self._store.upsert(snap)
