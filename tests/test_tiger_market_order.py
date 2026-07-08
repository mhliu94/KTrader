import threading
import unittest
from datetime import datetime, timezone

from trading_server.tiger import TigerBroker, TradingCommandConsumer


class FakeTradeClient:
    def __init__(self, orders=None):
        self.placed_orders = []
        self.orders = list(orders or [])
        self.get_orders_calls = []
        self.cancel_order_calls = []

    def place_order(self, order):
        self.placed_orders.append(order)
        return "tiger-order-1"

    def get_orders(self, **kwargs):
        self.get_orders_calls.append(kwargs)
        return list(self.orders)

    def cancel_order(self, **kwargs):
        self.cancel_order_calls.append(kwargs)
        return kwargs.get("id") or kwargs.get("order_id")


class TigerMarketOrderFactory:
    def __init__(self):
        self.share_orders = []
        self.amount_orders = []

    def stock_contract(self, symbol, currency):
        return {"symbol": symbol, "currency": currency}

    def market_order(self, account, contract, action, quantity):
        order = {
            "kind": "shares",
            "account": account,
            "contract": contract,
            "action": action,
            "quantity": quantity,
        }
        self.share_orders.append(order)
        return order

    def market_order_by_amount(self, account, contract, action, amount):
        order = {
            "kind": "amount",
            "account": account,
            "contract": contract,
            "action": action,
            "amount": amount,
        }
        self.amount_orders.append(order)
        return order


def make_broker(factory, trade_client):
    broker = TigerBroker.__new__(TigerBroker)
    broker.dry_run = False
    broker.currency = "USD"
    broker.default_account = "TIGER-123"
    broker.account_map = {"UI-A": "TIGER-123"}
    broker.ui_account_ids = ["UI-A"]
    broker._trading_state_lock = threading.RLock()
    broker._trading_enabled_default = True
    broker._trading_enabled_by_account = {"UI-A": True}
    broker._trade_client = trade_client
    broker._stock_contract = factory.stock_contract
    broker._market_order = factory.market_order
    broker._market_order_by_amount = factory.market_order_by_amount
    broker._open_order_statuses = ["NEW", "PARTIALLY_FILLED", "PENDING_NEW", "HELD"]
    broker.cancel_order_fetch_limit = 100
    return broker


class FakeCommandBroker:
    def __init__(self):
        self.ui_account_ids = ["UI-A"]
        self.cancel_commands = []
        self.market_commands = []

    def is_trading_enabled_for_command(self, command):
        return False

    def cancel_open_orders(self, command):
        self.cancel_commands.append(command)

    def place_market_order(self, command):
        self.market_commands.append(command)


def make_command_consumer(broker):
    consumer = TradingCommandConsumer.__new__(TradingCommandConsumer)
    consumer._broker = broker
    consumer._scheduler = None
    consumer._reporter = None
    consumer._max_command_age_seconds = 300
    consumer._tiger_account_ids = {"UI-A"}
    consumer._allow_broker_fallback = False
    return consumer


def fresh_ts():
    return datetime.now(timezone.utc).isoformat()


class TigerMarketOrderCompatibilityTests(unittest.TestCase):
    def test_qty_shares_message_uses_quantity_market_order(self):
        factory = TigerMarketOrderFactory()
        trade_client = FakeTradeClient()
        broker = make_broker(factory, trade_client)

        broker.place_market_order(
            {
                "type": "MARKET_ORDER",
                "command_id": "cmd-quick",
                "account_id": "UI-A",
                "symbol": "aapl",
                "side": "buy",
                "qty_shares": 81,
            }
        )

        self.assertEqual(len(factory.share_orders), 1)
        self.assertEqual(factory.share_orders[0]["account"], "TIGER-123")
        self.assertEqual(factory.share_orders[0]["contract"], {"symbol": "AAPL", "currency": "USD"})
        self.assertEqual(factory.share_orders[0]["action"], "BUY")
        self.assertEqual(factory.share_orders[0]["quantity"], 81)
        self.assertEqual(factory.amount_orders, [])
        self.assertEqual(trade_client.placed_orders, factory.share_orders)

    def test_notional_message_still_uses_amount_market_order(self):
        factory = TigerMarketOrderFactory()
        trade_client = FakeTradeClient()
        broker = make_broker(factory, trade_client)

        broker.place_market_order(
            {
                "type": "MARKET_ORDER",
                "command_id": "cmd-notional",
                "account_id": "UI-A",
                "symbol": "AAPL",
                "side": "BUY",
                "notional_usd": 10000,
            }
        )

        self.assertEqual(factory.share_orders, [])
        self.assertEqual(len(factory.amount_orders), 1)
        self.assertEqual(factory.amount_orders[0]["amount"], 10000)
        self.assertEqual(trade_client.placed_orders, factory.amount_orders)


