"""Mode H: fixed 5/8/15% POV buying, using available USD cash and limit orders."""

from datetime import datetime
from decimal import Decimal

from .orders import algo_account_conflict_error
from .smart_buying import SmartBuyingEngine, PARTICIPATION_PERCENTAGES
from .smart_selling_runtime import SmartSellingRuntime
from .smart_selling_policy import parse_input_decimal, round_down_to_step, round_up_to_step
from ..smart_selling_config import canonical_symbol, normalize_smart_selling_config


def normalize_smart_buying_config(raw=None):
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("smart_buying must be an object")
    config = normalize_smart_selling_config({
        "state_db": "./logs/smart_buying.sqlite3", **(raw or {}),
        "participation_percentages": PARTICIPATION_PERCENTAGES,
    })
    rules = config["instrument_defaults"]
    rules["min_quantity"] = str(max(Decimal(100), Decimal(str(rules["min_quantity"]))))
    rules["quantity_step"] = str(max(Decimal(1), Decimal(str(rules["quantity_step"]))))
    return config


class SmartBuyingRuntime(SmartSellingRuntime):
    engine_type = SmartBuyingEngine
    normalize_config = staticmethod(normalize_smart_buying_config)
    trading_mode = "H"

    def __init__(self, *args, market_data_store, **kwargs):
        self.market_data_store = market_data_store
        super().__init__(*args, **kwargs)

    def _available_position(self, account_id, symbol):
        """The shared heartbeat calls this resource check before each decision."""
        snapshot = self.snapshots_provider().get(account_id)
        if snapshot is None or not snapshot.ts:
            raise ValueError("A recent USD cash snapshot is required")
        timestamp = datetime.fromisoformat(str(snapshot.ts).replace("Z", "+00:00"))
        if timestamp.tzinfo is None or not -1 <= self.wall_clock() - timestamp.timestamp() <= self.config["account_max_age"]:
            raise ValueError("Account cash snapshot is stale")
        if not snapshot.trading_enabled:
            raise ValueError("Trading is disabled for this account")
        available = snapshot.available_cash_by_currency or {}
        balances = snapshot.cash_by_currency or {}
        raw_cash = available.get("USD", balances.get("USD", snapshot.cash))
        cash = max(Decimal(0), parse_input_decimal(raw_cash, "USD cash"))
        self.engine.cash_limits[account_id] = cash
        return cash

    def start_parent(self, settings):
        if not isinstance(settings, dict):
            raise ValueError("Smart buying settings must be an object")
        accounts = settings.get("account_ids", [settings.get("account_id", "")])
        if not isinstance(accounts, list) or not accounts or any(not isinstance(a, str) or not a.strip() for a in accounts):
            raise ValueError("Select at least one account")
        accounts = [a.strip() for a in accounts]
        if len(set(accounts)) != len(accounts) or any(a not in self.account_metas_provider() for a in accounts):
            raise ValueError("Invalid or duplicate account selection")
        symbol = canonical_symbol(settings.get("symbol"))
        if not symbol:
            raise ValueError("A symbol is required")
        with self._lock:
            owned = [a for a in accounts if self.owns_account(a) or self.fast_owns_account(a)]
            if owned:
                raise ValueError(algo_account_conflict_error(owned))
            if any(state != "live" for state in self.feeds.values()):
                raise ValueError("Wait for live order-status and trading-volume feeds")
            book = self.market_data_store.get_book(symbol, 1)
            if not book.asof_epoch or not -1 <= self.wall_clock() - book.asof_epoch <= self.config["quote_max_age"] or not book.asks:
                raise ValueError("A fresh best ask is required to size the buying target")
            rules = {**self.config["instrument_defaults"], **self.config["instruments"].get(symbol, {})}
            step = max(Decimal(1), Decimal(str(rules["quantity_step"])))
            minimum = round_up_to_step(max(Decimal(100), Decimal(str(rules["min_quantity"]))), step)
            rules.update(min_quantity=str(minimum), quantity_step=str(step))
            reference_price = round_up_to_step(
                parse_input_decimal(book.asks[0].price, "best ask", positive=True),
                Decimal(str(rules["tick_size"])),
            )
            budgets, quantities = {}, {}
            for account in accounts:
                if self.feedback.has_open_orders(account, symbol):
                    raise ValueError("Existing orders must reach terminal status first")
                budgets[account] = self._available_position(account, symbol)
                affordable = round_down_to_step(budgets[account] / reference_price, step)
                quantities[account] = affordable if affordable >= minimum else Decimal(0)
            quantity = sum(quantities.values(), Decimal(0))
            if quantity < minimum:
                raise ValueError(f"At least one selected account must afford {minimum} shares")
            mode = str(settings.get("mode", "normal")).lower()
            mode = {"1": "patient", "2": "normal", "3": "urgent"}.get(mode, mode)
            if mode not in PARTICIPATION_PERCENTAGES:
                raise ValueError("mode must be patient, normal, or urgent")
            permitted = {k: settings[k] for k in ("start_time", "deadline", "maximum_price") if k in settings}
            permitted.update(
                mode=mode, participation_percentage=PARTICIPATION_PERCENTAGES[mode],
                account_id=accounts[0], account_ids=accounts,
                account_quantities={a: str(q) for a, q in quantities.items()},
                account_budgets={a: str(c) for a, c in budgets.items()},
                quantity=str(quantity), symbol=symbol, instrument_rules=rules,
                quote_max_age=self.config["quote_max_age"], trade_max_age=self.config["trade_max_age"],
            )
            parent = self.engine.start_parent(permitted)
            self._sync_reporting([parent], parameters=permitted)
            return parent

    def _prepare_submit(self, action):
        client_id = action["client_order_id"]
        if any(state != "live" for state in self.feeds.values()):
            self.engine.abort_dispatch(client_id, "waiting for live order and volume feeds")
            return None
        self._available_position(action["account_id"], action["symbol"])
        if self.feedback.has_open_orders(action["account_id"], action["symbol"]):
            self.engine.abort_dispatch(client_id, "conflicting open order", stop_parent=True)
            return None
        return self.engine.prepare_dispatch(client_id)
