"""OHLCV data fetchers: Alpaca (primary) and Yahoo Finance (fallback)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .config import (
    YAHOO_INTRADAY_MAX_DAYS,
    YAHOO_SUPPORTED_MINUTES,
    MarketDataSettings,
)
from .models import OHLCVBar, PriceSnapshot

logger = logging.getLogger(__name__)


class DataUnavailableError(Exception):
    """Raised when market data cannot be fetched from any source."""


class OHLCVFetcherProtocol(Protocol):
    def fetch(self, symbol: str, period_days: int = 60) -> list[OHLCVBar]: ...

    def fetch_intraday(
        self,
        symbol: str,
        period_days: int = 5,
        timeframe_minutes: int = 15,
    ) -> list[OHLCVBar]: ...

    def latest_price(self, symbol: str) -> PriceSnapshot | None: ...


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

    def fetch_intraday(
        self,
        symbol: str,
        period_days: int = 5,
        timeframe_minutes: int = 15,
    ) -> list[OHLCVBar]:
        """Fetch intraday bars at given minute resolution. Requires Alpaca API key."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=period_days)

        try:
            if self._is_crypto(symbol):
                return self._fetch_crypto_intraday(symbol, start, end, timeframe_minutes)
            return self._fetch_stock_intraday(symbol, start, end, timeframe_minutes)
        except Exception as exc:
            raise DataUnavailableError(f"Alpaca intraday fetch failed for {symbol}: {exc}") from exc

    def latest_price(self, symbol: str) -> PriceSnapshot | None:
        """Most recent trade price. This is the real-time read used for stops/exits."""
        try:
            if self._is_crypto(symbol):
                return self._latest_crypto_price(symbol)
            return self._latest_stock_price(symbol)
        except Exception as exc:
            logger.warning("Alpaca latest price failed for %s: %s", symbol, exc)
            return None

    def _latest_stock_price(self, symbol: str) -> PriceSnapshot | None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        client = StockHistoricalDataClient(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
        )
        request = StockLatestTradeRequest(
            symbol_or_symbols=symbol,
            feed=self._settings.alpaca_feed,
        )
        trades = client.get_stock_latest_trade(request)
        trade = trades.get(symbol) if hasattr(trades, "get") else None
        if trade is None:
            return None
        return PriceSnapshot(
            symbol=symbol,
            price=float(trade.price),
            timestamp=_as_utc(getattr(trade, "timestamp", None)),
            source="alpaca_trade",
        )

    def _latest_crypto_price(self, symbol: str) -> PriceSnapshot | None:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoLatestTradeRequest

        client = CryptoHistoricalDataClient(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
        )
        trades = client.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        )
        trade = trades.get(symbol) if hasattr(trades, "get") else None
        if trade is None:
            return None
        return PriceSnapshot(
            symbol=symbol,
            price=float(trade.price),
            timestamp=_as_utc(getattr(trade, "timestamp", None)),
            source="alpaca_trade",
        )

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

    def _fetch_stock_intraday(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        minutes: int,
    ) -> list[OHLCVBar]:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        client = StockHistoricalDataClient(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
        )
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
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

    def _fetch_crypto_intraday(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        minutes: int,
    ) -> list[OHLCVBar]:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        client = CryptoHistoricalDataClient(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
        )
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
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
    """Fetches OHLCV bars from Yahoo Finance (fallback, no API key required).

    Yahoo needs no credentials but its intraday feed is delayed (typically ~15
    minutes). It is the keyless fallback, not a substitute for a real-time feed.
    """

    def fetch(self, symbol: str, period_days: int = 60) -> list[OHLCVBar]:
        return self._history(symbol, period=f"{period_days}d", interval="1d")

    def fetch_intraday(
        self,
        symbol: str,
        period_days: int = 5,
        timeframe_minutes: int = 15,
    ) -> list[OHLCVBar]:
        """Fetch intraday bars. Snaps the interval and window to what Yahoo serves."""
        minutes = _nearest_yahoo_interval(timeframe_minutes)
        max_days = YAHOO_INTRADAY_MAX_DAYS.get(minutes, 59)
        days = max(1, min(period_days, max_days))
        if days < period_days:
            logger.info(
                "Yahoo caps %d-minute history at %d days — requested %d, using %d",
                minutes,
                max_days,
                period_days,
                days,
            )
        return self._history(symbol, period=f"{days}d", interval=f"{minutes}m")

    def latest_price(self, symbol: str) -> PriceSnapshot | None:
        """Last traded price from the most recent 1-minute bar."""
        try:
            bars = self._history(symbol, period="1d", interval="1m")
        except DataUnavailableError as exc:
            logger.debug("Yahoo latest price unavailable for %s: %s", symbol, exc)
            return None
        if not bars:
            return None
        last = bars[-1]
        return PriceSnapshot(
            symbol=symbol,
            price=last.close,
            timestamp=last.timestamp,
            source="yahoo_1m",
        )

    def _history(self, symbol: str, period: str, interval: str) -> list[OHLCVBar]:
        try:
            import yfinance as yf

            # Normalize crypto symbols for Yahoo Finance (BTC/USD -> BTC-USD)
            yf_symbol = symbol.replace("/", "-")
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(period=period, interval=interval, auto_adjust=True)

            if df.empty:
                raise DataUnavailableError(
                    f"Yahoo Finance returned no {interval} data for {symbol}"
                )

            bars: list[OHLCVBar] = []
            for ts, row in df.iterrows():
                bars.append(
                    OHLCVBar(
                        symbol=symbol,
                        timestamp=_as_utc(ts),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=float(row.get("Volume", 0.0)),
                    )
                )
            return sorted(bars, key=lambda b: b.timestamp)

        except DataUnavailableError:
            raise
        except Exception as exc:
            raise DataUnavailableError(f"Yahoo Finance fetch failed for {symbol}: {exc}") from exc


