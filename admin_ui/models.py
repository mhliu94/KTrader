from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StrategySnapshot:
    strategy_key: str
    status: str
    trading_mode: str = ""
    symbol: str = ""
    account_ids: List[str] = field(default_factory=list)
    command_id: str = ""
    started_at: str = ""
    updated_at: str = ""
    detail: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategySummary:
    total: int
    running: int
    stopped: int
    errored: int
    last_update: str = ""
