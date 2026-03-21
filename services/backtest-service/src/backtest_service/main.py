"""FastAPI application for backtest service."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from market_data import MarketDataSettings, get_fetcher
from market_data.fetcher import DataUnavailableError

from .engine import run_backtest
from .models import BacktestRequest, BacktestResult

logger = logging.getLogger(__name__)

app = FastAPI(title="backtest-service", version="0.1.0")


@app.get("/backtest/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/backtest", response_model=BacktestResult)
async def backtest(request: BacktestRequest) -> BacktestResult:
    """Run a backtest for the given symbol and strategy."""
    settings = MarketDataSettings()
    fetcher = get_fetcher(settings)
    try:
        bars = fetcher.fetch(request.symbol, period_days=request.period_days)
    except DataUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Market data unavailable: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if len(bars) < 30:
        raise HTTPException(
            status_code=422,
            detail=f"Insufficient data: {len(bars)} bars returned, need at least 30",
        )

    try:
        result = run_backtest(request, bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return result
