#!/usr/bin/env bash
# Trade_pilot — Status check
PIDDIR="/tmp/tradepilot/pids"
echo "=== Trade_pilot Service Status ==="
declare -A PORTS=(
  [audit-logger]=8006 [policy-service]=8001 [execution-service]=8002
  [portfolio-service]=8004 [strategy-service]=8003 [sentiment-aggregator]=8008
  [notification-service]=8009 [approval-gateway]=8010 [research-service]=8005
  [autonomy-orchestrator]=8007
)
for name in "${!PORTS[@]}"; do
  port="${PORTS[$name]}"
  pidfile="$PIDDIR/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    http=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/health" 2>/dev/null || echo "ERR")
    [ "$http" = "200" ] && echo "  [$name] ✅ running :$port" || echo "  [$name] ⚠️  pid alive but /health returned $http"
  else
    echo "  [$name] ❌ not running"
  fi
done
