#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

is_true() {
  local value="${1:-}"
  value="${value,,}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "on" ]]
}

ensure_self_signed_cert() {
  local tls_name="${UI_TLS_NAME:-$UI_HOST}"
  local san_prefix="DNS"

  if [[ "$tls_name" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    san_prefix="IP"
  fi

  mkdir -p "$(dirname "$UI_SSL_CERTFILE")"

  if [[ -f "$UI_SSL_CERTFILE" && -f "$UI_SSL_KEYFILE" ]]; then
    return
  fi

  if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate the HTTPS certificate." >&2
    exit 1
  fi

  echo "Generating self-signed TLS certificate for $tls_name"
  openssl req \
    -x509 \
    -nodes \
    -newkey rsa:2048 \
    -keyout "$UI_SSL_KEYFILE" \
    -out "$UI_SSL_CERTFILE" \
    -days 365 \
    -subj "/CN=$tls_name" \
    -addext "subjectAltName=${san_prefix}:$tls_name"
}

REDIRECT_PID=""

cleanup() {
  if [[ -n "$REDIRECT_PID" ]]; then
    kill "$REDIRECT_PID" 2>/dev/null || true
    wait "$REDIRECT_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT

stop_old_trading_ui_sessions() {
  local current_pid="$$"
  local parent_pid="$PPID"
  local pids=()
  local pid ppid cmd

  while read -r pid ppid cmd; do
    [[ -n "${pid:-}" ]] || continue
    if [[ "$pid" == "$current_pid" || "$pid" == "$parent_pid" ]]; then
      continue
    fi
    if [[ "$cmd" == *"python3 -m uvicorn trading_ui.webserver:app"* && "$cmd" == *"--port $UI_PORT"* ]]; then
      pids+=("$pid")
    elif is_true "$UI_REDIRECT_ENABLED" && [[ "$cmd" == *"python3 -m trading_ui.http_redirect"* ]]; then
      pids+=("$pid")
    fi
  done < <(ps -eo pid=,ppid=,args=)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    return
  fi

  echo "Stopping previous trading UI process(es): ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null || true
  sleep 1

  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "Force-stopping trading UI process $pid"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
}
export ACCOUNT_DASHBOARD_CONFIG="${ACCOUNT_DASHBOARD_CONFIG:-$SCRIPT_DIR/trading_ui/sample/config.local.json}"

export KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-127.0.0.1:9092}"
export KAFKA_MARKET_DATA_BOOTSTRAP_SERVERS="${KAFKA_MARKET_DATA_BOOTSTRAP_SERVERS:-45.32.121.19:9092}"
export KAFKA_ACCOUNT_DETAILS_TOPIC="${KAFKA_ACCOUNT_DETAILS_TOPIC:-account-details}"
export KAFKA_TRADING_COMMANDS_TOPIC="${KAFKA_TRADING_COMMANDS_TOPIC:-trading-commands}"
export KAFKA_MARKET_DATA_TOPIC="${KAFKA_MARKET_DATA_TOPIC:-price-books}"
export KAFKA_GROUP_ID="${KAFKA_GROUP_ID:-account-dashboard}"
export KAFKA_MARKET_DATA_GROUP_ID="${KAFKA_MARKET_DATA_GROUP_ID:-market-data-dashboard}"
export KAFKA_AUTO_OFFSET_RESET="${KAFKA_AUTO_OFFSET_RESET:-latest}"
export KAFKA_MARKET_DATA_AUTO_OFFSET_RESET="${KAFKA_MARKET_DATA_AUTO_OFFSET_RESET:-earliest}"

export MARKET_DATA_HISTORICAL_PRICES_CSV="${MARKET_DATA_HISTORICAL_PRICES_CSV:-$SCRIPT_DIR/market_data/historical_prices.csv}"
export MARKET_INSIGHTS_MAX_LEVELS="${MARKET_INSIGHTS_MAX_LEVELS:-20}"

export UI_HOST="${UI_HOST:-45.32.121.19}"
export UI_SSL_ENABLED="${UI_SSL_ENABLED:-true}"
if [[ -z "${UI_PORT:-}" ]]; then
  if is_true "$UI_SSL_ENABLED"; then
    export UI_PORT="443"
  else
    export UI_PORT="80"
  fi
else
  export UI_PORT
fi
export UI_SSL_CERTFILE="${UI_SSL_CERTFILE:-$SCRIPT_DIR/certs/server.crt}"
export UI_SSL_KEYFILE="${UI_SSL_KEYFILE:-$SCRIPT_DIR/certs/server.key}"
export UI_TLS_NAME="${UI_TLS_NAME:-$UI_HOST}"
export UI_REDIRECT_ENABLED="${UI_REDIRECT_ENABLED:-$UI_SSL_ENABLED}"
export UI_REDIRECT_HOST="${UI_REDIRECT_HOST:-0.0.0.0}"
if [[ -z "${UI_REDIRECT_PORT:-}" ]]; then
  export UI_REDIRECT_PORT="80"
else
  export UI_REDIRECT_PORT
fi
export UI_REDIRECT_TARGET_HOST="${UI_REDIRECT_TARGET_HOST:-$UI_HOST}"
export UI_REDIRECT_TARGET_PORT="${UI_REDIRECT_TARGET_PORT:-$UI_PORT}"

stop_old_trading_ui_sessions

echo "Starting trading UI with:"
echo "  ACCOUNT_DASHBOARD_CONFIG=$ACCOUNT_DASHBOARD_CONFIG"
echo "  UI_HOST=$UI_HOST"
echo "  UI_PORT=$UI_PORT"
echo "  UI_SSL_ENABLED=$UI_SSL_ENABLED"
echo "  UI_SSL_CERTFILE=$UI_SSL_CERTFILE"
echo "  UI_SSL_KEYFILE=$UI_SSL_KEYFILE"
echo "  UI_REDIRECT_ENABLED=$UI_REDIRECT_ENABLED"
echo "  UI_REDIRECT_HOST=$UI_REDIRECT_HOST"
echo "  UI_REDIRECT_PORT=$UI_REDIRECT_PORT"
echo "  KAFKA_BOOTSTRAP_SERVERS=$KAFKA_BOOTSTRAP_SERVERS"
echo "  KAFKA_MARKET_DATA_BOOTSTRAP_SERVERS=$KAFKA_MARKET_DATA_BOOTSTRAP_SERVERS"
echo "  MARKET_DATA_HISTORICAL_PRICES_CSV=$MARKET_DATA_HISTORICAL_PRICES_CSV"
echo "  MARKET_INSIGHTS_MAX_LEVELS=$MARKET_INSIGHTS_MAX_LEVELS"

UVICORN_ARGS=(
  trading_ui.webserver:app
  --host "$UI_HOST"
  --port "$UI_PORT"
)

if is_true "$UI_SSL_ENABLED"; then
  ensure_self_signed_cert
  UVICORN_ARGS+=(--ssl-certfile "$UI_SSL_CERTFILE" --ssl-keyfile "$UI_SSL_KEYFILE")
fi

if is_true "$UI_REDIRECT_ENABLED"; then
  python3 -m trading_ui.http_redirect &
  REDIRECT_PID=$!
fi

python3 -m uvicorn "${UVICORN_ARGS[@]}"
