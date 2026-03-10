AI Trading Stack + AAHP + Codex Build Guide
1. Objective

Build a production-minded AI trading stack that:

separates reasoning from execution

allows safe automation

minimizes LLM token usage

supports Codex-driven development

The architecture must follow one rule:

Strategy proposes
Reasoning explains
Policy approves
Execution places orders

LLMs must never directly place trades.

2. System Architecture
Market Data / News / Fundamentals
        ↓
     Data Service
        ↓
    Feature Service
        ↓
   Strategy Service
        ↓
  Reasoning Service
        ↓
     Policy Gate
        ↓
   Execution Service
        ↓
   Broker / Exchange
        ↓
Monitoring + Dashboard
3. Services
data-service

Handles:

market prices

fundamentals

news feeds

timestamps

symbol normalization

Responsibilities:

ingest market feeds

validate freshness

store timeseries

publish events

feature-service

Builds trading state vectors.

Examples:

RSI
MACD
VWAP
Volatility
Order imbalance
News sentiment
Macro indicators

Output example:

{
  "symbol": "AAPL",
  "timestamp": "2026-03-10T10:00:00Z",
  "features": {
    "rsi": 61,
    "macd": 1.4,
    "volatility": 0.02,
    "news_sentiment": 0.32
  }
}
strategy-service

Produces signals.

Possible engines:

RL agents

ML models

factor models

simple rule systems

Output:

{
  "signal_id": "uuid",
  "symbol": "AAPL",
  "action": "BUY",
  "confidence": 0.71,
  "target_size_pct": 0.02,
  "model_version": "v0.1"
}
reasoning-service

LLM-powered reasoning layer.

Responsibilities:

thesis generation

counter-thesis

event risk detection

explain signals

Example output:

{
 "thesis": "Momentum remains strong after earnings revision.",
 "counter_thesis": "Price extended into macro event risk.",
 "risk_flags": ["earnings_proximity"],
 "recommendation": "ALLOW_WITH_CAUTION"
}

Important rule:

LLM layer cannot place orders
policy-service

Deterministic safety engine.

Checks:

stale data
max position size
portfolio exposure
daily drawdown
symbol allowlist
liquidity threshold
event blackout windows
confidence floor

Example output:

{
 "decision": "REJECT",
 "reasons": ["stale_data"]
}
execution-service

Handles broker communication.

Responsibilities:

submit orders
track order lifecycle
reconcile fills
cancel/replace
audit logging

Order states:

NEW
ACCEPTED
FILLED
PARTIAL_FILL
REJECTED
CANCELLED

Adapters:

paper broker
alpaca
interactive brokers
binance
coinbase
portfolio-service

Tracks:

positions
pnl
exposure
drawdown
trade history
eval-service

Used for:

backtesting
replay simulation
performance attribution
strategy evaluation
drift detection
4. Shared Contracts
SignalCandidate
{
 "signal_id": "uuid",
 "symbol": "AAPL",
 "ts": "ISO8601",
 "candidate_action": "BUY",
 "confidence": 0.72,
 "size_pct": 0.02,
 "horizon": "intraday",
 "source": "strategy-service",
 "model_version": "v0.1"
}
ReasoningMemo
{
 "signal_id": "uuid",
 "thesis": "Momentum remains intact",
 "counter_thesis": "Macro risk tomorrow",
 "risk_flags": ["earnings_proximity"],
 "recommendation": "ALLOW_WITH_CAUTION"
}
PolicyDecision
{
 "signal_id": "uuid",
 "decision": "APPROVE",
 "reasons": [],
 "max_size_pct": 0.01,
 "policy_version": "risk_policy_v1"
}
ExecutionOrder
{
 "order_id": "uuid",
 "symbol": "AAPL",
 "side": "BUY",
 "qty": 10,
 "order_type": "MARKET",
 "time_in_force": "DAY"
}
5. Repository Structure
trading-stack/

 apps/
   dashboard/
   backtest-runner/

 services/
   data-service/
   feature-service/
   strategy-service/
   reasoning-service/
   policy-service/
   execution-service/
   portfolio-service/
   eval-service/

 libs/
   contracts/
   broker-adapters/
   data-adapters/
   feature-lib/
   risk-lib/

 docs/
   architecture.md

 tests/

 .ai/
   handoff/
     manifest.json
     task_briefs/
     summaries/
     decisions/
     prompts/
     checksums/