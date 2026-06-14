import time
import threading
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

    def upsert(self, snap: AccountSnapshot) -> None:
        with self._lock:
            self._accounts[snap.account_id] = snap
            self._kafka_seen_any = True
            self._kafka_last_ts = time.time()

    def get_all(self) -> Dict[str, AccountSnapshot]:
        with self._lock:
            return dict(self._accounts)

    def kafka_seen_any(self) -> bool:
        with self._lock:
            return self._kafka_seen_any

    def kafka_last_seen(self) -> Optional[float]:
        with self._lock:
            return self._kafka_last_ts
