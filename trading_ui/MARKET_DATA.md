# Market Data: five-minute trading windows

Only the exact authenticated username `lmh` can open `/market-data` or read
`GET /api/market-data?symbol=XYZ`. Other signed-in users receive HTTP 403 and
do not see the tab. Anonymous page requests redirect to sign-in; anonymous API
requests receive HTTP 401. Responses are not cached. The authenticated
`/api/quotes` endpoint supplies the existing price lookups in trading forms.

The symbol selector follows `market_data.publisher_config` (or the configured
symbol list). The page refreshes every five seconds. It shows the last trade's
time, share quantity, and price above the table. Times are displayed in
`America/New_York`, including its daylight-saving offset.

The four rows are the current clock-aligned five-minute window and the previous
three windows, newest first. At 10:23 the rows are 10:20–10:25, 10:15–10:20,
10:10–10:15, and 10:05–10:10. Each row contains market volume, our bought/sold/gross
volume, and estimated market VWAP. Gross is bought plus sold. There is no TWAP.

## Measurement

`stock-quotes` contains sampled latest-trade prices and daily cumulative market
volume, not every execution. For consecutive healthy samples, market volume is
the increase in the cumulative counter. Estimated VWAP is
`sum(current sample price × positive volume increase) / sum(volume increases)`.
An unchanged counter adds neither volume nor VWAP weight; last-trade size is
displayed separately and is never used as window volume. Quote increments are
assigned to the observation ending the interval, with an observation exactly at
a window boundary closing the preceding window. Boundary attribution is therefore
approximate. No-volume windows have no VWAP.

Bad samples, daily resets, negative counter corrections, and observation gaps
longer than 30 seconds break the interval. Missing history and broken intervals
mark market figures with `*`. The page suppresses stale last-trade fields after
30 seconds without a healthy quote. An old trade time on a fresh quote remains
valid for illiquid symbols or a closed market.

Our volume covers all visible configured accounts, all trading modes and manual
orders. A dedicated, independent `order-status` consumer reads the shared
`order_ui.kafka` connection settings, so this does not depend on the Order UI or
either smart-trading mode being enabled. It stores cumulative fill snapshots by
account, broker order ID, and sequence. Duplicate reports do not add volume;
late replay can recover preceding snapshots. Unfilled orders and commands add
no volume. A status-only update never moves an existing fill into another window.

Positive fill differences are assigned by broker `updated_at` to half-open
windows `[start, end)`. The stream does not provide execution timestamps for
each fill: increases spanning window boundaries are marked `*`. Downward
cumulative corrections reduce the most recent attributed fills first and mark
affected windows uncertain. These corrections preserve the final cumulative
quantity but cannot establish exact historical execution timing. Fractional
shares are retained. Hidden and unconfigured accounts are excluded.

A catching-up, unavailable, or invalid order feed marks our figures incomplete.
Unavailable values display `—`; observed zero is displayed as zero. The current
window always represents the period so far.

## Persistence and startup

History uses `market_quote_samples` and `market_order_samples` in the existing
`reporting.db_path` SQLite database. The schema is additive. One day of quotes
and order observations is retained, plus an older cumulative baseline for each
order. Cleanup runs at most once per five minutes during quote ingestion.

On assignment the shared market consumer requests 25 minutes of retained
`stock-quotes` using Kafka timestamp offsets, to populate the four windows.
Price-book replay retains its existing record-count limit. If the timestamp
lookup fails, quote replay falls back to that limit and missing history remains
marked incomplete. A new independent order consumer replays the retained broker
snapshots; durable sequence keys prevent repeated volume after restart.

Restart the trading UI to load the feature. No market-data publisher changes or
Order UI restart are required. Restarting the trading UI follows its existing
algorithm shutdown behavior, so deployment should use the usual trading pause.

## Validation

Run `.venv/bin/python -m pytest tests order_ui/tests -q`. The market activity
tests cover window rollover, volume-weighted estimates, zero versus missing
data, duplicate/out-of-order reports, counter resets, fill corrections, stale
feeds, account filtering, persistence, replay and authentication. Isolated
Chrome tests cover English and Chinese rendering, symbol changes, stale
responses, request failures and permission loss without live broker access.
