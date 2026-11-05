"""Bounded market-data replay with fake Kafka consumers; no broker required."""

import json
from unittest.mock import Mock

import pytest
from confluent_kafka import KafkaError, KafkaException, OFFSET_END, TopicPartition

from trading_ui.config import load_config
from trading_ui.kafka.consumer_market_data import PriceBookConsumer
from trading_ui.services.market_data import MarketDataStore


@pytest.mark.parametrize(
    "low,high,expected",
    [(0, 1_000_000, 999_900), (0, 42, 0), (950, 1000, 950), (0, 0, 0), (500, 500, 500)],
)
def test_assignment_replays_only_recent_retained_records(low, high, expected):
    client = Mock()
    client.get_watermark_offsets.return_value = (low, high)
    consumer = PriceBookConsumer({}, MarketDataStore())
    partition = TopicPartition("price-books", 0, 12)

    consumer._on_assign(client, [partition])

    assert partition.offset == expected
    client.get_watermark_offsets.assert_called_once_with(partition, timeout=5.0, cached=False)
    client.assign.assert_called_once_with([partition])


def test_each_topic_and_partition_gets_its_own_recent_window_on_reassignment():
    client = Mock()
    client.get_watermark_offsets.side_effect = [
        (0, 1000), (0, 500), (800, 810), (0, 2000), (0, 800), (800, 1000),
    ]
    consumer = PriceBookConsumer({"market_data_replay_records": 25}, MarketDataStore())
    partitions = [
        TopicPartition("price-books", 0), TopicPartition("price-books", 1),
        TopicPartition("stock-quotes", 0),
    ]

    consumer._on_assign(client, partitions)
    assert [p.offset for p in partitions] == [975, 475, 800]
    consumer._on_assign(client, partitions)
    assert [p.offset for p in partitions] == [1975, 775, 975]
    assert client.assign.call_count == 2


def test_zero_replay_waits_for_live_data_without_watermark_requests():
    client = Mock()
    consumer = PriceBookConsumer({"market_data_replay_records": 0}, MarketDataStore())
    partitions = [TopicPartition("price-books", 0), TopicPartition("stock-quotes", 0)]

    consumer._on_assign(client, partitions)

    assert [p.offset for p in partitions] == [OFFSET_END, OFFSET_END]
    client.get_watermark_offsets.assert_not_called()
    client.assign.assert_called_once_with(partitions)


@pytest.mark.parametrize("failure", [None, KafkaException(KafkaError(KafkaError._TIMED_OUT))])
def test_missing_offsets_fall_back_to_live_data_without_losing_other_partitions(failure, caplog):
    client = Mock()
    client.get_watermark_offsets.side_effect = [failure, (0, 500)]
    consumer = PriceBookConsumer({}, MarketDataStore())
    partitions = [TopicPartition("price-books", 0), TopicPartition("stock-quotes", 0)]

    consumer._on_assign(client, partitions)

    assert [p.offset for p in partitions] == [OFFSET_END, 400]
    client.assign.assert_called_once_with(partitions)
    assert "waiting for new data" in caplog.text


def test_polling_skips_old_history_then_consumes_new_books_with_original_offsets():
    store = MarketDataStore()
    callback = Mock()
    health = Mock()
    consumer = PriceBookConsumer(
        {"market_data_topic": "price-books"}, store,
        payload_callback=callback, health_callback=health,
    )

    class FakeConsumer:
        def __init__(self):
            self.cursor = None
            self.high = 1000
            self.caught_up = False

        def get_watermark_offsets(self, partition, **kwargs):
            return (0, self.high)

        def assign(self, partitions):
            self.cursor = partitions[0].offset

        def poll(self, timeout):
            if self.cursor == self.high:
                if self.caught_up:
                    consumer._stop.set()
                else:
                    # No data at the end, then a new record arrives.
                    self.caught_up = True
                    self.high += 1
                return None
            offset = self.cursor
            self.cursor += 1
            payload = {
                "symbol": "AAPL", "sequence_num": offset,
                "bid_side": {"levels": [{"price": offset, "quantity": 10}]},
                "offer_side": {"levels": [{"price": offset + 1, "quantity": 10}]},
            }
            message = Mock()
            message.error.return_value = None
            message.value.return_value = json.dumps(payload).encode()
            message.topic.return_value = "price-books"
            message.partition.return_value = 0
            message.offset.return_value = offset
            return message

    client = FakeConsumer()
    consumer._consumer = client
    consumer._on_assign(client, [TopicPartition("price-books", 0)])
    consumer._run()

    assert [call.kwargs["offset"] for call in callback.call_args_list] == list(range(900, 1001))
    assert callback.call_args.kwargs == {"topic": "price-books", "partition": 0, "offset": 1000}
    assert store.get_book("AAPL").bids[0].price == 1000
    assert client.caught_up
    health.assert_called_with(True)


@pytest.fixture
def load_replay_config(tmp_path, monkeypatch):
    monkeypatch.delenv("KAFKA_MARKET_DATA_REPLAY_RECORDS", raising=False)
    monkeypatch.delenv("KAFKA_MARKET_DATA_AUTO_OFFSET_RESET", raising=False)

    def load(kafka=None):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "accounts": [{"id": "test"}], "symbols": ["AAPL"],
            "auth": {"users": {
                "admin": {"password": "test"}, "trader": {"password": "test"},
            }},
            "kafka": kafka or {},
        }))
        return load_config(str(path))["kafka"]

    return load


def test_default_config_enables_bounded_replay(load_replay_config):
    config = load_replay_config()
    assert config["market_data_replay_records"] == 100
    assert config["market_data_auto_offset_reset"] == "latest"


def test_environment_sets_replay_window_and_json_takes_precedence(load_replay_config, monkeypatch):
    monkeypatch.setenv("KAFKA_MARKET_DATA_REPLAY_RECORDS", "250")
    assert load_replay_config()["market_data_replay_records"] == 250
    assert load_replay_config({"market_data_replay_records": 0})["market_data_replay_records"] == 0


@pytest.mark.parametrize("value", [-1, "-1", True, 1.5, None, "", "earliest", "10.5"])
def test_invalid_replay_configuration_is_rejected(load_replay_config, value):
    with pytest.raises(ValueError, match="market_data_replay_records must be a non-negative integer"):
        load_replay_config({"market_data_replay_records": value})


def test_invalid_environment_replay_configuration_is_rejected(load_replay_config, monkeypatch):
    monkeypatch.setenv("KAFKA_MARKET_DATA_REPLAY_RECORDS", "-100")
    with pytest.raises(ValueError, match="market_data_replay_records"):
        load_replay_config()