def _as_utc(value: object) -> datetime:
    """Coerce a pandas Timestamp / datetime / None into an aware UTC datetime."""
    if value is None:
        return datetime.now(timezone.utc)
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _nearest_yahoo_interval(minutes: int) -> int:
    """Snap an arbitrary minute resolution down to one Yahoo actually serves."""
    if minutes in YAHOO_SUPPORTED_MINUTES:
        return minutes
    candidates = [m for m in YAHOO_SUPPORTED_MINUTES if m <= minutes]
    snapped = max(candidates) if candidates else min(YAHOO_SUPPORTED_MINUTES)
    logger.info("Yahoo has no %d-minute bars — using %d-minute", minutes, snapped)
    return snapped


class FileDropFetcher:
    """Serves bars and quotes from files an external feeder writes.

    For environments where the process may not open a connection to a market
    data provider at all — an egress-restricted network, an airgapped test rig,
    a deterministic replay — but where *something* trusted can drop data into a
    directory. The stack runs unmodified; only the transport differs.

    Layout, one directory, plain JSON:

        <dir>/AAPL_1d.json    {"bars": [{"timestamp", "open", "high",
        <dir>/AAPL_15m.json     "low", "close", "volume"}, ...]}
        <dir>/quotes.json     {"quotes": {"AAPL": {"price": 314.12,
                                "ts": "2026-08-31T18:01:00+00:00"}}}

    Fail-closed throughout. A missing or unparseable file raises
    DataUnavailableError — the same refusal an unreachable provider produces —
    and a quote without a timestamp is not served, because the price-age guard
    downstream can only reject what it can date. Nothing here invents a bar,
    interpolates a gap, or serves a default.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    def fetch(self, symbol: str, period_days: int = 60) -> list[OHLCVBar]:
        bars = self._read_bars(symbol, "1d")
        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        return [b for b in bars if b.timestamp >= cutoff] or bars[-period_days:]

    def fetch_intraday(
        self,
        symbol: str,
        period_days: int = 5,
        timeframe_minutes: int = 15,
    ) -> list[OHLCVBar]:
        bars = self._read_bars(symbol, f"{timeframe_minutes}m")
        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        return [b for b in bars if b.timestamp >= cutoff]

    def latest_price(self, symbol: str) -> PriceSnapshot | None:
        """The feeder's most recent quote, dated by the feeder.

        Returns None rather than raising when quotes are absent — the realtime
        resolver then falls back to the newest bar's close, exactly as it does
        for a provider with no trade endpoint. Age is judged downstream by
        MAX_PRICE_AGE_SECONDS against the timestamp served here, so serving an
        undated quote would disable that guard; it is refused instead.
        """
        path = self._dir / "quotes.json"
        try:
            payload = json.loads(path.read_text())
            quote = payload["quotes"][symbol.upper()]
            price = float(quote["price"])
            # Feeders have written both spellings in practice, and the
            # mismatch does not fail — it silently degrades every price to
            # the last bar close. Accept either; undated is still refused.
            raw_stamp = quote.get("ts", quote.get("timestamp"))
            if raw_stamp is None:
                raise KeyError("ts")
            stamp = datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00"))
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("FileDropFetcher: unusable quote for %s: %s", symbol, exc)
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if price <= 0:
            return None
        return PriceSnapshot(
            symbol=symbol.upper(), price=price, timestamp=stamp, source="file_quote"
        )

    def _read_bars(self, symbol: str, timeframe: str) -> list[OHLCVBar]:
        path = self._dir / f"{symbol.upper()}_{timeframe}.json"
        try:
            payload = json.loads(path.read_text())
            rows = payload["bars"]
        except FileNotFoundError as exc:
            raise DataUnavailableError(
                f"No bar file for {symbol} at {path} — the feeder has not "
                f"written this symbol/timeframe"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise DataUnavailableError(f"Unreadable bar file {path}: {exc}") from exc

        bars: list[OHLCVBar] = []
        for row in rows:
            try:
                stamp = datetime.fromisoformat(str(row["timestamp"]))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                bars.append(
                    OHLCVBar(
                        symbol=symbol.upper(),
                        timestamp=stamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0.0)),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                # One malformed row poisons the file: silently dropping it
                # would serve a series with an invisible hole.
                raise DataUnavailableError(f"Malformed bar in {path}: {exc}") from exc
        if not bars:
            raise DataUnavailableError(f"Bar file {path} is empty")
        bars.sort(key=lambda b: b.timestamp)
        return bars


def get_fetcher(
    settings: MarketDataSettings,
) -> AlpacaFetcher | YahooFinanceFetcher | FileDropFetcher:
    """Return the appropriate fetcher based on config.

    Priority:
      1. MARKET_DATA_PROVIDER=file   -> serve from MARKET_DATA_FILE_DIR
      2. MARKET_DATA_PROVIDER=yahoo  -> always use Yahoo Finance
      3. ALPACA_API_KEY + ALPACA_SECRET_KEY set -> use Alpaca
      4. fallback -> Yahoo Finance (no API key required)
    """
    if os.getenv("MARKET_DATA_PROVIDER", "").lower() == "file":
        directory = os.getenv("MARKET_DATA_FILE_DIR", "").strip()
        if not directory:
            # Refused rather than defaulted: a file provider pointed at an
            # accidental directory serves nothing and looks like an outage.
            raise DataUnavailableError(
                "MARKET_DATA_PROVIDER=file requires MARKET_DATA_FILE_DIR"
            )
        logger.info("Market data: file drop at %s", directory)
        return FileDropFetcher(directory)
    if settings.has_alpaca_credentials:
        logger.info("Market data: Alpaca")
        return AlpacaFetcher(settings)
    logger.info("Market data: Yahoo Finance (set MARKET_DATA_PROVIDER=yahoo to make explicit)")
    return YahooFinanceFetcher()


def _archive(symbol: str, bars: list[OHLCVBar], timeframe: str, source: str) -> None:
    """Record bars to the point-in-time archive. Never raises."""
    try:
        from journal import get_journal

        get_journal().record_bars(symbol, timeframe, bars, source=source)
    except Exception as exc:  # pragma: no cover - archiving is best effort
        logger.debug("Bar archiving skipped for %s: %s", symbol, exc)


def fetch_bars(symbol: str, settings: MarketDataSettings) -> list[OHLCVBar]:
    """Fetch bars at the configured timeframe.

    Intraday resolves through Alpaca when credentials are present and falls back
    to Yahoo's (delayed) intraday feed otherwise. Only if intraday fails on both
    providers do we degrade to daily bars, and that degradation is logged loudly
    because it silently changes what every downstream indicator means.
    """
    fetcher = get_fetcher(settings)
    provider = type(fetcher).__name__
    if not settings.is_intraday:
        daily = fetcher.fetch(symbol, period_days=settings.default_lookback_days)
        _archive(symbol, daily, "1d", provider)
        return daily

    try:
        intraday = fetcher.fetch_intraday(
            symbol,
            period_days=settings.intraday_lookback_days,
            timeframe_minutes=settings.intraday_minutes,
        )
        _archive(symbol, intraday, f"{settings.intraday_minutes}m", provider)
        return intraday
    except DataUnavailableError as exc:
        logger.warning(
            "Intraday fetch failed for %s via %s: %s", symbol, type(fetcher).__name__, exc
        )

    if isinstance(fetcher, AlpacaFetcher):
        try:
            logger.warning("Falling back to Yahoo intraday for %s", symbol)
            fallback = YahooFinanceFetcher().fetch_intraday(
                symbol,
                period_days=settings.intraday_lookback_days,
                timeframe_minutes=settings.intraday_minutes,
            )
            _archive(symbol, fallback, f"{settings.intraday_minutes}m", "YahooFinanceFetcher")
            return fallback
        except DataUnavailableError as exc:
            logger.warning("Yahoo intraday fallback also failed for %s: %s", symbol, exc)

    logger.error(
        "No intraday data for %s on any provider — degrading to DAILY bars. "
        "Indicators and stops are no longer intraday.",
        symbol,
    )
    degraded = fetcher.fetch(symbol, period_days=settings.default_lookback_days)
    _archive(symbol, degraded, "1d", provider)
    return degraded


def latest_price(symbol: str, settings: MarketDataSettings) -> PriceSnapshot | None:
    """Most recent price for a symbol from the configured provider."""
    fetcher = get_fetcher(settings)
    snapshot = fetcher.latest_price(symbol)
    if snapshot is None and isinstance(fetcher, AlpacaFetcher):
        snapshot = YahooFinanceFetcher().latest_price(symbol)
    return snapshot
