"""FastAPI application for backtest service."""

from __future__ import annotations

import logging

from contracts.auth import verify_internal_key
from fastapi import Depends, FastAPI, HTTPException
from market_data import MarketDataSettings
from market_data.fetcher import DataUnavailableError, get_fetcher
from market_data.models import OHLCVBar
from pydantic import Field

from .engine import MIN_WARMUP_BARS, run_backtest, run_cost_sensitivity
from .models import (
    BacktestRequest,
    BacktestResult,
    CostSensitivityResult,
    ParameterGrid,
    ParameterSensitivityResult,
    PortfolioResult,
    WalkForwardResult,
)
from .portfolio import ALLOCATIONS, build_sleeves, run_portfolio
from .strategies import REGISTRY, strategy_names
from .validation import parameter_sensitivity, walk_forward

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
async def backtest(
    request: BacktestRequest,
    _: None = Depends(verify_internal_key),
) -> BacktestResult:
    """Run a backtest for the given symbol, timeframe and cost assumptions."""
    bars = _bars_or_422(request)
    try:
        return run_backtest(request, bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/backtest/cost-sensitivity", response_model=CostSensitivityResult)
async def cost_sensitivity(
    request: BacktestRequest,
    _: None = Depends(verify_internal_key),
) -> CostSensitivityResult:
    """Re-run across a ladder of spreads to find where the edge disappears."""
    bars = _bars_or_422(request)
    try:
        return run_cost_sensitivity(request, bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ValidationRequest(BacktestRequest):
    """A backtest request plus the search to run over it."""

    grid: ParameterGrid = Field(default_factory=ParameterGrid)
    n_splits: int = Field(default=4, ge=1, le=20)
    embargo_bars: int | None = Field(default=None, ge=0)
    """Bars dropped between each training window and the test window that
    follows it. Defaults to the indicator warm-up length."""
    objective: str = Field(default="sharpe", pattern="^(sharpe|return|profit_factor)$")


@app.post("/backtest/walk-forward", response_model=WalkForwardResult)
async def walk_forward_endpoint(
    request: ValidationRequest,
    _: None = Depends(verify_internal_key),
) -> WalkForwardResult:
    """Choose parameters on past data, judge them on the data that followed.

    Read `sharpe_degradation` and `deflated_sharpe_ratio` before anything else.
    A large drop from in-sample to out-of-sample, or a deflated ratio below
    0.95, means the result is a description of this sample rather than evidence
    about the next one.
    """
    bars = _bars_or_422(request)
    try:
        return walk_forward(
            request,
            bars,
            grid=request.grid,
            n_splits=request.n_splits,
            embargo_bars=request.embargo_bars,
            objective=request.objective,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/backtest/parameter-sensitivity", response_model=ParameterSensitivityResult)
async def parameter_sensitivity_endpoint(
    request: ValidationRequest,
    _: None = Depends(verify_internal_key),
) -> ParameterSensitivityResult:
    """Score the whole grid and report the shape of the surface.

    In-sample by design: the question is whether the best configuration sits on
    a plateau or a spike. A spike is a fit, whatever its Sharpe says.
    """
    bars = _bars_or_422(request)
    try:
        return parameter_sensitivity(request, bars, grid=request.grid)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/backtest/strategies")
def list_strategies() -> dict[str, object]:
    """The strategies this service can run, and what each one bets on."""
    return {
        "strategies": [
            {
                "name": strategy.name,
                "description": strategy.description,
                "parameters": list(strategy.param_fields),
            }
            for strategy in (REGISTRY[name] for name in strategy_names())
        ]
    }


class PortfolioRequest(BacktestRequest):
    """Run several strategies over several symbols and combine them.

    `symbol` is inherited from BacktestRequest and ignored here; `symbols` is
    what gets traded.
    """

    symbol: str = "PORTFOLIO"
    symbols: list[str] = Field(min_length=1)
    strategies: list[str] = Field(default_factory=lambda: list(strategy_names()))
    allocation: str = Field(default="equal", pattern="^(equal|inverse_volatility)$")
    considered_count: int | None = Field(default=None, ge=1)
    """How many (symbol, strategy) combinations you actually looked at before
    settling on this list. Defaults to the number of sleeves, which is only
    correct if you never dropped any — screening fifty symbols and running the
    best three is a fifty-trial search, and the deflated Sharpe ratio can only
    price that in if you say so."""


@app.post("/backtest/portfolio", response_model=PortfolioResult)
async def portfolio_endpoint(
    request: PortfolioRequest,
    _: None = Depends(verify_internal_key),
) -> PortfolioResult:
    """Simulate each sleeve, combine them, and report whether combining helped.

    Read `max_correlation` and `diversification_ratio` before the return. Two
    sleeves correlating near 1.0 are one position paying two sets of costs.
    """
    for name in request.strategies:
        if name not in REGISTRY:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown strategy {name!r}. Available: {', '.join(strategy_names())}",
            )
    if request.allocation not in ALLOCATIONS:
        raise HTTPException(status_code=422, detail=f"Unknown allocation {request.allocation!r}")

    bars_by_symbol: dict[str, list[OHLCVBar]] = {}
    for symbol in request.symbols:
        per_symbol = request.model_copy(update={"symbol": symbol})
        try:
            bars_by_symbol[symbol.upper()] = load_bars(per_symbol)
        except DataUnavailableError as exc:
            raise HTTPException(
                status_code=503, detail=f"Market data unavailable for {symbol}: {exc}"
            ) from exc

    sleeves = build_sleeves(request.symbols, request.strategies, request.params)
    try:
        return run_portfolio(
            request,
            sleeves,
            bars_by_symbol,
            allocation=request.allocation,
            n_trials=request.considered_count or len(sleeves),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
