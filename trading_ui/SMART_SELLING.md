# Smart selling / 智能卖出

Mode **G** adds fixed-participation liquidation to the control panel. The control
server publishes existing `LIMIT_ORDER` and account/symbol-scoped
`CANCEL_OPEN_ORDERS` commands. Trading-server and broker scripts are unchanged.
Each parent owns one instrument and one or more selected accounts. It places one
child at a time and waits for an authoritative terminal cumulative-fill snapshot
before placing the next child. Accounts rotate in selection order; if an account
has fewer shares than a planned slice, the remainder passes to the next account
after that child's terminal report. Exhausted accounts are skipped, and sales are
capped by each account's inventory. Short selling is not allowed.

The implementation follows the supplied **POV liquidation research handoff v2.0
(21 October 2026)**. Trend adaptation, automatic participation increases,
multi-level sweeps, and timed price ladders are excluded from this baseline.

## Code map

| Module | Responsibility |
|---|---|
| `market_data/trading_volume.py` | TradeStation volume validation and Kafka records |
| `smart_selling_config.py` | Application defaults, participation settings, and instrument-rule validation |
| `services/smart_selling_policy.py` | Pure decimal sizing, price selection, and rounding |
| `services/smart_selling.py` | Durable credit ledger and parent/child state transitions |
| `services/smart_selling_market.py` | Quote/volume admission, freshness, and halt recovery |
| `services/smart_selling_feedback.py` | Order identity validation and cumulative-fill mapping |
| `services/smart_selling_feedback_store.py` | Durable order and external-command watermarks |
| `services/smart_selling_commands.py` | Existing limit/cancel command payloads; no I/O |
| `services/smart_selling_runtime.py` | Heartbeat, feed coordination, and asynchronous handoff |
| `kafka/consumer_smart_selling.py` | Independent Kafka connections and replay gates |

The policy contains no Kafka or HTTP calls. The engine commits intent before the
runtime submits it to the publisher worker. Feed callbacks, final dispatch checks,
and the existing publisher's nonblocking enqueue use serialized state access.
Stale cancellation intents are dropped after their child becomes terminal.
Broker-specific behavior remains in the trading servers.

## Settings and execution

Choose mode G and click **Start** to open its settings dialog. Select one or more
accounts, with holdings shown beside each account, and choose urgency,
start time, and deadline. The UI sells all sellable inventory across the selected
accounts; stop manually to end early. Each account's available inventory is capped
by its holdings and rounded down to the configured quantity step, so unsellable
fractional remainders stay in that account. The backend still accepts an explicit
`quantity` for future callers and the legacy single `account_id` field. There is
no quantity control in the current dialog.

Start can be **Now** or a configured time. Deadline can be a configured time or
**No deadline**; a session without a deadline runs until inventory is sold or it is
stopped. Browser-local times are converted to UTC before submission. Defaults are
start now, a two-hour deadline, and normal urgency. An optional minimum sale price
is never relaxed. API callers send `account_ids` in rotation order, omit `quantity`
to sell available inventory, use `start_time: null` for now, and `deadline: null`
for no deadline.

| Setting | Patient | Normal | Urgent |
|---|---|---|---|
| Default participation in newly observed volume | 3% | 6% | 10% |
| Normal limit | Best ask | Midpoint rounded upward | Best bid |
| Cancel-request age | 5 seconds | 5 seconds | 2 seconds |

Participation is configured in the control UI server's JSON settings. The
urgency selector shows only Patient, Normal, and Urgent; it does not show rates.
The default configuration is:

```json
{
  "smart_selling": {
    "participation_percentages": {
      "patient": "3",
      "normal": "6",
      "urgent": "10"
    }
  }
}
```

Values are percentage units: `"6"` means 6%, not 0.06%. Partial overrides keep the
other defaults. Each value must be finite, greater than zero, and at most 100;
unknown modes, booleans, and unsupported decimal precision are rejected. Decimal
strings preserve exact configured values. Restart the control UI to load changed
settings. A session saves its selected rate when created, including a scheduled
session, and retains that rate after recovery. Configuration changes apply only
to newly created sessions; rates do not update automatically during a session.
See [`sample/config.json`](sample/config.json) and the full
[`Smart selling settings example`](sample/smart_selling.settings.example.json).

Patient and normal modes use the best bid on the next eligible child after an
unfilled passive attempt, when behind the linear progress reference by more than
one child cap, or when a backend caller sets `imminent_event: true`. A working
order's limit stays fixed. Price direction does not reduce the participation rate.

The backend retains **Adverse event is imminent** (`imminent_event`) for future
use; it defaults to `false` and has no control in the current UI. It means a caller
expects a near-term event that could hurt the share price, such as unfavorable
news or an announcement. Setting it to `true` makes patient and normal modes use
the best bid for new orders, prioritizing execution while preserving the selected
participation rate and minimum price. This is a caller-provided signal; the
algorithm does not detect or predict events.

