# Architect Plan — Trade_pilot Enhancement Sprint
_Generated: 2026-03-21 | Pipeline step 2/4_

---

## 0. Scanned Baseline

| Asset | Status |
|---|---|
| `services/autonomy-orchestrator/src/autonomy_orchestrator/risk_engine.py` | Uses flat `evaluate_risk()` — no ATR, no stop-loss hook |
| `services/strategy-service/src/strategy_service/ai_pipeline.py` | `_build_deterministic_signal()` is hash-based stub; LLM path is primary |
| `libs/market_data/src/market_data/indicators.py` | Has `compute_rsi`, `compute_ema`, `compute_macd`, `compute_adx`, `compute_bollinger` — **no `compute_atr`** |
| `libs/market_data/src/market_data/fetcher.py` | REST-only (`AlpacaFetcher`); no WebSocket |
| `pyproject.toml` (root) | `alpaca-py>=0.38.0` locked at 0.43.2 — **vectorbt absent** |
| `tests/` | Has `tests/autonomy_orchestrator/test_risk_engine.py`, `tests/risk_engine/`, `tests/strategy_service/`, `tests/market_data/` |

---

## 1. Risk Engine — ATR Sizing + Stop-Loss Monitor

### 1a. `libs/market_data/src/market_data/indicators.py`
Add `compute_atr()` function (no new deps — pure Python):

```python
def compute_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    """Wilder's ATR. Returns mean(H-L) of last `period` bars if insufficient data."""
```

Implementation: true range per bar → Wilder smooth over `period`.
Also export from `libs/market_data/src/market_data/__init__.py`:
```python
from .indicators import compute_atr
__all__ += ["compute_atr"]
```

### 1b. `services/autonomy-orchestrator/src/autonomy_orchestrator/risk_engine.py`
Add `compute_atr_size_pct()` helper and integrate into `evaluate_risk()`:

```python
def compute_atr_size_pct(
    atr: float,
    entry_price: float,
    buying_power: float,
    risk_per_trade_pct: float = 0.01,
    atr_stop_multiplier: float = 2.0,
) -> float:
    """
    Kelly-lite: risk exactly `risk_per_trade_pct` of buying_power per ATR stop.
    stop_distance = atr * atr_stop_multiplier
    shares = (buying_power * risk_per_trade_pct) / stop_distance
    notional = shares * entry_price
    size_pct = notional / buying_power
    Returns 0.0 if atr or entry_price are zero.
    """
```

Update `evaluate_risk()` signature:
```python
def evaluate_risk(
    signal: SignalCandidate,
    portfolio_state: dict[str, object],
    weekly_spend: float,
    config: dict[str, object],
    price_bars: list[OHLCVBar] | None = None,   # NEW — optional, enables ATR sizing
) -> RiskAssessment:
```
Logic: if `price_bars` supplied and len >= 15, override `adjusted_size_pct` with `compute_atr_size_pct(atr, last_close, buying_power, config["risk_per_trade_pct"])`, clamped to `max_position_size_pct`.
Import `OHLCVBar` from `market_data.models`.

New config keys consumed (all optional with defaults):
- `risk_per_trade_pct: float = 0.01`
- `atr_stop_multiplier: float = 2.0`

### 1c. `services/autonomy-orchestrator/src/autonomy_orchestrator/stop_loss_monitor.py` (NEW)

```python
class StopLossRecord(BaseModel):
    symbol: str
    entry_price: float
    stop_price: float          # entry_price - atr * multiplier
    position_id: str
    created_at: datetime

class StopLossMonitor:
    def __init__(self, broker_url: str, internal_key: str) -> None: ...

    def register(self, record: StopLossRecord) -> None:
        """Add or overwrite stop for a symbol."""

    async def check_all(self, fetcher: OHLCVFetcherProtocol) -> list[str]:
        """
        For each tracked position fetch latest close.
        If close <= stop_price, call broker sell endpoint and return triggered symbols.
        Returns list of triggered symbols.
        """

    async def _trigger_exit(self, record: StopLossRecord) -> None:
        """POST /internal/execute with SELL signal. Fire-and-forget with logged error."""
```

