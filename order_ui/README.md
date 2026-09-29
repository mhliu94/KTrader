# KTrader Order UI

A separate, read-only FastAPI server showing today's orders across the same
visible trading accounts as `trading_ui`. The browser fetches updates every two
seconds without page reloads. It includes account/status/search filters, fill
information, order IDs, messages, and Kafka connection/replay status.

## Run

From the **KTrader repository root**, with Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r order_ui/requirements.txt
.venv/bin/python -m order_ui.webserver --config /path/to/your/trading-ui/config.json
```

Open **http://127.0.0.1:8030**. Sign in using a username and password from the
selected configuration's `auth.users`, including `admin`, `trader`, or additional
configured users. Order UI sessions expire after 12 hours and use their own
cookie, `ktrader_order_ui_session`, so signing in/out does not overwrite trading
UI sessions. All configured login users can read all configured visible accounts,
matching the trading UI's account visibility model.

This process starts only the order consumer and web server. No orders can be
placed, amended, or canceled through it. The existing trading UI can run at the
same time on its own port.

Config path precedence: `--config`, then `ORDER_UI_CONFIG`, then
`ACCOUNT_DASHBOARD_CONFIG`, then `trading_ui/sample/config.local.json` if present,
otherwise `trading_ui/sample/config.json`. Pass the same path as your trading UI
in production. **Restart this server after editing accounts, passwords, or settings.**

## Configuration

Reuse these entries from your existing trading UI configuration:

| Entry | Purpose |
| --- | --- |
| `accounts_file` | Shared trading accounts JSON file, resolved relative to the selected config file. `trading_accounts_file` and `TRADING_ACCOUNTS_CONFIG` are supported as in the trading UI. Legacy inline `accounts` are also supported. |
| `auth.users` | The existing user/password objects; built-in `admin` and `trader` entries must be present. |
| `kafka.bootstrap_servers` | The shared Kafka cluster. Falls back to `KAFKA_BOOTSTRAP_SERVERS`, then `localhost:9092`. |

The shared account parser is reused directly: `string_id`, `numeric_id`, `broker`,
and `trading_medium` retain the trading UI's validation and aliases. Hidden
accounts are excluded. Unknown or hidden account IDs in Kafka are ignored.

Merge the `order_ui` object in
[`sample/order_ui.settings.example.json`](sample/order_ui.settings.example.json)
into that same configuration file. This file is a **settings fragment**, not a
replacement for your shared configuration. All `order_ui` entries are optional:

| Entry under `order_ui` | Default | Meaning |
| --- | --- | --- |
| `timezone` | `America/New_York` | IANA timezone defining today's creation date and displayed times, including daylight saving. |
| `refresh_interval_seconds` | `2` | Browser polling period, 0.5–60 seconds. |
| `server.host` | `127.0.0.1` | Independent listening interface; use `0.0.0.0` for access from other machines. |
| `server.port` | `8030` | Independent TCP port. |
| `server.ssl_certfile` | empty | Optional HTTPS certificate path relative to this config file. |
| `server.ssl_keyfile` | empty | HTTPS private key path; supply both certificate and key to enable HTTPS. |
| `kafka.bootstrap_servers` | shared `kafka.bootstrap_servers` | Optional separate order-status cluster. |
| `kafka.topic` | `order-status` | Topic that account listeners publish full order snapshots to. |
| `kafka.group_id` | `order-dashboard` | Consumer group **prefix**; a unique suffix is generated per process to let each dashboard read all partitions. |
| `kafka.poll_timeout_sec` | `1.0` | Kafka consumer poll timeout, 0.1–5 seconds. |
| `kafka.client_config` | `{}` | Optional librdkafka SASL/TLS connection settings, described below. |

`--host` and `--port` override `order_ui.server` settings. The shared top-level
`server` configuration and `UI_*` environment variables do not change this
server's listening address, port, or TLS settings.

For an authenticated Kafka cluster, set connection properties explicitly in
`order_ui.kafka.client_config` using librdkafka names, for example:

```json
{
  "security.protocol": "SASL_SSL",
  "sasl.mechanism": "PLAIN",
  "sasl.username": "orders-reader",
  "sasl.password": "YOUR_PASSWORD",
  "ssl.ca.location": "certs/kafka-ca.pem"
}
```

Also supported: `sasl.mechanisms`, `ssl.certificate.location`, `ssl.key.location`,
`ssl.key.password`, and `ssl.endpoint.identification.algorithm`. Certificate paths
are relative to the selected config file. Consumer/replay settings cannot be
overridden here. Use credentials with read access to the topic and group access
to the configured prefix. Use HTTPS directly or through your existing reverse
proxy when serving authenticated users over a network.

## Kafka publisher contract

The machine-readable contract is
[`schema/order-status.v1.schema.json`](schema/order-status.v1.schema.json), JSON
Schema Draft 2020-12. The consumer validates this exact file at runtime, plus the
cross-field rules below. A complete example is in
[`sample/order-status.v1.json`](sample/order-status.v1.json).

Each account listener must publish **one full JSON snapshot per order whenever
its status or fills change**. Kafka value: plain UTF-8 JSON, with no Schema
Registry framing. Kafka key: the compact JSON array `[account_id,order_id]`,
encoded as UTF-8 (for example `["GIGA-HWK","broker-order-12345"]`). Keep this key
stable so all updates to one order use the same partition. The value's IDs are
authoritative; the consumer can still read snapshots with a missing key.

| Required field | Type / semantics |
| --- | --- |
| `schema_version` | Integer `1`. |
| `account_id` | Exact shared account `string_id`, not the broker's account number. |
| `order_id` | Stable string ID unique within this account. Keep it unchanged from submission through completion; use `client_order_id` separately if needed. Do not reuse IDs for different orders. |
| `sequence` | Nonnegative integer up to 2^53−1, strictly increasing for each `(account_id, order_id)`. Persist it across publisher restarts; duplicate retries retain their original sequence. |
| `created_at` | Original order creation time, immutable across updates. RFC 3339 with `Z` or explicit UTC offset. |
| `updated_at` | Broker/source update time, RFC 3339 with offset, at or after creation; must not decrease as sequence increases. |
| `symbol` | Nonempty security symbol; any symbol is accepted, independent of the trading UI watchlist. |
| `side` | `BUY` or `SELL`. |
| `order_type` | `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT`, `TRAILING_STOP`, or `OTHER`. |
| `status` | One of the status values below; map broker-specific names before publishing. |
| `quantity` | Positive number of shares/units. For an amount-based order with unknown quantity, use `null` and provide positive `notional`. |
| `filled_quantity` | Cumulative filled shares/units, not the size of the latest fill; nonnegative and no larger than a known quantity. |
| `average_fill_price` | Cumulative weighted average fill price, nonnegative or `null` when unavailable/unfilled. |

Optional fields: `account_num_id` (must match the configured numeric ID),
`limit_price`, `stop_price`, `notional`, `currency` (three uppercase letters),
`time_in_force`, `command_id` (the originating KTrader command), `client_order_id`,
and `message` (up to 2,000 characters). `limit_price` is required for `LIMIT` and
`STOP_LIMIT`; `stop_price` is required for `STOP` and `STOP_LIMIT`. Prices must be
nonnegative. All numeric values must be finite. Snapshots are limited to 64 KiB.
Unknown fields are rejected in v1. Omitted optional fields are cleared by the
next snapshot, because updates replace the complete order state.

| Status | Meaning |
| --- | --- |
| `PENDING_NEW` | Submitted, awaiting broker acceptance. |
| `NEW` | Accepted/open, no partial fill reported. |
| `PARTIALLY_FILLED` | Positive fill, less than the known total quantity. |
| `PENDING_CANCEL` | Cancellation requested but not confirmed; may still fill. |
| `FILLED` | Fully executed; positive filled quantity equal to known total quantity. |
| `CANCELED` | Broker confirmed cancellation; may include partial fills. |
| `REJECTED` | Broker rejected the order. Put the reason in `message`. |
| `EXPIRED` | Order expired; may include partial fills. |
| `UNKNOWN` | Broker state unavailable/unmapped; describe it in `message`. |

The highest sequence wins; equal or lower sequences are ignored. A higher
sequence can correct a prior terminal status. The publisher is responsible for
authoritative order state, IDs, sequence persistence, and accurate timestamps.
Malformed messages are skipped and counted without stopping consumption.

A Python publishing pattern, running from KTrader with these dependencies:

```python
import json
from confluent_kafka import Producer
from order_ui.config import load_config
from order_ui.events import parse_order_message

