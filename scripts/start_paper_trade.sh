#!/usr/bin/env bash
# Trade Pilot — fail-closed paper-trading launcher.
# Usage: ./scripts/start_paper_trade.sh
# Stop:  ./scripts/stop_paper_trade.sh

set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/trade-pilot"
LOGDIR="$STATE_ROOT/logs"
PIDDIR="$STATE_ROOT/pids"

for directory in "$STATE_ROOT" "$LOGDIR" "$PIDDIR"; do
  if [ -L "$directory" ]; then
    echo "Refusing symlinked state directory: $directory" >&2
    exit 1
  fi
  mkdir -p "$directory"
  chmod 700 "$directory"
done

cd "$ROOT"
UV="${UV_BIN:-$(command -v uv || true)}"
if [ -z "$UV" ]; then
  echo "uv is required but was not found." >&2
  exit 1
fi

UV_ENV_ARGS=()
if [ -f "$ROOT/.env" ]; then
  UV_ENV_ARGS=(--env-file "$ROOT/.env")
fi

PAPER_ENV=(
  BROKER=paper
  ALPACA_PAPER=true
  ETORO_DEMO=true
  APP_ENV=production
  ALLOW_INSECURE_DEV_AUTH=false
)

if ! env "${PAPER_ENV[@]}" "$UV" run "${UV_ENV_ARGS[@]}" python -c '
import os
required = ("INTERNAL_API_KEY", "ADMIN_API_KEY", "LIFECYCLE_DATABASE_URL")
values = {name: os.environ.get(name, "") for name in required}
valid = all(values.values()) and values["INTERNAL_API_KEY"] != values["ADMIN_API_KEY"]
raise SystemExit(0 if valid else 1)
'; then
  echo "Missing durable-state configuration or distinct service/admin keys." >&2
  echo "Set them in the process environment or .env; no value will be printed." >&2
  exit 1
fi

start_service() {
  local name=$1 module=$2 port=$3
  local pidfile="$PIDDIR/$name.pid"
  if [ -L "$pidfile" ]; then
    echo "Refusing symlinked PID file: $pidfile" >&2
    exit 1
  fi
  if [ -f "$pidfile" ]; then
    local existing
    existing=$(<"$pidfile")
    if [[ "$existing" =~ ^[0-9]+$ ]] && kill -0 "$existing" 2>/dev/null; then
      echo "  [$name] already running (pid $existing)"
      return
    fi
  fi
  env "${PAPER_ENV[@]}" "$UV" run "${UV_ENV_ARGS[@]}"     uvicorn "$module" --host 127.0.0.1 --port "$port"     >"$LOGDIR/$name.log" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$pidfile"
  chmod 600 "$pidfile"
  echo "  [$name] started on 127.0.0.1:$port (pid $pid)"
}

echo "=== Trade Pilot paper mode — starting loopback-only services ==="
echo "Broker routing is forced to paper; live credentials are ignored."

if [ "${MARKET_DATA_TIMEFRAME:-daily}" = "intraday" ]   && [ "${SKIP_PREFLIGHT:-false}" != "true" ]; then
  echo "=== Intraday preflight ==="
  env "${PAPER_ENV[@]}" "$UV" run "${UV_ENV_ARGS[@]}"     python scripts/verify_intraday.py
fi

start_service "audit-logger" "audit_logger.main:app" 8006
start_service "policy-service" "policy_service.main:app" 8001
start_service "execution-service" "execution_service.main:app" 8002
start_service "portfolio-service" "portfolio_service.main:app" 8004
start_service "strategy-service" "strategy_service.main:app" 8003
start_service "sentiment-aggregator" "sentiment_aggregator.main:app" 8008
start_service "notification-service" "notification_service.main:app" 8009
start_service "approval-gateway" "approval_gateway.main:app" 8010
start_service "research-service" "research_service.main:app" 8005

echo "Waiting 5s for service startup..."
sleep 5
start_service "autonomy-orchestrator" "autonomy_orchestrator.main:app" 8007

echo "=== Health check ==="
for port in 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010; do
  status=$(curl -sS -o /dev/null -w "%{http_code}"     "http://127.0.0.1:$port/health" 2>/dev/null || true)
  [ "$status" = "200" ]     && echo "  127.0.0.1:$port healthy"     || echo "  127.0.0.1:$port unavailable ($status)"
done

echo "Dashboard: uv run python scripts/serve_dashboard.py"
echo "Logs: $LOGDIR"
echo "Stop: ./scripts/stop_paper_trade.sh"
