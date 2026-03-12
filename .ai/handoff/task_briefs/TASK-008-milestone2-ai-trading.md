# TASK-008: Milestone 2 — AI-Driven Real Trading System

## Status: COMPLETE

## Objective

Evolve Trade Pilot from a deterministic paper trading demo (Milestone 1) into a real AI-driven trading system with live market data, Claude-powered signals, Alpaca broker integration, web research, automated scheduling, and a fully interactive dashboard.

## Scope

### New Components
- `libs/market_data` — OHLCV fetching (Alpaca primary, Yahoo Finance fallback) + technical indicators (RSI, MACD, Bollinger Bands, EMA)
- `libs/brokers` — `AlpacaBroker` + `PaperBroker` behind `BrokerInterface`; `get_broker()` auto-selects on `ALPACA_API_KEY`
- `services/research-service` (port 8005) — Claude + `web_search_20250305` tool; 30-min SQLite cache; `POST /v1/research/report`, `GET /v1/research/reports`

### Enhanced Services
- **strategy-service** — `AISignalPipeline` (Claude Haiku for TA), deterministic fallback; `TradeWorker` runs full pipeline loop; `AsyncIOScheduler` every 15 min; market data API endpoints (`/v1/market/quote/{symbol}`, `/v1/market/chart/{symbol}`, `/v1/market/quotes`, `/v1/trade/manual`)
- **policy-service** — Risk-tier routing: HIGH → auto-reject, LOW → auto-approve (skips confidence floor), optional Alpaca clock check
- **execution-service** — Uses `get_broker()` from `libs/brokers`; real fill prices from Alpaca; `GET /v1/account` for wallet balance
- **contracts** — Added `RiskScore`, `TechnicalSummaryContract`, `ResearchReport`, `AccountInfo`, `WorkerStatus`; extended `SignalCandidate` + `PolicyEvaluationRequest`

### Dashboard
- Live price ticker bar (Yahoo Finance / Alpaca, auto-refresh 60s)
- TradingView Lightweight Charts candlestick + EMA overlays
- RSI, MACD, Bollinger indicator panel
- Manual Buy/Sell trade form → policy → execution pipeline
- AI Signal Generator panel
- Wallet panel with PAPER/LIVE mode badge
- Worker status panel with "Run Now" button
- AI Research panel with per-symbol sentiment cards

## Key Decisions
- `ANTHROPIC_API_KEY` absent → deterministic fallback (zero-config preserved)
- `ALPACA_API_KEY` absent → Yahoo Finance data + PaperBroker ($100k simulated)
- `WORKER_ENABLED=false` default — live auto-trading must be explicitly opted in
- `extra="ignore"` on `SignalCandidate` for forward-compatible field additions
- Research service degrades gracefully — returns neutral stub on API failure

## ADRs Produced
- ADR-003: Broker abstraction layer (`libs/brokers`)
- ADR-004: Market data fallback chain (Alpaca → Yahoo Finance)
- ADR-005: Research service isolation and caching
