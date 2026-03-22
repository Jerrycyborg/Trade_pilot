"""OHLCV data fetchers: Alpaca (primary) and Yahoo Finance (fallback)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .config import MarketDataSettings
from .models import OHLCVBar

logger = logging.getLogger(__name__)


class DataUnavailableError(Exception):
    """Raised when market data cannot be fetched from any source."""


class OHLCVFetcherProtocol(Protocol):
    def fetch(self, symbol: str, period_days: int = 60) -> list[OHLCVBar]: ...


class AlpacaFetcher:
    """Fetches OHLCV bars from Alpaca Markets data API."""

    def __init__(self, settings: MarketDataSettings) -> None:
        self._settings = settings

    def _is_crypto(self, symbol: str) -> bool:
        return "/" in symbol or symbol.upper().endswith("USD") and len(symbol) > 4

    def fetch(self, symbol: str, period_days: int = 60) -> list[OHLCVBar]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=period_days)

        try:
            if self._is_crypto(symbol):
                return self._fetch_crypto(symbol, start, end)
            return self._fetch_stock(symbol, start, end)
        except Exception as exc:
            raise DataUnavailableError(f"Alpaca fetch failed for {symbol}: {exc}") from exc

    def _fetch_stock(self, symbol: str, start: datetime, end: datetime) -> list[OHLCVBar]:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
        )
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars_response = client.get_stock_bars(request)
        raw_bars = bars_response[symbol] if symbol in bars_response else []
        return [
            OHLCVBar(
                symbol=symbol,
                timestamp=b.timestamp,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in sorted(raw_bars, key=lambda x: x.timestamp)
        ]

    def _fetch_crypto(self, symbol: str, start: datetime, end: datetime) -> list[OHLCVBar]:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = CryptoHistoricalDataClient(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
        )
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars_response = client.get_crypto_bars(request)
        raw_bars = bars_response[symbol] if symbol in bars_response else []
        return [
            OHLCVBar(
                symbol=symbol,
                timestamp=b.timestamp,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in sorted(raw_bars, key=lambda x: x.timestamp)
        ]


class YahooFinanceFetcher:
    """Fetches OHLCV bars from Yahoo Finance (fallback, no API key required)."""

    def fetch(self, symbol: str, period_days: int = 60) -> list[OHLCVBar]:
        try:
            import yfinance as yf

            # Normalize crypto symbols for Yahoo Finance (BTC/USD -> BTC-USD)
            yf_symbol = symbol.replace("/", "-")
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(period=f"{period_days}d", interval="1d", auto_adjust=True)

            if df.empty:
                raise DataUnavailableError(f"Yahoo Finance returned no data for {symbol}")

            bars: list[OHLCVBar] = []
            for ts, row in df.iterrows():
                # pandas Timestamp — convert to UTC datetime
                if hasattr(ts, "to_pydatetime"):
                    dt = ts.to_pydatetime()
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = datetime.now(timezone.utc)

                bars.append(OHLCVBar(
                    symbol=symbol,
                    timestamp=dt,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0)),
                ))
            return sorted(bars, key=lambda b: b.timestamp)

        except DataUnavailableError:
            raise
        except Exception as exc:
            raise DataUnavailableError(f"Yahoo Finance fetch failed for {symbol}: {exc}") from exc


def get_fetcher(settings: MarketDataSettings) -> AlpacaFetcher | YahooFinanceFetcher:
    """Return the appropriate fetcher based on config.

    Priority:
      1. MARKET_DATA_PROVIDER=yahoo  → always use Yahoo Finance
      2. ALPACA_API_KEY + ALPACA_SECRET_KEY set → use Alpaca
      3. fallback → Yahoo Finance (no API key required)
    """
    if settings.has_alpaca_credentials:
        logger.info("Market data: Alpaca")
        return AlpacaFetcher(settings)
    logger.info("Market data: Yahoo Finance (set MARKET_DATA_PROVIDER=yahoo to make explicit)")
    return YahooFinanceFetcher()
