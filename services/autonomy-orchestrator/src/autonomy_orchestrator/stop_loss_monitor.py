"""Stop-loss monitor: polls live prices and triggers exits when price <= stop_price.

Resolution matters here. Checking a daily bar's close means a stop can only fire
once a day; an intraday stop has to read the current price. The monitor therefore
takes a price source (``get_price(symbol) -> float | None``) rather than a bar
fetcher.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def load_records(path: Path | None, model: type[BaseModel], label: str) -> dict:
    """Tracked risk records from disk, or an empty book.

    A corrupt or unreadable state file starts the monitor empty — the same
    protection a restart gave before persistence existed — but says so at
    ERROR: the positions it named are unwatched until re-registered, and
    that must not be silent.
    """
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records: dict[str, BaseModel] = {}
        for stored_key, row in dict(payload.get("records", {})).items():
            record = model.model_validate(row)
            key = str(stored_key)
            records[key if key.startswith("[") else key.upper()] = record
        return records
    except Exception as exc:
        logger.error(
            "%s: state file %s unreadable (%s) — starting with no tracked "
            "records; positions it named are UNWATCHED until re-registered",
            label, path, exc,
        )
        return {}


def save_records(path: Path | None, records: dict, label: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write must leave the previous state
        # file intact, not a truncated one. A truncated file loads as empty
        # on the next start — every tracked stop orphaned, which is the exact
        # restart failure this persistence exists to close.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"records": {s: r.model_dump(mode="json") for s, r in records.items()}},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception as exc:
        # The in-memory record still protects the position for this process's
        # life; only restart durability is degraded. Loud, not fatal.
        logger.error("%s: could not persist records to %s: %s", label, path, exc)


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
    side: str = "BUY"
    """Direction the position was opened in. P&L on a short inverts, so booking
    it long-only turns a losing short into a recorded profit."""
    strategy_id: str = ""
    """The sleeve whose position this stop watches. Carried into the close
    request so the exit fill is journalled under the same scope the entry
    was — otherwise the position ledger never sees the exit."""
    account_id: str = "default"
    created_at: datetime


def risk_record_key(record: BaseModel) -> str:
    """Stable identity for one protected position."""
    strategy = str(getattr(record, "strategy_id", "") or "")
    symbol = str(getattr(record, "symbol", "")).upper()
    if not strategy:
        return symbol
    return json.dumps(
        [
            str(getattr(record, "account_id", "default") or "default"),
            strategy,
            symbol,
            str(getattr(record, "position_id", "") or ""),
        ],
        separators=(",", ":"),
    )


class StopLossMonitor:
    """Durable stop protection keyed by account, sleeve, symbol and position."""

    def __init__(
        self, broker_url: str, internal_key: str, state_path: Path | str | None = None
    ) -> None:
        self._broker_url = broker_url
        self._internal_key = internal_key
        self._state_path = Path(state_path) if state_path is not None else None
        self._stops: dict[str, StopLossRecord] = load_records(
            self._state_path, StopLossRecord, "StopLossMonitor"
        )
        if self._stops:
            logger.info(
                "StopLossMonitor: restored %d tracked stop(s) from %s",
                len(self._stops),
                self._state_path,
            )

    def register(self, record: StopLossRecord) -> None:
        key = risk_record_key(record)
        self._stops[key] = record
        save_records(self._state_path, self._stops, "StopLossMonitor")
        logger.info(
            "StopLossMonitor: registered %s/%s/%s at %.4f",
            record.account_id,
            record.strategy_id or "legacy",
            record.symbol,
            record.stop_price,
        )

    def get(
        self,
        symbol: str,
        *,
        strategy_id: str = "",
        account_id: str = "default",
        position_id: str = "",
    ) -> StopLossRecord | None:
        if strategy_id:
            probe = StopLossRecord(
                symbol=symbol,
                entry_price=1.0,
                stop_price=1.0,
                position_id=position_id,
                strategy_id=strategy_id,
                account_id=account_id,
                created_at=datetime.min,
            )
            return self._stops.get(risk_record_key(probe))
        matches = [r for r in self._stops.values() if r.symbol.upper() == symbol.upper()]
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
            probe = StopLossRecord(
                symbol=symbol,
                entry_price=1.0,
                stop_price=1.0,
                position_id=position_id,
                strategy_id=strategy_id,
                account_id=account_id,
                created_at=datetime.min,
            )
            self._stops.pop(risk_record_key(probe), None)
        else:
            for key, record in list(self._stops.items()):
                if record.symbol.upper() == symbol.upper():
                    self._stops.pop(key, None)
        save_records(self._state_path, self._stops, "StopLossMonitor")

    def remove_key(self, key: str) -> None:
        self._stops.pop(key, None)
        save_records(self._state_path, self._stops, "StopLossMonitor")

    def records(self) -> dict[str, StopLossRecord]:
        return dict(self._stops)

    async def check_all(self, price_source: PriceSourceProtocol) -> list[str]:
        triggered: list[str] = []
        for key, record in list(self._stops.items()):
            symbol = record.symbol.upper()
            try:
                price = price_source.get_price(symbol)
                if price is None:
                    logger.warning("StopLossMonitor: no price for %s", symbol)
                    continue
                is_short = record.side.upper() == "SELL"
                breached = (
                    float(price) >= record.stop_price
                    if is_short
                    else float(price) <= record.stop_price
                )
                if not breached:
                    continue
                logger.warning(
                    "StopLossMonitor: stop triggered for %s %s "
                    "(price=%.4f, stop=%.4f)",
                    record.side,
                    symbol,
                    price,
                    record.stop_price,
                )
                if await self._trigger_exit(record):
                    triggered.append(key)
                    self.remove_key(key)
                else:
                    logger.error(
                        "StopLossMonitor: exit for %s failed; stop retained",
                        symbol,
                    )
            except Exception as exc:
                logger.error("StopLossMonitor: error checking %s: %s", symbol, exc)
        return triggered

    async def _trigger_exit(self, record: StopLossRecord) -> bool:
        from uuid import uuid4

        key = self._internal_key or os.environ.get("INTERNAL_API_KEY", "")
        headers = {"X-Internal-Key": key} if key else {}
        payload = {
            "signal_id": str(uuid4()),
            "symbol": record.symbol,
            "qty": record.qty or None,
            "position_id": record.position_id,
            "strategy_id": record.strategy_id or "ema_rsi_macd",
            "account_id": record.account_id,
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
                return True
        except Exception as exc:
            logger.error(
                "StopLossMonitor: failed to trigger exit for %s: %s",
                record.symbol,
                exc,
            )
            return False
