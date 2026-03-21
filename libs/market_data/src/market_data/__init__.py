"""Market data library: OHLCV fetching and technical indicators."""

from .config import MarketDataSettings
from .fetcher import get_fetcher
from .stream import AlpacaStreamFetcher
from .indicators import build_ta_summary, compute_adx, compute_atr, detect_patterns
from .models import OHLCVBar, TASummary, TechnicalIndicators

__all__ = [
    "MarketDataSettings",
    "OHLCVBar",
    "TASummary",
    "TechnicalIndicators",
    "build_ta_summary",
    "compute_adx",
    "compute_atr",
    "detect_patterns",
    "AlpacaStreamFetcher",
    "get_fetcher",
]
