"""Stop-loss monitor: polls live prices and triggers exits when price <= stop_price.

Resolution matters here. Checking a daily bar's close means a stop can only fire
once a day; an intraday stop has to read the current price. The monitor therefore
takes a price source (``get_price(symbol) -> float | None``) rather than a bar
fetcher.
"""

from __future__ import annotations

import json
import logging
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
        return {
            str(symbol).upper(): model.model_validate(row)
            for symbol, row in dict(payload.get("records", {})).items()
        }
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
        path.write_text(
            json.dumps(
                {"records": {s: r.model_dump(mode="json") for s, r in records.items()}},
                indent=2,
            ),
            encoding="utf-8",
        )
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
    created_at: datetime


class StopLossMonitor:
    """Client-side stop-loss polling monitor.

    Records persist to `state_path` when one is given: they used to live only
    in this dict, so every orchestrator restart silently orphaned the stops of
    every open position — the position survived in the broker, the thing
    watching it did not. Found by the first orchestrator drill, which left a
    pre-restart lot unwatched while a fresh lot's stop fired correctly.
    """

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
                "StopLossMonitor: restored %d tracked stop(s) from %s: %s",
                len(self._stops), self._state_path, sorted(self._stops),
            )

    def register(self, record: StopLossRecord) -> None:
        """Add or overwrite stop for a symbol."""
        self._stops[record.symbol.upper()] = record
        save_records(self._state_path, self._stops, "StopLossMonitor")
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
        save_records(self._state_path, self._stops, "StopLossMonitor")

    def records(self) -> dict[str, StopLossRecord]:
        """Snapshot of tracked stops. check_all() removes them as they fire, so
        callers that need a fired record must take this first."""
        return dict(self._stops)

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
                # A short's stop sits above its entry and fires on a rise.
                # Testing only `price <= stop` leaves every short stop inert.
                is_short = record.side.upper() == "SELL"
                breached = (
                    float(price) >= record.stop_price
                    if is_short
                    else float(price) <= record.stop_price
                )
                if breached:
                    logger.warning(
                        "StopLossMonitor: stop triggered for %s %s (price=%.4f, stop=%.4f)",
                        record.side,
                        symbol,
                        price,
                        record.stop_price,
                    )
                    if await self._trigger_exit(record):
                        triggered.append(symbol)
                        self.remove(symbol)
                    else:
                        # The close failed. Reporting it as triggered would book
                        # a day trade and a realised loss for a position that is
                        # still open, and dropping the stop would leave that
                        # position with nothing watching it. Keep it tracked and
                        # retry on the next check.
                        logger.error(
                            "StopLossMonitor: exit for %s failed — position still "
                            "open, stop retained", symbol,
                        )
            except Exception as exc:
                logger.error("StopLossMonitor: error checking %s: %s", symbol, exc)
        return triggered

    async def _trigger_exit(self, record: StopLossRecord) -> bool:
        """POST close request. `qty=0.0` means the broker should close the full position.

        Returns whether the broker confirmed the close. Callers must not book a
        realised loss or a day trade on a close that did not happen.
        """
        import os
        from uuid import uuid4

        headers = {"X-Internal-Key": self._internal_key or os.environ.get("INTERNAL_API_KEY", "")}
        payload = {
            "signal_id": str(uuid4()),
            "symbol": record.symbol,
            "qty": record.qty,
            "position_id": record.position_id,
            "strategy_id": record.strategy_id,
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
                "StopLossMonitor: failed to trigger exit for %s: %s", record.symbol, exc
            )
            return False
