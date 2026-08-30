"""Real-time price plumbing: a live cache fed by the bar stream, with fallbacks.

The trading loop needs one question answered cheaply and often: *what is this
symbol worth right now?* Answering it from a historical bar fetch is both slow
and wrong at intraday resolution. This module resolves the price through three
tiers, cheapest and freshest first:

    1. the websocket bar stream's in-memory cache (sub-second, Alpaca only)
    2. the provider's latest-trade endpoint (one HTTP call)
    3. the close of the most recent bar (last resort)

Every answer carries a timestamp so callers can reject a price that has gone
stale rather than trading on it blindly.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

from .config import MarketDataSettings
from .fetcher import OHLCVFetcherProtocol, get_fetcher
from .models import OHLCVBar, PriceSnapshot

logger = logging.getLogger(__name__)


class LivePriceCache:
    """Thread-safe most-recent-price-per-symbol store."""

    def __init__(self, max_age_seconds: float = 120.0) -> None:
        self._max_age_seconds = max_age_seconds
        self._prices: dict[str, PriceSnapshot] = {}
        self._lock = threading.Lock()

    def record(self, snapshot: PriceSnapshot) -> None:
        with self._lock:
            self._prices[snapshot.symbol.upper()] = snapshot

    def record_bar(self, bar: OHLCVBar) -> None:
        self.record(
            PriceSnapshot(
                symbol=bar.symbol,
                price=bar.close,
                timestamp=bar.timestamp,
                source="stream_bar",
            )
        )

    def get(self, symbol: str, max_age_seconds: float | None = None) -> PriceSnapshot | None:
        """Return the cached price, or None if it is older than the age limit."""
        snapshot = self.peek(symbol)
        if snapshot is None:
            return None
        limit = self._max_age_seconds if max_age_seconds is None else max_age_seconds
        if snapshot.age_seconds() > limit:
            return None
        return snapshot

    def peek(self, symbol: str) -> PriceSnapshot | None:
        """Return the cached price regardless of age."""
        with self._lock:
            return self._prices.get(symbol.upper())

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._prices)

    def clear(self) -> None:
        with self._lock:
            self._prices.clear()


class RealtimePriceSource:
    """Resolves the current price for a symbol across the three tiers above."""

    def __init__(
        self,
        settings: MarketDataSettings | None = None,
        cache: LivePriceCache | None = None,
        fetcher: OHLCVFetcherProtocol | None = None,
    ) -> None:
        self._settings = settings or MarketDataSettings()
        self._cache = cache or LivePriceCache(self._settings.max_price_age_seconds)
        self._fetcher = fetcher

    @property
    def cache(self) -> LivePriceCache:
        return self._cache

    def _resolve_fetcher(self) -> OHLCVFetcherProtocol | None:
        if self._fetcher is None:
            try:
                self._fetcher = get_fetcher(self._settings)
            except Exception as exc:
                logger.warning("Could not build market data fetcher: %s", exc)
                return None
        return self._fetcher

    def get_snapshot(self, symbol: str) -> PriceSnapshot | None:
        """Current price with provenance, or None if no tier can supply one."""
        cached = self._cache.get(symbol, self._settings.max_price_age_seconds)
        if cached is not None:
            return cached

        fetcher = self._resolve_fetcher()
        if fetcher is None:
            return None

        try:
            snapshot = fetcher.latest_price(symbol)
        except Exception as exc:
            logger.warning("latest_price failed for %s: %s", symbol, exc)
            snapshot = None

        if snapshot is None:
            snapshot = self._from_last_bar(symbol, fetcher)

        if snapshot is not None:
            self._cache.record(snapshot)
        return snapshot

    def get_price(self, symbol: str) -> float | None:
        snapshot = self.get_snapshot(symbol)
        return snapshot.price if snapshot else None

    def age_seconds(self, symbol: str) -> float | None:
        """How old our best price for this symbol is, in seconds."""
        snapshot = self.get_snapshot(symbol)
        return snapshot.age_seconds() if snapshot else None

    def _from_last_bar(
        self, symbol: str, fetcher: OHLCVFetcherProtocol
    ) -> PriceSnapshot | None:
        try:
            if self._settings.is_intraday:
                bars = fetcher.fetch_intraday(
                    symbol,
                    period_days=1,
                    timeframe_minutes=self._settings.intraday_minutes,
                )
            else:
                bars = fetcher.fetch(symbol, period_days=1)
        except Exception as exc:
            logger.warning("Last-bar price lookup failed for %s: %s", symbol, exc)
            return None
        if not bars:
            return None
        last = bars[-1]
        return PriceSnapshot(
            symbol=symbol,
            price=float(last.close),
            timestamp=last.timestamp,
            source="last_bar",
        )


class StreamManager:
    """Runs the Alpaca bar stream in the background and feeds a LivePriceCache.

    Start is a no-op when streaming is disabled or credentials are missing, so
    callers can invoke it unconditionally at startup.
    """

    def __init__(
        self,
        settings: MarketDataSettings,
        symbols: list[str],
        cache: LivePriceCache,
    ) -> None:
        self._settings = settings
        self._symbols = [s.upper() for s in symbols]
        self._cache = cache
        self._stream = None
        self._task: asyncio.Task | None = None
        self._started_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, object]:
        return {
            "enabled": self._settings.can_stream,
            "running": self.running,
            "symbols": self._symbols,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "cached_symbols": len(self._cache.symbols()),
        }

    async def start(self) -> bool:
        """Begin streaming. Returns True if a stream task was actually started."""
        if not self._settings.can_stream:
            logger.info(
                "Bar streaming disabled (STREAMING_ENABLED=%s, alpaca_credentials=%s) "
                "— prices resolve via polling",
                self._settings.streaming_enabled,
                self._settings.has_alpaca_credentials,
            )
            return False
        if self.running:
            return True
        if not self._symbols:
            logger.warning("Bar streaming requested but the symbol list is empty")
            return False

        from .stream import AlpacaStreamFetcher

        self._stream = AlpacaStreamFetcher(
            settings=self._settings,
            symbols=self._symbols,
            on_bar=self._on_bar,
        )
        self._task = asyncio.create_task(self._stream.start())
        self._started_at = datetime.now(timezone.utc)
        logger.info("Bar streaming started for %d symbols", len(self._symbols))
        return True

    async def stop(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown is best effort
                pass
            self._task = None
        self._started_at = None

    async def _on_bar(self, bar: OHLCVBar) -> None:
        self._cache.record_bar(bar)
