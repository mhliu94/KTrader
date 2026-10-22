"""Independent feed/replay tests; all Kafka consumers are fakes."""

import json
import threading
import time
import unittest
from types import SimpleNamespace

from confluent_kafka import KafkaError, OFFSET_BEGINNING

from trading_ui.kafka.consumer_smart_selling import SmartSellingConsumer


class Runtime:
    def __init__(self):
        self.states = []
        self.orders = []
        self.volumes = []
        self.reject_orders = False

    def on_feed_state(self, feed, state):
        self.states.append((feed, state))

    def on_order_payload(self, raw):
        if self.reject_orders:
            raise ValueError("invalid order")
        self.orders.append(raw)

    def on_volume_payload(self, payload, **metadata):
        self.volumes.append((payload, metadata))


class Message:
    def __init__(self, value=b"{}", *, topic="order-status", partition=0, offset=0, error=None):
        self._value, self._topic, self._partition, self._offset, self._error = (
            value,
            topic,
            partition,
            offset,
            error,
        )

    def value(self):
        return self._value

    def topic(self):
        return self._topic

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def error(self):
        return self._error


class FakeConsumer:
    def __init__(self, config):
        self.config = config
        self.closed = False
        self.assigned = []
        self.subscriptions = []

    def assign(self, partitions):
        self.assigned = partitions

    def subscribe(self, topics, **callbacks):
        self.subscriptions = topics

    def poll(self, timeout):
        time.sleep(0.005)
        return None

    def close(self):
        self.closed = True


class SmartSellingConsumerTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.created = []

        def factory(config):
            consumer = FakeConsumer(config)
            self.created.append(consumer)
            return consumer

        self.factory = factory
        self.consumer = SmartSellingConsumer(
            {
                "kafka": {
                    "bootstrap_servers": "orders:9092",
                    "market_data_bootstrap_servers": "market:9093",
                }
            },
            self.runtime,
            consumer_factory=factory,
        )

    def assign(self, name="orders", count=2):
        feed = self.consumer.feeds[name]
        partitions = [
            SimpleNamespace(topic=feed.topic, partition=index, offset=99) for index in range(count)
        ]
        fake = FakeConsumer({})
        feed._on_assign(fake, partitions)
        self.assertTrue(all(part.offset == OFFSET_BEGINNING for part in fake.assigned))
        return feed

    def eof(self, feed, partition):
        feed._handle_message(
            Message(
                topic=feed.topic, partition=partition, error=KafkaError(KafkaError._PARTITION_EOF)
            )
        )

    def test_two_topics_and_clusters_use_independent_groups(self):
        orders, volume = self.consumer.feeds["orders"], self.consumer.feeds["volume"]
        self.assertEqual(orders.topic, "order-status")
        self.assertEqual(volume.topic, "trading-volume")
        self.assertEqual(orders._config()["bootstrap.servers"], "orders:9092")
        self.assertEqual(volume._config()["bootstrap.servers"], "market:9093")
        self.assertNotEqual(orders.group_id, volume.group_id)
        another = SmartSellingConsumer({}, self.runtime, consumer_factory=self.factory)
        self.assertNotEqual(orders.group_id, another.feeds["orders"].group_id)

    def test_explicit_topics_and_bootstraps_override_defaults(self):
        consumer = SmartSellingConsumer(
            {
                "kafka": {"bootstrap_servers": "default"},
                "smart_selling": {
                    "order_status_topic": "existing-orders",
                    "trading_volume_topic": "prints",
                    "order_status_bootstrap_servers": "other-orders",
                    "trading_volume_bootstrap_servers": "other-volume",
                },
            },
            self.runtime,
            consumer_factory=self.factory,
        )
        self.assertEqual(
            (consumer.feeds["orders"].topic, consumer.feeds["orders"].bootstrap_servers),
            ("existing-orders", "other-orders"),
        )
        self.assertEqual(
            (consumer.feeds["volume"].topic, consumer.feeds["volume"].bootstrap_servers),
            ("prints", "other-volume"),
        )

    def test_consumer_settings_never_split_or_commit_replay(self):
        config = self.consumer.feeds["orders"]._config()
        self.assertEqual(config["auto.offset.reset"], "earliest")
        self.assertFalse(config["enable.auto.commit"])
        self.assertFalse(config["enable.auto.offset.store"])
        self.assertFalse(config["allow.auto.create.topics"])
        self.assertTrue(config["enable.partition.eof"])

    def test_live_requires_eof_from_every_assigned_partition(self):
        feed = self.assign()
        self.assertEqual(self.runtime.states[-1], ("orders", "replaying"))
        self.eof(feed, 0)
        feed._handle_message(Message())
        self.assertNotIn(("orders", "live"), self.runtime.states)
        self.eof(feed, 1)
        self.assertEqual(self.runtime.states[-1], ("orders", "live"))

    def test_one_feed_live_does_not_mark_other_feed_live(self):
        orders = self.assign("orders", 1)
        self.assign("volume", 1)
        self.eof(orders, 0)
        self.assertIn(("orders", "live"), self.runtime.states)
        self.assertNotIn(("volume", "live"), self.runtime.states)

    def test_disconnect_requires_all_partitions_to_catch_up_again(self):
        feed = self.assign()
        self.eof(feed, 0)
        self.eof(feed, 1)
        with self.assertLogs("trading-ui.smart-selling-consumer", level="WARNING"):
            feed._on_error(KafkaError(KafkaError._ALL_BROKERS_DOWN))
        self.assertTrue(feed._reconnect_requested.is_set())
        self.assertEqual(self.runtime.states[-1], ("orders", "disconnected"))
        self.eof(feed, 0)
        self.assertEqual(self.runtime.states[-1], ("orders", "replaying"))
        self.eof(feed, 1)
        self.assertEqual(self.runtime.states[-1], ("orders", "live"))

    def test_revoke_and_reassign_restart_replay(self):
        feed = self.assign("orders", 1)
        self.eof(feed, 0)
        feed._on_revoke(None, [])
        self.assertEqual(self.runtime.states[-1], ("orders", "starting"))
        self.assign("orders", 1)
        self.assertEqual(self.runtime.states[-1], ("orders", "replaying"))

    def test_orders_forward_existing_raw_protocol(self):
        self.consumer.feeds["orders"]._handle_message(Message(b'{"schema_version":1}'))
        self.assertEqual(self.runtime.orders, [b'{"schema_version":1}'])
        self.assertEqual(self.runtime.volumes, [])

    def test_volume_json_carries_exact_kafka_event_identity(self):
        raw = b'{"symbol":"XYZ","quantity":250,"event_id":"print-1"}'
        self.consumer.feeds["volume"]._handle_message(
            Message(raw, topic="prints", partition=3, offset=42)
        )
        self.assertEqual(
            self.runtime.volumes,
            [(json.loads(raw), {"topic": "prints", "partition": 3, "offset": 42})],
        )

    def test_invalid_volume_and_oversized_payloads_are_bounded(self):
        feed = self.consumer.feeds["volume"]
        for raw in (b"[]", b'{"quantity":NaN}', None, b"x" * 65537, b"\xff"):
            feed._handle_message(Message(raw, topic=feed.topic))
        self.assertEqual(feed.rejected_messages, 5)
        self.assertEqual(self.runtime.volumes, [])

    def test_runtime_validation_error_does_not_stop_polling(self):
        feed = self.consumer.feeds["orders"]
        self.runtime.reject_orders = True
        feed._handle_message(Message())
        self.assertEqual(feed.rejected_messages, 1)
        self.runtime.reject_orders = False
        feed._handle_message(Message(b'{"ok":true}'))
        self.assertEqual(self.runtime.orders, [b'{"ok":true}'])

    def test_statistics_cannot_mark_live_before_partition_eof(self):
        feed = self.assign("volume", 1)
        up = json.dumps({"brokers": {"b": {"nodeid": 1, "state": "UP"}}})
        feed._on_stats(up)
        self.assertEqual(self.runtime.states[-1], ("volume", "replaying"))
        self.eof(feed, 0)
        feed._on_stats(json.dumps({"brokers": {"b": {"nodeid": 1, "state": "DOWN"}}}))
        self.assertEqual(self.runtime.states[-1], ("volume", "disconnected"))
        feed._on_stats(up)
        self.assertEqual(self.runtime.states[-1], ("volume", "replaying"))

    def test_start_and_stop_close_both_independent_consumers(self):
        self.consumer.start()
        self.consumer.stop()
        self.assertEqual(len(self.created), 2)
        self.assertTrue(all(client.closed for client in self.created))
        self.assertIn(("orders", "stopped"), self.runtime.states)
        self.assertIn(("volume", "stopped"), self.runtime.states)

    def test_second_consumer_start_failure_closes_first(self):
        def factory(config):
            if "volume" in config["client.id"]:
                raise ValueError("invalid volume client config")
            return self.factory(config)

        consumer = SmartSellingConsumer({}, self.runtime, consumer_factory=factory)
        with self.assertRaises(ValueError):
            consumer.start()
        self.assertTrue(self.created[0].closed)

    def test_connection_error_recreates_consumer_instead_of_waiting_for_old_eof(self):
        feed = self.consumer.feeds["orders"]

        class NoDelayStop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                return self.stopped

        stop = NoDelayStop()
        clients = []

        class ReconnectingConsumer(FakeConsumer):
            def poll(self, timeout):
                if len(clients) == 1:
                    feed._on_error(KafkaError(KafkaError._ALL_BROKERS_DOWN))
                else:
                    stop.stopped = True
                return None

        def factory(config):
            client = ReconnectingConsumer(config)
            clients.append(client)
            return client

        feed._stop = stop
        feed.factory = factory
        with self.assertLogs("trading-ui.smart-selling-consumer", level="WARNING"):
            feed._run()
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.closed for client in clients))


if __name__ == "__main__":
    unittest.main()
