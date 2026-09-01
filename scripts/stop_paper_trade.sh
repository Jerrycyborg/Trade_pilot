#!/usr/bin/env bash
# Trade Pilot — stop services started by start_paper_trade.sh.

set -euo pipefail
umask 077

STATE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/trade-pilot"
PIDDIR="$STATE_ROOT/pids"
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

if [ -L "$PIDDIR" ]; then
  echo "Refusing symlinked PID directory: $PIDDIR" >&2
  exit 1
fi

echo "=== Stopping Trade Pilot services ==="
for name in "${!MODULES[@]}"; do
  pidfile="$PIDDIR/$name.pid"
  [ -f "$pidfile" ] || continue
  if [ -L "$pidfile" ]; then
    echo "  [$name] refusing symlinked PID file" >&2
    continue
  fi
  pid=$(<"$pidfile")
  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "  [$name] invalid PID file" >&2
    continue
  fi
  command=$(ps -p "$pid" -o command= 2>/dev/null || true)
  if [[ "$command" == *"uvicorn ${MODULES[$name]}"* ]]; then
    kill "$pid"
    echo "  [$name] stopped (pid $pid)"
  elif [ -n "$command" ]; then
    echo "  [$name] PID belongs to another process; not stopping it" >&2
    continue
  else
    echo "  [$name] already stopped"
  fi
  rm -f -- "$pidfile"
done
echo "Done."
