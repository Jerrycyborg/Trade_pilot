"""Configuration for market data library."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# yfinance only serves intraday history for a bounded window per interval.
# Exceeding these silently returns an empty frame, so we clamp instead.
YAHOO_INTRADAY_MAX_DAYS: dict[int, int] = {
    1: 7,
    2: 59,
    5: 59,
    15: 59,
    30: 59,
    60: 729,
    90: 59,
}

# Minute resolutions yfinance accepts, used to snap an arbitrary request to the
# nearest supported bar size.
YAHOO_SUPPORTED_MINUTES: tuple[int, ...] = (1, 2, 5, 15, 30, 60, 90)


@dataclass(frozen=True)
class MarketDataSettings:
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_paper: bool = field(
        default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() == "true"
    )
    alpaca_feed: str = field(default_factory=lambda: os.getenv("ALPACA_FEED", "iex"))
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("MARKET_DATA_CACHE_TTL_SECONDS", "300"))
    )
    default_lookback_days: int = field(
        default_factory=lambda: int(os.getenv("MARKET_DATA_LOOKBACK_DAYS", "60"))
    )
    timeframe: str = field(
        default_factory=lambda: os.getenv("MARKET_DATA_TIMEFRAME", "daily")
    )
    intraday_minutes: int = field(
        default_factory=lambda: int(os.getenv("INTRADAY_MINUTES", "15"))
    )
    intraday_lookback_days: int = field(
        default_factory=lambda: int(os.getenv("INTRADAY_LOOKBACK_DAYS", "5"))
    )
    # Real-time websocket bar stream (Alpaca only).
    streaming_enabled: bool = field(
        default_factory=lambda: os.getenv("STREAMING_ENABLED", "false").lower() == "true"
    )
    # A cached price older than this is treated as unusable for a trading decision.
    max_price_age_seconds: int = field(
        default_factory=lambda: int(os.getenv("MAX_PRICE_AGE_SECONDS", "120"))
    )

    force_yahoo: bool = field(
        default_factory=lambda: os.getenv("MARKET_DATA_PROVIDER", "").lower()
        in ("yahoo", "yfinance")
    )

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key) and not self.force_yahoo

    @property
    def is_intraday(self) -> bool:
        return self.timeframe.lower() == "intraday"

    @property
    def can_stream(self) -> bool:
        """Streaming needs both the opt-in flag and usable Alpaca credentials."""
        return self.streaming_enabled and self.has_alpaca_credentials
