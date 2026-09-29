"""Thread-safe projection of today's latest order snapshots."""

from datetime import datetime, timezone
from threading import RLock
from zoneinfo import ZoneInfo

from .events import OrderEvent


class OrderStore:
    def __init__(self, accounts, timezone_name: str, now=None):
        self.accounts = dict(accounts)
        self.zone = ZoneInfo(timezone_name)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._orders = {}
        self._day = self._now().astimezone(self.zone).date()
        self._feed = {"state": "starting", "last_message_at": None, "invalid_messages": 0,
                      "ignored_messages": 0, "detail": "Connecting to Kafka"}

    def _rollover(self):
        today = self._now().astimezone(self.zone).date()
        if today != self._day:
            self._orders.clear()
            self._day = today
        return today

    def update_feed(self, **fields):
        with self._lock:
            self._feed.update(fields)

    def received(self):
        self.update_feed(last_message_at=self._now().isoformat())

    def reject_message(self):
        with self._lock:
            self._feed["invalid_messages"] += 1

    def upsert(self, event: OrderEvent) -> bool:
        data = event.payload
        with self._lock:
            today = self._rollover()
            meta = self.accounts.get(data["account_id"])
            if meta is None or event.created_at.astimezone(self.zone).date() != today:
                self._feed["ignored_messages"] += 1
                return False
            if data.get("account_num_id", meta.num_id) != meta.num_id:
                raise ValueError("account_num_id does not match the configured account_id.")
            key = (meta.id, data["order_id"])
            previous = self._orders.get(key)
            if previous:
                if data["sequence"] <= previous.payload["sequence"]:
                    return False
                if event.created_at != previous.created_at:
                    raise ValueError("created_at is immutable for an order.")
                if event.updated_at < previous.updated_at:
                    raise ValueError("updated_at cannot decrease when sequence increases.")
            self._orders[key] = event
            return True

    def snapshot(self):
        with self._lock:
            today = self._rollover()
            rows = []
            for event in sorted(self._orders.values(), key=lambda e: (e.updated_at, e.payload["account_id"], e.payload["order_id"]), reverse=True):
                meta = self.accounts[event.payload["account_id"]]
                rows.append({**event.payload, "account_num_id": meta.num_id, "broker": meta.broker,
                             "trading_medium": meta.trading_medium})
            return {"date": today.isoformat(), "timezone": self.zone.key,
                    "generated_at": self._now().isoformat(), "orders": rows, "feed": dict(self._feed)}
