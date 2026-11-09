# Admin Control: broker order history requests

`/admin-control` replaces the Currency Conversion tab. Only the authenticated
username `lmh` can view the page or submit `POST /submit-broker-orders`.
Other authenticated users, including `admin`, receive HTTP 403; anonymous users
are redirected to sign in. The former currency conversion page and submission
routes are removed.

Choose a trading date and one or more configured accounts, or **All accounts**.
The UI accepts today or an earlier date in `America/New_York`. It validates the
entire selection before publishing and sends one message per submission.
All accounts expands to an explicit list of the accounts configured at submission
time, sorted by numeric UI ID. Duplicate selections are removed. No wildcard is
sent to consumers, and the UI does not use current balances or trading-enabled
status to exclude accounts.

## Kafka contract

- Topic: the existing configured `trading_commands_topic` (`trading-commands` by default).
- Key: UTF-8 `GET_BROKER_ORDERS`.
- Value: one UTF-8 JSON object.

```json
{
  "type": "GET_BROKER_ORDERS",
  "command_id": "broker_orders_7a0b0938dbd746b08902ea90ef437102",
  "ts": "2026-11-09T13:30:00Z",
  "trading_date": "2026-11-05",
  "timezone": "America/New_York",
  "account_ids": ["GIGA-WJY", "TIGER-WJY"]
}
```

| Field | Meaning |
| --- | --- |
| `type` | Exactly `GET_BROKER_ORDERS`. |
| `command_id` | Unique request identifier; `broker_orders_` followed by a UUID hex string. |
| `ts` | UTC ISO-8601 request creation time, independent of the requested trading date. |
| `trading_date` | Strict `YYYY-MM-DD` calendar date. |
| `timezone` | `America/New_York`; use the named timezone to handle daylight saving. |
| `account_ids` | Nonempty, deduplicated list of exact configured UI account IDs. |

This multi-account control message is an exception to the single-account
`account_id` / account-key routing described in the older command-schema DOCX
files. It has no `account_id`, `broker`, or `broker_id`: each consumer must match
its own account IDs against `account_ids`. Each broker consumer that needs this
message must receive it through the deployment's appropriate consumer-group
routing; a Kafka key does not broadcast a message to every member of one group.

## Broker implementation semantics

Retrieve every broker order **created** from local midnight on `trading_date`
(inclusive) to local midnight on the next calendar date (exclusive) in the
specified timezone. Include all symbols and statuses, including canceled,
rejected, expired, partially filled, and filled orders; exhaust the broker's
pagination. An order created earlier and merely updated on the requested day
does not fall in the requested creation-date window. For today, return the
orders available when processing the request.

Calculate each midnight independently in the named timezone. For example,
November 1, 2026 spans `2026-11-01T04:00:00Z` through
`2026-11-02T05:00:00Z` (25 hours). Do not add a fixed 24 hours to the UTC start.

This is a history query: it must not place/cancel orders, change trading-enabled
state, or stop algorithms. The UI can queue it while algorithms own an account.
Use `command_id` for correlation and duplicate-delivery handling. The UI's
success message means the request was queued by the existing Kafka producer;
it does not claim broker completion or render retrieved results.

The broker-side query, pagination, and response publication are intentionally
left for the separate broker-script implementation. This change adds no broker
consumer or result storage.

## Validation

Run `.venv/bin/python -m pytest -q tests/test_admin_control.py tests/test_layout_refresh.py`.
Tests use a fake producer and isolated Chrome profiles. They do not start live
Kafka consumers or publish production requests.
