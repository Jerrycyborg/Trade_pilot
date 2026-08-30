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

### Intraday / real-time

| Variable | Default | Description |
|----------|---------|-------------|
| `MARKET_DATA_TIMEFRAME` | `daily` | Set to `intraday` to trade on intraday bars |
| `INTRADAY_MINUTES` | `15` | Bar size. Yahoo serves 1, 2, 5, 15, 30, 60, 90 |
| `INTRADAY_LOOKBACK_DAYS` | `5` | Bar history for indicators (Yahoo caps 1m at 7 days) |
| `MARKET_DATA_PROVIDER` | auto | `yahoo` forces Yahoo; blank uses Alpaca when keys are set |
| `STREAMING_ENABLED` | `false` | Real-time websocket bar stream (Alpaca only) |
| `STREAM_SYMBOLS` | allowlist | Symbols to stream |
| `STREAM_SYMBOL_LIMIT` | `30` | Cap on concurrent subscriptions (free IEX feed) |
| `ALPACA_FEED` | `iex` | `sip` on a paid Alpaca data plan |
| `MAX_PRICE_AGE_SECONDS` | `120` | Older prices are treated as unusable |
| `ORCHESTRATOR_INTERVAL_SECONDS` | — | Cycle cadence; overrides the minutes setting |
| `STOP_LOSS_CHECK_INTERVAL_MINUTES` | 1 intraday / 5 daily | Stop-loss poll interval |
| `TAKE_PROFIT_CHECK_INTERVAL_MINUTES` | 1 intraday / 5 daily | Take-profit poll interval |
| `PAPER_STARTING_CASH` | `100000` | Paper broker opening cash |
| `PAPER_SLIPPAGE_BPS` | `2` | Simulated slippage, always against the trader |
| `PAPER_STATE_PATH` | `./paper-broker-state.json` | Paper position ledger |

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

## Real-Time Intraday Trading

Two data paths are supported. Both run the same trading loop.

| | Alpaca | Yahoo |
|---|---|---|
| API key | free account required | none |
| Intraday bars | real-time | delayed, typically ~15 min |
| Websocket stream | yes | no |
| Market calendar | authoritative (holidays, half-days) | weekday heuristic only |
| Paper fills | real Alpaca paper account | local simulator |

### Yahoo (no signup)

```bash
MARKET_DATA_PROVIDER=yahoo
MARKET_DATA_TIMEFRAME=intraday
INTRADAY_MINUTES=15
BROKER=paper
```

Yahoo's intraday feed is delayed, so this is intraday but not truly real-time.
It is the right setting for validating the pipeline before committing to a data
provider. Prices are resolved by polling.

### Alpaca (real-time)

```bash
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
MARKET_DATA_PROVIDER=          # blank — auto-selects Alpaca
MARKET_DATA_TIMEFRAME=intraday
INTRADAY_MINUTES=5
STREAMING_ENABLED=true
BROKER=alpaca
```

Keys come from the Alpaca dashboard (alpaca.markets → Paper Trading → API
Keys). With `STREAMING_ENABLED=true` the orchestrator subscribes to 1-minute
bars over a websocket and serves prices from an in-memory cache, so stops are
evaluated against a price that is seconds old rather than one HTTP call away.

### Verify before trading

Unit tests cannot prove that *this host* can reach a data provider. Run the
preflight on the machine that will trade:

```bash
uv run python scripts/verify_intraday.py
uv run python scripts/verify_intraday.py --symbols AAPL,MSFT --stream 30
```

It checks configuration, the market session, that intraday bars really arrive
at the configured resolution, and that prices are fresh enough for the policy
service to accept. It exits non-zero if anything fails.

### Observing the loop

```bash
curl http://localhost:8007/v1/orchestrator/realtime
```

Reports the resolution the loop is actually running at: timeframe, provider,
cycle and risk-check cadence, stream state, and the age of every cached price.
Check this first if trades are being rejected — a `degrading to DAILY bars`
error in the orchestrator log means intraday data could not be fetched and the
strategy is no longer running at intraday resolution.

### How prices are resolved

Each price lookup tries three tiers, freshest first:

1. the websocket stream's in-memory cache (sub-second, Alpaca only)
2. the provider's latest-trade endpoint (one HTTP call)
3. the close of the most recent bar

Every result carries a timestamp. If no tier can supply a price, the strategy
reports the data as **stale** rather than fresh, and the policy service rejects
the trade. The system fails closed: no price means no order.

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
