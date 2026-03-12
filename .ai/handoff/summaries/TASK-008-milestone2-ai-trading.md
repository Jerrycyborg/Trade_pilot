# TASK-008 Summary: Milestone 2 — AI-Driven Real Trading System

## Completed: 2026-03-12

## What Was Built

### New Libraries
| Path | Purpose |
|---|---|
| `libs/market_data/` | OHLCV fetching + RSI/MACD/Bollinger/EMA computation |
| `libs/brokers/` | `AlpacaBroker` (paper/live) + `PaperBroker` + `get_broker()` factory |

### New Service
| Path | Port | Purpose |
|---|---|---|
| `services/research-service/` | 8005 | Claude + web search, per-symbol research, 30-min cache |

### Files Modified
- `libs/contracts/src/contracts/models.py` — `RiskScore`, `TechnicalSummaryContract`, `ResearchReport`, `AccountInfo`, `WorkerStatus`; extended `SignalCandidate` (risk_score, ta_summary, research_summary) and `PolicyEvaluationRequest` (risk_score); relaxed `extra="forbid"` → `extra="ignore"` on `SignalCandidate`
- `services/strategy-service/` — `AISignalPipeline`, `TradeWorker`, `AsyncIOScheduler`, market data API, manual trade endpoint
- `services/policy-service/src/policy_service/rules.py` — Risk-tier routing (HIGH→REJECT, LOW→APPROVE)
- `services/execution-service/src/execution_service/main.py` — Real fill prices, `GET /v1/account`
- `apps/dashboard/index.html` — Ticker bar, chart panel, manual trade form, worker button
- `apps/dashboard/mvp.js` — TradingView chart, ticker, trade/signal handlers
- `apps/dashboard/styles.css` — All new component styles
- `Makefile` — Added `run-research`, `run-dashboard` targets
- `pyproject.toml` — Added all new workspace members

## Test Results
- 37 tests passing, 7 skipped (live service integration tests)
- Existing Milestone 1 acceptance tests preserved

## Runtime Verification (2026-03-12)
- All 5 services healthy on ports 8001–8005
- Dashboard served on port 8080
- Real AAPL quote: $254.84, RSI 42.31, trend neutral (Yahoo Finance)
- Manual trade BUY 5 MSFT: ACCEPTED, policy APPROVE
- End-to-end pipeline: signal → policy → execution → fill → portfolio reconciliation

## Env Var Reference
| Variable | Default | Required For |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | AI signals + research |
| `ALPACA_API_KEY` | none | Real market data + Alpaca trading |
| `ALPACA_SECRET_KEY` | none | Paired with API key |
| `ALPACA_PAPER` | `true` | Set `false` for live money |
| `WORKER_ENABLED` | `false` | Auto-trade scheduler |
| `WORKER_INTERVAL_MINUTES` | `15` | Scheduler cadence |
| `STRATEGY_WATCHLIST` | `AAPL,MSFT,GOOGL,BTC/USD,ETH/USD` | Symbols to trade |
| `RESEARCH_CLAUDE_MODEL` | `claude-opus-4-6` | Research model |
| `STRATEGY_CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | TA analysis model |
