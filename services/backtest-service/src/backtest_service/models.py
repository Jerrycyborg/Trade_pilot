"""Request/response models for backtest service."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "ema_rsi_macd"
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
