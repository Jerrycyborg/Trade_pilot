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


def _archive_price(snapshot: PriceSnapshot, accepted: bool) -> None:
    """Record a resolved price, accepted or refused. Never raises."""
    try:
        from journal import get_journal

        get_journal().record_price(
            snapshot.symbol,
            snapshot.price,
            price_ts=snapshot.timestamp,
            source=snapshot.source,
            accepted=accepted,
        )
    except Exception as exc:  # pragma: no cover - archiving is best effort
        logger.debug("Price archiving skipped for %s: %s", snapshot.symbol, exc)


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

    def newest(self) -> PriceSnapshot | None:
        """Freshest cached observation across all subscribed symbols."""
        with self._lock:
            return max(
                self._prices.values(),
                key=lambda snapshot: snapshot.timestamp,
                default=None,
            )

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
        """Current price with provenance, or None if no tier can supply a fresh one.

        The age limit applies to every tier, not only to cache hits. A provider
        outage makes the last-bar tier return yesterday's close, and callers
        here are stop-loss monitors and the fill simulator — they must get None
        and refuse to act rather than a stale number they cannot tell apart
        from a live one.
        """
        limit = self._settings.price_age_limit_seconds
        cached = self._cache.get(symbol, limit)
        if cached is not None:
            return cached
        return self._resolve_uncached(symbol, limit)

    def _resolve_uncached(self, symbol: str, limit: float) -> PriceSnapshot | None:
        """Tiers 2 and 3: the provider, then the last bar, bounded by `limit`."""
        fetcher = self._resolve_fetcher()
        if fetcher is None:
            return None

        try:
            snapshot = fetcher.latest_price(symbol)
        except Exception as exc:
            logger.warning("latest_price failed for %s: %s", symbol, exc)
            snapshot = None

        if snapshot is None:
            # Provider down. A cached live quote inside the overall limit is
            # always fresher than a bar close — on daily cadence the last bar
            # can be a session old — so serve it before falling to tier 3.
            cached = self._cache.get(symbol, limit)
            if cached is not None:
                return cached
            snapshot = self._from_last_bar(symbol, fetcher)

        if snapshot is None:
            return None

        # Cache it either way — a stale observation is still the freshest thing
        # we have, and callers that tolerate age can read it via the cache —
        # but never let an older observation clobber a fresher one already held.
        prior = self._cache.peek(symbol)
        if prior is None or snapshot.timestamp >= prior.timestamp:
            self._cache.record(snapshot)

        age = snapshot.age_seconds()
        accepted = age <= limit
        # Archived either way: a price we refused explains a trade we did not
        # make, which a later post-mortem otherwise cannot account for.
        _archive_price(snapshot, accepted)
        if not accepted:
            logger.warning(
                "Refusing stale price for %s: %.0fs old via %s, limit is %.0fs",
                symbol,
                age,
                snapshot.source,
                limit,
            )
            return None
        return snapshot

    def get_price(self, symbol: str) -> float | None:
        snapshot = self.get_snapshot(symbol)
        return snapshot.price if snapshot else None

    # A cache entry younger than this is as good as a provider round trip; it
    # keeps a burst of same-symbol fills from hammering the provider without
    # letting a fill price drift minutes behind the market.
    FILL_CACHE_TOLERANCE_SECONDS = 5.0

    def get_fresh_price(self, symbol: str) -> float | None:
        """The freshest obtainable price — for fills, not displays.

        get_price serves any cache entry younger than the timeframe-scaled age
        limit, which for a daily-cadence deployment is a *day*: fine for a
        ticker, poison for a fill simulator, where the first orchestrator
        drill's stop fired on a live 185 and the exit then filled from a
        two-minute-old cached 220 — a realised loss of cents on a 16% move.
        This consults the provider unless the cache is seconds old; the
        overall staleness limit still applies, so an unpriceable market stays
        None and the fill is refused rather than guessed.
        """
        snapshot = self._snapshot_with_tolerance(symbol, self.FILL_CACHE_TOLERANCE_SECONDS)
        return snapshot.price if snapshot else None

    def _snapshot_with_tolerance(
        self, symbol: str, cache_tolerance_seconds: float
    ) -> PriceSnapshot | None:
        limit = self._settings.price_age_limit_seconds
        cached = self._cache.get(symbol, min(cache_tolerance_seconds, limit))
        if cached is not None:
            return cached
        return self._resolve_uncached(symbol, limit)

    def age_seconds(self, symbol: str) -> float | None:
        """How old our best price for this symbol is, in seconds."""
        snapshot = self.get_snapshot(symbol)
        return snapshot.age_seconds() if snapshot else None

    def _from_last_bar(self, symbol: str, fetcher: OHLCVFetcherProtocol) -> PriceSnapshot | None:
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
        latest = self._cache.newest()
        return {
            "enabled": self._settings.can_stream,
            "running": self.running,
            "symbols": self._symbols,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "cached_symbols": len(self._cache.symbols()),
            "latest_price_at": latest.timestamp.isoformat() if latest else None,
            "latest_price_age_seconds": round(latest.age_seconds(), 3) if latest else None,
            "latest_price_source": latest.source if latest else None,
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
            on_price=self._on_price,
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

    async def _on_price(self, snapshot: PriceSnapshot) -> None:
        self._cache.record(snapshot)
