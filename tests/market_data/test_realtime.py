"""Tests for the real-time price cache, resolver and stream manager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from market_data.config import MarketDataSettings
from market_data.models import OHLCVBar, PriceSnapshot
from market_data.realtime import LivePriceCache, RealtimePriceSource, StreamManager


def _snapshot(symbol: str = "AAPL", price: float = 100.0, seconds_old: float = 0.0):
    return PriceSnapshot(
        symbol=symbol,
        price=price,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=seconds_old),
        source="test",
    )


class StubFetcher:
    """Records what the resolver asked for, and at which tier."""

    def __init__(self, latest=None, bars=None) -> None:
        self._latest = latest
        self._bars = bars or []
        self.latest_calls = 0
        self.fetch_calls = 0
        self.intraday_calls = 0

    def latest_price(self, symbol: str):
        self.latest_calls += 1
        return self._latest

    def fetch(self, symbol: str, period_days: int = 60):
        self.fetch_calls += 1
        return self._bars

    def fetch_intraday(self, symbol: str, period_days: int = 5, timeframe_minutes: int = 15):
        self.intraday_calls += 1
        return self._bars


class TestLivePriceCache:
    def test_records_and_returns_price(self) -> None:
        cache = LivePriceCache(max_age_seconds=60)
        cache.record(_snapshot(price=101.5))
        assert cache.get("AAPL").price == 101.5

    def test_symbol_lookup_is_case_insensitive(self) -> None:
        cache = LivePriceCache()
        cache.record(_snapshot(symbol="aapl"))
        assert cache.get("AAPL") is not None

    def test_stale_entry_is_withheld_but_still_peekable(self) -> None:
        """A stale price must not silently pass as current — that is exactly how
        a stop ends up evaluated against yesterday's number."""
        cache = LivePriceCache(max_age_seconds=30)
        cache.record(_snapshot(seconds_old=120))
        assert cache.get("AAPL") is None
        assert cache.peek("AAPL") is not None

    def test_record_bar_uses_close_and_bar_timestamp(self) -> None:
        cache = LivePriceCache()
        stamp = datetime.now(timezone.utc) - timedelta(seconds=5)
        cache.record_bar(
            OHLCVBar(
                symbol="MSFT", timestamp=stamp, open=1, high=2, low=0.5, close=1.75, volume=10
            )
        )
        snapshot = cache.get("MSFT")
        assert snapshot.price == 1.75
        assert snapshot.source == "stream_bar"


@pytest.mark.real_price_source
class TestRealtimePriceSource:
    def test_cache_hit_avoids_any_network_call(self) -> None:
        fetcher = StubFetcher(latest=_snapshot(price=999.0))
        cache = LivePriceCache(max_age_seconds=60)
        cache.record(_snapshot(price=100.0))
        source = RealtimePriceSource(MarketDataSettings(), cache=cache, fetcher=fetcher)

        assert source.get_price("AAPL") == 100.0
        assert fetcher.latest_calls == 0

    def test_falls_back_to_latest_trade_when_cache_is_cold(self) -> None:
        fetcher = StubFetcher(latest=_snapshot(price=123.0))
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )

        assert source.get_price("AAPL") == 123.0
        assert fetcher.latest_calls == 1

    def test_falls_back_to_last_bar_when_no_latest_trade(self) -> None:
        bar = OHLCVBar(
            symbol="AAPL",
            timestamp=datetime.now(timezone.utc),
            open=1, high=2, low=0.5, close=77.0, volume=10,
        )
        fetcher = StubFetcher(latest=None, bars=[bar])
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )

        snapshot = source.get_snapshot("AAPL")
        assert snapshot.price == 77.0
        assert snapshot.source == "last_bar"

    def test_returns_none_when_no_tier_can_supply_a_price(self) -> None:
        fetcher = StubFetcher(latest=None, bars=[])
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )
        assert source.get_price("AAPL") is None

    def test_resolved_price_is_cached_for_the_next_caller(self) -> None:
        fetcher = StubFetcher(latest=_snapshot(price=55.0))
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(max_age_seconds=60), fetcher=fetcher
        )

        source.get_price("AAPL")
        source.get_price("AAPL")
        assert fetcher.latest_calls == 1

    def test_last_bar_tier_uses_intraday_bars_under_intraday_timeframe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
        fetcher = StubFetcher(latest=None, bars=[])
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )

        source.get_snapshot("AAPL")
        assert fetcher.intraday_calls == 1
        assert fetcher.fetch_calls == 0