Decisions run once per second. The child cap is the parent quantity times
30 seconds divided by its horizon, rounded down to the quantity step and at least
the legal minimum. No-deadline sessions retain the default two-hour sizing horizon
without acquiring a stop time or a deadline-based progress reference. Each child
is also capped at 10% of displayed accessible best
bid size, remaining inventory, and earned credit. Free credit is capped at two
children. Five seconds must separate submissions; after 30 seconds a legal
smaller child may use a partial budget. No credit is invented at startup.

The default instrument rules are a constant $0.01 tick, whole shares, a one-share
minimum, and no additional minimum notional. Override these for the instrument
and downstream execution contract using
[`sample/smart_selling.settings.example.json`](sample/smart_selling.settings.example.json).
The current implementation supports constant tick grids, not tiered tick ladders.
The configured price-book feed must represent depth accessible to the trading
servers. Prices and quantities use decimal arithmetic inside the controller.
Values that cannot round-trip through the existing numeric command format are
rejected before publication rather than rounded silently.

Recent account snapshots, enabled trading for every selected account, sufficient
available holdings, and a reconciled order-status feed are required to start.
Known open orders and unacknowledged manual commands for the selected account and
instrument must resolve before starting. Only one algorithm session may run at a
time across E, F, and G, including scheduled sessions and sessions still reconciling
a stop. Stop the current session and allow its outstanding orders to resolve before
starting another.

Manual order instructions, including cancellations and orders for other symbols,
are rejected for accounts owned by an algorithm session. Other accounts remain
available for manual trading. External systems must honor this ownership too:
the existing cancel command cancels **all open orders for that account and symbol**.

## Independent trading-volume feed

### Last-trade quotes for the account summary

The price-book publisher also publishes **`stock-quotes`** on its configured
Kafka cluster, using the fixed `symbols` list in
`market_data/md_producer_config.json`. It reuses the quote poller's existing
requests and interval (five seconds by default). Volume-only symbols are not
automatically added to this topic.

The topic has been provisioned on the configured market-data cluster with one
partition and one replica, matching `price-books`.

Each record is keyed by symbol and represents the latest observed trade, **not
every individual trade**. `last` comes from TradeStation's `Last`, `trade_time`
from `TradeTime`, and optional `last_size` from `LastSize`. Prices are decimal
strings; timestamps are UTC ISO 8601. An example record is:

```json
{"schema_version":1,"type":"STOCK_QUOTE","symbol":"AAPL","source":"TradeStation","last":"125.1250","last_size":7,"trade_time":"2026-10-23T19:59:58Z","observed_at":"2026-10-23T20:00:00Z","is_delayed":false,"is_halted":false,"healthy":true}
```

Missing or invalid prices/timestamps, provider errors, and delayed or unknown
realtime status produce `healthy:false`, `last:null`, and a `reason`. An old
last-trade timestamp is valid when markets are closed or a stock is illiquid.
These records are independent of Smart selling's volume eligibility rules.

The dashboard consumes this topic alongside `price-books` and stores trade
prices separately from depth-derived prices. Its account summary uses only
trade prices for **Last / 最新** and **Market Value / 市值**; holdings with no
usable trade quote show `--` for both. Unavailable observations clear a previous
price, and older observations cannot overwrite newer ones.

The producer setting `stock_quotes_topic` and dashboard Kafka setting of the
same name default to `stock-quotes`; the dashboard also accepts
`KAFKA_STOCK_QUOTES_TOPIC`. Provision the topic on the market-data cluster if
broker topic auto-creation is disabled. Restart the publisher and dashboard to
load these changes.

### Volume observations

The Kafka topic **`trading-volume`** was created on the configured market-data
cluster with one partition and one replica, matching `price-books`.

[`market_data/md_producer_config.json`](../market_data/md_producer_config.json)
has independent `trading_volume_symbols`, `trading_volume_topic`, and
`trading_volume_poll_interval_seconds` settings. The ticker list initially matches
the current book list and can be changed independently. The publisher batches the
union of both lists into TradeStation quote requests, up to 100 symbols per
request, every five seconds. It publishes volume observations even when depth
does not change.

