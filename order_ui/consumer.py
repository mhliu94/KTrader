"""A dedicated consumer per server process; rebuild the projection on restart."""

import json
import logging
import threading
import uuid

from confluent_kafka import Consumer, KafkaError, OFFSET_BEGINNING

from .events import parse_order_message

LOGGER = logging.getLogger(__name__)


class OrderStatusConsumer:
    def __init__(self, config, store, consumer_factory=Consumer):
        self.config = config
        self.store = store
        self._factory = consumer_factory
        self._stop = threading.Event()
        self._thread = None
        self._consumer = None
        self._assigned = set()
        self._caught_up = set()
        # Independent dashboards must not divide the topic's partitions between them.
        self.group_id = f"{config['group_id']}-{uuid.uuid4().hex}"

    def _client_config(self):
        return {
            **self.config["client_config"],
            "bootstrap.servers": self.config["bootstrap_servers"],
            "group.id": self.group_id,
            "client.id": "ktrader-order-ui",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
            "enable.partition.eof": True,
            "allow.auto.create.topics": False,
            "partition.assignment.strategy": "range",
            "group.protocol": "classic",
            "session.timeout.ms": 10000,
            "statistics.interval.ms": 5000,
            "error_cb": self._on_error,
            "stats_cb": self._on_stats,
        }

    def start(self):
        self._stop.clear()
        # Invalid client settings fail startup visibly, rather than silently losing a thread.
        self._consumer = self._factory(self._client_config())
        self._thread = threading.Thread(target=self._run, name="order-status-consumer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                LOGGER.warning("Order consumer is still closing its Kafka connection.")

    def _on_assign(self, consumer, partitions):
        self._assigned = {(p.topic, p.partition) for p in partitions}
        self._caught_up.clear()
        for partition in partitions:
            partition.offset = OFFSET_BEGINNING
        consumer.assign(partitions)
        self.store.update_feed(state="replaying", detail="Loading retained order snapshots")

    def _on_revoke(self, consumer, partitions):
        self._assigned.clear()
        self._caught_up.clear()
        self.store.update_feed(state="starting", detail="Waiting for Kafka partitions")

    def _on_error(self, error):
        # Client errors can contain connection details; the browser gets a generic message.
        LOGGER.warning("Order Kafka connection error (code %s)", error.code())
        self.store.update_feed(state="disconnected", detail="Kafka unavailable; reconnecting")

    def _connected(self):
        if self._assigned:
            live = self._assigned <= self._caught_up
            self.store.update_feed(state="live" if live else "replaying",
                                   detail="Kafka connected" if live else "Loading retained order snapshots")

    def _on_stats(self, raw):
        stats = json.loads(raw)
        brokers = [b for b in stats.get("brokers", {}).values() if b.get("nodeid", -1) >= 0]
        if brokers and all(b.get("state") == "UP" for b in brokers):
            self._connected()
        elif brokers:
            self.store.update_feed(state="disconnected", detail="Kafka connection interrupted; reconnecting")

    def _handle_message(self, msg):
        error = msg.error()
        if error:
            if error.code() == KafkaError._PARTITION_EOF:
                self._caught_up.add((msg.topic(), msg.partition()))
                self._connected()
            else:
                self._on_error(error)
            return
        self._connected()
        self.store.received()
        try:
            raw = msg.value()
            if raw is None:
                raise ValueError("Tombstones are not order snapshots.")
            self.store.upsert(parse_order_message(raw))
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
            self.store.reject_message()
            LOGGER.warning("Ignoring invalid order snapshot topic=%s partition=%s offset=%s (%s)",
                           msg.topic(), msg.partition(), msg.offset(), type(exc).__name__)

    def _run(self):
        while not self._stop.is_set():
            try:
                if self._consumer is None:
                    self._consumer = self._factory(self._client_config())
                self._consumer.subscribe([self.config["topic"]], on_assign=self._on_assign,
                                         on_revoke=self._on_revoke)
                while not self._stop.is_set():
                    msg = self._consumer.poll(self.config["poll_timeout_sec"])
                    if msg is not None:
                        self._handle_message(msg)
            except Exception:
                LOGGER.exception("Order consumer failed; reconnecting")
                self.store.update_feed(state="disconnected", detail="Kafka unavailable; reconnecting")
            finally:
                if self._consumer is not None:
                    try:
                        self._consumer.close()
                    except Exception:
                        LOGGER.exception("Could not close the order consumer")
                    self._consumer = None
            self._stop.wait(3)
        self.store.update_feed(state="stopped", detail="Order consumer stopped")
