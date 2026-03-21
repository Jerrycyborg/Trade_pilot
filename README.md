![Trade Pilot banner](assets/trade-pilot-banner.svg)

# Trade_pilot — Autonomous Trading Platform

Production-minded AI trading stack: strategy proposes, policy approves, execution fills, and portfolio reconciles. This repo includes autonomous orchestration, approvals, notifications, sentiment, audit logging, and dashboard controls on top of the core trading services.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Autonomy Orchestrator :8007                  │
│        scheduler → signal fetch → risk → policy → execute      │
└──────┬──────────────┬────────────────┬───────────────┬──────────┘
       │              │                │               │
       ▼              ▼                ▼               ▼
  Risk Engine    Policy Gate      Execution        Audit Logger
  (sizing,       (hard rules,     Service :8002    :8006
   drawdown,      sector conc,    (broker order)   (append-only)
   PDT, sector)   event block)         │
                       │               ▼
                       │         Broker (eToro/Paper)
                       │
              ┌────────┴─────────┐
              ▼                  ▼
     Notification :8009    Approval Gateway :8010
     (webhook, tiered)     (PENDING/APPROVE/REJECT)

── External Data ──────────────────────────────────────────────────
  Strategy Service :8003   (signals, TA, ADX, patterns)
  Portfolio Service :8004  (positions, NAV)
  Research Service :8005   (AI research summaries)
  Sentiment Aggregator :8008 (NewsAPI, AlphaVantage)
  Dashboard :8080          (kill switch UI, approvals, stats)
```

## Service Port Map

| Service | Port | Purpose |
|---------|------|---------|
| policy-service | 8001 | Policy evaluation (hard rules gate) |
| execution-service | 8002 | Order routing to broker |
| strategy-service | 8003 | Signal generation (TA, ADX, patterns, volume) |
| portfolio-service | 8004 | Position tracking and NAV |
| research-service | 8005 | AI-powered research summaries |
| audit-logger | 8006 | Append-only audit trail (SQLite) |
| autonomy-orchestrator | 8007 | Main loop scheduler and decision engine |
| sentiment-aggregator | 8008 | News/sentiment scoring |
| notification-service | 8009 | Webhook notifications (tiered) |
| approval-gateway | 8010 | Human approval flow (PENDING/APPROVE/REJECT) |
| dashboard | 8080 | Web UI (kill switch, approvals, stats bar) |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `INTERNAL_API_KEY` | Yes (prod) | Shared secret for service-to-service auth |
| `ADMIN_API_KEY` | Yes (prod) | Extra key for kill switch / live mode endpoints |
| `ETORO_API_KEY` | Yes | eToro public API key |
| `ETORO_USER_KEY` | Yes | eToro user key |
| `ETORO_DEMO` | No | Set `true` for eToro demo account (default true) |
| `ANTHROPIC_API_KEY` | Yes | For AI research summaries |
| `NEWSAPI_KEY` | No | NewsAPI.org key for sentiment |
| `ALPHAVANTAGE_KEY` | No | AlphaVantage key for sentiment |
| `WEBHOOK_URL` | No | Slack/Discord/custom webhook for notifications |
| `BROKER` | No | `etoro` or `paper` (default paper) |
| `WORKER_ENABLED` | No | `true` to enable strategy worker polling |
| `ORCHESTRATOR_INTERVAL_MINUTES` | No | Cycle interval (default 5) |
| `STOP_LOSS_PCT` | No | Stop loss % (default 0.03 = 3%) |
| `TAKE_PROFIT_PCT` | No | Take profit % (default 0.06 = 6%) |
| `MAX_HOLD_HOURS` | No | Max position hold time in hours (default 48) |
| `VOLUME_CONFIRM_ENABLED` | No | Require above-avg volume for BUY (default true) |
| `STRATEGY_WATCHLIST` | No | Comma-separated symbols to trade |

## Getting eToro API Keys

1. Log into your eToro account at etoro.com
2. Go to Settings → API (or developer.etoro.com)
3. Create an API key pair — copy the public key and user key
4. Set `ETORO_API_KEY` and `ETORO_USER_KEY` in your `.env`
5. Keep `ETORO_DEMO=true` until you are ready for live trading

## Running in Demo Mode

```bash
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY and ETORO_API_KEY/USER_KEY
# Leave ETORO_DEMO=true and BROKER=paper (or etoro with demo=true)

# Install dependencies
uv sync

# Start all services (each in its own terminal or use a process manager)
uv run uvicorn policy_service.main:app --port 8001
uv run uvicorn execution_service.main:app --port 8002
uv run uvicorn strategy_service.main:app --port 8003
uv run uvicorn portfolio_service.main:app --port 8004
uv run uvicorn research_service.main:app --port 8005
uv run uvicorn audit_logger.main:app --port 8006
uv run uvicorn autonomy_orchestrator.main:app --port 8007
uv run uvicorn sentiment_aggregator.main:app --port 8008
uv run uvicorn notification_service.main:app --port 8009
uv run uvicorn approval_gateway.main:app --port 8010

# Open dashboard
open apps/dashboard/index.html
```

## Enabling Live Mode (Step-by-Step)

⚠️ Only proceed after at least 30 days of demo/paper trading with no policy violations.

1. Set `ETORO_DEMO=false` and `BROKER=etoro` in `.env`
2. Set `ADMIN_API_KEY` to a strong random secret in `.env`
3. Restart all services
4. Verify kill switch is OFF in the dashboard
5. Set weekly cap in `config/policy-baseline.yaml` (`weekly_notional_cap_usd`)
6. Call the live-mode endpoint with admin credentials:
   ```bash
   curl -X POST http://localhost:8007/v1/orchestrator/live-mode \
     -H "X-Internal-Key: $INTERNAL_API_KEY" \
     -H "X-Admin-Key: $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"enable": true, "confirmation": "I CONFIRM LIVE TRADING"}'
   ```
7. Monitor the first 10 trades manually via the dashboard

## Running Tests

```bash
uv run pytest tests/ -x -q --ignore=tests/integration
```
