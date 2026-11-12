from copy import deepcopy
from unittest.mock import Mock
import pytest
from trading_ui.account_delta import AccountDeltaPublisher
from trading_ui.kafka.consumer_account_details import AccountDetailsConsumer, parse_account_message
from trading_ui.models import AccountMeta
from trading_ui.store import AccountStore

def test_consumer_merges_partial_fill_removes_closed_position_and_recovers_gap():
    store = AccountStore()
    consumer = AccountDetailsConsumer({}, store, {'A': AccountMeta(id='A', num_id=1, broker='Test')})
    publisher = AccountDeltaPublisher(clock=lambda: 0)
    events = []
    state = dict(account_id='A', account_num_id=1, ts='2026-11-11T00:00:00Z',
                 cash=100, trading_enabled=True, positions=[dict(symbol='X', qty=10), dict(symbol='Y', qty=20)])
    publisher.publish(state, events.append)
    assert consumer.process_payload(events[-1], Mock())
    state['positions'][0]['qty'] = 7
    state['cash'] = 130
    publisher.publish(state, events.append)
    delta = deepcopy(events[-1])
    assert len(delta['positions']) == 1
    assert consumer.process_payload(delta, Mock())
    assert [(p.symbol, p.qty) for p in store.get_all()['A'].positions] == [('X', 7), ('Y', 20)]
    assert store.get_all()['A'].cash == 130
    assert not consumer.process_payload(delta, Mock())
    state['cash'] = 140
    publisher.publish(state, events.append)  # Deliberately lose this event.
    state['positions'] = [state['positions'][1]]
    publisher.publish(state, events.append)
    assert not consumer.process_payload(events[-1], Mock())
    assert store.get_all()['A'].trading_enabled is False
    publisher.publish(state, events.append, force=True)
    assert consumer.process_payload(events[-1], Mock())
    assert store.get_all()['A'].trading_enabled is True
    assert [p.symbol for p in store.get_all()['A'].positions] == ['Y']
    assert store.get_all()['A'].cash == 140
    assert not consumer.process_payload(dict(events[-1], account_num_id=2), Mock())

def test_cold_consumer_waits_for_checkpoint_and_legacy_snapshot_still_works():
    import json
    store = AccountStore()
    consumer = AccountDetailsConsumer({}, store, {'A': AccountMeta(id='A', num_id=1, broker='Test')})
    publisher = AccountDeltaPublisher(clock=lambda: 0)
    events = []
    state = dict(account_id='A', account_num_id=1, ts='2026-11-11T00:00:00Z', cash=1,
                 positions=[], trading_enabled=True)
    publisher.publish(state, events.append)
    state['cash'] = 2
    publisher.publish(state, events.append)
    assert parse_account_message(json.dumps(events[-1]).encode()) is None
    assert not consumer.process_payload(events[-1], Mock())
    assert not store.get_all()
    assert consumer.process_payload(state, Mock())
    assert store.get_all()['A'].cash == 2
