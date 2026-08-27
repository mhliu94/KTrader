import asyncio
import threading
import time
from typing import Dict, Optional

from .models import AccountSnapshot


class AccountStore:
    """
    Thread-safe store of latest snapshot per account.
    Tracks whether we've ever seen Kafka data.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: Dict[str, AccountSnapshot] = {}
        self._kafka_seen_any = False
        self._kafka_last_ts: Optional[float] = None
        self._update_version = 0
        self._update_subscribers: Dict[
            object,
            tuple[asyncio.AbstractEventLoop, asyncio.Queue[int]],
        ] = {}

    def upsert(self, snap: AccountSnapshot) -> None:
        with self._lock:
            self._accounts[snap.account_id] = snap
            self._kafka_seen_any = True
            self._kafka_last_ts = time.time()
            self._update_version += 1
            version = self._update_version
            subscribers = list(self._update_subscribers.items())

        stale_subscribers = []
        for token, (loop, updates) in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer_update, updates, version)
            except RuntimeError:
                stale_subscribers.append(token)
        if stale_subscribers:
            with self._lock:
                for token in stale_subscribers:
                    self._update_subscribers.pop(token, None)

    def get_all(self) -> Dict[str, AccountSnapshot]:
        with self._lock:
            return dict(self._accounts)

    def kafka_seen_any(self) -> bool:
        with self._lock:
            return self._kafka_seen_any

    def kafka_last_seen(self) -> Optional[float]:
        with self._lock:
            return self._kafka_last_ts

    @staticmethod
    def _offer_update(updates: asyncio.Queue[int], version: int) -> None:
        if updates.full():
            try:
                updates.get_nowait()
            except asyncio.QueueEmpty:
                pass
        updates.put_nowait(version)

    def subscribe_updates(self) -> tuple[object, int, asyncio.Queue[int]]:
        loop = asyncio.get_running_loop()
        updates: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        token = object()
        with self._lock:
            version = self._update_version
            self._update_subscribers[token] = (loop, updates)
        return token, version, updates

    def unsubscribe_updates(self, token: object) -> None:
        with self._lock:
            self._update_subscribers.pop(token, None)
