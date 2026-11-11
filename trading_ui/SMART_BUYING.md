# Smart buying (mode H)

Mode H buys at a fixed percentage of newly observed trading volume. The urgency
only changes participation: Patient **5%**, Normal **8%**, Urgent **15%**. These
rates are fixed in this first version and cannot be overridden by an API request.

E and H are labeled **buying**; F and G are labeled **selling**. At start time,
each selected account may have at most one buying session and any number of
selling sessions, all on distinct symbols. Scheduled, paused, stopping, and
reconciling sessions retain their reservations until they end. A conflict error
identifies the account, existing session, mode, symbol, and violated rule.

Select accounts, a symbol, urgency, start time, an optional deadline, and an
optional maximum buy price in the control panel. Each account's target is its
available USD cash divided by the current best ask (rounded upward to the price
tick), rounded down to whole shares or the configured quantity step. The targets
are fixed at creation; unused cash is not automatically reinvested as prices fall.
Available USD cash takes precedence over total USD cash and the legacy cash field.
Accounts unable to afford the order minimum are skipped.

Each child requires at least **100 shares**, or a larger configured broker minimum.
The controller waits for sufficient earned participation credit; it never rounds
an unfunded slice up to 100. Subminimum target remainders are left unfilled. The
same parent rotates across accounts, with at most one unresolved child at a time.

The simple execution policy uses a best-ask limit within the optional maximum
price. It caps size by earned volume credit, displayed ask liquidity, remaining
target, each account's initial cash budget less confirmed spending, and its latest
available cash. It rechecks those bounds immediately before publication and keeps
the planned limit fixed. Confirmed fills consume cash at the order's limit price,
a conservative upper bound, so delayed account snapshots cannot reuse spent cash.
This is a gross purchase budget; broker fees are not estimated by the controller.

G and H also check the user's price bound against actual displayed depth before
planning and immediately before dispatch. Buying pauses when at least five
distinct bid prices lie below the best offer and at or above the maximum buy
price. The fifth level is blocked; gaps do not count as extra levels and duplicate
prices count once. If a nonmarketable maximum lies beyond the available depth,
the controller waits for more depth. A maximum at or above the best offer needs
no deeper levels. A newly blocked unsent order refunds its reservation; earned
credit remains available within the normal cap. See [Smart selling](SMART_SELLING.md)
for the corresponding sell-side rule and examples.

Mode H reuses mode G's durable credit ledger, order correlation, replay protection,
freshness and halt checks, cancellation and reconciliation. Trading-volume changes
earn credit; order-book updates do not. Replayed, stale, duplicate, or uncertain
volume does not fund new orders. Starts require reconciled order and volume feeds,
a fresh ask, enabled accounts, and fresh account snapshots. Submissions are at least
five seconds apart; working children request cancellation after two seconds.
Timeouts never free an order reservation without terminal broker evidence.

The implementation does not use mode G's horizon-based slice sizing,
passive pricing, progress escalation, or partial-credit flush timer. A
deadline stops execution rather than increasing participation. Stop controls cancel
outstanding children and retain account ownership until reconciliation finishes.
Accounts cannot overlap with E/F/G sessions or manual trading instructions.

The control panel shows sessions starting today (browser-local date) plus active
sessions from any date, including scheduled, paused, and stopping sessions. Older
inactive sessions are hidden from the panel; their saved history is retained.

The backend provides `GET/POST /api/smart-buying` and
`POST /api/smart-buying/{parent_id}/stop`. It publishes existing `LIMIT_ORDER`
commands with `side: BUY` and `trading_mode: H`; no broker protocol changes are
required. State defaults to `./logs/smart_buying.sqlite3`, separate from mode G.
The optional `smart_buying` configuration supports the same feed endpoints,
freshness limits and instrument rules as `smart_selling`; participation remains
5/8/15% and the minimum remains at least 100 shares.

The control server must be restarted to load this implementation. No running
strategy is automatically started by enabling the feature.
