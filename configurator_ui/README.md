# KTrader Configurator

A standalone FastAPI app for editing the trading UI's `config.json` and its linked
`trading_accounts.json`. It runs independently of Kafka and the trading servers.

From the repository root, using your Python environment:

```sh
python -m pip install -r configurator_ui/requirements.txt
python -m configurator_ui.webserver --config trading_ui/sample/config.json
```

Open **http://127.0.0.1:8020** and sign in as `admin` using
`auth.users.admin.password` from the selected configuration. Only the admin account
can edit configuration. The app has its own session cookie and does not share
trading UI sessions. Changing the admin password expires other configurator sessions.

The configuration path defaults to `ACCOUNT_DASHBOARD_CONFIG`, then
`trading_ui/sample/config.local.json` if present, otherwise
`trading_ui/sample/config.json`. The linked accounts path is read from `accounts_file`
(or `trading_accounts_file`) and resolved relative to that configuration file.
Pass the same configuration path that the trading UI uses in production.

Use `--host` and `--port` to change the listening address. It binds to localhost by
default. For HTTPS, supply `--ssl-certfile` and `--ssl-keyfile`. The configurator's
listening settings are separate from the trading UI server settings being edited.

## Editing and saving

- The form editor includes E/F aggression levels, opposing book depth, cycle times,
  per-account cooldowns, symbols, server, Kafka, market data, and authentication.
- Add, edit, hide, monitor, or remove accounts. Hidden accounts remain editable.
- The JSON editor supports extra fields and adding or removing optional settings.
  Unknown fields and relative paths are preserved, including broker-specific data.
- Each file is saved independently. The server validates data before writing;
  account IDs must be unique, including hidden accounts. A changed accounts-file
  reference must point to an existing valid accounts file.
- Saves atomically replace the selected file, preserving its permission bits. The
  previous bytes are retained in `<filename>.bak`, readable only by the service user.
  Each save replaces that file's previous backup. A revision check rejects stale
  edits, and a file lock serializes configurator saves across processes on Linux.
- Edits in other programs should be reloaded before saving. External editors do
  not participate in the configurator's file lock.
- Certificates need not exist on the configurator machine to save their paths;
  the trading UI checks those files when HTTPS starts.

**Restart the affected trading UI and account listeners after saving.** The
configurator edits disk files; it does not restart services or change running
strategies. Existing environment overrides in startup scripts continue to apply.

The trading UI now uses live Kafka account snapshots only. Until data arrives,
accounts show the existing waiting state; no sample account balances are loaded.

## Checks

```sh
python -m pip install httpx pytest
python -m pytest tests/test_configurator.py tests/test_fast_trading_config.py
```

The wider trading UI suite additionally uses `python-multipart` and
`confluent-kafka`.
