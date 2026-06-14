from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any


@dataclass
class AccountMeta:
    id: str
    num_id: int
    broker_id: str
    broker: str


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: Optional[float] = None


@dataclass
class AccountSnapshot:
    account_id: str
    cash: float
    account_num_id: Optional[int] = None
    cash_by_currency: Dict[str, float] = field(default_factory=dict)
    positions: List[Position] = field(default_factory=list)
    ts: Optional[str] = None
    trading_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_num_id": self.account_num_id,
            "cash": self.cash,
            "cash_by_currency": dict(self.cash_by_currency),
            "ts": self.ts,
            "trading_enabled": self.trading_enabled,
            "positions": [asdict(p) for p in self.positions],
        }
