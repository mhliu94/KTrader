"""Map the existing order-status v1 stream into Smart selling's durable ledger.

This is a control-server adapter, not a broker adapter. Publishers own the
authoritative status, cumulative fill and persisted per-order sequence under
``order_ui/schema/order-status.v1.schema.json``. Replaying retained snapshots
cannot force a fresh broker query or prove that an unknown order never existed.
"""

from collections import OrderedDict
from decimal import Decimal
import json
import threading

from order_ui.events import OrderEvent, parse_order_message


from .smart_selling_feedback_store import (
    OrderFeedbackStore,
    TERMINAL_STATUSES,
    canonical_order_symbol,
)


def parse_smart_selling_snapshot(value):
    """Validate the existing schema rather than inventing another wire format."""
    if isinstance(value, OrderEvent):
        value = value.payload
    if isinstance(value, dict):
        try:
            value = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Order snapshot must contain finite JSON values") from exc
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes):
        raise ValueError("Order snapshot must be UTF-8 JSON or a JSON object")
    return parse_order_message(value)


class SmartSellingFeedback:
    """Correlate exact owned children and retain bounded reconciliation evidence.

    Submit commands must use the engine's ``client_order_id`` as the existing
    command's ``command_id`` (or carry it through ``client_order_id``). Once an
    authoritative snapshot identifies the broker ``order_id``, later snapshots
    may omit those optional client identifiers. Symbol/size matching alone never
    establishes ownership. Archived children remain eligible for conflict checks
    in the engine, so a late report cannot release a newer child's reservation.
    """

    def __init__(self, engine, *, max_cached_orders=10000, state_path=":memory:"):
        if isinstance(max_cached_orders, bool) or int(max_cached_orders) < 1:
            raise ValueError("max_cached_orders must be positive")
        self.engine = engine
        self.max_cached_orders = int(max_cached_orders)
        self._latest = OrderedDict()
        self._lock = threading.RLock()
        self._store = OrderFeedbackStore(state_path)

    def track_external_order(self, command):
        """Persist a manual/other-algorithm handoff before it leaves the server.

        Unknown outcomes survive restarts. Only an exact account, instrument and
        command correlation with terminal evidence closes the pending handoff.
        """
        with self._lock:
            self._store.track_external_order(command)

    def has_open_orders(self, account_id, symbol):
        """Include retained working/unknown orders and unresolved local handoffs."""
        with self._lock:
            return self._store.has_open_orders(account_id, symbol)

    def close(self):
        with self._lock:
            self._store.close()

    def _find_owned_context(self, payload):
        matches = {}
        for field in ("client_order_id", "command_id"):
            identity = payload.get(field)
            if identity:
                context = self.engine.get_order_context(identity)
                if context is not None:
                    matches[context["client_order_id"]] = context
        broker_context = self.engine.find_order_by_broker_id(
            payload["account_id"], payload["order_id"]
        )
        if broker_context is not None:
            matches[broker_context["client_order_id"]] = broker_context
        if len(matches) > 1:
            for context in matches.values():
                self._hold_identified_conflict(
                    context, payload, "conflicting order identifiers require reconciliation"
                )
            raise ValueError("Order snapshot identifiers refer to different Smart selling children")
        return next(iter(matches.values()), None)

    @staticmethod
    def _validate_owned_snapshot(payload, context):
        if payload["account_id"] != context["account_id"]:
            raise ValueError("Order snapshot account does not match the owned child")
        if canonical_order_symbol(payload["symbol"]) != canonical_order_symbol(context["symbol"]):
            raise ValueError("Order snapshot symbol does not match the owned child")
        if payload["side"] != "SELL" or payload["order_type"] != "LIMIT":
            raise ValueError("Smart selling feedback requires the owned ordinary sell limit")
        quantity = payload["quantity"]
        expected_quantity = Decimal(str(context["quantity"]))
        if quantity is None or Decimal(str(quantity)) != expected_quantity:
            raise ValueError("Order snapshot quantity does not match the original child")
        if Decimal(str(payload["limit_price"])) != Decimal(str(context["limit_price"])):
            raise ValueError("Order snapshot limit price does not match the fixed child price")
        if context["broker_order_id"] not in (None, payload["order_id"]):
            raise ValueError("Order snapshot broker ID does not match the owned child")

    def _hold_identified_conflict(self, context, payload, reason):
        # A reused client identifier in another account is insufficient evidence
        # to quarantine this account's strategy.
        if context is not None and context["account_id"] == payload["account_id"]:
            self.engine.hold_order(context["client_order_id"], reason)

    @staticmethod
    def _older_report_involves_unknown(payload, previous):
        return bool(
            previous
            and payload["sequence"] < previous["sequence"]
            and "UNKNOWN" in (payload["status"], previous["status"])
        )

    def _should_ignore_retained_report(self, event, account_previous, cached):
        payload = event.payload
        if self._older_report_involves_unknown(payload, account_previous):
            return True
        if cached is None:
            return False
        if event.created_at != cached.created_at:
            raise ValueError("Order snapshot changed the original creation time")
        if (
            payload["sequence"] > cached.payload["sequence"]
            and event.updated_at < cached.updated_at
        ):
            raise ValueError("Order snapshot source time decreased as sequence increased")
        # UNKNOWN does not advance the core's authoritative fill sequence. An
        # older terminal must nevertheless not replace that newer uncertainty.
        return self._older_report_involves_unknown(payload, cached.payload)

    def _apply_cumulative_report(self, client_id, payload):
        status = payload["status"]
        # UNKNOWN says that the publisher cannot establish current broker state.
        # Its last-known fill must not be promoted to new authority.
        authoritative = status != "UNKNOWN"
        self.engine.on_order_report(
            client_id,
            status,
            str(payload["filled_quantity"]),
            broker_order_id=payload["order_id"],
            sequence=payload["sequence"],
            authoritative=authoritative,
            final=authoritative and status in TERMINAL_STATUSES,
        )

    def _remember_snapshot(self, client_id, event):
        previous = self._latest.get(client_id)
        if previous is None or event.payload["sequence"] > previous.payload["sequence"]:
            self._latest[client_id] = event
        self._latest.move_to_end(client_id)
        while len(self._latest) > self.max_cached_orders:
            self._latest.popitem(last=False)

    def apply_snapshot(self, value):
        """Apply matched evidence; return False for unrelated orders.

        Validation failures never apply fills or refunds. Contradictions for a
        positively identified child in the same account quarantine its ledger.
        Duplicate and reordered reports still reach the engine's durable
        sequencing checks, including contradictory late cumulative fills.
        """
        event = parse_smart_selling_snapshot(value)
        payload = event.payload
        with self._lock:
            context = self._find_owned_context(payload)
            try:
                if context is not None:
                    self._validate_owned_snapshot(payload, context)
                account_previous = self._store.record_snapshot(event)
            except ValueError:
                self._hold_identified_conflict(
                    context, payload, "conflicting order metadata requires reconciliation"
                )
                raise
            if context is None:
                return False
            client_id = context["client_order_id"]
            cached = self._latest.get(client_id)
            if self._should_ignore_retained_report(event, account_previous, cached):
                return True
            self._apply_cumulative_report(client_id, payload)
            self._remember_snapshot(client_id, event)
            return True

    def reconcile(self, client_order_id):
        """Replay retained evidence only; missing evidence leaves state unknown.

        This method emits no trading command and does not refresh timestamps or
        sequence numbers. A stale working snapshot cannot prove terminality.
        """
        with self._lock:
            event = self._latest.get(str(client_order_id))
            return self.apply_snapshot(event) if event is not None else False

    def latest_snapshot(self, client_order_id):
        with self._lock:
            event = self._latest.get(str(client_order_id))
            return json.loads(json.dumps(event.payload)) if event is not None else None
