"""Account and symbol admission rules shared by all algorithm runtimes."""

from dataclasses import dataclass
import threading
from typing import Callable, Iterable

from ..smart_selling_config import canonical_symbol


# A mode that can spend cash belongs to buying, even if it can also sell.
ALGO_MODE_CATEGORIES = {"E": "buying", "F": "selling", "G": "selling", "H": "buying"}


@dataclass(frozen=True)
class AlgoSession:
    session_id: str
    trading_mode: str
    symbol: str
    account_ids: Iterable[str]


class AlgoSessionCoordinator:
    def __init__(self):
        # Hold through validation AND registration in the runtime. This also
        # protects direct runtime starts, outside the HTTP control lock.
        self.lock = threading.RLock()
        self._providers: list[Callable[[], Iterable[AlgoSession]]] = []

    def register(self, provider: Callable[[], Iterable[AlgoSession]]) -> None:
        with self.lock:
            self._providers.append(provider)

    def require_available(self, trading_mode, symbol, account_ids) -> None:
        """Caller holds lock until its new session is visible to its provider.

        Providers include scheduled, paused, stopping and reconciling sessions.
        Reading runtime state also includes durable sessions restored on startup.
        """
        category = ALGO_MODE_CATEGORIES[trading_mode]
        symbol = canonical_symbol(symbol)
        selected = set(account_ids)
        conflicts = []
        for provider in self._providers:
            for session in provider():
                overlap = sorted(selected.intersection(session.account_ids))
                if not overlap:
                    continue
                existing_category = ALGO_MODE_CATEGORIES[session.trading_mode]
                existing_symbol = canonical_symbol(session.symbol)
                reasons = []
                if symbol == existing_symbol:
                    reasons.append(
                        f"symbol {symbol} is already in use; all strategies on an account "
                        "must use distinct symbols"
                    )
                if category == existing_category == "buying":
                    reasons.append("each account may run at most 1 buying strategy")
                if reasons:
                    conflicts.append(
                        f"Account(s) {', '.join(overlap)}: {'; '.join(reasons)}. "
                        f"Conflicting {existing_category} session {session.session_id} "
                        f"(mode {session.trading_mode}, symbol {existing_symbol})"
                    )
        if conflicts:
            raise ValueError(
                "Cannot start algorithm: " + ". ".join(conflicts)
                + ". Stop the conflicting session and await outstanding order confirmations first."
            )
