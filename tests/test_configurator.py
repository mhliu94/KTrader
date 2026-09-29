import copy
import json
from pathlib import Path
import stat
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from configurator_ui.storage import ConfigRepository, decode_json
from configurator_ui.webserver import create_app
from trading_ui.config import load_config, load_account_metas


@pytest.fixture
def files(tmp_path):
    config = {
        "accounts_file": "trading_accounts.json",
        "symbols": ["AAPL"],
        "auth": {"users": {"admin": {"password": "test-admin"}, "trader": {"password": "test-trader"}}},
        "server": {"host": "127.0.0.1", "port": 8000, "ssl_enabled": False},
        "fast_trading": {
            "aggression_levels": {
                "1": {"book_levels": 2, "cycle_seconds": 30},
                "2": {"book_levels": 3, "cycle_seconds": 20},
                "3": {"book_levels": 5, "cycle_seconds": 10},
            },
            "minimum_cycle_seconds_by_medium": {"API": 3, "WEB": 30, "WINDOWS": 40, "EMULATOR": 40},
        },
        "extension": {"opaque": [1, {"retain": True}]},
    }
    accounts = {"accounts": [
        {"string_id": "A", "numeric_id": 1, "broker": "Tiger", "trading_medium": "API", "broker_id": "broker-A", "hidden": False, "monitor": True, "custom": {"retain": [1, 2]}},
        {"string_id": "B", "numeric_id": 2, "broker": "Other", "trading_medium": "WEB", "hidden": True},
    ], "metadata": {"retain": "yes"}}
    config_path = tmp_path / "config.json"
    accounts_path = tmp_path / "trading_accounts.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    accounts_path.write_text(json.dumps(accounts, indent=2) + "\n")
    config_path.chmod(0o640)
    return config_path, accounts_path


@pytest.fixture
def client(files):
    with TestClient(create_app(files[0])) as client:
        yield client


def login(client, password="test-admin"):
    response = client.post('/api/login', json={"username": "admin", "password": password}, headers={"X-Configurator-Request": "1"})
    assert response.status_code == 200
    loaded = client.get('/api/documents')
    assert loaded.status_code == 200
    return loaded.json()


def save(client, state, name, document):
    return client.put('/api/documents/' + name, json={"document": document, "revision": state["documents"][name]["revision"]}, headers={"X-Configurator-Request": "1", "X-CSRF-Token": state["csrf_token"]})


def test_requires_admin_login_and_does_not_embed_secrets(client):
    assert client.get('/api/documents').status_code == 401
    assert client.put('/api/documents/config', json={}).status_code == 401
    response = client.get('/')
    assert response.status_code == 200
    assert 'test-admin' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-frame-options'] == 'DENY'
    for username, password in [('trader', 'test-trader'), ('admin', 'wrong')]:
        response = client.post('/api/login', json={"username": username, "password": password}, headers={"X-Configurator-Request": "1"})
        assert response.status_code == 401


def test_rejects_cross_origin_login_and_save_and_missing_csrf(client):
    response = client.post('/api/login', json={"username": "admin", "password": "test-admin"}, headers={"Origin": "https://example.invalid", "X-Configurator-Request": "1"})
    assert response.status_code == 403
    state = login(client)
    payload = {"document": state['documents']['config']['data'], "revision": state['documents']['config']['revision']}
    assert client.put('/api/documents/config', json=payload, headers={"X-Configurator-Request": "1"}).status_code == 403
    assert client.put('/api/documents/config', json=payload, headers={"X-Configurator-Request": "1", "X-CSRF-Token": state['csrf_token'], "Origin": "https://example.invalid"}).status_code == 403


def test_saves_configuration_to_disk_for_next_trading_ui_load(client, files):
    state = login(client)
    original = files[0].read_bytes()
    old_runtime_config = load_config(str(files[0]))
    data = copy.deepcopy(state['documents']['config']['data'])
    data['fast_trading']['aggression_levels']['3'] = {"book_levels": 7, "cycle_seconds": 8}
    data['fast_trading']['minimum_cycle_seconds_by_medium']['WEB'] = 45
    response = save(client, state, 'config', data)
    assert response.status_code == 200
    assert json.loads(files[0].read_bytes()) == data
    assert files[0].with_name('config.json.bak').read_bytes() == original
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o640
    assert stat.S_IMODE(files[0].with_name('config.json.bak').stat().st_mode) == 0o600
    assert old_runtime_config['fast_trading']['aggression_levels'][3]['book_levels'] == 5
    restarted = load_config(str(files[0]))
    assert restarted['fast_trading']['aggression_levels'][3]['book_levels'] == 7
    assert restarted['fast_trading']['minimum_cycle_seconds_by_medium']['WEB'] == 45
    assert restarted['extension'] == data['extension']
    assert data['accounts_file'] == 'trading_accounts.json'


def test_edit_add_remove_and_unhide_accounts_preserves_other_fields(client, files):
    state = login(client)
    original = files[1].read_bytes()
    data = copy.deepcopy(state['documents']['accounts']['data'])
    data['accounts'][0]['machine_alias'] = 'new-machine'
    data['accounts'][1]['hidden'] = False
    data['accounts'].append({"string_id": "C", "numeric_id": 3, "broker": "Other", "trading_medium": "WINDOWS", "monitor": True})
    response = save(client, state, 'accounts', data)
    assert response.status_code == 200
    assert json.loads(files[1].read_bytes()) == data
    assert data['accounts'][0]['custom'] == {"retain": [1, 2]}
    assert files[1].with_name('trading_accounts.json.bak').read_bytes() == original
    assert set(load_account_metas(load_config(str(files[0])))) == {'A', 'B', 'C'}
    state = client.get('/api/documents').json()
    data['accounts'].pop(0)
    assert save(client, state, 'accounts', data).status_code == 200
    assert set(load_account_metas(load_config(str(files[0])))) == {'B', 'C'}


