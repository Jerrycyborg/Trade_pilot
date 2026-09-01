#!/usr/bin/env bash
# Trade Pilot — read-only status for the local paper stack.

set -euo pipefail

STATE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/trade-pilot"
PIDDIR="$STATE_ROOT/pids"
declare -A SERVICES=(
  [audit-logger]="audit_logger.main:app 8006"
  [policy-service]="policy_service.main:app 8001"
  [execution-service]="execution_service.main:app 8002"
  [portfolio-service]="portfolio_service.main:app 8004"
  [strategy-service]="strategy_service.main:app 8003"
  [sentiment-aggregator]="sentiment_aggregator.main:app 8008"
  [notification-service]="notification_service.main:app 8009"
  [approval-gateway]="approval_gateway.main:app 8010"
  [research-service]="research_service.main:app 8005"
  [autonomy-orchestrator]="autonomy_orchestrator.main:app 8007"
)

echo "=== Trade Pilot service status ==="
for name in "${!SERVICES[@]}"; do
  read -r module port <<<"${SERVICES[$name]}"
  pidfile="$PIDDIR/$name.pid"
  if [ ! -f "$pidfile" ] || [ -L "$pidfile" ]; then
    echo "  [$name] not running"
    continue
  fi
  pid=$(<"$pidfile")
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "  [$name] not running"
    continue
  fi
  command=$(ps -p "$pid" -o command= 2>/dev/null || true)
  if [[ "$command" != *"uvicorn $module"* ]]; then
    echo "  [$name] PID conflict"
    continue
  fi
  http=$(curl -sS -o /dev/null -w "%{http_code}"     "http://127.0.0.1:$port/health" 2>/dev/null || true)
  [ "$http" = "200" ]     && echo "  [$name] healthy on 127.0.0.1:$port"     || echo "  [$name] process alive; health returned $http"
done
