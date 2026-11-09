# Smart selling / 智能卖出

Mode **G** adds fixed-participation liquidation to the control panel. The control
server publishes existing `LIMIT_ORDER` and account/symbol-scoped
`CANCEL_OPEN_ORDERS` commands. Trading-server and broker scripts are unchanged.
Each parent owns one instrument and one or more selected accounts. It places one
child at a time and waits for an authoritative terminal cumulative-fill snapshot
before placing the next child. Accounts are selected by **average holding cost
closest to the current market price**, using the absolute distance from the live
best bid/ask midpoint. Costs and market prices are refreshed before each new child.
For example, at a market price of $12, an account with $11.50 average cost is chosen
before one with $11.00; $12.10 would rank ahead of both, while $13.00 would rank
behind $11.50. Costs above and below market are treated equally, and the configured
minimum sale price still applies. Equal distances rotate in selection order; unknown costs rank last and rotate
among themselves. Multiple long position rows for the same normalized symbol use
their share-weighted average; incomplete or invalid costs remain unknown.
If the closest account has fewer shares than a planned slice, sell its available
inventory first, subject to the 100-share minimum and any larger broker minimum. After that child's
terminal report, send the unfilled slice remainder to the next closest eligible
account. Repeat as needed until the slice is covered. Exhausted accounts are
skipped, and sales are capped by each account's inventory. Short selling is not
allowed.

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
accounts in cost-priority order; stop manually to end early. Each account's available inventory is capped
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

| Setting | Patient | Normal | Urgent | Ultra |
|---|---|---|---|---|
| Default participation in newly observed volume | 5% | 8% | 15% | 25% |
| Normal limit | Best ask | Midpoint rounded upward | Best bid | Best bid |
| Cancel-request age | 5 seconds | 5 seconds | 2 seconds | 2 seconds |

Participation is configured in the control UI server's JSON settings. The
urgency selector shows only Patient, Normal, Urgent, and Ultra; it does not show rates.
The default configuration is:

```json
{
  "smart_selling": {
    "participation_percentages": {
      "patient": "5",
      "normal": "8",
      "urgent": "15",
      "ultra": "25"
    }
  }
}
```

Values are percentage units: `"8"` means 8%, not 0.08%. Partial overrides keep the
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

Decisions run once per second. By default, the child cap is the parent quantity times
30 seconds divided by its horizon, rounded down to the quantity step and at least
the legal minimum. This cap is independent of urgency; a higher participation rate
earns credit faster without increasing the calculated cap. No-deadline sessions retain the default two-hour sizing horizon
without acquiring a stop time or a deadline-based progress reference. Each child
is also capped by remaining inventory and earned credit.
**The 10% displayed-size rule is not enforced.** Its sizing and pre-dispatch
checks are commented out. Free credit is capped at two
children. Five seconds must separate submissions; after 30 seconds a legal
smaller child may use a partial budget, but never fewer than 100 shares. If POV
credit is below 100, the controller waits until enough actual volume earns the
full minimum. Inventory or split remainders below the minimum cannot create
an order, including when size is checked again immediately before dispatch.
No credit is invented at startup.

The **Disable calculated per-order share cap** checkbox applies to a new Smart
selling session and is unchecked by default. API callers can send the boolean
`disable_child_cap: true`. This removes the horizon-derived cap both when sizing
and immediately before dispatch. Free credit is then bounded by remaining,
unreserved inventory instead of two calculated children, and a legal funded
order can proceed without the 30-second partial-budget wait. The participation
rate, per-account inventory limits, 100-share minimum,
five-second submission spacing, price limits, deadline, and halt checks still
apply. Account-split remainders are preserved. The calculated size remains the
reference for deciding whether execution is behind schedule. The choice is
saved with the session and survives restart; existing sessions keep the cap.
The status API returns `disable_child_cap` and reports `child_cap: null` when
disabled. Smart buying has no corresponding checkbox.

The default instrument rules are a constant $0.01 tick, whole shares, a 100-share
minimum, and no additional minimum notional. A configured lower minimum cannot
reduce the 100-share floor; larger broker minimums and quantity steps still apply.
Existing sessions adopt this floor for new children while outstanding orders keep
their reservations until reconciliation. Override the instrument rules for the instrument
and downstream execution contract using
[`sample/smart_selling.settings.example.json`](sample/smart_selling.settings.example.json).
The current implementation supports constant tick grids, not tiered tick ladders.
The configured price-book feed must represent depth accessible to the trading
servers. Prices and quantities use decimal arithmetic inside the controller.
Values that cannot round-trip through the existing numeric command format are
rejected before publication rather than rounded silently.

