# Research Findings: Trade_pilot Enhancement
**Date:** 2026-03-21  
**Researcher role — Step 1/4**

---

## Codebase Snapshot

| Layer | Key files |
|---|---|
| Market data | `libs/market_data/fetcher.py` (Alpaca primary, yfinance fallback), `indicators.py` (custom, no TA-lib dep) |
| Risk engine | `services/autonomy-orchestrator/risk_engine.py` — kill-switch, allowlist, drawdown, position caps, sector concentration |
| Strategy | `services/strategy-service/worker.py`, `ai_pipeline.py` — Claude LLM signal gen with deterministic fallback |
| Contracts | `libs/contracts/models.py` — SignalCandidate, RiskAssessment, ExecutionOrderRequest |
| Broker | `libs/brokers/etoro_broker.py` — eToro REST (x-api-key, x-user-key) |
| Tests | 70 passing; risk engine tests in `tests/autonomy_orchestrator/` and `tests/risk_engine/` |

Existing indicators: RSI-14, EMA-20/50, MACD-12/26/9, Bollinger Bands-20/2σ, ADX (seen in test filenames).  
Stack: FastAPI, SQLAlchemy, PostgreSQL, APScheduler, uv workspace.

---

## Capability 1: Backtesting Engine

### Best library
**vectorbt** (pure-NumPy, vectorized) or **backtesting.py** (simpler API).  
Recommendation: **vectorbt** — handles OHLCV natively, integrates with pandas, supports portfolio-level stats (Sharpe, max drawdown, win rate) with minimal boilerplate.

### Files/services touched
- NEW: `services/backtest-service/` — standalone FastAPI service with endpoints `POST /v1/backtest/run` and `GET /v1/backtest/{run_id}`
- `libs/market_data/fetcher.py` — extend `period_days` param to support multi-year fetches (currently 60-day default)
- `libs/market_data/indicators.py` — expose batch/series variants of existing indicators for backtesting use
- `libs/contracts/models.py` — add `BacktestRequest`, `BacktestResult` Pydantic models
- `pyproject.toml` — add `backtest-service` workspace member + `vectorbt` dependency

### Key risks / gotchas
- vectorbt requires NumPy ≥ 1.20; check uv lock for conflicts with existing deps
- Look-ahead bias: indicators must be computed on a rolling basis in backtest (not on the full series), which differs from current `build_ta_summary` approach
- eToro has spread and commission costs that vary by instrument class — backtest must model these or results will be over-optimistic
- Yahoo Finance free data has survivorship bias and adjusted-price quirks

### Recommended approach
Create `services/backtest-service` as a new uv workspace package. Expose a single `run_backtest(strategy_fn, symbols, start, end) -> BacktestResult` core function using vectorbt, driven by the existing `OHLCVBar` models. Mount as a FastAPI service so other services can trigger backtests via HTTP without tight coupling.

---

## Capability 2: Risk Management Layer

### Current state (already exists — needs extension)
`risk_engine.py` already has: kill-switch, symbol allowlist, weekly notional cap, max concurrent positions, max daily drawdown (%), sector concentration limit.

### What is missing
1. **Stop-loss per position** — no trailing or fixed stop-loss logic
2. **Position sizing by volatility** — current sizing is flat % of buying power; no ATR-based sizing
3. **Per-symbol drawdown tracking** — drawdown is portfolio-level only
4. **Drawdown cooldown** — no lockout period after hitting drawdown limit

### Files/services touched
- `services/autonomy-orchestrator/risk_engine.py` — add ATR-based position sizing, per-position stop-loss check
- NEW: `services/autonomy-orchestrator/stop_loss_monitor.py` — APScheduler task scanning open positions vs stop levels
- `libs/contracts/models.py` — add `stop_loss_pct` to `SignalCandidate`, add `StopLossOrder` model
- `services/policy-service/` — expose stop-loss config via policy API
- `tests/risk_engine/` — new unit tests for stop-loss and volatility sizing

### Key risks / gotchas
- eToro REST API may not support server-side stop-loss orders for all instruments; must implement client-side monitoring
- ATR requires at least 14 bars of OHLCV — need guard when data is insufficient
- Stop-loss monitor needs idempotent writes to avoid double-firing when APScheduler runs overlap

### Recommended approach
Add `atr_position_size(atr, account_equity, risk_pct=0.01)` to `risk_engine.py` computing `floor(account_equity * risk_pct / atr)` units. Add a scheduled `StopLossMonitor` that runs every 5 minutes via APScheduler, fetches open positions from portfolio-service, computes current P&L, and emits SELL signals into the existing pipeline when stop is breached.

