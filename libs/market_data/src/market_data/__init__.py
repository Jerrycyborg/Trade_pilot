"""Market data library: OHLCV fetching, real-time prices and technical indicators."""

from .clock import MarketSession, is_market_open, market_session
from .config import MarketDataSettings
from .fetcher import (
    DataUnavailableError,
    OHLCVFetcherProtocol,
    fetch_bars,
    get_fetcher,
    latest_price,
)
from .indicators import build_ta_summary, compute_adx, compute_atr, detect_patterns
from .models import OHLCVBar, PriceSnapshot, TASummary, TechnicalIndicators
from .realtime import LivePriceCache, RealtimePriceSource, StreamManager
from .stream import AlpacaStreamFetcher

__all__ = [
    "AlpacaStreamFetcher",
    "DataUnavailableError",
    "LivePriceCache",
    "MarketSession",
    "MarketDataSettings",
    "OHLCVBar",
    "OHLCVFetcherProtocol",
    "PriceSnapshot",
    "RealtimePriceSource",
    "StreamManager",
    "TASummary",
    "TechnicalIndicators",
    "build_ta_summary",
    "compute_adx",
    "compute_atr",
    "detect_patterns",
    "fetch_bars",
    "get_fetcher",
    "is_market_open",
    "latest_price",
    "market_session",
]