cfg = load_config("/path/to/your/trading-ui/config.json")
kafka = cfg["kafka"]
producer = Producer({
    **kafka["client_config"],  # Or separate credentials with write permission.
    "bootstrap.servers": kafka["bootstrap_servers"],
    "enable.idempotence": True,
    "acks": "all",
})

def publish_snapshot(snapshot):
    value = json.dumps(snapshot, allow_nan=False, separators=(",", ":")).encode()
    parse_order_message(value)  # Validate before publishing.
    key = json.dumps([snapshot["account_id"], snapshot["order_id"]],
                     separators=(",", ":")).encode()
    errors = []
    producer.produce(kafka["topic"], key=key, value=value,
                     on_delivery=lambda error, msg: errors.append(error) if error else None)
    if producer.flush(10) or errors:
        raise RuntimeError("Snapshot was not confirmed delivered; retry the same sequence.")
```

The repository's Tiger/Futu listeners are not automatically adapted by this
dashboard. They (and other account implementations) must publish this contract
to populate it. No placement success is inferred from trading commands.

## Today's orders, restart, and deployment behavior

- Today is the calendar date of **`created_at` in America/New_York** by default.
  This is not an exchange-session calendar. An order created yesterday and filled
  today is excluded. New York midnight clears old rows on the next poll, even if
  no new Kafka event arrives. Keep producer and server clocks synchronized.
- On each start or partition reassignment, the consumer reads from the beginning
  of retained Kafka data and builds an in-memory projection of today's latest
  snapshots. This avoids missing morning orders when starting in the afternoon.
  It never commits offsets and uses an independent group for each process.
  Replay may take time on large topics; the UI shows a loading state until every
  assigned partition reaches its end. It then follows new messages continuously.
- Create the configured topic before running this server. Automatic topic creation
  is disabled. Retain at least a full trading day plus restart margin (for example
  72 hours); Kafka cannot replay events that retention has already removed. A
  compacted topic also works if stable keys retain the full latest snapshots for
  today's completed orders. Do not tombstone today's orders.
- Publishers restarting mid-day should republish full snapshots of all today's
  orders, including completed orders. Reuse a sequence only for identical retries;
  increment it for changed snapshots. Prior-day, future-day, unknown, and hidden
  account events are ignored.
- Run one Uvicorn worker per server instance. State and login sessions are in
  memory. Separate instances receive all partitions, but do not share sessions;
  a multi-instance proxy needs sticky sessions. No database is required.
- The UI shows stale/connection warnings during outages and retries automatically.
  Recovery is best effort within the topic's retention window. The authenticated
  `GET /api/orders` endpoint includes the current date, timezone, order snapshots,
  visible account labels, and feed status/counters; it never returns credentials.

Kafka replay and callbacks use the official
[Confluent Python consumer API](https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#pythonclient-consumer).

## Tests

```sh
.venv/bin/python -m pip install pytest httpx
.venv/bin/python -m pytest order_ui/tests -q
```

Tests use an isolated fake consumer, with no connection to a production broker.
They cover event validation, sequencing, shared accounts, authentication,
replay/shutdown, automatic data updates, midnight expiry, and DST boundaries.
