from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List
import json


@dataclass
class PriceLevel:
    prc: float
    total_qty: int
    num_orders: int
    levels_from_best: int


@dataclass
class PriceBookSide:
    side: bool  # True for bid, and False for ask
    update_time: int  # Time since epoch in nanos
    levels: List[PriceLevel] = field(default_factory=list)


@dataclass
class PriceBook:
    symbol: str
    bid_side: PriceBookSide
    offer_side: PriceBookSide
    update_time: int  # Time since epoch in nanos
    sequence_num: int  # Sequence number for the price book

    def to_json(self) -> str:
        self_dict = asdict(self)
        for side_name in ("bid_side", "offer_side"):
            side = self_dict.get(side_name)
            if not isinstance(side, dict):
                continue
            levels = side.get("levels")
            if not isinstance(levels, list):
                continue
            for level in levels:
                if not isinstance(level, dict):
                    continue
                _add_level_ui_aliases(level)
        return json.dumps(self_dict)


def _add_level_ui_aliases(level: Dict[str, Any]) -> None:
    if "qty" not in level:
        level["qty"] = level.get("total_qty")
    if "order_count" not in level:
        level["order_count"] = level.get("num_orders")
