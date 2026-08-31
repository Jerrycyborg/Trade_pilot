"""Request/response models for backtest service."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# A regular US equity session is 6.5 hours = 390 minutes. Used to convert an
# intraday bar size into periods-per-year for annualising Sharpe.
US_SESSION_MINUTES = 390
TRADING_DAYS_PER_YEAR = 252


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "ema_rsi_macd"
    period_days: int = Field(default=180, ge=1, le=730)
    initial_capital: float = Field(default=100_000.0, gt=0)
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.05)
    atr_stop_multiplier: float = Field(default=2.0, gt=0)

    # --- Timeframe -------------------------------------------------------
    timeframe: str = Field(default="daily", pattern="^(daily|intraday)$")
    intraday_minutes: int = Field(default=15, ge=1, le=390)

    # --- Trading costs ---------------------------------------------------
    # Intraday strategies trade often, so costs compound fast. They are modelled
    # explicitly rather than folded into one number, because they behave
    # differently: commission may be zero while the spread never is.
    commission_pct: float = Field(default=0.0, ge=0.0)
    """Broker commission as a fraction of notional, per side. 0 for Alpaca."""

    spread_bps: float = Field(default=5.0, ge=0.0)
    """Full quoted bid-ask spread in basis points. A market order crosses half
    of it on entry and half on exit."""

    slippage_bps: float = Field(default=1.0, ge=0.0)
    """Additional adverse fill vs the quote, per side, in basis points."""

    @property
    def is_intraday(self) -> bool:
        return self.timeframe == "intraday"

    @property
    def periods_per_year(self) -> float:
        """Return periods per year for this bar size, for annualising Sharpe."""
        if self.timeframe != "intraday":
            return float(TRADING_DAYS_PER_YEAR)
        bars_per_day = US_SESSION_MINUTES / self.intraday_minutes
        return TRADING_DAYS_PER_YEAR * bars_per_day

    @property
    def cost_per_side_pct(self) -> float:
        """Total one-way cost as a fraction of notional."""
        return self.commission_pct + (self.spread_bps / 2.0 + self.slippage_bps) / 10_000.0


class TradeRecord(BaseModel):
    entry_date: datetime
    exit_date: datetime
    symbol: str
    action: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    costs: float = 0.0
    """Total commission + spread + slippage paid on this round trip."""
    exit_reason: str = "signal"
    """signal | stop | end_of_data"""
    same_day: bool = False
    """True when entry and exit fall on the same session — a day trade."""


class BacktestResult(BaseModel):
    symbol: str
    strategy: str
    period_days: int
    timeframe: str = "daily"
    intraday_minutes: int = 15
    bars_count: int = 0
    initial_capital: float
    final_value: float
    total_return_pct: float
    gross_return_pct: float = 0.0
    """Return before any trading costs. The gap to total_return_pct is what
    costs took, and on an intraday strategy it is usually the whole edge."""
    total_costs: float = 0.0
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    win_rate: float
    profit_factor: float = 0.0
    """Gross profit / gross loss. Below 1.0 means the strategy loses money."""
    avg_trade_pnl: float = 0.0
    day_trades: int = 0
    """Round trips opened and closed in the same session. Relevant to the US
    pattern-day-trader rule, which restricts accounts under $25k equity."""
    max_day_trades_in_5_sessions: int = 0
    trades: list[TradeRecord]
    generated_at: datetime


class CostScenario(BaseModel):
    """One row of a cost sensitivity sweep."""

    spread_bps: float
    commission_pct: float
    total_return_pct: float
    sharpe_ratio: float
    profit_factor: float
    total_trades: int
    total_costs: float


class CostSensitivityResult(BaseModel):
    """Where a strategy stops being profitable as costs rise.

    A strategy that only works at zero cost is not a strategy.
    """

    symbol: str
    timeframe: str
    gross_return_pct: float
    scenarios: list[CostScenario]
    breakeven_spread_bps: float | None = None
    """Highest spread at which the strategy still returns > 0, if any."""
