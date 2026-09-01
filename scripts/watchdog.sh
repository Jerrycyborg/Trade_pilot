#!/usr/bin/env bash
# Trade Pilot paper watchdog — restarts only the loopback paper stack.

set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/trade-pilot"
PIDDIR="$STATE_ROOT/pids"
INTERVAL_SECONDS="${WATCHDOG_INTERVAL_SECONDS:-60}"

if [[ ! "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || [ "$INTERVAL_SECONDS" -lt 10 ]; then
  echo "WATCHDOG_INTERVAL_SECONDS must be an integer of at least 10." >&2
  exit 1
fi
if [ -L "$STATE_ROOT" ] || [ -L "$PIDDIR" ]; then
  echo "Refusing symlinked watchdog state." >&2
  exit 1
fi

declare -A MODULES=(
  [audit-logger]="audit_logger.main:app"
  [policy-service]="policy_service.main:app"
  [execution-service]="execution_service.main:app"
  [portfolio-service]="portfolio_service.main:app"
  [strategy-service]="strategy_service.main:app"
  [sentiment-aggregator]="sentiment_aggregator.main:app"
  [notification-service]="notification_service.main:app"
  [approval-gateway]="approval_gateway.main:app"
  [research-service]="research_service.main:app"
  [autonomy-orchestrator]="autonomy_orchestrator.main:app"
)

echo "[watchdog] monitoring the paper stack"
while true; do
  needs_restart=false
  for name in "${!MODULES[@]}"; do
    pidfile="$PIDDIR/$name.pid"
    if [ ! -f "$pidfile" ] || [ -L "$pidfile" ]; then
      needs_restart=true
      break
    fi
    pid=$(<"$pidfile")
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
      needs_restart=true
      break
    fi
    command=$(ps -p "$pid" -o command= 2>/dev/null || true)
    if [[ "$command" != *"uvicorn ${MODULES[$name]}"* ]]; then
      echo "[watchdog] PID conflict for $name; refusing automatic action" >&2
      exit 1
    fi
  done
  if [ "$needs_restart" = true ]; then
    "$ROOT/scripts/start_paper_trade.sh"
  fi
  sleep "$INTERVAL_SECONDS"
done
