import json
import threading
import time
from typing import Any, Dict, Optional

from confluent_kafka import Consumer, KafkaError, TopicPartition, OFFSET_BEGINNING

from ..services.market_data import MarketDataStore, order_book_from_price_book, quote_row_from_price_book


def parse_market_data_message(raw: bytes):
    try:
        payload = json.loads(raw.decode("utf-8"))
        return quote_row_from_price_book(payload)
    except Exception:
        return None


class PriceBookConsumer:
    def __init__(self, kafka_cfg: Dict[str, Any], store: MarketDataStore) -> None:
        self._kafka_cfg = kafka_cfg
        self._store = store
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._consumer: Optional[Consumer] = None

    def start(self) -> None:
        bootstrap_servers = self._kafka_cfg.get("market_data_bootstrap_servers", self._kafka_cfg["bootstrap_servers"])
        conf = {
            "bootstrap.servers": bootstrap_servers,
            "group.id": self._kafka_cfg["market_data_group_id"],
            "enable.auto.commit": False,
            "auto.offset.reset": self._kafka_cfg["market_data_auto_offset_reset"],
            "session.timeout.ms": 10000,
            "max.poll.interval.ms": 300000,
        }

        topic = self._kafka_cfg["market_data_topic"]
        self._consumer = Consumer(conf)
        self._consumer.subscribe([topic], on_assign=self._on_assign)
        print(f"[market-data] consuming topic='{topic}' bootstrap='{bootstrap_servers}'")

        self._thread = threading.Thread(target=self._run, name="price-book-consumer", daemon=True)
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
                    print(f"[market-data] consume error: {msg.error()}")
                    time.sleep(0.2)
                continue

            val = msg.value()
            if not val:
                continue

            row = parse_market_data_message(val)
            if row is not None:
                self._store.upsert(row)

            try:
                payload = json.loads(val.decode("utf-8"))
            except Exception:
                payload = None

            if isinstance(payload, dict):
                depth_limit = int(self._kafka_cfg.get("market_insights_max_levels", 20))
                book = order_book_from_price_book(payload, depth_limit=depth_limit)
                if book is not None:
                    self._store.upsert_book(book)

    def _on_assign(self, consumer: Consumer, partitions: list[TopicPartition]) -> None:
        for partition in partitions:
            partition.offset = OFFSET_BEGINNING
        assigned = ", ".join(f"{p.topic}[{p.partition}]@{p.offset}" for p in partitions)
        print(f"[market-data] assigned: {assigned}")
        consumer.assign(partitions)
