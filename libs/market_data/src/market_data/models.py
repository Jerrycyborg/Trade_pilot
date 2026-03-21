"""Data models for market data library."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class TechnicalIndicators(BaseModel):
    rsi_14: float = Field(default=50.0)
    macd_line: float = Field(default=0.0)
    macd_signal: float = Field(default=0.0)
    macd_histogram: float = Field(default=0.0)
    bb_upper: float = Field(default=0.0)
    bb_middle: float = Field(default=0.0)
    bb_lower: float = Field(default=0.0)
    bb_position: float = Field(default=0.5, ge=0.0, le=1.0)  # 0=at lower band, 1=at upper
    ema_20: float = Field(default=0.0)
    ema_50: float = Field(default=0.0)


class TASummary(BaseModel):
    symbol: str
    as_of: datetime
    bars_count: int
    indicators: TechnicalIndicators
    adx: float = 25.0
    patterns: list[str] = []
    signal_tags: list[str] = Field(default_factory=list)
    trend_direction: str = "neutral"  # "bullish" | "bearish" | "neutral"
    data_source: str = "unknown"
    current_price: Optional[float] = None
