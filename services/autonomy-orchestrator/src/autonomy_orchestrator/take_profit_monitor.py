"""Take-profit monitor — mirrors StopLossMonitor."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from .stop_loss_monitor import load_records, risk_record_key, save_records

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
    strategy_id: str = ""
    """The sleeve whose position this target watches — see StopLossRecord."""
    target_gain_usd: float = 20.0
    account_id: str = "default"
    created_at: datetime


class TakeProfitMonitor:
    """Durable targets keyed by account, sleeve, symbol and position."""

    def __init__(
        self, broker_url: str, internal_key: str, state_path: Path | str | None = None
    ) -> None:
        self._broker_url = broker_url
        self._key = internal_key
        self._state_path = Path(state_path) if state_path is not None else None
        self._records: dict[str, TakeProfitRecord] = load_records(
            self._state_path, TakeProfitRecord, "TakeProfitMonitor"
        )

    def register(self, record: TakeProfitRecord) -> None:
        self._records[risk_record_key(record)] = record
        save_records(self._state_path, self._records, "TakeProfitMonitor")

    def get(
        self,
        symbol: str,
        *,
        strategy_id: str = "",
        account_id: str = "default",
        position_id: str = "",
    ) -> TakeProfitRecord | None:
        if strategy_id:
            probe = TakeProfitRecord(
                symbol=symbol,
                entry_price=1.0,
                target_price=1.0,
                position_id=position_id,
                strategy_id=strategy_id,
                account_id=account_id,
                created_at=datetime.min,
            )
            return self._records.get(risk_record_key(probe))
        matches = [r for r in self._records.values() if r.symbol.upper() == symbol.upper()]
        return matches[0] if len(matches) == 1 else None

    def remove(
        self,
        symbol: str,
        *,
        strategy_id: str = "",
        account_id: str = "default",
        position_id: str = "",
    ) -> None:
        if strategy_id:
            probe = TakeProfitRecord(
                symbol=symbol,
                entry_price=1.0,
                target_price=1.0,
                position_id=position_id,
                strategy_id=strategy_id,
                account_id=account_id,
                created_at=datetime.min,
            )
            self._records.pop(risk_record_key(probe), None)
        else:
            for key, record in list(self._records.items()):
                if record.symbol.upper() == symbol.upper():
                    self._records.pop(key, None)
        save_records(self._state_path, self._records, "TakeProfitMonitor")

    def remove_key(self, key: str) -> None:
        self._records.pop(key, None)
        save_records(self._state_path, self._records, "TakeProfitMonitor")

    def records(self) -> dict[str, TakeProfitRecord]:
        return dict(self._records)

    async def check_all(self, price_source: PriceSourceProtocol) -> list[str]:
        triggered: list[str] = []
        for key, record in list(self._records.items()):
            symbol = record.symbol.upper()
            try:
                price = price_source.get_price(symbol)
                if price is None:
                    logger.warning("TakeProfitMonitor: no price for %s", symbol)
                    continue
                is_short = record.side.upper() == "SELL"
                reached = (
                    float(price) <= record.target_price
                    if is_short
                    else float(price) >= record.target_price
                )
                if reached and await self._trigger_close(record):
                    self.remove_key(key)
                    triggered.append(key)
            except Exception as exc:
                logger.warning("Take-profit check failed for %s: %s", symbol, exc)
        return triggered

    async def _trigger_close(self, record: TakeProfitRecord) -> bool:
        import os
        from uuid import uuid4

        payload = {
            "signal_id": str(uuid4()),
            "symbol": record.symbol,
            "qty": record.qty or None,
            "position_id": record.position_id,
            "strategy_id": record.strategy_id or "ema_rsi_macd",
            "account_id": record.account_id,
        }
        key = self._key or os.environ.get("INTERNAL_API_KEY", "")
        headers = {"X-Internal-Key": key} if key else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._broker_url}/v1/orders/close",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                logger.info("Take-profit close submitted for %s", record.symbol)
                return True
        except Exception as exc:
            logger.error(
                "Take-profit _trigger_close failed for %s: %s",
                record.symbol,
                exc,
            )
            return False
