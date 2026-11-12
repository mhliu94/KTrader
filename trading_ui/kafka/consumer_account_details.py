import json
import logging
import time
import threading
from typing import Any, Dict, Optional

from confluent_kafka import Consumer, KafkaError

from ..account_delta import AccountReplica
from ..store import AccountStore
from ..models import AccountMeta, AccountSnapshot
from ..services.account_snapshots import parse_snapshot_obj

LOGGER = logging.getLogger(__name__)


def parse_account_message(raw: bytes) -> Optional[AccountSnapshot]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("message_type") == "account_delta":
            return None
        return parse_snapshot_obj(payload)
    except Exception:
        return None


class AccountDetailsConsumer:
    def __init__(self, kafka_cfg: Dict[str, Any], store: AccountStore, account_metas: Dict[str, AccountMeta]) -> None:
        self._kafka_cfg = kafka_cfg
        self._store = store
        self._account_metas = dict(account_metas)
        self._account_ids_by_num = {meta.num_id: meta.id for meta in account_metas.values()}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._consumer: Optional[Consumer] = None
        self._replicas = {}

    def start(self) -> None:
        conf = {
            "bootstrap.servers": self._kafka_cfg["bootstrap_servers"],
            "group.id": self._kafka_cfg["group_id"],
            "enable.auto.commit": True,
            "auto.offset.reset": self._kafka_cfg["auto_offset_reset"],
            "session.timeout.ms": 10000,
            "max.poll.interval.ms": 300000,
        }

        topic = self._kafka_cfg["account_details_topic"]
        self._consumer = Consumer(conf)
        self._consumer.subscribe([topic])

        self._thread = threading.Thread(target=self._run, name="account-details-consumer", daemon=True)
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

            try:
                self.process_payload(json.loads(val.decode("utf-8")), msg)
            except (ValueError, TypeError, KeyError, OverflowError):
                LOGGER.warning("Ignoring invalid account update topic=%s partition=%s offset=%s",
                               msg.topic(), msg.partition(), msg.offset())

    def process_payload(self, payload, msg):
        if not isinstance(payload, dict):
            raise ValueError("Account update must be an object")
        identity = parse_snapshot_obj(payload)
        if identity is None or not self._validate_snapshot(identity, msg):
            return False
        if "account_update_version" not in payload:
            if "message_type" in payload:
                raise ValueError("Typed account update has no version")
            # Older publishers remain supported during a rolling upgrade.
            self._replicas.pop(identity.account_id, None)
            self._store.upsert(identity)
            return True
        replica = self._replicas.setdefault(identity.account_id, AccountReplica())
        was_missing = replica.needs_snapshot
        payload = dict(payload, account_id=identity.account_id, account_num_id=identity.account_num_id)
        state = replica.apply(payload)
        if state is None:
            if replica.needs_snapshot and not was_missing:
                self._store.invalidate(identity.account_id)
                LOGGER.warning("Account sequence gap account_id=%s; awaiting checkpoint", identity.account_id)
            return False
        snapshot = parse_snapshot_obj(state)
        if snapshot is None:
            raise ValueError("Invalid reconstructed account state")
        self._store.upsert(snapshot)
        return True

    def _validate_snapshot(self, snap: AccountSnapshot, msg) -> bool:
        account_id = str(snap.account_id or "").strip()
        meta = self._account_metas.get(account_id)
        if meta is None:
            LOGGER.warning(
                "Ignoring account snapshot with unknown account_id=%s account_num_id=%s topic=%s partition=%s offset=%s",
                account_id,
                snap.account_num_id,
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )
            return False

        snap.account_id = account_id
        if snap.account_num_id is None:
            snap.account_num_id = meta.num_id
            return True

        expected_account_id = self._account_ids_by_num.get(snap.account_num_id)
        if expected_account_id is None:
            LOGGER.warning(
                "Ignoring account snapshot with unknown account_num_id=%s account_id=%s topic=%s partition=%s offset=%s",
                snap.account_num_id,
                account_id,
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )
            return False

        if expected_account_id != account_id or snap.account_num_id != meta.num_id:
            LOGGER.warning(
                "Ignoring account snapshot with mismatched account identifiers account_id=%s account_num_id=%s expected_account_id=%s expected_num_id=%s topic=%s partition=%s offset=%s",
                account_id,
                snap.account_num_id,
                expected_account_id,
                meta.num_id,
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )
            return False

        return True
