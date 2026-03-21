# Trade_pilot Operational Runbook

## Kill Switch Procedure

**To halt all trading immediately:**
```bash
curl -X POST http://localhost:8007/v1/orchestrator/kill-switch \
  -H "X-Internal-Key: $INTERNAL_API_KEY" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"active": true}'
```

Verify: dashboard shows red "KILL SWITCH ACTIVE" banner.
The orchestrator will halt on its next cycle check (within `ORCHESTRATOR_INTERVAL_MINUTES`).
Open positions are NOT auto-closed by the kill switch — close them manually.

**To re-enable trading:**
```bash
curl -X POST http://localhost:8007/v1/orchestrator/kill-switch \
  -H "X-Internal-Key: $INTERNAL_API_KEY" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"active": false}'
```

## Adjusting the Weekly Cap

Edit `config/policy-baseline.yaml`:
```yaml
weekly_notional_cap_usd: 5000  # change to desired amount
```
The orchestrator reloads this file on every cycle — no restart needed.

## Adding Symbols to Allowlist

Edit `config/policy-baseline.yaml`:
```yaml
symbol_allowlist:
  - AAPL
  - MSFT
  - NEW_SYMBOL  # add here
```
Then run allowlist validation:
```bash
curl -X POST http://localhost:8007/v1/orchestrator/validate \
  -H "X-Internal-Key: $INTERNAL_API_KEY"
```
Review the response — remove any "invalid" symbols.

## Sector Concentration Control

Edit `config/policy-baseline.yaml`:
```yaml
max_sector_concentration: 2  # max positions per sector
```
Supported sectors (hardcoded): equity_index (SPY/QQQ/IWM), bonds (TLT/BND/SHY), commodities (GLD), tech (AAPL/MSFT/GOOGL/AMZN/NVDA/META).

## Promoting Demo → Live

1. Confirm 30+ days paper trading with audit log review
2. Confirm all approval tiers have been tested
3. Set `ETORO_DEMO=false` in `.env` and restart execution-service
4. Enable live mode (see README.md step-by-step)
5. Monitor first 10 live trades via dashboard and audit log:
   ```bash
   curl http://localhost:8006/v1/audit/summary
   ```

## Approving/Rejecting Tier 2/3 Trades

List pending approvals:
```bash
curl http://localhost:8010/v1/approvals/pending
```

Approve:
```bash
curl -X POST http://localhost:8010/v1/approvals/{approval_id}/approve \
  -H "X-Internal-Key: $INTERNAL_API_KEY"
```

Reject:
```bash
curl -X POST http://localhost:8010/v1/approvals/{approval_id}/reject \
  -H "X-Internal-Key: $INTERNAL_API_KEY"
```

Tier 2 approvals auto-expire after 15 minutes (configurable in policy-baseline.yaml).

## Emergency Procedures

### Service crash
Check service logs. Restart the crashed service. The orchestrator will resume on next cycle.
Kill switch is persisted in `policy-baseline.yaml` — it survives restarts.

### Unexpected position opened
1. Activate kill switch immediately (see above)
2. Manually close the position via eToro web/app
3. Review audit log: `curl http://localhost:8006/v1/audit/logs?limit=50`
4. Identify the signal that triggered it and fix the policy config

### Weekly cap exhausted
The system will reject all new BUY signals until Monday (cap resets weekly).
To override for an emergency: increase `weekly_notional_cap_usd` in policy-baseline.yaml.

### Drawdown circuit breaker triggered
If portfolio drops >5% from peak, all new entries are paused.
Review positions, decide if you want to override: increase `drawdown_circuit_breaker_pct` in policy-baseline.yaml temporarily.

## Audit Log Review

```bash
# Last 50 events
curl "http://localhost:8006/v1/audit/logs?limit=50"

# Only rejections
curl "http://localhost:8006/v1/audit/logs?event_type=signal.rejected"

# Summary stats
curl "http://localhost:8006/v1/audit/summary"
```

## Health Check

```bash
# All services
for port in 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010; do
  echo -n "Port $port: "
  curl -s http://localhost:$port/health | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))"
done

# Dependency status from orchestrator
curl http://localhost:8007/v1/orchestrator/health/deps
```
