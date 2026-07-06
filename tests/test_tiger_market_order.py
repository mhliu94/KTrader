import threading
import unittest

from trading_server.tiger import TigerBroker


class FakeTradeClient:
    def __init__(self):
        self.placed_orders = []

    def place_order(self, order):
        self.placed_orders.append(order)
        return "tiger-order-1"


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
    return broker


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


if __name__ == "__main__":
    unittest.main()
