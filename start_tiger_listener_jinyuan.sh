#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
  cat <<'EOF'
Usage: ./start_tiger_listener_new_zealand.sh [options]

Options:
  --tiger-props PATH, --properties PATH
      Tiger OpenAPI properties file or directory. When PATH is a directory,
      it should contain tiger_openapi_config.properties.

  -h, --help
      Show this help.

Environment alternatives:
  TIGEROPEN_PROPS_PATH, TIGER_PROPERTIES_FILE, or TIGER_PROPS_PATH

Positional usage is also accepted:
  ./start_tiger_listener_new_zealand.sh tiger_openapi_config.properties
EOF
}

is_true() {
  local value="${1:-}"
  value="${value,,}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "on" ]]
}

trim() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

strip_optional_quotes() {
  local value
  value="$(trim "${1:-}")"
  if [[ ${#value} -ge 2 && "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ ${#value} -ge 2 && "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

resolve_path() {
  local path="$1"
  if [[ "$path" == /* ]]; then
    printf '%s' "$path"
  else
    printf '%s/%s' "$SCRIPT_DIR" "$path"
  fi
}

read_property() {
  local file="$1"
  local key="$2"
  local line raw_key raw_value

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$(trim "$line")" ]] && continue
    [[ "$line" =~ ^[[:space:]]*[#!] ]] && continue

    if [[ "$line" == *"="* ]]; then
      raw_key="${line%%=*}"
      raw_value="${line#*=}"
    elif [[ "$line" == *":"* ]]; then
      raw_key="${line%%:*}"
      raw_value="${line#*:}"
    else
      continue
    fi

    raw_key="$(trim "$raw_key")"
    if [[ "$raw_key" == "$key" ]]; then
      strip_optional_quotes "$raw_value"
      return 0
    fi
  done < "$file"

  return 1
}

first_property() {
  local file="$1"
  shift
  local key value

  for key in "$@"; do
    value="$(read_property "$file" "$key" || true)"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return 0
    fi
  done

  return 1
}

export_if_unset() {
  local name="$1"
  local value="${2:-}"
  if [[ -n "$value" && -z "${!name:-}" ]]; then
    export "$name=$value"
  fi
}

TIGER_PROPERTIES_PATH="${TIGEROPEN_PROPS_PATH:-${TIGER_PROPERTIES_FILE:-${TIGER_PROPS_PATH:-}}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tiger-props|--properties|--props|--config)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
      TIGER_PROPERTIES_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -z "$TIGER_PROPERTIES_PATH" ]]; then
        TIGER_PROPERTIES_PATH="$1"
      else
        echo "Unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      shift
      ;;
  esac
done

TIGER_PROPERTIES_FILE_RESOLVED=""
if [[ -n "$TIGER_PROPERTIES_PATH" ]]; then
  TIGER_PROPERTIES_PATH="$(resolve_path "$TIGER_PROPERTIES_PATH")"
  export TIGEROPEN_PROPS_PATH="$TIGER_PROPERTIES_PATH"

  if [[ -d "$TIGER_PROPERTIES_PATH" ]]; then
    TIGER_PROPERTIES_FILE_RESOLVED="$TIGER_PROPERTIES_PATH/tiger_openapi_config.properties"
  else
    TIGER_PROPERTIES_FILE_RESOLVED="$TIGER_PROPERTIES_PATH"
  fi

  if [[ ! -f "$TIGER_PROPERTIES_FILE_RESOLVED" ]]; then
    echo "Tiger properties file not found: $TIGER_PROPERTIES_FILE_RESOLVED" >&2
    exit 2
  fi

  export_if_unset TIGER_ID "$(read_property "$TIGER_PROPERTIES_FILE_RESOLVED" tiger_id || true)"
  export_if_unset TIGEROPEN_TIGER_ID "${TIGER_ID:-}"
  export_if_unset TIGER_ACCOUNT "$(read_property "$TIGER_PROPERTIES_FILE_RESOLVED" account || true)"
  export_if_unset TIGEROPEN_ACCOUNT "${TIGER_ACCOUNT:-}"
  export_if_unset TIGER_SECRET_KEY "$(read_property "$TIGER_PROPERTIES_FILE_RESOLVED" secret_key || true)"
  export_if_unset TIGEROPEN_SECRET_KEY "${TIGER_SECRET_KEY:-}"
  export_if_unset TIGER_LICENSE "$(read_property "$TIGER_PROPERTIES_FILE_RESOLVED" license || true)"
  export_if_unset TIGEROPEN_LICENSE "${TIGER_LICENSE:-}"
  export_if_unset TIGER_PRIVATE_KEY "$(first_property "$TIGER_PROPERTIES_FILE_RESOLVED" private_key_pk8 private_key_pk1 private_key || true)"
  export_if_unset TIGEROPEN_PRIVATE_KEY "${TIGER_PRIVATE_KEY:-}"
fi

unset TIGER_TOKEN TIGEROPEN_TOKEN TIGEROPEN_TOKEN_PATH TIGER_TOKEN_FILE

export KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-45.32.121.19:9092}"
export KAFKA_TRADING_COMMANDS_TOPIC="${KAFKA_TRADING_COMMANDS_TOPIC:-trading-commands}"
export KAFKA_ACCOUNT_DETAILS_TOPIC="${KAFKA_ACCOUNT_DETAILS_TOPIC:-account-details}"
export KAFKA_AUTO_OFFSET_RESET="${KAFKA_AUTO_OFFSET_RESET:-latest}"
export KAFKA_POLL_TIMEOUT_SEC="${KAFKA_POLL_TIMEOUT_SEC:-1.0}"

# Used as the stable identifier when this listener publishes server status updates.
export SERVER_ID="${SERVER_ID:-tiger-listener-jinyuan}"

export TIGER_KAFKA_GROUP_ID="${TIGER_KAFKA_GROUP_ID:-tiger-trading-server-jinyuan}"
export TIGER_UI_ACCOUNT_IDS="${TIGER_UI_ACCOUNT_IDS:-ACC-TIGER-JINYUAN}"
export TIGER_UI_ACCOUNT_NUM_ID_MAP="${TIGER_UI_ACCOUNT_NUM_ID_MAP:-ACC-TIGER-JINYUAN:2}"
export TIGER_ACCOUNT_MAP="${TIGER_ACCOUNT_MAP:-}"
export TIGER_MAX_COMMAND_AGE_SECONDS="${TIGER_MAX_COMMAND_AGE_SECONDS:-300}"
export TIGER_PRECHECK_ONLY="${TIGER_PRECHECK_ONLY:-false}"

export TIGER_DRY_RUN="${TIGER_DRY_RUN:-false}"
export TIGER_SANDBOX_DEBUG="${TIGER_SANDBOX_DEBUG:-false}"
export TIGER_CURRENCY="${TIGER_CURRENCY:-USD}"
export TIGER_CASH_CURRENCIES="${TIGER_CASH_CURRENCIES:-USD,NZD,HKD,AUD}"
export TIGER_FOREX_SEG_TYPE="${TIGER_FOREX_SEG_TYPE:-SEC}"
if [[ -n "${TIGEROPEN_PROPS_PATH:-}" ]]; then
  export TIGER_ID="${TIGER_ID:-${TIGEROPEN_TIGER_ID:-}}"
  export TIGER_ACCOUNT="${TIGER_ACCOUNT:-${TIGEROPEN_ACCOUNT:-}}"
  export TIGER_SECRET_KEY="${TIGER_SECRET_KEY:-${TIGEROPEN_SECRET_KEY:-}}"
else
  # Fill these in here, or set them in the environment/properties file.
  export TIGER_ID="${TIGER_ID:-${TIGEROPEN_TIGER_ID:-20159464}}"
  export TIGER_ACCOUNT="${TIGER_ACCOUNT:-${TIGEROPEN_ACCOUNT:-6871313}}"
  export TIGER_SECRET_KEY="${TIGER_SECRET_KEY:-${TIGEROPEN_SECRET_KEY:-9f1db971-2623-3783-ae77-41aaa03a351e}}"
fi
export TIGEROPEN_TIGER_ID="${TIGEROPEN_TIGER_ID:-$TIGER_ID}"
export TIGEROPEN_ACCOUNT="${TIGEROPEN_ACCOUNT:-$TIGER_ACCOUNT}"
export TIGEROPEN_SECRET_KEY="${TIGEROPEN_SECRET_KEY:-$TIGER_SECRET_KEY}"
export TIGER_LICENSE="${TIGER_LICENSE:-${TIGEROPEN_LICENSE:-TBNZ}}"
export TIGEROPEN_LICENSE="${TIGEROPEN_LICENSE:-$TIGER_LICENSE}"

if [[ -z "${TIGER_PRIVATE_KEY_PATH:-}" && -z "${TIGER_PRIVATE_KEY:-}" ]]; then
  if [[ -f "$SCRIPT_DIR/tiger_private_key.pem.raw" ]]; then
    export TIGER_PRIVATE_KEY_PATH="$SCRIPT_DIR/tiger_private_key.pem.raw"
  elif [[ -f "$SCRIPT_DIR/tiger_private_key.pem" ]]; then
    export TIGER_PRIVATE_KEY_PATH="$SCRIPT_DIR/tiger_private_key.pem"
  else
    export TIGER_PRIVATE_KEY_PATH=""
  fi
fi

# Tiger private keys are the base64 key body only, without BEGIN/END headers.
export TIGER_PRIVATE_KEY="${TIGER_PRIVATE_KEY:-${TIGEROPEN_PRIVATE_KEY:-}}"
export TIGEROPEN_PRIVATE_KEY="${TIGEROPEN_PRIVATE_KEY:-$TIGER_PRIVATE_KEY}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

echo "Starting Tiger trading listener with:"
echo "  KAFKA_BOOTSTRAP_SERVERS=$KAFKA_BOOTSTRAP_SERVERS"
echo "  KAFKA_TRADING_COMMANDS_TOPIC=$KAFKA_TRADING_COMMANDS_TOPIC"
echo "  KAFKA_ACCOUNT_DETAILS_TOPIC=$KAFKA_ACCOUNT_DETAILS_TOPIC"
echo "  KAFKA_AUTO_OFFSET_RESET=$KAFKA_AUTO_OFFSET_RESET"
echo "  SERVER_ID=$SERVER_ID"
echo "  TIGER_KAFKA_GROUP_ID=$TIGER_KAFKA_GROUP_ID"
echo "  TIGER_UI_ACCOUNT_IDS=$TIGER_UI_ACCOUNT_IDS"
echo "  TIGER_UI_ACCOUNT_NUM_ID_MAP=$TIGER_UI_ACCOUNT_NUM_ID_MAP"
echo "  TIGER_MAX_COMMAND_AGE_SECONDS=$TIGER_MAX_COMMAND_AGE_SECONDS"
echo "  TIGER_PRECHECK_ONLY=$TIGER_PRECHECK_ONLY"
echo "  TIGER_DRY_RUN=$TIGER_DRY_RUN"
echo "  TIGER_SANDBOX_DEBUG=$TIGER_SANDBOX_DEBUG"
echo "  TIGER_CURRENCY=$TIGER_CURRENCY"
echo "  TIGER_CASH_CURRENCIES=$TIGER_CASH_CURRENCIES"
echo "  TIGER_FOREX_SEG_TYPE=$TIGER_FOREX_SEG_TYPE"
echo "  TIGEROPEN_PROPS_PATH_SET=$([[ -n "${TIGEROPEN_PROPS_PATH:-}" ]] && echo yes || echo no)"
echo "  TIGER_ID_SET=$([[ -n "$TIGER_ID" ]] && echo yes || echo no)"
echo "  TIGER_ACCOUNT_SET=$([[ -n "$TIGER_ACCOUNT" ]] && echo yes || echo no)"
echo "  TIGER_SECRET_KEY_SET=$([[ -n "$TIGER_SECRET_KEY" || -n "$TIGEROPEN_SECRET_KEY" ]] && echo yes || echo no)"
echo "  TIGER_LICENSE_SET=$([[ -n "${TIGER_LICENSE:-}" || -n "${TIGEROPEN_LICENSE:-}" ]] && echo yes || echo no)"
echo "  TIGER_ACCOUNT_MAP_SET=$([[ -n "$TIGER_ACCOUNT_MAP" ]] && echo yes || echo no)"
echo "  TIGER_PRIVATE_KEY_PATH_SET=$([[ -n "$TIGER_PRIVATE_KEY_PATH" ]] && echo yes || echo no)"
echo "  TIGER_PRIVATE_KEY_SET=$([[ -n "$TIGER_PRIVATE_KEY" ]] && echo yes || echo no)"
echo "  LOG_LEVEL=$LOG_LEVEL"

if is_true "$TIGER_PRECHECK_ONLY"; then
  echo "TIGER_PRECHECK_ONLY is true. The listener will run read-only account checks and exit before consuming Kafka commands."
elif ! is_true "$TIGER_DRY_RUN"; then
  echo "WARNING: TIGER_DRY_RUN is false. Matching Tiger orders will be sent to Tiger OpenAPI."
fi

exec python3 "$SCRIPT_DIR/trading_server/tiger.py"
