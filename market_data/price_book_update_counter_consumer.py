import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict

from kafka import KafkaConsumer, TopicPartition

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOGGER = logging.getLogger("market-data.price-book-update-counter")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )


def load_producer_config() -> Dict[str, object]:
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "md_producer_config.json"
    with config_path.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def print_counts(counts: Dict[str, int]) -> None:
    if not counts:
        LOGGER.info("No updates received.")
        return

    LOGGER.info("Update counts by symbol:")
    for symbol in sorted(counts.keys()):
        LOGGER.info("  %s: %s", symbol, counts[symbol])


def seek_to_last_hour(consumer: KafkaConsumer, topic: str) -> None:
    partitions = consumer.partitions_for_topic(topic)
    if not partitions:
        raise RuntimeError(f"No partitions found for topic '{topic}'.")

    topic_partitions = [TopicPartition(topic, p) for p in partitions]
    consumer.assign(topic_partitions)

    one_hour_ago_ms = int((time.time() - 3600) * 1000)
    offsets_query = {tp: one_hour_ago_ms for tp in topic_partitions}
    offsets = consumer.offsets_for_times(offsets_query)

    for tp in topic_partitions:
        oat = offsets.get(tp)
        if oat is not None and oat.offset is not None:
            consumer.seek(tp, oat.offset)
        else:
            consumer.seek_to_end(tp)


def main() -> None:
    configure_logging()
    cfg = load_producer_config()
    kafka_server_ip = str(cfg["kafka_server_ip"])
    kafka_port = int(cfg["kafka_port"])
    kafka_topic = str(cfg["kafka_topic"])

    consumer = KafkaConsumer(
        bootstrap_servers=f"{kafka_server_ip}:{kafka_port}",
        enable_auto_commit=False,
        group_id=None,
        value_deserializer=lambda v: v.decode("utf-8"),
    )

    counts: Dict[str, int] = defaultdict(int)

    LOGGER.info("Consuming topic %s from %s:%s", kafka_topic, kafka_server_ip, kafka_port)
    LOGGER.info("Starting from messages published in the last hour. Press Ctrl+C to stop.")

    try:
        seek_to_last_hour(consumer, kafka_topic)

        while True:
            polled = consumer.poll(timeout_ms=1000)
            for messages in polled.values():
                for msg in messages:
                    try:
                        payload = json.loads(msg.value)
                    except json.JSONDecodeError as exc:
                        LOGGER.warning("Skipping invalid JSON message: %s", exc)
                        continue

                    symbol = payload.get("symbol")
                    volume = payload.get("volume")
                    if not symbol:
                        LOGGER.warning("Skipping message without 'symbol' field.")
                        continue
                    LOGGER.info("Volume: %s", volume)

                    counts[str(symbol)] += 1

    except KeyboardInterrupt:
        LOGGER.info("Stopping consumer...")
    finally:
        LOGGER.info("Final totals (last hour + live while running):")
        print_counts(counts)
        consumer.close()


if __name__ == "__main__":
    main()
