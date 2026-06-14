import threading
from typing import Dict

from .models import StrategySnapshot, StrategySummary


class StrategyStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._strategies: Dict[str, StrategySnapshot] = {}
        self._last_update = ""

    def upsert(self, snap: StrategySnapshot) -> None:
        with self._lock:
            self._strategies[snap.strategy_key] = snap
            if snap.updated_at:
                self._last_update = snap.updated_at

    def get_all(self) -> Dict[str, StrategySnapshot]:
        with self._lock:
            return dict(self._strategies)

    def summary(self) -> StrategySummary:
        with self._lock:
            items = list(self._strategies.values())
            total = len(items)
            running = sum(1 for item in items if item.status == "RUNNING")
            stopped = sum(1 for item in items if item.status in {"STOPPED", "COMPLETED"})
            errored = sum(1 for item in items if item.status in {"ERROR", "FAILED"})
            return StrategySummary(
                total=total,
                running=running,
                stopped=stopped,
                errored=errored,
                last_update=self._last_update,
            )
