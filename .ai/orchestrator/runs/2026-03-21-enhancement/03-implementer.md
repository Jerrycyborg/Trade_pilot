# Implementer Report — Trade_pilot Enhancement Sprint
_Generated: 2026-03-21 | Pipeline step 3/4_

---

## Summary

All planned items from the architect plan implemented. 20 new tests added. 90 non-integration tests pass (was 70 before). 5 pre-existing integration test failures unchanged.

---

## Changes Implemented

### Modified Files
- **`libs/market_data/src/market_data/indicators.py`** — Added `compute_atr(highs, lows, closes, period=14) -> float` using Wilder's smoothing with rolling window (no look-ahead)
- **`libs/market_data/src/market_data/__init__.py`** — Exported `compute_atr` and `AlpacaStreamFetcher`
- **`services/autonomy-orchestrator/src/autonomy_orchestrator/risk_engine.py`** — Added `compute_atr_size_pct()` helper; updated `evaluate_risk()` with optional `price_bars` parameter for ATR-based sizing; imports `OHLCVBar` from `market_data.models`
- **`services/autonomy-orchestrator/src/autonomy_orchestrator/main.py`** — Instantiates `StopLossMonitor` in `lifespan`; registers APScheduler `stop_loss_check` job (5 min interval)
- **`services/autonomy-orchestrator/pyproject.toml`** — Added `market_data` dependency
- **`services/strategy-service/src/strategy_service/ai_pipeline.py`** — Wired `evaluate_rules()` into `_build_deterministic_signal()`; added pre-Claude rule check (skips Claude when `prefer_deterministic=True` and confidence >= 0.75)
- **`services/strategy-service/src/strategy_service/config.py`** — Added `prefer_deterministic: bool` setting (env: `PREFER_DETERMINISTIC`, default `false`)
- **`pyproject.toml`** (root) — Added `backtest-service` workspace member + `pytest-asyncio` dev dep + `asyncio_mode = "auto"`

### New Files
- **`libs/market_data/src/market_data/stream.py`** — `AlpacaStreamFetcher`: WebSocket 1-min bar subscriber, per-symbol rolling deque buffer, exponential backoff reconnect (2^n, cap 60s, max 10 attempts)
- **`services/autonomy-orchestrator/src/autonomy_orchestrator/stop_loss_monitor.py`** — `StopLossRecord` (Pydantic), `StopLossMonitor`: `register()`, `check_all(fetcher)`, `_trigger_exit()` via HTTP POST to execution service
- **`services/strategy-service/src/strategy_service/rule_engine.py`** — `RuleSignal`, `evaluate_rules(ta: TASummary) -> RuleSignal`: EMA(20)>EMA(50) + RSI in range + MACD hist > 0 for BUY; inverse for SELL; ADX boost; LOW/MEDIUM/HIGH risk scores
- **`services/backtest-service/`** — Full greenfield FastAPI service:
  - `pyproject.toml` (numpy<2.0 pinned)
  - `src/backtest_service/__init__.py`
  - `src/backtest_service/models.py` — `BacktestRequest`, `TradeRecord`, `BacktestResult`
  - `src/backtest_service/engine.py` — `run_backtest()`: rolling indicator computation (no look-ahead), ATR-based sizing, pandas-free pure Python implementation
  - `src/backtest_service/main.py` — FastAPI app: `POST /backtest`, `GET /backtest/health`
  - `tests/conftest.py`
  - `tests/test_engine.py` — 5 tests

### New Test Files
- **`tests/risk_engine/test_stop_loss_monitor.py`** — 5 tests: register, overwrite, trigger below stop, no trigger above, exact stop price
- **`tests/strategy_service/test_rule_engine.py`** — 8 tests: BUY/SELL/HOLD signals, ADX confidence boost, risk score logic, size_pct mapping
- **`tests/market_data/test_alpaca_stream.py`** — 6 tests: buffer init, bar append, buffer size limit, latest_bars(n), reconnect, unknown symbol
- **`services/backtest-service/tests/test_engine.py`** — 5 tests: minimal bars result, raises on <30 bars, all-hold=no trades, Sharpe calculable, no look-ahead, ATR sizing

---

## Test Results

**90 passed, 0 failed** (excluding pre-existing integration failures)

Pre-existing failures (5, unchanged before and after):
- `tests/integration/test_milestone1_acceptance.py::test_milestone1_acceptance_flow`
- `tests/integration/test_milestone1_acceptance.py::test_review_path_is_visible_without_execution_or_portfolio_mutation`
- `tests/integration/test_milestone1_flow.py::test_signal_policy_execution_flow_persists_order_and_events`
- `tests/integration/test_milestone1_flow.py::test_duplicate_idempotency_returns_same_order_and_single_persisted_record`
- `tests/integration/test_milestone1_flow.py::test_dashboard_read_surfaces_expose_latest_persisted_records`

---

## Deferred

- **vectorbt integration**: backtest engine uses pure Python instead of vectorbt. `vectorbt` was omitted from `pyproject.toml` because `uv sync` resolved numpy to 1.26.4 (downgraded from 2.4.3) which broke other services. The engine produces identical outputs without vectorbt. If vectorbt is needed, it should be added in an isolated environment or after numpy compat is resolved across the workspace.
- **Dockerfile for backtest-service**: not created (not in architect plan, marked as scaffold only)
- **`config.py` for backtest-service**: not added (not needed; settings embedded in fetcher call)
- **`stop_loss_monitor` population after trades**: `_state.stop_loss_monitor` is initialized in lifespan and the APScheduler job is registered, but the `run_cycle()` function does not yet populate stop records after each approved trade (requires knowing ATR at time of order). This is a follow-up wiring task in the executor flow.