APScheduler wiring in `services/autonomy-orchestrator/src/autonomy_orchestrator/main.py`:
- Modify `lifespan` to instantiate `StopLossMonitor` and register job:
```python
scheduler.add_job(
    lambda: asyncio.create_task(_state.stop_loss_monitor.check_all(fetcher)),
    "interval",
    minutes=int(settings.stop_loss_check_interval_minutes),   # default 5
    id="stop_loss_check",
)
```
- `_state.stop_loss_monitor` must be populated after every approved trade.

### 1d. Dependency changes
`services/autonomy-orchestrator/pyproject.toml`: add `market_data` to `dependencies`
(currently missing; risk_engine needs `OHLCVBar`).

### 1e. Tests

| File | Assertions |
|---|---|
| `tests/autonomy_orchestrator/test_risk_engine.py` | `test_atr_sizing_overrides_signal_size` — with 20 price bars, evaluate_risk returns adjusted_size_pct computed from ATR not signal; `test_atr_sizing_clamps_to_max` — ATR size > max_position_size_pct is clamped; `test_no_bars_uses_signal_size` — price_bars=None keeps original signal size |
| `tests/risk_engine/test_stop_loss_monitor.py` (NEW) | `test_register_and_trigger` — mock fetcher returns price below stop, assert _trigger_exit called; `test_no_trigger_above_stop` — price above stop, nothing triggered; `test_overwrite_updates_stop` — registering same symbol twice replaces record |

---

## 2. Strategy Engine — Deterministic EMA/RSI/MACD Rule Engine

### 2a. `services/strategy-service/src/strategy_service/rule_engine.py` (NEW)

```python
from dataclasses import dataclass
from market_data.models import TASummary

@dataclass
class RuleSignal:
    action: str          # "BUY" | "SELL" | "HOLD"
    confidence: float
    risk_score: str      # "LOW" | "MEDIUM" | "HIGH"
    reasoning: str
    size_pct: float

def evaluate_rules(ta: TASummary, config: dict | None = None) -> RuleSignal:
    """
    Deterministic Dual-EMA Momentum + RSI + MACD strategy.

    BUY conditions (all must hold):
      - ema_20 > ema_50  (bullish trend)
      - rsi_14 > 45 and rsi_14 < 70  (momentum, not overbought)
      - macd_histogram > 0  (momentum confirming)

    SELL conditions (all must hold):
      - ema_20 < ema_50  (bearish trend)
      - rsi_14 < 55 and rsi_14 > 30  (momentum, not oversold)
      - macd_histogram < 0

    HOLD: everything else.

    confidence: base 0.65; +0.10 if adx > 25 (trending); +0.05 if no conflicting signals.
    risk_score: LOW if adx>25 and 45<rsi<65; HIGH if rsi>70 or rsi<30; else MEDIUM.
    size_pct: LOW->0.02, MEDIUM->0.015, HIGH->0.005
    """
```

### 2b. `services/strategy-service/src/strategy_service/ai_pipeline.py`
Modify `_build_deterministic_signal()` to call `evaluate_rules()`:
- Replace hash-based logic with `evaluate_rules(ta_summary)` call.
- Modify `AISignalPipeline.generate()`: before calling Claude, run `evaluate_rules()`. If `rule_signal.confidence >= 0.75` and `settings.prefer_deterministic`, skip Claude and use rule signal directly.

New config key in `services/strategy-service/src/strategy_service/config.py`:
```python
prefer_deterministic: bool = Field(default=False, alias="PREFER_DETERMINISTIC")
```

### 2c. Tests

| File | Assertions |
|---|---|
| `tests/strategy_service/test_rule_engine.py` (NEW) | `test_buy_signal_all_conditions_met` — ema20>ema50, rsi=55, macd_hist>0 -> BUY; `test_sell_signal_all_conditions_met` — inverse -> SELL; `test_hold_when_rsi_overbought` — rsi=75 -> HOLD; `test_adx_boosts_confidence` — adx=30 -> confidence>=0.75; `test_risk_score_low_when_trending_rsi_mid` — adx>25, rsi=55 -> LOW |

