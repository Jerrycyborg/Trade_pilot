"""Tests for AlpacaStreamFetcher."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_data.config import MarketDataSettings
from market_data.stream import AlpacaStreamFetcher


def _settings() -> MarketDataSettings:
    return MarketDataSettings(
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
    )


def _make_bar(symbol: str = "AAPL", close: float = 150.0) -> MagicMock:
    bar = MagicMock()
    bar.symbol = symbol
    bar.timestamp = datetime.now(timezone.utc)
    bar.open = close * 0.999
    bar.high = close * 1.01
    bar.low = close * 0.99
    bar.close = close
    bar.volume = 10000.0
    return bar


async def _noop_callback(bar) -> None:
    pass


def test_buffer_initialized_for_symbols() -> None:
    """Buffers are created for each subscribed symbol."""
    fetcher = AlpacaStreamFetcher(_settings(), ["AAPL", "MSFT"], _noop_callback)
    assert "AAPL" in fetcher._buffers
    assert "MSFT" in fetcher._buffers


@pytest.mark.asyncio
async def test_bar_appended_to_buffer() -> None:
    """Mock Alpaca bar event -> latest_bars() returns it."""
    fetcher = AlpacaStreamFetcher(_settings(), ["AAPL"], _noop_callback)
    bar = _make_bar("AAPL", 150.0)
    await fetcher._handle_bar(bar)
    bars = fetcher.latest_bars("AAPL")
    assert len(bars) == 1
    assert bars[0].close == 150.0
    assert bars[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_buffer_size_limit() -> None:
    """Inserting 250 bars into buffer_size=200 -> len==200."""
    fetcher = AlpacaStreamFetcher(_settings(), ["AAPL"], _noop_callback, buffer_size=200)
    for i in range(250):
        bar = _make_bar("AAPL", 100.0 + i)
        await fetcher._handle_bar(bar)
    bars = fetcher.latest_bars("AAPL", n=300)
    assert len(bars) == 200


@pytest.mark.asyncio
async def test_latest_bars_returns_n() -> None:
    """60 bars buffered, latest_bars(n=10) returns last 10."""
    fetcher = AlpacaStreamFetcher(_settings(), ["AAPL"], _noop_callback)
    for i in range(60):
        await fetcher._handle_bar(_make_bar("AAPL", 100.0 + i))
    bars = fetcher.latest_bars("AAPL", n=10)
    assert len(bars) == 10
    # Should be the last 10 (highest closes)
    assert bars[-1].close == pytest.approx(159.0)


@pytest.mark.asyncio
async def test_reconnect_on_disconnect() -> None:
    """Simulate disconnect, assert _connect called twice."""
    fetcher = AlpacaStreamFetcher(
        _settings(), ["AAPL"], _noop_callback, max_reconnect_attempts=2
    )
    call_count = 0

    async def fake_connect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("simulated disconnect")
        # Second call: stop running so loop exits
        fetcher._running = False

    with patch.object(fetcher, "_connect", side_effect=fake_connect):
        fetcher._running = True
        await fetcher._reconnect_loop()

    assert call_count == 2


@pytest.mark.asyncio
async def test_unknown_symbol_not_buffered() -> None:
    """Bar for unregistered symbol doesn't crash, just not buffered."""
    fetcher = AlpacaStreamFetcher(_settings(), ["AAPL"], _noop_callback)
    bar = _make_bar("TSLA", 200.0)
    await fetcher._handle_bar(bar)
    assert fetcher.latest_bars("TSLA") == []