class TigerCancelOpenOrdersTests(unittest.TestCase):
    def test_symbol_scoped_cancel_fetches_target_symbol_and_filters_before_cancel(self):
        factory = TigerMarketOrderFactory()
        trade_client = FakeTradeClient(
            orders=[
                {"id": 101, "symbol": "AAPL"},
                {"id": 202, "symbol": "MSFT"},
                {"id": 303, "symbol": "US.AAPL"},
            ]
        )
        broker = make_broker(factory, trade_client)

        broker.cancel_open_orders(
            {
                "type": "CANCEL_OPEN_ORDERS",
                "command_id": "cancel-aapl",
                "account_id": "UI-A",
                "symbol": "aapl",
            }
        )

        self.assertEqual(len(trade_client.get_orders_calls), 1)
        self.assertEqual(trade_client.get_orders_calls[0]["account"], "TIGER-123")
        self.assertEqual(trade_client.get_orders_calls[0]["symbol"], "AAPL")
        self.assertFalse(trade_client.get_orders_calls[0]["is_brief"])
        self.assertEqual(
            trade_client.cancel_order_calls,
            [
                {"account": "TIGER-123", "id": 101},
                {"account": "TIGER-123", "id": 303},
            ],
        )

    def test_blank_symbol_cancel_fetches_all_open_orders_and_uses_available_cancel_ids(self):
        factory = TigerMarketOrderFactory()
        trade_client = FakeTradeClient(
            orders=[
                {"id": 101, "symbol": "AAPL"},
                {"order_id": 202, "symbol": "MSFT"},
                {"id": 0, "order_id": 0, "symbol": "TSLA"},
            ]
        )
        broker = make_broker(factory, trade_client)

        broker.cancel_open_orders(
            {
                "type": "CANCEL_OPEN_ORDERS",
                "command_id": "cancel-all",
                "account_id": "UI-A",
            }
        )

        self.assertEqual(len(trade_client.get_orders_calls), 1)
        self.assertEqual(trade_client.get_orders_calls[0]["account"], "TIGER-123")
        self.assertIsNone(trade_client.get_orders_calls[0]["symbol"])
        self.assertTrue(trade_client.get_orders_calls[0]["is_brief"])
        self.assertEqual(
            trade_client.cancel_order_calls,
            [
                {"account": "TIGER-123", "id": 101},
                {"account": "TIGER-123", "order_id": 202},
            ],
        )


class TigerCommandHandlerCancelTests(unittest.TestCase):
    def test_cancel_open_orders_dispatches_even_when_trading_is_disabled(self):
        broker = FakeCommandBroker()
        consumer = make_command_consumer(broker)
        command = {
            "type": "CANCEL_OPEN_ORDERS",
            "command_id": "cancel-disabled",
            "ts": fresh_ts(),
            "account_id": "UI-A",
            "broker": "Tiger",
            "symbol": "AAPL",
        }

        consumer._handle_command(command)

        self.assertEqual(broker.cancel_commands, [command])
        self.assertEqual(broker.market_commands, [])

    def test_non_cancel_order_is_blocked_when_trading_is_disabled(self):
        broker = FakeCommandBroker()
        consumer = make_command_consumer(broker)
        command = {
            "type": "MARKET_ORDER",
            "command_id": "market-disabled",
            "ts": fresh_ts(),
            "account_id": "UI-A",
            "broker": "Tiger",
            "symbol": "AAPL",
            "side": "BUY",
            "qty_shares": 1,
        }

        consumer._handle_command(command)

        self.assertEqual(broker.cancel_commands, [])
        self.assertEqual(broker.market_commands, [])


if __name__ == "__main__":
    unittest.main()
