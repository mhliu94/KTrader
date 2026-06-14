import json
import threading
from typing import Any, Dict

from confluent_kafka import Producer


class TradingCommandsProducer:
    """
    Serialize produce+poll with a lock to be safe when called from request threads.
    """

    def __init__(self, kafka_cfg: Dict[str, Any]) -> None:
        prod_cfg = kafka_cfg.get("producer", {})
        self._topic = kafka_cfg["trading_commands_topic"]
        self._lock = threading.RLock()
        self._producer = Producer(
            {
                "bootstrap.servers": kafka_cfg["bootstrap_servers"],
                "acks": prod_cfg.get("acks", "all"),
                "enable.idempotence": bool(prod_cfg.get("enable_idempotence", True)),
                "linger.ms": int(prod_cfg.get("linger_ms", 5)),
            }
        )

    def publish_order(self, command: Dict[str, Any], key: str) -> None:
        payload = json.dumps(command).encode("utf-8")
        with self._lock:
            self._producer.produce(
                topic=self._topic,
                key=key.encode("utf-8"),
                value=payload,
            )
            self._producer.poll(0)

    def flush(self, timeout_sec: float = 2.0) -> None:
        with self._lock:
            self._producer.flush(timeout_sec)