TradeStation's current `GET /v3/marketdata/quotes/{symbols}` returns **`Volume`**
as daily shares/contracts. `LastSize` is one trade's size; `PreviousVolume` is the
previous day. Neither is used as cumulative volume. `TradeTime` is the last trade
time, so an unchanged old last trade alone does not make an illiquid market stale.
See the [official TradeStation specification](https://api.tradestation.com/docs/specification/).

Example record (Kafka key is the symbol):

```json
{
  "schema_version": 1,
  "type": "TRADING_VOLUME",
  "symbol": "XYZ",
  "source": "TradeStation",
  "feed_id": "producer-instance-id",
  "sequence": 42,
  "session": "2026-10-22",
  "total_volume": 123456,
  "observed_at": "2026-10-22T15:00:05Z",
  "interval_start": "2026-10-22T15:00:00Z",
  "trade_time": "2026-10-22T15:00:04Z",
  "request_duration_ms": 120,
  "healthy": true,
  "is_halted": false,
  "is_delayed": false
}
```

Unhealthy observations retain identity/timestamps, set `healthy:false`, include a
reason, and carry `total_volume:null`. Unknown halt/delay flags remain `null`.
Sessions use the New York calendar date for this equity-oriented feed.

Smart selling uses **only this topic** to earn credit; cached volume in a price
book never earns credit. The first observation establishes a baseline. Only
timely, ordered increments wholly after a parent's start are eligible. Own public
prints are included once through the tape; broker fills earn no extra credit.
Duplicate messages are ignored. Reconnects and gaps discard uncertain intervals,
and unidentified downward corrections hold the strategy for reconciliation.
Five-second cumulative snapshots cannot reconstruct individual trade corrections
or precisely exclude every delayed print. This is a conservative sampled-volume
baseline, not a trade-by-trade SIP feed.

The consumer defaults to `kafka.market_data_bootstrap_servers` for volumes and
`kafka.bootstrap_servers` for orders. Override
`smart_selling.trading_volume_bootstrap_servers` and
`smart_selling.order_status_bootstrap_servers` if necessary. Each control process
reads all partitions independently and completes retained replay before trading.

## Order feedback, stop, and restart

Trading servers publish the existing
[`order-status.v1` contract](../order_ui/schema/order-status.v1.schema.json) to
`order-status`. A submitted child's `command_id` equals its stable
`client_order_id`; snapshots must preserve one of these until the broker order ID
is bound. Account, symbol, original quantity, side, type, limit, cumulative fill,
and per-order sequence are checked before applying a report.

Cancel requests, Kafka acknowledgments, missing status, and timers do not prove
that an order is gone. Confirmed cumulative fills reduce reservations exactly
once. Only authoritative terminal status refunds verified leaves. An unresolved
submit/cancel enters reconciliation after three seconds with five-second retry
spacing. The existing command vocabulary has no broker-query operation, so
control-side reconciliation reuses retained snapshots and waits for fresh
downstream reports; it cannot force a fresh broker query. A trading server that
does not publish these reports leaves the parent safely held with its reservation.

SQLite persists order intent and reservations **before** publication, terminal
transitions, tape IDs/corrections, and telemetry. Ambiguous submissions are never
automatically re-sent after restart. Preserve the journal and its feedback
database while any parent or external command is unresolved. An exclusive process
lock prevents two controllers using the same journal; run a single control-server
worker for this state directory.

Stop and deadline prevent new children, request cancellation, and continue order
processing. Confirmed remaining inventory and outstanding reservations are shown
separately. The result remains provisional until the outstanding order reconciles.
The global stop button includes Smart selling.

## LULD and reopening

Fresh TradeStation `MarketFlags.IsHalted` triggers a halt hold: no new children or
volume credit, cancellation requested, reservations retained. An unknown flag
does not establish normal trading. Resumption requires explicit non-halted,
non-delayed observations, a ten-second cooldown, at least two healthy
observations, fresh post-reopening depth, and resolution of the previous order.
The volume baseline resets through recovery, so reopening auction volume does not
produce a burst of credit. The parent deadline and minimum price stay unchanged.

This is **halt detection**, not full LULD band awareness. The quote API does not
provide equity LULD bands or the preceding limit-state signal; its `MinPrice` and
`MaxPrice` fields describe futures limits. Five-second polling plus network delay
can detect a pause late. Earlier limit-state/band handling needs a suitable
exchange/SIP market-status feed. No fixed five-minute timer is treated as proof of
reopening. Orders may remain on the book during a pause; see the
[Nasdaq LULD FAQ](https://nasdaqtrader.com/content/MarketRegulation/LULD_FAQ.pdf).

## Diagnostics and validation

The status endpoint `/api/smart-selling` returns parent progress, free credit,
reserved quantity, fills, discarded credit, market/order state, and the binding
reason. Every 30 seconds a rolling observed-volume estimate reports required
remaining volume and base/75%/50% volume scenarios for sessions with a deadline.
No-deadline sessions omit this completion forecast. These are diagnostics, not an
automatic rate change or a calibrated completion forecast. The durable event log
records IDs, quote/size decisions, accounting transitions, recovery, and latency
counts/medians/observed maxima.

Run offline checks from the repository root:

```sh
.venv/bin/python -m pytest tests order_ui/tests -q
```

Tests cover volume replay/deduplication, exact accounting, cancel/fill races,
late acknowledgments, unknown orders, restart, deadline, halt/reopening, API
authentication, and English/Chinese browser interaction. HTTP/Kafka/broker
effects are mocked. These tests do not establish live execution performance,
market impact, or provider completeness. Restart the market-data publisher and
control UI to load the new code; applying code changes alone does not restart
running processes.
