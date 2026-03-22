"""Configuration for market data library."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketDataSettings:
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_paper: bool = field(
        default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() == "true"
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("MARKET_DATA_CACHE_TTL_SECONDS", "300"))
    )
    default_lookback_days: int = field(
        default_factory=lambda: int(os.getenv("MARKET_DATA_LOOKBACK_DAYS", "60"))
    )

    force_yahoo: bool = field(
        default_factory=lambda: os.getenv("MARKET_DATA_PROVIDER", "").lower() in ("yahoo", "yfinance")
    )

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key) and not self.force_yahoo
