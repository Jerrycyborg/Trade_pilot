#!/usr/bin/env bash
# Trade_pilot watchdog — checks every 60s, restarts dead services
# Run in background: nohup bash scripts/watchdog.sh &

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDDIR="/tmp/tradepilot/pids"
LOGDIR="/tmp/tradepilot/logs"
UV="$HOME/.local/bin/uv"

cd "$ROOT"
set -a; source .env; set +a

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

echo "[watchdog] started at $(date)"
while true; do
  for name in "${!SERVICES[@]}"; do
    read -r module port <<< "${SERVICES[$name]}"
    pidfile="$PIDDIR/$name.pid"
    if [ ! -f "$pidfile" ] || ! kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "[watchdog] $(date) — $name dead, restarting on :$port"
      "$UV" run uvicorn "$module" --host 0.0.0.0 --port "$port" \
        >> "$LOGDIR/$name.log" 2>&1 &
      echo $! > "$pidfile"
    fi
  done
  sleep 60
done
