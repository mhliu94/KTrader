import json

import pytest

from trading_ui.config import load_config


def test_market_tab_follows_publisher_symbols_without_changing_trading_symbols(tmp_path):
    publisher = tmp_path / "publisher.json"
    publisher.write_text(json.dumps({"symbols": [" aehl ", "AAPL", "AEHL"]}))
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({
        "accounts": [{"string_id": "test", "numeric_id": 1, "broker": "Tiger"}],
        "symbols": ["MSFT", "TSLA"],
        "market_data": {"publisher_config": "publisher.json"},
        "auth": {"users": {"admin": {"password": "test"}, "trader": {"password": "test"}}},
    }))
    config = load_config(str(path))
    assert config["market_data"]["symbols"] == ["AEHL", "AAPL"]
    assert config["market_data"]["publisher_config"] == str(publisher)
    assert config["symbols"] == ["MSFT", "TSLA"]
    assert "historical_prices_csv" not in config["market_data"]
    publisher.write_text(json.dumps({"symbols": []}))
    with pytest.raises(ValueError, match="non-empty symbols list"):
        load_config(str(path))