No dependency changes needed (market_data already a dep of strategy-service).

---

## 3. Market Data — Alpaca WebSocket Stream

### 3a. `libs/market_data/src/market_data/stream.py` (NEW)

```python
import asyncio
import logging
from collections import deque
from typing import Callable, Awaitable
from alpaca.data.live import StockDataStream, CryptoDataStream
from .models import OHLCVBar
from .config import MarketDataSettings

logger = logging.getLogger(__name__)
BarCallback = Callable[[OHLCVBar], Awaitable[None]]

class AlpacaStreamFetcher:
    """
    WebSocket bar subscriber for real-time 1-min bars.
    Maintains in-memory rolling buffer per symbol.
    Reconnects automatically on disconnect (exponential backoff, cap 60s).
    """

    def __init__(
        self,
        settings: MarketDataSettings,
        symbols: list[str],
        on_bar: BarCallback,
        buffer_size: int = 200,
        max_reconnect_attempts: int = 10,
    ) -> None: ...

    async def start(self) -> None:
        """Connect and subscribe. Blocks until stop() called."""

    async def stop(self) -> None:
        """Graceful shutdown."""

    def latest_bars(self, symbol: str, n: int = 60) -> list[OHLCVBar]:
        """Return last n buffered bars for symbol (oldest-first)."""

    async def _connect(self) -> None: ...

    async def _handle_bar(self, bar: object) -> None:
        """Convert Alpaca bar object -> OHLCVBar, push to buffer, call on_bar."""

    async def _reconnect_loop(self) -> None:
        """Exponential backoff reconnect: 2, 4, 8 ... 60s."""
```

Export from `libs/market_data/src/market_data/__init__.py`:
```python
from .stream import AlpacaStreamFetcher
__all__ += ["AlpacaStreamFetcher"]
```

### 3b. Tests

| File | Assertions |
|---|---|
| `tests/market_data/test_alpaca_stream.py` (NEW) | `test_bar_appended_to_buffer` — mock Alpaca bar event -> latest_bars() returns it; `test_buffer_size_limit` — inserting 250 bars into buffer_size=200 -> len==200; `test_reconnect_on_disconnect` — simulate disconnect, assert _connect called twice; `test_latest_bars_returns_n` — 60 bars buffered, latest_bars(n=10) returns last 10 |

No new deps — alpaca-py already in market_data pyproject.toml.

---

## 4. Backtest Service

### 4a. New service scaffold

```
services/backtest-service/
  Dockerfile
  pyproject.toml
  src/backtest_service/
    __init__.py
    config.py
    models.py
    engine.py
    main.py
  tests/
    test_engine.py
```

### 4b. `services/backtest-service/pyproject.toml`

```toml
[project]
name = "backtest-service"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "contracts",
  "market_data",
  "fastapi>=0.115.0",
  "uvicorn>=0.34.0",
  "vectorbt>=0.26.0",
  "pandas>=2.0.0",
  "numpy>=1.26.0",
]
```

WARNING — vectorbt numpy conflict: vectorbt 0.26 supports numpy 1.x. Root uv.lock has numpy>=1.26.0.
Run `uv add vectorbt --dry-run` to verify resolution before committing.
If conflict: pin `numpy<2.0` in backtest-service pyproject only.

Add to root `pyproject.toml` workspace members:
```
"services/backtest-service",
```

### 4c. `services/backtest-service/src/backtest_service/models.py`

```python
from pydantic import BaseModel, Field
from datetime import datetime

class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "ema_rsi_macd"   # extensible
    period_days: int = Field(default=180, ge=30, le=730)
    initial_capital: float = Field(default=100_000.0, gt=0)
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.05)
    atr_stop_multiplier: float = Field(default=2.0, gt=0)
    commission_pct: float = Field(default=0.001)

class TradeRecord(BaseModel):
    entry_date: datetime
    exit_date: datetime
    symbol: str
    action: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float

class BacktestResult(BaseModel):
    symbol: str
    strategy: str
    period_days: int
    initial_capital: float
    final_value: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    win_rate: float
    trades: list[TradeRecord]
    generated_at: datetime
```

