"""FastAPI application for backtest service."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from market_data import MarketDataSettings
from market_data.fetcher import DataUnavailableError, get_fetcher
from market_data.models import OHLCVBar

from .engine import MIN_WARMUP_BARS, run_backtest, run_cost_sensitivity
from .models import BacktestRequest, BacktestResult, CostSensitivityResult

logger = logging.getLogger(__name__)

app = FastAPI(title="backtest-service", version="0.2.0")


@app.get("/backtest/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def load_bars(request: BacktestRequest) -> list[OHLCVBar]:
    """Fetch bars at the timeframe the request asks for.

    The timeframe is taken from the request, not from MARKET_DATA_TIMEFRAME:
    backtesting an intraday strategy must not depend on how the live loop
    happens to be configured at the time.
    """
    settings = MarketDataSettings()
    fetcher = get_fetcher(settings)

    if request.timeframe == "intraday":
        bars = fetcher.fetch_intraday(
            request.symbol,
            period_days=request.period_days,
            timeframe_minutes=request.intraday_minutes,
        )
        if not bars:
            raise DataUnavailableError(
                f"No intraday bars for {request.symbol}. Providers cap intraday "
                f"history — Yahoo serves 1-minute bars for 7 days and most other "
                f"resolutions for 60."
            )
        return bars

    return fetcher.fetch(request.symbol, period_days=request.period_days)


def _bars_or_422(request: BacktestRequest) -> list[OHLCVBar]:
    try:
        bars = load_bars(request)
    except DataUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Market data unavailable: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if len(bars) < MIN_WARMUP_BARS + 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Insufficient data: {len(bars)} bars returned, need at least "
                f"{MIN_WARMUP_BARS + 1} for indicator warm-up. Increase period_days "
                f"or use a smaller intraday_minutes."
            ),
        )
    return bars


@app.post("/backtest", response_model=BacktestResult)
async def backtest(request: BacktestRequest) -> BacktestResult:
    """Run a backtest for the given symbol, timeframe and cost assumptions."""
    bars = _bars_or_422(request)
    try:
        return run_backtest(request, bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/backtest/cost-sensitivity", response_model=CostSensitivityResult)
async def cost_sensitivity(request: BacktestRequest) -> CostSensitivityResult:
    """Re-run across a ladder of spreads to find where the edge disappears."""
    bars = _bars_or_422(request)
    try:
        return run_cost_sensitivity(request, bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
