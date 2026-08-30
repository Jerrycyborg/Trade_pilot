"""Take-profit monitor — mirrors StopLossMonitor."""

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


class TakeProfitRecord(BaseModel):
    symbol: str
    entry_price: float
    target_price: float
    position_id: str
    qty: float = 0.0
    target_gain_usd: float = 20.0
    created_at: datetime


class TakeProfitMonitor:
    def __init__(self, broker_url: str, internal_key: str) -> None:
        self._broker_url = broker_url
        self._key = internal_key
        self._records: dict[str, TakeProfitRecord] = {}

    def register(self, record: TakeProfitRecord) -> None:
        self._records[record.symbol.upper()] = record

    def get(self, symbol: str) -> TakeProfitRecord | None:
        return self._records.get(symbol.upper())

    def records(self) -> dict[str, TakeProfitRecord]:
        """Snapshot of tracked targets. check_all() removes them as they fire, so
        callers that need a fired record must take this first."""
        return dict(self._records)

    async def check_all(self, price_source: PriceSourceProtocol) -> list[str]:
        triggered: list[str] = []
        for symbol, record in list(self._records.items()):
            try:
                price = price_source.get_price(symbol)
                if price is None:
                    logger.warning(
                        "TakeProfitMonitor: no price for %s — target not evaluated", symbol
                    )
                    continue
                if float(price) >= record.target_price:
                    logger.info(
                        "Take-profit triggered for %s: price=%.2f >= target=%.2f",
                        symbol,
                        price,
                        record.target_price,
                    )
                    if await self._trigger_close(record):
                        del self._records[symbol]
                        triggered.append(symbol)
            except Exception as exc:
                logger.warning("Take-profit check failed for %s: %s", symbol, exc)
        return triggered

    async def _trigger_close(self, record: TakeProfitRecord) -> bool:
        from uuid import uuid4

        payload = {
            "signal_id": str(uuid4()),
            "symbol": record.symbol,
            "qty": record.qty,
            "position_id": record.position_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"X-Internal-Key": self._key} if self._key else {}
                resp = await client.post(
                    f"{self._broker_url}/v1/orders/close",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                logger.info("Take-profit close submitted for %s", record.symbol)
                return True
        except Exception as exc:
            logger.error("Take-profit _trigger_close failed for %s: %s", record.symbol, exc)
            return False