class TestStreamManager:
    def test_start_is_a_no_op_without_credentials(self) -> None:
        cache = LivePriceCache()
        manager = StreamManager(MarketDataSettings(), ["AAPL"], cache)

        assert asyncio.run(manager.start()) is False
        assert manager.running is False
        assert manager.status()["enabled"] is False

    def test_start_is_a_no_op_when_streaming_not_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        manager = StreamManager(MarketDataSettings(), ["AAPL"], LivePriceCache())
        assert asyncio.run(manager.start()) is False

    def test_streamed_bar_lands_in_the_cache(self) -> None:
        cache = LivePriceCache()
        manager = StreamManager(MarketDataSettings(), ["AAPL"], cache)
        bar = OHLCVBar(
            symbol="AAPL",
            timestamp=datetime.now(timezone.utc),
            open=1, high=2, low=0.5, close=250.25, volume=10,
        )

        asyncio.run(manager._on_bar(bar))

        assert cache.get("AAPL").price == 250.25


@pytest.mark.real_price_source
class TestFreshnessAppliesToEveryTier:
    """A stale price must be refused wherever it came from.

    Stop-loss monitors and the fill simulator call get_price() directly. During
    a provider outage the last-bar tier returns yesterday's close, and they
    cannot tell it from a live quote.
    """

    def test_stale_latest_trade_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
        fetcher = StubFetcher(latest=_snapshot(price=100.0, seconds_old=3_600))
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )
        assert source.get_price("AAPL") is None

    def test_stale_last_bar_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
        old_bar = OHLCVBar(
            symbol="AAPL",
            timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            open=1, high=2, low=0.5, close=77.0, volume=10,
        )
        fetcher = StubFetcher(latest=None, bars=[old_bar])
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )
        assert source.get_snapshot("AAPL") is None

    def test_fresh_price_still_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
        fetcher = StubFetcher(latest=_snapshot(price=100.0, seconds_old=5))
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )
        assert source.get_price("AAPL") == 100.0

    def test_daily_timeframe_tolerates_an_old_bar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """120 seconds is an intraday limit. A daily bar is hours old by
        construction, so the limit scales with the timeframe."""
        monkeypatch.delenv("MARKET_DATA_TIMEFRAME", raising=False)
        monkeypatch.delenv("MAX_PRICE_AGE_SECONDS", raising=False)
        fetcher = StubFetcher(latest=_snapshot(price=100.0, seconds_old=3_600 * 6))
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )
        assert source.get_price("AAPL") == 100.0

    def test_explicit_limit_overrides_the_timeframe_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAX_PRICE_AGE_SECONDS", "10")
        fetcher = StubFetcher(latest=_snapshot(price=100.0, seconds_old=60))
        source = RealtimePriceSource(
            MarketDataSettings(), cache=LivePriceCache(), fetcher=fetcher
        )
        assert source.get_price("AAPL") is None

    def test_a_refused_price_is_still_cached_for_inspection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operators need to see what the last observation was and how old."""
        monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
        cache = LivePriceCache()
        fetcher = StubFetcher(latest=_snapshot(price=100.0, seconds_old=3_600))
        source = RealtimePriceSource(MarketDataSettings(), cache=cache, fetcher=fetcher)

        assert source.get_price("AAPL") is None
        assert cache.peek("AAPL") is not None