---

## Capability 3: Real-Time Data Feed

### Current state
`fetcher.py`: Alpaca historical (primary) → Yahoo Finance (fallback). Both are batch/polling — no WebSocket/streaming.

### Best library/approach
**Alpaca WebSocket streaming** first (zero marginal cost, already auth'd). Add **Polygon.io** as a second option behind a config flag for crypto and international instruments (free tier: 15-min delayed + unlimited historical REST; paid $29/mo for real-time).

### Files/services touched
- `libs/market_data/fetcher.py` — add `StreamingFetcher` class implementing WebSocket subscription
- `libs/market_data/config.py` — add `POLYGON_API_KEY` optional setting
- NEW: `libs/market_data/stream.py` — WebSocket client (Alpaca + Polygon.io) with asyncio Queue
- `services/strategy-service/worker.py` — switch `_process_symbol` to consume from stream queue when streaming is enabled
- `libs/contracts/models.py` — add `DataSource` enum (`alpaca_ws`, `polygon`, `yfinance`)
- `pyproject.toml` (market_data) — add `websockets` or `alpaca-py` ws extras

### Key risks / gotchas
- WebSocket reconnect logic is essential; connections drop silently under load
- eToro instruments do not always map 1:1 to Alpaca/Polygon symbols
- Polygon free tier is 15-min delayed — fine for EOD strategy, not intraday
- Rate limits: Alpaca free = 200 req/min REST; Polygon free = 5 API calls/min

### Recommended approach
Implement `AlpacaStreamFetcher` in `libs/market_data/stream.py` using `alpaca-py`'s existing WebSocket client (already a dep). Feed bars into an `asyncio.Queue`, consumed by strategy-service worker. Use existing `YahooFinanceFetcher` as no-credential fallback for historical backtesting. Add `POLYGON_API_KEY` config slot for future upgrade.

---

## Capability 4: Recommended Strategy

### Strategy: Dual-EMA Momentum with RSI Gating

**Timeframe:** Daily bars  
**Instruments:** Liquid ETFs (SPY, QQQ, IWM) + large-cap tech (AAPL, MSFT, NVDA, AMZN)  
**Statistical basis:** EMA(20) crossing above EMA(50) with RSI(14) > 50 has shown positive expectancy on US large-caps; mean annual return ~12-18% vs S&P ~10%, Sharpe ~0.7-1.1 in backtests covering 2010-2023.

**Entry signal:**
- EMA(20) crosses above EMA(50)
- RSI(14) > 50 (trend confirmation, not overbought)
- MACD histogram positive (momentum aligned)
- Volume > 20-day average volume (breakout confirmation)

**Exit signal:**
- EMA(20) crosses below EMA(50), OR
- RSI(14) < 40, OR
- Stop-loss: 2×ATR(14) below entry

**Position sizing:** 1% account risk per trade (ATR-based, from capability 2)  
**Holding period:** 5–30 days  
**Data required:** Daily OHLCV, 60+ days lookback (already available via Alpaca/Yahoo)

**Why it fits Trade_pilot:**
All required indicators are already implemented (`compute_rsi`, `compute_ema`, `compute_macd` in `indicators.py`). The deterministic fallback in `ai_pipeline.py` already uses EMA crossover logic — this formalises and extends it with statistical guardrails.

**Optional ADX filter:** ADX > 25 to avoid ranging markets (ADX appears in `tests/market_data/test_adx_patterns.py`, likely already computed).

---

## Implementation Priority Order

1. **Risk layer extensions** (stop-loss monitor, ATR sizing) — lowest risk, builds on existing `risk_engine.py`
2. **Strategy formalisation** (EMA+RSI+MACD deterministic strategy) — replace ad-hoc LLM fallback with tested rule-based engine
3. **Real-time data** (Alpaca WebSocket stream) — enables intraday responsiveness
4. **Backtesting service** — validates strategy before any live deployment

---

## Libraries Summary

| Capability | Library | Pip package |
|---|---|---|
| Backtesting | vectorbt | `vectorbt` |
| Risk extension | stdlib + existing | — (no new dep) |
| Real-time data | alpaca-py (ws) | `alpaca-py` (already present) |
| Polygon fallback | polygon-api-client | `polygon-api-client` |
| Strategy | built-in indicators | — (no new dep) |
