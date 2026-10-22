#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

fail() {
  log "$*" >&2
  exit 1
}

is_true() {
  local value="${1,,}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "on" ]]
}

export ORDER_UI_CONFIG="${ORDER_UI_CONFIG:-${ACCOUNT_DASHBOARD_CONFIG:-$SCRIPT_DIR/trading_ui/sample/config.local.json}}"
ORDER_UI_PYTHON="${ORDER_UI_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
ORDER_UI_HOST="${ORDER_UI_HOST:-45.32.121.19}"
ORDER_UI_PORT="${ORDER_UI_PORT:-8030}"
ORDER_UI_SSL_ENABLED="${ORDER_UI_SSL_ENABLED:-true}"
ORDER_UI_SSL_CERTFILE="${ORDER_UI_SSL_CERTFILE:-$SCRIPT_DIR/certs/server.crt}"
ORDER_UI_SSL_KEYFILE="${ORDER_UI_SSL_KEYFILE:-$SCRIPT_DIR/certs/server.key}"

command -v "$ORDER_UI_PYTHON" >/dev/null 2>&1 || fail \
  "Python not found: $ORDER_UI_PYTHON. Run python3 -m venv .venv, then .venv/bin/python -m pip install -r order_ui/requirements.txt."

# Validate before stopping a working instance. Do not print any credentials.
"$ORDER_UI_PYTHON" - "$ORDER_UI_PORT" <<'PY'
import sys
import uvicorn
from order_ui.config import default_config_path, load_config
from order_ui.webserver import create_app

if not sys.argv[1].isdigit() or not 1 <= int(sys.argv[1]) <= 65535:
    raise SystemExit("ORDER_UI_PORT must be an integer between 1 and 65535.")
load_config(default_config_path())
PY

UVICORN_ARGS=(order_ui.webserver:app --host "$ORDER_UI_HOST" --port "$ORDER_UI_PORT" --app-dir "$SCRIPT_DIR")
if is_true "$ORDER_UI_SSL_ENABLED"; then
  [[ -r "$ORDER_UI_SSL_CERTFILE" && -r "$ORDER_UI_SSL_KEYFILE" ]] || fail \
    "HTTPS certificate/key missing or unreadable. Set ORDER_UI_SSL_CERTFILE and ORDER_UI_SSL_KEYFILE to your certificate files."
  "$ORDER_UI_PYTHON" - "$ORDER_UI_SSL_CERTFILE" "$ORDER_UI_SSL_KEYFILE" <<'PY'
import ssl
import sys

ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(sys.argv[1], sys.argv[2])
PY
  UVICORN_ARGS+=(--ssl-certfile "$ORDER_UI_SSL_CERTFILE" --ssl-keyfile "$ORDER_UI_SSL_KEYFILE")
fi

stop_old_order_ui_sessions() {
  local pid comm args attempt
  local pids=()
  while read -r pid comm args; do
    # Match this app, repository, and exact port, never the trading UI.
    [[ "$comm" == python* ]] || continue
    [[ " $args " == *" -m uvicorn order_ui.webserver:app "* ]] || continue
    [[ " $args " == *" --port $ORDER_UI_PORT "* ]] || continue
    # Some hosts restrict /proc/PID/cwd even for the process owner. New
    # launches identify their repository with --app-dir; also recognize
    # existing launches using this repository's virtual environment.
    if [[ " $args " != *" --app-dir $SCRIPT_DIR "* && "$args" != "$SCRIPT_DIR/"* ]]; then
      [[ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" == "$SCRIPT_DIR" ]] || continue
    fi
    pids+=("$pid")
  done < <(ps -eo pid=,comm=,args=)

  for pid in "${pids[@]}"; do
    log "Stopping previous order UI process $pid"
    kill "$pid" 2>/dev/null || true
    for ((attempt = 0; attempt < 20; attempt++)); do
      if ! kill -0 "$pid" 2>/dev/null || [[ "$(ps -o stat= -p "$pid")" == Z* ]]; then
        break
      fi
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null && [[ "$(ps -o stat= -p "$pid")" != Z* ]]; then
      fail "Order UI process $pid did not stop within 20 seconds; inspect it before retrying."
    fi
  done
}

stop_old_order_ui_sessions

log "Starting order UI with:"
log "  ORDER_UI_CONFIG=$ORDER_UI_CONFIG"
log "  ORDER_UI_HOST=$ORDER_UI_HOST"
log "  ORDER_UI_PORT=$ORDER_UI_PORT"
log "  ORDER_UI_SSL_ENABLED=$ORDER_UI_SSL_ENABLED"
log "  ORDER_UI_PYTHON=$ORDER_UI_PYTHON"

exec "$ORDER_UI_PYTHON" -m uvicorn "${UVICORN_ARGS[@]}"
