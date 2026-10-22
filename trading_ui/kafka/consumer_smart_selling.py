"""Independent retained order/volume feeds for the Smart selling controller.

The streams may live on different Kafka clusters. Each process reads every
partition of both topics from retained history; runtime callbacks deduplicate
events and prevent replayed tape from earning fresh participation credit.
"""

import json
import logging
import threading
import time
import uuid

from confluent_kafka import Consumer, KafkaError, OFFSET_BEGINNING

LOGGER = logging.getLogger("trading-ui.smart-selling-consumer")
MAX_MESSAGE_BYTES = 65536


def _reject_json_constant(_value):
    raise ValueError("Non-finite volume values are not valid JSON")


def _decode_volume_message(raw):
    payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(payload, dict):
        raise ValueError("Volume snapshot must be an object")
    return payload


class _SmartSellingFeed:
    """Own one cluster/topic connection and its replay/health lifecycle."""

    def __init__(
        self, name, topic, bootstrap_servers, runtime, consumer_factory, *, client_config=None
    ):
        self.name = name
        self.topic = str(topic)
        self.bootstrap_servers = str(bootstrap_servers)
        self.runtime = runtime
        self.factory = consumer_factory
        self.client_config = dict(client_config or {})
        self.group_id = f"ktrader-smart-selling-{name}-{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._reconnect_requested = threading.Event()
        self._thread = None
        self._consumer = None
        self._assigned = set()
        self._caught_up = set()
        self._last_warning = float("-inf")
        self._state = None
        self.rejected_messages = 0

    def _set_state(self, state):
        if state != self._state:
            self._state = state
            self.runtime.on_feed_state(self.name, state)

    def _config(self):
        return {
            **self.client_config,
            "bootstrap.servers": self.bootstrap_servers,
            "group.id": self.group_id,
            "client.id": f"ktrader-smart-selling-{self.name}",
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

    def _warn(self, reason):
        now = time.monotonic()
        if now - self._last_warning >= 5:
            self._last_warning = now
            # Never log raw payloads or client connection configuration.
            LOGGER.warning("Smart selling %s feed: %s", self.name, reason)

    def _on_assign(self, consumer, partitions):
        self._assigned = {(part.topic, part.partition) for part in partitions}
        self._caught_up.clear()
        for part in partitions:
            part.offset = OFFSET_BEGINNING
        consumer.assign(partitions)
        self._set_state("replaying")

    def _on_revoke(self, _consumer, _partitions):
        self._assigned.clear()
        self._caught_up.clear()
        self._set_state("starting")

    def _connected(self):
        if self._assigned:
            self._set_state("live" if self._assigned <= self._caught_up else "replaying")

    def _on_error(self, error):
        self._caught_up.clear()
        self._reconnect_requested.set()
        self._set_state("disconnected")
        self._warn(f"Kafka connection error ({error.code()})")

    def _on_stats(self, raw):
        try:
            stats = json.loads(raw)
            brokers = [
                item for item in stats.get("brokers", {}).values() if item.get("nodeid", -1) >= 0
            ]
            if brokers and all(item.get("state") == "UP" for item in brokers):
                self._connected()
            elif brokers:
                self._caught_up.clear()
                self._reconnect_requested.set()
                self._set_state("disconnected")
        except (ValueError, TypeError, AttributeError):
            self._warn("invalid Kafka connection statistics")

    def _handle_message(self, message):
        error = message.error()
        if error:
            if error.code() == KafkaError._PARTITION_EOF:
                self._caught_up.add((message.topic(), message.partition()))
                self._connected()
            else:
                self._on_error(error)
            return
        self._connected()
        try:
            self._deliver_message(message)
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            self.rejected_messages += 1
            self._warn("invalid snapshot ignored")

    def _deliver_message(self, message):
        """Enforce wire bounds, then hand data and source identity to runtime."""
        raw = message.value()
        if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Snapshot must be at most 64 KiB of UTF-8 JSON")
        if self.name == "orders":
            self.runtime.on_order_payload(raw)
            return
        self.runtime.on_volume_payload(
            _decode_volume_message(raw),
            topic=message.topic(),
            partition=message.partition(),
            offset=message.offset(),
        )

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_state("starting")
        self._consumer = self.factory(self._config())
        self._thread = threading.Thread(
            target=self._run, name=f"smart-selling-{self.name}-consumer", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self._warn("consumer is still closing")
                return
        elif self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        self._set_state("stopped")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._reconnect_requested.clear()
                if self._consumer is None:
                    self._consumer = self.factory(self._config())
                self._consumer.subscribe(
                    [self.topic], on_assign=self._on_assign, on_revoke=self._on_revoke
                )
                while not self._stop.is_set() and not self._reconnect_requested.is_set():
                    message = self._consumer.poll(1.0)
                    if message is not None and not self._reconnect_requested.is_set():
                        self._handle_message(message)
            except Exception:
                self._caught_up.clear()
                self._set_state("disconnected")
                self._warn("consumer unavailable; reconnecting")
            finally:
                if self._consumer is not None:
                    try:
                        self._consumer.close()
                    except Exception:
                        self._warn("consumer close failed")
                    self._consumer = None
                # Recreate after connection errors rather than waiting for an
                # EOF at an unchanged offset: librdkafka may already have emitted
                # that EOF before the outage. Replaying reestablishes every
                # partition's gate even when the order topic remains quiet.
                self._assigned.clear()
                self._caught_up.clear()
            if not self._stop.is_set():
                self._stop.wait(3)
        self._set_state("stopped")


class SmartSellingConsumer:
    """Two independent consumers with one lifecycle for the control server.

    ``config`` is the complete application configuration. ``runtime`` supplies
    ``on_order_payload(raw)``, ``on_volume_payload(payload, topic, partition,
    offset)`` and ``on_feed_state(feed, state)`` callbacks. Only the runtime
    decides when received evidence permits new orders or earns volume credit.
    """

    def __init__(self, config, runtime, consumer_factory=Consumer):
        kafka = config.get("kafka", {})
        settings = config.get("smart_selling", {})
        order_config = config.get("order_ui", {}).get("kafka", {})
        bootstrap = kafka.get("bootstrap_servers", "127.0.0.1:9092")
        market_bootstrap = kafka.get("market_data_bootstrap_servers", bootstrap)
        order_topic = settings.get("order_status_topic", order_config.get("topic", "order-status"))
        volume_topic = settings.get("trading_volume_topic", "trading-volume")
        order_bootstrap = settings.get("order_status_bootstrap_servers", bootstrap)
        volume_bootstrap = settings.get("trading_volume_bootstrap_servers", market_bootstrap)
        order_client_config = settings.get(
            "order_status_client_config", order_config.get("client_config", {})
        )
        volume_client_config = settings.get("trading_volume_client_config", {})
        self.feeds = {
            "orders": _SmartSellingFeed(
                "orders",
                order_topic,
                order_bootstrap,
                runtime,
                consumer_factory,
                client_config=order_client_config,
            ),
            "volume": _SmartSellingFeed(
                "volume",
                volume_topic,
                volume_bootstrap,
                runtime,
                consumer_factory,
                client_config=volume_client_config,
            ),
        }

    def start(self):
        try:
            for feed in self.feeds.values():
                feed.start()
        except Exception:
            for feed in self.feeds.values():
                feed.stop()
            raise

    def stop(self):
        # Signal both first so shutdown is parallel despite separate clusters.
        for feed in self.feeds.values():
            feed._stop.set()
        for feed in self.feeds.values():
            feed.stop()
