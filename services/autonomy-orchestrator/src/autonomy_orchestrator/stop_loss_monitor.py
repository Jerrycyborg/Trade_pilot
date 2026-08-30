"""Stop-loss monitor: polls live prices and triggers exits when price <= stop_price.

Resolution matters here. Checking a daily bar's close means a stop can only fire
once a day; an intraday stop has to read the current price. The monitor therefore
takes a price source (``get_price(symbol) -> float | None``) rather than a bar
fetcher.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PriceSourceProtocol(Protocol):
    """Minimal price protocol: get_price(symbol) -> current price or None."""

    def get_price(self, symbol: str) -> float | None:
        ...


class StopLossRecord(BaseModel):
    """Tracked stop-loss state. `qty=0.0` means close the full position at the broker."""

    symbol: str
    entry_price: float
    stop_price: float  # entry_price - atr * multiplier
    position_id: str
    qty: float = 0.0
    created_at: datetime


class StopLossMonitor:
    """Client-side stop-loss polling monitor."""

    def __init__(self, broker_url: str, internal_key: str) -> None:
        self._broker_url = broker_url
        self._internal_key = internal_key
        self._stops: dict[str, StopLossRecord] = {}

    def register(self, record: StopLossRecord) -> None:
        """Add or overwrite stop for a symbol."""
        self._stops[record.symbol.upper()] = record
        logger.info(
            "StopLossMonitor: registered stop for %s at %.4f (entry=%.4f)",
            record.symbol,
            record.stop_price,
            record.entry_price,
        )

    def get(self, symbol: str) -> StopLossRecord | None:
        return self._stops.get(symbol.upper())

    def remove(self, symbol: str) -> None:
        self._stops.pop(symbol.upper(), None)

    async def check_all(self, price_source: PriceSourceProtocol) -> list[str]:
        """
        Read the current price for each tracked position. If it is at or below
        the stop, close the position at the broker and return the symbol.
        """
        triggered: list[str] = []
        for symbol, record in list(self._stops.items()):
            try:
                price = price_source.get_price(symbol)
                if price is None:
                    # A stop we cannot evaluate is a risk we cannot see.
                    logger.warning("StopLossMonitor: no price for %s — stop not evaluated", symbol)
                    continue
                if float(price) <= record.stop_price:
                    logger.warning(
                        "StopLossMonitor: stop triggered for %s (price=%.4f <= stop=%.4f)",
                        symbol,
                        price,
                        record.stop_price,
                    )
                    await self._trigger_exit(record)
                    triggered.append(symbol)
                    self.remove(symbol)
            except Exception as exc:
                logger.error("StopLossMonitor: error checking %s: %s", symbol, exc)
        return triggered

    async def _trigger_exit(self, record: StopLossRecord) -> None:
        """POST close request. `qty=0.0` means the broker should close the full position."""
        import os
        from uuid import uuid4

        headers = {"X-Internal-Key": self._internal_key or os.environ.get("INTERNAL_API_KEY", "")}
        payload = {
            "signal_id": str(uuid4()),
            "symbol": record.symbol,
            "qty": record.qty,
            "position_id": record.position_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._broker_url}/v1/orders/close",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                logger.info("StopLossMonitor: exit order placed for %s", record.symbol)
        except Exception as exc:
            logger.error("StopLossMonitor: failed to trigger exit for %s: %s", record.symbol, exc)
