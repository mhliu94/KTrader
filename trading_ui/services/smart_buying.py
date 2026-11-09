"""Simple cash-limited POV buying on the existing durable order/volume ledger."""

from decimal import Decimal

from .smart_selling import SmartSellingEngine
from .smart_selling_policy import (
    ZERO, ChildPlan, decimal_string, meets_order_minimum, parse_decimal,
    parse_input_decimal, round_down_to_step, round_up_to_step,
)

PARTICIPATION_PERCENTAGES = {"patient": "5", "normal": "8", "urgent": "15"}


def plan_buy(parent, quote, now):
    price = round_up_to_step(quote["ask"], parse_decimal(parent["tick_size"]))
    ceiling = parent["maximum_price"]
    if ceiling is not None and price > parse_decimal(ceiling):
        return ChildPlan(None, price, True, False, "best ask exceeds maximum price")
    remaining = parse_decimal(parent["quantity"]) - parse_decimal(parent["F"]) - parse_decimal(parent["R"])
    depth = max(ZERO, quote["ask_size"] - quote["consumed"])
    quantity = round_down_to_step(
        min(remaining, depth, max(ZERO, parse_decimal(parent["C"]))),
        parse_decimal(parent["quantity_step"]),
    )
    if not meets_order_minimum(parent, quantity, price):
        return ChildPlan(None, price, True, depth < 100, "waiting for volume credit and at least 100 shares of liquidity")
    return ChildPlan(quantity, price, True, depth < remaining)


class SmartBuyingEngine(SmartSellingEngine):
    side = "BUY"
    liquidity_side = "ask"
    participation_percentages = PARTICIPATION_PERCENTAGES
    child_policy = staticmethod(plan_buy)

    def __init__(self, *args, **kwargs):
        self.cash_limits = {}
        super().__init__(*args, **kwargs)

    def _configure_parent(self, parent, settings):
        ceiling = settings.get("maximum_price")
        parent["maximum_price"] = (
            decimal_string(parse_input_decimal(ceiling, "maximum_price", positive=True))
            if ceiling not in (None, "") else None
        )
        if parse_decimal(parent["min_quantity"]) < 100:
            raise ValueError("Smart buying requires a minimum of 100 shares")
        parent["participation"] = str(Decimal(PARTICIPATION_PERCENTAGES[parent["mode"]]) / 100)
        # No horizon-derived slices, partial flush timers, or progress escalation.
        parent["child_cap"] = parent["base_child_cap"] = parent["quantity"]
        for account_id, account in parent["accounts"].items():
            account["cash_budget"] = decimal_string(parse_input_decimal(
                settings["account_budgets"][account_id], "cash budget", nonnegative=True,
            ))
            account["spent"] = "0"

    def on_quote(self, symbol, bid, ask, bid_size, *, ask_size=None, **kwargs):
        if ask_size is None or parse_decimal(ask_size, nonnegative=True) <= 0:
            kwargs["healthy"] = False
        return super().on_quote(symbol, bid, ask, bid_size, ask_size=ask_size, **kwargs)

    def _depth_budget(self, quote):
        return max(ZERO, quote["ask_size"] - quote["consumed"])

    def _dispatch_price(self, parent, order, quote):
        # Keep the planned limit fixed, including if the ask moves while queued.
        return parse_decimal(order["limit_price"]), True

    def _account_capacity(self, parent, account_id, price, *, include_order=False):
        account = parent["accounts"][account_id]
        shares = super()._account_capacity(parent, account_id, price, include_order=include_order)
        cash = max(ZERO, min(
            parse_decimal(account["cash_budget"]) - parse_decimal(account["spent"]),
            self.cash_limits.get(account_id, ZERO),
        ))
        return min(shares, round_down_to_step(cash / price, parse_decimal(parent["quantity_step"])))

    def _book_cumulative_fill(self, parent, order, cumulative):
        delta = cumulative - parse_decimal(order["cumulative_filled"])
        account = parent["accounts"][order["account_id"]]
        # Limit price is a conservative upper bound on spend; do not depend on
        # delayed account snapshots or recycle apparent cash after a partial fill.
        account["spent"] = decimal_string(
            parse_decimal(account["spent"]) + delta * parse_decimal(order["limit_price"])
        )
        super()._book_cumulative_fill(parent, order, cumulative)

    def _save(self, parent):
        for account_id, account in parent["accounts"].items():
            spent = parse_decimal(account["spent"])
            reserved_cash = ZERO
            order = parent["order"]
            if order and order["account_id"] == account_id:
                reserved_cash = parse_decimal(account["R"]) * parse_decimal(order["limit_price"])
            if spent < 0 or spent + reserved_cash > parse_decimal(account["cash_budget"]):
                raise AssertionError("Smart buying cash budget invariant violated")
        super()._save(parent)

    def _finish_if_needed(self, parent, now):
        super()._finish_if_needed(parent, now)
        if parent["active"] and not parent["order"] and all(
            parse_decimal(a["quantity"]) - parse_decimal(a["F"]) < parse_decimal(parent["min_quantity"])
            for a in parent["accounts"].values()
        ):
            parent.update(active=False, state="COMPLETED", reason="remaining target below 100-share minimum")

    def _action_dict(self, parent, kind):
        return {**super()._action_dict(parent, kind), "trading_mode": "H"}

    def _public(self, parent):
        result = super()._public(parent)
        result.update(maximum_price=parent["maximum_price"], trading_mode="H")
        for account_id, account in parent["accounts"].items():
            result["accounts"][account_id].update(cash_budget=account["cash_budget"], spent=account["spent"])
        return result
