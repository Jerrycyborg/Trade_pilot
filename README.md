![Trade Pilot banner](assets/trade-pilot-banner.svg)

# Trade Pilot

Production-minded AI trading stack: strategy proposes → policy approves → execution fills → portfolio reconciles. Milestone 2 adds real AI signal generation (Claude), live market data, Alpaca broker integration, web research, and an automated trade worker.

Suggested GitHub description:
`AI-driven trading stack with Claude-powered signals, Alpaca integration, live charts, manual trading UI, and fill-driven portfolio reconciliation.`

Suggested GitHub topics:
`ai-trading`, `algorithmic-trading`, `fastapi`, `python`, `alpaca`, `anthropic`, `claude`, `portfolio-management`, `risk-management`

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                        Trade Pilot                            │
│                                                               │
│  research-service ──► strategy-service ──► policy-service    │
│       (Claude)          (AI signals)       (risk rules)      │
│                                │                              │
│                         execution-service                     │
│                         (Alpaca / paper)                      │
│                                │                              │
│                        portfolio-service                      │
│                        (fill-driven PnL)                      │
│                                                               │
│                     dashboard (port 8080)                     │
│              charts · ticker · manual trades · AI research    │
└───────────────────────────────────────────────────────────────┘
```

**Core flow:**
1. Research service fetches web news + fundamentals via Claude with `web_search` tool
2. Strategy service generates AI signals (Claude Haiku for TA analysis) with risk scoring (LOW / MEDIUM / HIGH)
3. Policy service approves / reviews / rejects based on risk tier and hard rules
4. Execution service places orders via Alpaca (paper or live) or paper broker fallback
5. Portfolio service derives positions and PnL from fills only (ADR-002)
6. Trade worker runs the full pipeline on a configurable schedule

**ADR boundaries preserved:**
- ADR-001: Only execution-service places orders
- ADR-002: Portfolio state derived exclusively from fills

## Services

| Service | Port | Purpose |
|---|---|---|
| `libs/contracts` | — | Shared Pydantic contracts |
| `libs/market_data` | — | OHLCV fetching + technical indicators (RSI, MACD, Bollinger, EMA) |
| `libs/brokers` | — | AlpacaBroker + PaperBroker behind common interface |
| `services/research-service` | 8005 | Claude + web search per-symbol research with 30-min cache |
| `services/strategy-service` | 8003 | AI signal generation, market data API, trade worker/scheduler |
| `services/policy-service` | 8001 | Risk-tier routing + hard reject rules |
| `services/execution-service` | 8002 | Order placement, fills, idempotency, account balance |
| `services/portfolio-service` | 8004 | Positions, snapshots, PnL reconciliation |
| `apps/dashboard` | 8080 | Live charts, ticker bar, manual trades, AI research |

## Quick Start

### Prerequisites
- Python 3.11+
- `uv` package manager

### 1. Install
```bash
uv sync --all-packages --group dev
```

### 2. Configure (optional — all have safe defaults)
```bash
cp .env.example .env
# Edit .env with your API keys
```

Key environment variables:
```bash
ANTHROPIC_API_KEY=sk-ant-...     # enables AI signals + web research
ALPACA_API_KEY=PK...             # enables real market data + trading
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true                # paper trading (default; set false for live)
WORKER_ENABLED=true              # enables 15-min auto-trade loop
STRATEGY_WATCHLIST=AAPL,MSFT,GOOGL,BTC/USD,ETH/USD
```

### 3. Start all services
```bash
# Terminal 1-5 (or use a process manager)
make run-research     # port 8005
make run-strategy     # port 8003
make run-policy       # port 8001
make run-execution    # port 8002
make run-portfolio    # port 8004

# Dashboard
python3 -m http.server 8080 --directory apps/dashboard
```

Then open **http://localhost:8080**

### 4. Without API keys (zero-config mode)
All services work without any API keys:
- Signals use deterministic hash algorithm (Milestone 1 fallback)
- Market data uses Yahoo Finance (free)
- Orders go through PaperBroker ($100k simulated balance)
- Research returns neutral stubs

## Dashboard Features

- **Live Ticker Bar** — real-time prices with % change (Yahoo Finance / Alpaca)
- **Price Chart** — TradingView Lightweight Charts candlestick + EMA-20/50 overlays
- **Technical Indicators** — RSI, MACD, Bollinger Bands shown below chart
- **Manual Trade Panel** — BUY / SELL form → policy check → execution
- **AI Signal Generator** — generate and preview signals per symbol
- **Wallet Panel** — buying power, equity, cash + PAPER/LIVE mode badge
- **Worker Status** — last/next run, "Run Now" button
- **AI Research Panel** — per-symbol sentiment, headlines, risk factors
- **Lifecycle Drill-down** — full signal → policy → order → fill → position chain

## Development

```bash
make lint       # ruff check
make test       # pytest (37 passing, 7 skipped for live services)
make setup      # uv sync all packages
```

Service-specific make targets:
```bash
make run-research
make run-strategy
make run-policy
make run-execution
make run-portfolio
```

## Repository Layout

```text
libs/
  contracts/          Shared Pydantic models (SignalCandidate, FillRecord, etc.)
  market_data/        OHLCV + technical indicators library
  brokers/            AlpacaBroker + PaperBroker

services/
  research-service/   Claude web research (port 8005)
  strategy-service/   AI signals + market data API + trade worker (port 8003)
  policy-service/     Risk-tier routing + hard rules (port 8001)
  execution-service/  Order placement + fills + account (port 8002)
  portfolio-service/  Positions + PnL reconciliation (port 8004)

apps/
  dashboard/          Live trading dashboard (serve on port 8080)

tests/
.ai/handoff/          AAHP task briefs, summaries, ADRs, checksums
```

## AAHP Handoff

Multi-agent handoff structure for maintainability:
```bash
python3 tools/aahp.py validate-manifest
python3 tools/aahp.py generate-checksums
```

## Status

**Milestone 2 (complete):**
- Claude-powered AI signal generation with TA + fundamental research
- Alpaca Markets broker integration (paper + live toggle)
- Real OHLCV market data via Alpaca / Yahoo Finance fallback
- Technical indicators: RSI, MACD, Bollinger Bands, EMA
- Research service: Claude + `web_search_20250305` tool, 30-min cache
- Risk-tier policy routing (LOW auto-approve, HIGH auto-reject)
- Automated trade worker with APScheduler (15-min intervals)
- Live ticker bar, price charts, manual trade UI
- Wallet panel with real Alpaca account balance

**Milestone 1 (complete):**
- Shared contracts, service boundaries, paper broker
- Fill-driven portfolio reconciliation with PnL
- Execution events audit stream
- Refresh-based operator dashboard

**Out of scope:**
- Options or margin logic
- Real-time WebSocket streaming
- Authentication / multi-user
- Advanced order types (stop-loss, trailing stop)
