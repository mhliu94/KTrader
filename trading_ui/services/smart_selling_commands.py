"""Translate controller intents into KTrader's existing command vocabulary.

This module does no I/O and makes no execution decisions. The runtime publishes
only intents that the engine has durably reserved and validated for dispatch.
"""

from decimal import Decimal

from .orders import build_cancel_open_orders_command, build_limit_order_command


def _wire_number(value, name, *, allow_integer=False):
    """Reject precision loss at the existing numeric command contract boundary."""
    exact = Decimal(value)
    if allow_integer and exact == int(exact):
        return int(exact)
    number = float(exact)
    if Decimal(str(number)) != exact:
        raise ValueError(f"{name} cannot be represented exactly by the order command")
    return number


def build_smart_selling_command(action, account_metas):
    """Preserve broker routing while correlating every child with order-status."""
    account_id = action["account_id"]
    symbol = action["symbol"]
    if action["type"] == "submit":
        command = build_limit_order_command(
            account_id=account_id,
            symbol=symbol,
            side="SELL",
            shares=_wire_number(action["quantity"], "Quantity", allow_integer=True),
            limit_price=_wire_number(action["limit_price"], "Limit price"),
            account_metas=account_metas,
        )
        command.update(
            command_id=action["client_order_id"],
            client_order_id=action["client_order_id"],
            time_in_force=action["time_in_force"],
            ts=action["send_time"],
            deadline=action["deadline"],
        )
    elif action["type"] == "cancel":
        # The existing command cancels every open order for this account/symbol.
        # Ownership and conflict checks are therefore enforced by the runtime.
        command = build_cancel_open_orders_command(account_id, account_metas, symbol)
    else:
        raise ValueError("Only submit/cancel intents translate to trading commands")
    command.update(parent_id=action["parent_id"], trading_mode="G")
    return command
