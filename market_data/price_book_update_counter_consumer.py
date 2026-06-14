import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict

from kafka import KafkaConsumer, TopicPartition


def load_producer_config() -> Dict[str, object]:
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "md_producer_config.json"
    with config_path.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def print_counts(counts: Dict[str, int]) -> None:
    if not counts:
        print("No updates received.")
        return

    print("Update counts by symbol:")
    for symbol in sorted(counts.keys()):
        print(f"  {symbol}: {counts[symbol]}")


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

    print(f"Consuming topic '{kafka_topic}' from {kafka_server_ip}:{kafka_port}")
    print("Starting from messages published in the last hour. Press Ctrl+C to stop.")

    try:
        seek_to_last_hour(consumer, kafka_topic)

        while True:
            polled = consumer.poll(timeout_ms=1000)
            for messages in polled.values():
                for msg in messages:
                    try:
                        payload = json.loads(msg.value)
                    except json.JSONDecodeError as exc:
                        print(f"Skipping invalid JSON message: {exc}")
                        continue

                    symbol = payload.get("symbol")
                    volume = payload.get("volume")
                    if not symbol:
                        print("Skipping message without 'symbol' field.")
                        continue
                    print("Volume: " + str(volume))

                    counts[str(symbol)] += 1

    except KeyboardInterrupt:
        print("\nStopping consumer...")
    finally:
        print("\nFinal totals (last hour + live while running):")
        print_counts(counts)
        consumer.close()


if __name__ == "__main__":
    main()