Both G and H pause new orders when the user's price bound is **five or more
displayed price levels away** from the executable market. For G, count distinct
ask prices above the best bid up to and including the minimum sale price. For H,
count distinct bid prices below the best offer down to and including the maximum
buy price. A bound at the fifth such level is blocked; four is allowed by this
guard. These are actual displayed prices, not tick increments: gaps do not create
levels, and duplicate prices count once. Zero-size or invalid levels do not count.
For example, with a $10 bid and asks at $10.01, $10.03, $10.10, $10.20 and $10.50,
a $10.50 minimum sell price pauses selling.

The check runs before reserving a child and again immediately before publication.
An unsent child blocked by a newer book is released and its credit refunded;
orders already handed to the broker retain their normal cancellation and
reconciliation behavior. If a nonmarketable bound lies beyond the available depth,
the controller waits for enough depth to locate it. Marketable bounds and sessions
with no price bound do not require additional depth. Eligible credit continues to
accrue under the normal cap while the controller waits.

Recent account snapshots, enabled trading for every selected account, sufficient
available holdings, and a reconciled order-status feed are required to start.
Known open orders and unacknowledged manual commands for the selected account and
instrument must resolve before starting. Several algorithm sessions may run at the
same time across E, F, and G, but each account participates in at most one session.
A scheduled session, or a session still reconciling a stop, keeps its accounts. Stop
the session that owns an account and allow its outstanding orders to resolve before
using that account in another session; other accounts can start a session right away.

Manual order instructions, including cancellations and orders for other symbols,
are rejected for accounts owned by an algorithm session. Other accounts remain
available for manual trading. External systems must honor this ownership too:
the existing cancel command cancels **all open orders for that account and symbol**.

## Independent trading-volume feed

### Last-trade quotes for the account summary

The price-book publisher also publishes **`stock-quotes`** on its configured
Kafka cluster, using the fixed `symbols` list in
`market_data/md_producer_config.json`. It reuses the quote poller's existing
requests and interval (configured here to a one-second delay between polls;
five seconds if the setting is omitted). Volume-only symbols are not
automatically added to this topic.

The **Market Data** tab also uses these snapshots: `PreviousClose` supplies the
previous trading day's close, `Last` supplies the actual last trade, and `Volume`
supplies daily volume. Change and percentage change are calculated from that same
snapshot. No historical CSV or daily updater is required. Missing optional fields
stay unavailable; they never reuse an earlier day's values. The tab marks quotes
stale after 30 seconds without a new observation, even if depth still updates.
Short interest is not supplied by this quote feed and remains unavailable.

The UI's `market_data.publisher_config` points to `md_producer_config.json` so
the tab follows the publisher's subscribed symbols rather than the trading forms'
symbol list. Restart the UI after changing that subscription configuration.

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

On startup and partition reassignment, this shared consumer reads only the last
100 records **per partition** of `price-books` and `stock-quotes`, then continues
with live updates. These feeds carry complete snapshots, so replaying their full
history is unnecessary. The limit is across symbols, not per symbol; a symbol
absent from the recent window appears when its next snapshot arrives.

Set `kafka.market_data_replay_records` to change the window, or
`KAFKA_MARKET_DATA_REPLAY_RECORDS` when the JSON setting is absent. The default is
100; zero skips history and waits for new data. Short or empty partitions start at
their earliest retained offset. If Kafka cannot return offsets within five
seconds per partition, the consumer logs a warning and waits for new data on
that partition. `market_data_auto_offset_reset` now defaults to `latest` as a
fallback for invalid offsets; it does not control this initial replay window.
Restart the dashboard to apply changes. Order-status and trading-volume recovery
still use their existing retained history.

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
request. This repository configures a one-second delay after each polling cycle;
request and publication time adds to that delay. It publishes volume observations
even when depth does not change.

TradeStation's [standard quote snapshot quota](https://api.tradestation.com/docs/fundamentals/rate-limiting/rate-limiting-overview/)
is 500 requests per rolling five minutes, per user. Polling every 300 ms would
require 1,000 requests in that window. The current seven symbols fit in one request,
so the configured one-second delay uses at most 300 requests per five minutes,
leaving room for other quote requests. More than 100 distinct symbols require
additional batches and a slower interval to stay within the same shared quota.
Restart the market-data publisher to apply interval changes; restarting only the
trading UI does not reload this setting.

TradeStation's current `GET /v3/marketdata/quotes/{symbols}` returns **`Volume`**
as daily shares/contracts. `LastSize` is one trade's size; `PreviousVolume` is the
previous day. Neither is used as cumulative volume. `TradeTime` is the last trade
time, so an unchanged old last trade alone does not make an illiquid market stale.
The daily volume counter may also increase while `TradeTime` remains unchanged.
Volume intervals use consecutive fresh quote observation times, not the last
trade timestamp. The producer still checks request duration, observation order,
realtime/halt flags, invalid or future timestamps, and missing trade timestamps
when volume increases. Counter corrections, gaps, and restarts retain their
existing consumer safeguards.
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
