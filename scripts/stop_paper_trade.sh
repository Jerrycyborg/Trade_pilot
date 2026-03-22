#!/usr/bin/env bash
# Trade_pilot — Stop all services
PIDDIR="/tmp/tradepilot/pids"
echo "=== Stopping Trade_pilot services ==="
for pidfile in "$PIDDIR"/*.pid; do
  [ -f "$pidfile" ] || continue
  name=$(basename "$pidfile" .pid)
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "  [$name] stopped (pid $pid)"
  else
    echo "  [$name] already stopped"
  fi
  rm -f "$pidfile"
done
echo "Done."
