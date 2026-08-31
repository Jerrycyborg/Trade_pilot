"""Take-profit monitor — mirrors StopLossMonitor."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from .stop_loss_monitor import load_records, save_records

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
    side: str = "BUY"
    """Direction the position was opened in. P&L on a short inverts, so booking
    it long-only turns a losing short into a recorded profit."""
    target_gain_usd: float = 20.0
    created_at: datetime


class TakeProfitMonitor:
    """Mirrors StopLossMonitor, restart durability included: a target that
    lives only in process memory dies with the process while its position
    does not."""

    def __init__(
        self, broker_url: str, internal_key: str, state_path: Path | str | None = None
    ) -> None:
        self._broker_url = broker_url
        self._key = internal_key
        self._state_path = Path(state_path) if state_path is not None else None
        self._records: dict[str, TakeProfitRecord] = load_records(
            self._state_path, TakeProfitRecord, "TakeProfitMonitor"
        )
        if self._records:
            logger.info(
                "TakeProfitMonitor: restored %d tracked target(s) from %s: %s",
                len(self._records), self._state_path, sorted(self._records),
            )

    def register(self, record: TakeProfitRecord) -> None:
        self._records[record.symbol.upper()] = record
        save_records(self._state_path, self._records, "TakeProfitMonitor")

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
                # A short takes profit as price falls, not rises.
                is_short = record.side.upper() == "SELL"
                reached = (
                    float(price) <= record.target_price
                    if is_short
                    else float(price) >= record.target_price
                )
                if reached:
                    logger.info(
                        "Take-profit triggered for %s %s: price=%.2f, target=%.2f",
                        record.side,
                        symbol,
                        price,
                        record.target_price,
                    )
                    if await self._trigger_close(record):
                        self._records.pop(symbol, None)
                        save_records(self._state_path, self._records, "TakeProfitMonitor")
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