@pytest.mark.parametrize('change', [
    lambda c: c['fast_trading']['aggression_levels']['1'].update(book_levels=21),
    lambda c: c['fast_trading']['aggression_levels']['1'].update(cycle_seconds=0),
    lambda c: c['fast_trading']['minimum_cycle_seconds_by_medium'].update(WEB=-1),
    lambda c: c['fast_trading']['aggression_levels'].pop('2'),
    lambda c: c['server'].update(port=70000),
    lambda c: c['server'].update(port=10**1000),
    lambda c: c.update(symbols=[]),
    lambda c: c['auth']['users'].pop('admin'),
    lambda c: c.update(accounts_file='missing.json'),
    lambda c: c.update(accounts_file='config.json'),
])
def test_invalid_config_does_not_modify_files(client, files, change):
    state = login(client)
    original = files[0].read_bytes()
    data = copy.deepcopy(state['documents']['config']['data'])
    change(data)
    response = save(client, state, 'config', data)
    assert response.status_code == 400
    assert files[0].read_bytes() == original
    assert not files[0].with_name('config.json.bak').exists()


@pytest.mark.parametrize('change', [
    lambda a: a['accounts'][1].update(string_id='A'),
    lambda a: a['accounts'][1].update(numeric_id=1),
    lambda a: a['accounts'][0].update(numeric_id=1.5),
    lambda a: a['accounts'][0].update(trading_medium='MOBILE'),
    lambda a: a['accounts'][0].update(hidden='false'),
    lambda a: a.update(accounts=[]),
])
def test_invalid_accounts_including_hidden_duplicates_are_rejected(client, files, change):
    state = login(client)
    original = files[1].read_bytes()
    data = copy.deepcopy(state['documents']['accounts']['data'])
    change(data)
    assert save(client, state, 'accounts', data).status_code == 400
    assert files[1].read_bytes() == original


def test_stale_revision_does_not_overwrite_external_edits(client, files):
    state = login(client)
    external = copy.deepcopy(state['documents']['config']['data'])
    external['symbols'] = ['MSFT']
    files[0].write_text(json.dumps(external))
    proposed = copy.deepcopy(state['documents']['config']['data'])
    proposed['symbols'] = ['TSLA']
    assert save(client, state, 'config', proposed).status_code == 409
    assert json.loads(files[0].read_bytes())['symbols'] == ['MSFT']


def test_password_change_keeps_editor_session_and_invalidates_others(client, files):
    second = TestClient(client.app)
    state = login(client)
    login(second)
    data = copy.deepcopy(state['documents']['config']['data'])
    data['auth']['users']['admin']['password'] = 'new-密码'
    assert save(client, state, 'config', data).status_code == 200
    assert client.get('/api/documents').status_code == 200
    assert second.get('/api/documents').status_code == 401
    login(second, 'new-密码')
    second.close()


def test_account_path_change_reloads_linked_file_and_blocks_old_revision(client, files):
    state = login(client)
    new_path = files[0].parent / 'other.json'
    new_data = [{"string_id": "OTHER", "numeric_id": 5, "broker": "Tiger", "trading_medium": "API"}]
    new_path.write_text(json.dumps(new_data))
    data = copy.deepcopy(state['documents']['config']['data'])
    data['accounts_file'] = 'other.json'
    assert save(client, state, 'config', data).status_code == 200
    fresh = client.get('/api/documents').json()
    assert fresh['documents']['accounts']['data'] == new_data
    assert save(client, state, 'accounts', state['documents']['accounts']['data']).status_code == 409
    new_data[0]['monitor'] = True
    assert save(client, fresh, 'accounts', new_data).status_code == 200
    assert isinstance(json.loads(new_path.read_bytes()), list)


def test_failed_atomic_replace_keeps_original_file(files):
    repo = ConfigRepository(files[0])
    state = repo.read()
    original = files[0].read_bytes()
    data = copy.deepcopy(state['config']['data'])
    data['symbols'] = ['MSFT']
    import os
    replace = os.replace

    def fail_target(source, destination):
        if Path(destination) == files[0]:
            raise OSError('Simulated write failure')
        replace(source, destination)

    with patch('configurator_ui.storage.os.replace', side_effect=fail_target):
        with pytest.raises(OSError):
            repo.save('config', data, state['config']['revision'])
    assert files[0].read_bytes() == original
    assert files[0].with_name('config.json.bak').read_bytes() == original
    assert not list(files[0].parent.glob('*.tmp'))


def test_json_rejects_non_finite_numbers_and_duplicate_fields():
    for raw in [b'{"a": NaN}', b'{"a": Infinity}', b'{"a": 1e999}', b'{"a": 1, "a": 2}']:
        with pytest.raises(ValueError):
            decode_json(raw)


def test_logout_invalidates_session(client):
    state = login(client)
    assert client.post('/api/logout', json={}, headers={"X-Configurator-Request": "1", "X-CSRF-Token": state['csrf_token']}).status_code == 200
    assert client.get('/api/documents').status_code == 401
