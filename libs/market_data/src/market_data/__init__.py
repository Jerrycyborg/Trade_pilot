"""Market data library: OHLCV fetching and technical indicators."""

from .config import MarketDataSettings
from .fetcher import get_fetcher
from .indicators import build_ta_summary, compute_adx, detect_patterns
from .models import OHLCVBar, TASummary, TechnicalIndicators

__all__ = [
    "MarketDataSettings",
    "OHLCVBar",
    "TASummary",
    "TechnicalIndicators",
    "build_ta_summary",
    "compute_adx",
    "detect_patterns",
    "get_fetcher",
]
