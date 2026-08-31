#!/usr/bin/env bash
# Trade_pilot — Paper Trade Startup Script
# Starts all 9 services in the background with logs in /tmp/tradepilot/
# Usage: ./scripts/start_paper_trade.sh
# Stop:  ./scripts/stop_paper_trade.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="/tmp/tradepilot/logs"
PIDDIR="/tmp/tradepilot/pids"
mkdir -p "$LOGDIR" "$PIDDIR"

cd "$ROOT"

# Load .env
set -a; source .env; set +a

UV="$HOME/.local/bin/uv"

start_service() {
  local name=$1 module=$2 port=$3
  if [ -f "$PIDDIR/$name.pid" ] && kill -0 "$(cat "$PIDDIR/$name.pid")" 2>/dev/null; then
    echo "  [$name] already running (pid $(cat "$PIDDIR/$name.pid"))"
    return
  fi
  "$UV" run uvicorn "$module" --host 0.0.0.0 --port "$port" \
    > "$LOGDIR/$name.log" 2>&1 &
  echo $! > "$PIDDIR/$name.pid"
  echo "  [$name] started on :$port (pid $!)"
}

MODE="${MARKET_DATA_TIMEFRAME:-daily}"
echo "=== Trade_pilot Paper Trade — Starting services ==="
echo "  Timeframe: $MODE${MARKET_DATA_TIMEFRAME:+ (${INTRADAY_MINUTES:-15}-minute bars)}"
echo "  Provider:  ${MARKET_DATA_PROVIDER:-auto}  |  Streaming: ${STREAMING_ENABLED:-false}"

# Intraday depends on reachable market data; check before trading rather than
# discovering it in the audit log as a run of stale_data rejections.
if [ "$MODE" = "intraday" ] && [ "${SKIP_PREFLIGHT:-false}" != "true" ]; then
  echo ""
  echo "=== Intraday preflight ==="
  if ! "$UV" run python scripts/verify_intraday.py; then
    echo ""
    echo "Preflight failed — not starting. Fix the above, or set SKIP_PREFLIGHT=true to override."
    exit 1
  fi
fi
echo ""
start_service "audit-logger"         "audit_logger.main:app"               8006
start_service "policy-service"       "policy_service.main:app"             8001
start_service "execution-service"    "execution_service.main:app"          8002
start_service "portfolio-service"    "portfolio_service.main:app"          8004
start_service "strategy-service"     "strategy_service.main:app"           8003
start_service "sentiment-aggregator" "sentiment_aggregator.main:app"       8008
start_service "notification-service" "notification_service.main:app"       8009
start_service "approval-gateway"     "approval_gateway.main:app"           8010
start_service "research-service"     "research_service.main:app"           8005

echo ""
echo "Waiting 5s for services to boot..."
sleep 5

echo ""
echo "=== Starting autonomy-orchestrator (trading engine) ==="
start_service "autonomy-orchestrator" "autonomy_orchestrator.main:app"    8007

echo ""
echo "=== Health check ==="
for port in 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010; do
  status=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/health" 2>/dev/null || curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/v1/portfolio/snapshot" 2>/dev/null || curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/v1/research/status" 2>/dev/null || echo "ERR")
  [ "$status" = "200" ] && echo "  :$port ✅" || echo "  :$port ❌ ($status)"
done

echo ""
echo "=== Dashboard ==="
echo "  Open: http://localhost:8443  (if nginx running)"
echo "  Or:   apps/dashboard/index.html (open directly in browser)"
echo ""
echo "Logs: $LOGDIR"
echo "Stop: ./scripts/stop_paper_trade.sh"
echo ""
echo "Paper trade mode: DEMO=true | Weekly cap: \$${WALLET_SIZE_USD:-50} | Loss limit: \$${MONTHLY_LOSS_LIMIT_USD:-10}"
echo ""
echo "Check the loop's actual resolution:"
echo "  curl http://localhost:8007/v1/orchestrator/realtime"
