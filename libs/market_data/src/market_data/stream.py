"""Alpaca WebSocket streaming client with auto-reconnect."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .config import MarketDataSettings
from .models import OHLCVBar, PriceSnapshot

logger = logging.getLogger(__name__)

BarCallback = Callable[[OHLCVBar], Awaitable[None]]
PriceCallback = Callable[[PriceSnapshot], Awaitable[None]]


class AlpacaStreamFetcher:
    """
    WebSocket bar subscriber for real-time 1-min bars.
    Maintains in-memory rolling buffer per symbol.
    Reconnects automatically on disconnect (exponential backoff, cap 60s).
    """

    def __init__(
        self,
        settings: MarketDataSettings,
        symbols: list[str],
        on_bar: BarCallback,
        buffer_size: int = 200,
        max_reconnect_attempts: int = 10,
        on_price: PriceCallback | None = None,
    ) -> None:
        self._settings = settings
        self._symbols = [s.upper() for s in symbols]
        self._on_bar = on_bar
        self._on_price = on_price
        self._buffer_size = buffer_size
        self._max_reconnect_attempts = max_reconnect_attempts
        self._buffers: dict[str, deque[OHLCVBar]] = {
            sym: deque(maxlen=buffer_size) for sym in self._symbols
        }
        self._stream = None
        self._running = False
        self._connect_count = 0

    async def start(self) -> None:
        """Connect and subscribe. Blocks until stop() called."""
        self._running = True
        await self._reconnect_loop()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._stream is not None:
            try:
                await self._stream.stop()
            except Exception as exc:
                logger.debug("Stream stop error: %s", exc)

    def latest_bars(self, symbol: str, n: int = 60) -> list[OHLCVBar]:
        """Return last n buffered bars for symbol (oldest-first)."""
        buf = self._buffers.get(symbol.upper(), deque())
        bars = list(buf)
        return bars[-n:] if n < len(bars) else bars

    async def _connect(self) -> None:
        """Create and start Alpaca stream subscription."""
        from alpaca.data.live import StockDataStream

        self._stream = StockDataStream(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
            feed="iex",  # free tier; use "sip" for paid
        )
        self._stream.subscribe_bars(self._handle_bar, *self._symbols)
        if self._on_price is not None:
            # Bars arrive only once per minute. Trade ticks keep execution and
            # protective exits on current prices between bar closes.
            self._stream.subscribe_trades(self._handle_trade, *self._symbols)
        self._connect_count += 1
        logger.info("AlpacaStreamFetcher: connecting (attempt %d)", self._connect_count)
        await self._stream.run()

    async def _handle_bar(self, bar: object) -> None:
        """Convert Alpaca bar object -> OHLCVBar, push to buffer, call on_bar."""
        try:
            symbol = str(getattr(bar, "symbol", "")).upper()
            ts = getattr(bar, "timestamp", None)
            if ts is None:
                ts = datetime.now(timezone.utc)
            elif not hasattr(ts, "tzinfo") or ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            ohlcv = OHLCVBar(
                symbol=symbol,
                timestamp=ts,
                open=float(getattr(bar, "open", 0.0)),
                high=float(getattr(bar, "high", 0.0)),
                low=float(getattr(bar, "low", 0.0)),
                close=float(getattr(bar, "close", 0.0)),
                volume=float(getattr(bar, "volume", 0.0)),
            )
            if symbol in self._buffers:
                self._buffers[symbol].append(ohlcv)
            await self._on_bar(ohlcv)
        except Exception as exc:
            logger.error("AlpacaStreamFetcher._handle_bar error: %s", exc)

    async def _handle_trade(self, trade: object) -> None:
        """Convert a provider trade into a timestamped cache observation."""
        if self._on_price is None:
            return
        try:
            symbol = str(getattr(trade, "symbol", "")).upper()
            timestamp = getattr(trade, "timestamp", None)
            price = float(getattr(trade, "price", 0.0))
            if (
                symbol not in self._buffers
                or not isinstance(timestamp, datetime)
                or not math.isfinite(price)
                or price <= 0.0
            ):
                raise ValueError("trade is missing a subscribed symbol, timestamp, or price")
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            await self._on_price(
                PriceSnapshot(
                    symbol=symbol,
                    price=price,
                    timestamp=timestamp.astimezone(timezone.utc),
                    source="stream_trade",
                )
            )
        except Exception as exc:
            logger.error("AlpacaStreamFetcher._handle_trade error: %s", exc)

    async def _reconnect_loop(self) -> None:
        """Exponential backoff reconnect: 2, 4, 8 ... 60s."""
        attempt = 0
        while self._running:
            try:
                await self._connect()
                # If _connect returns normally (stream ended), treat as disconnect
                if self._running:
                    logger.warning("AlpacaStreamFetcher: stream ended, reconnecting...")
                    attempt = 0  # reset backoff on clean disconnect
            except asyncio.CancelledError:
                logger.info("AlpacaStreamFetcher: cancelled")
                break
            except Exception as exc:
                attempt += 1
                if attempt > self._max_reconnect_attempts:
                    logger.error(
                        "AlpacaStreamFetcher: max reconnect attempts (%d) reached, giving up",
                        self._max_reconnect_attempts,
                    )
                    break
                delay = min(2**attempt, 60)
                logger.warning(
                    "AlpacaStreamFetcher: disconnected (%s), reconnecting in %ds (attempt %d/%d)",
                    exc,
                    delay,
                    attempt,
                    self._max_reconnect_attempts,
                )
                await asyncio.sleep(delay)