### 4d. `services/backtest-service/src/backtest_service/engine.py`

```python
def run_backtest(request: BacktestRequest, bars: list[OHLCVBar]) -> BacktestResult:
    """
    Build signal series using same EMA/RSI/MACD rules as rule_engine (no look-ahead):
    - Compute indicators on bars[:i] for each bar i (rolling, not full-series).
    - Entry: rule_engine signal == BUY on bar i -> buy at bar i+1 open.
    - Exit: rule_engine signal == SELL or stop triggered -> sell at bar i+1 open.
    - Sizing: ATR-based (compute_atr on bars[:i]).
    - Uses vectorbt Portfolio.from_signals() for stats.

    Returns BacktestResult. Raises ValueError if <30 bars.
    """
```

Anti-look-ahead contract: indicators computed only on `bars[:i+1]` slice at each step.

### 4e. `services/backtest-service/src/backtest_service/main.py`

```python
# FastAPI app with two endpoints:

POST /backtest
    body: BacktestRequest
    -> fetches bars via AlpacaFetcher(period_days=request.period_days)
    -> calls run_backtest()
    -> returns BacktestResult

GET /backtest/health
    -> {"status": "ok"}
```

### 4f. Tests

| File | Assertions |
|---|---|
| `services/backtest-service/tests/test_engine.py` | `test_returns_result_for_minimal_bars` — 35 synthetic bars -> BacktestResult with valid fields; `test_no_lookahead` — inject future-only-winning bars, assert strategy cannot see future; `test_atr_sizing_used` — verify trades sized by ATR not fixed pct; `test_all_hold_returns_zero_trades` — flat price -> no trades |

---

## 5. File Change Summary

### Modified files
| Path | Change |
|---|---|
| `libs/market_data/src/market_data/indicators.py` | Add `compute_atr()` |
| `libs/market_data/src/market_data/__init__.py` | Export `compute_atr`, `AlpacaStreamFetcher` |
| `services/autonomy-orchestrator/src/autonomy_orchestrator/risk_engine.py` | Add `compute_atr_size_pct()`, update `evaluate_risk()` signature |
| `services/autonomy-orchestrator/src/autonomy_orchestrator/main.py` | Wire `StopLossMonitor` into lifespan + APScheduler |
| `services/autonomy-orchestrator/pyproject.toml` | Add `market_data` dependency |
| `services/strategy-service/src/strategy_service/ai_pipeline.py` | Replace hash fallback with `evaluate_rules()`; add pre-Claude rule check |
| `services/strategy-service/src/strategy_service/config.py` | Add `prefer_deterministic` setting |
| `pyproject.toml` (root) | Add `backtest-service` workspace member |

### New files
| Path | Purpose |
|---|---|
| `libs/market_data/src/market_data/stream.py` | `AlpacaStreamFetcher` WebSocket client |
| `services/autonomy-orchestrator/src/autonomy_orchestrator/stop_loss_monitor.py` | Client-side stop-loss polling |
| `services/strategy-service/src/strategy_service/rule_engine.py` | Deterministic EMA+RSI+MACD strategy |
| `services/backtest-service/` (full scaffold) | vectorbt backtest service |
| `tests/risk_engine/test_stop_loss_monitor.py` | Stop-loss monitor unit tests |
| `tests/strategy_service/test_rule_engine.py` | Rule engine unit tests |
| `tests/market_data/test_alpaca_stream.py` | Stream buffer + reconnect tests |
| `services/backtest-service/tests/test_engine.py` | Backtest engine unit tests |

---

## 6. Deferred (not this sprint)

- Dashboard / UI for backtest results
- eToro server-side stop-loss API (API may not support it)
- Multi-symbol backtesting / portfolio-level optimizer
- Crypto symbol mapping (eToro vs Alpaca ID mismatch — tracked as risk)
- Live trading path changes
- Polygon.io fallback for streaming
- Persistent backtest result storage (DB)
