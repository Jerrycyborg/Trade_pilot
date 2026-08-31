"""Position reconciliation between the broker and the derived ledger.

The broker is the source of truth. ``portfolio-service`` derives holdings from
our own fill history, which makes it a cache — and a cache that silently
diverges will eventually place an order against a position that does not exist,
or leave a real position with nothing watching it.

**Divergence must persist to be believed.** A fill in flight legitimately shows
up at the broker before it shows up in our fills, so a single mismatched check
is normal. Only a divergence that survives consecutive checks is treated as
real, and even then the response is to stop opening new positions — never to
block an exit, because refusing to close a position you cannot account for is
strictly worse than closing it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Below this, a quantity difference is floating-point noise or a fractional
# share rounding, not a real break.
QTY_TOLERANCE = 1e-6


@dataclass(frozen=True)
class PositionBreak:
    """One symbol where the broker and the ledger disagree."""

    symbol: str
    broker_qty: float
    ledger_qty: float

    @property
    def difference(self) -> float:
        return self.broker_qty - self.ledger_qty

    @property
    def kind(self) -> str:
        if self.ledger_qty == 0:
            return "untracked_position"   # broker holds something we do not know about
        if self.broker_qty == 0:
            return "phantom_position"     # we think we hold something the broker does not
        return "quantity_mismatch"

    def describe(self) -> str:
        return (
            f"{self.symbol}: broker {self.broker_qty:g} vs ledger "
            f"{self.ledger_qty:g} ({self.kind})"
        )


@dataclass
class ReconciliationResult:
    checked_at: datetime
    ok: bool
    breaks: list[PositionBreak] = field(default_factory=list)
    broker_symbols: int = 0
    ledger_symbols: int = 0
    error: str | None = None
    consecutive_breaks: int = 0
    halted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "ok": self.ok,
            "error": self.error,
            "broker_symbols": self.broker_symbols,
            "ledger_symbols": self.ledger_symbols,
            "consecutive_breaks": self.consecutive_breaks,
            "halted": self.halted,
            "breaks": [
                {
                    "symbol": b.symbol,
                    "broker_qty": b.broker_qty,
                    "ledger_qty": b.ledger_qty,
                    "difference": b.difference,
                    "kind": b.kind,
                }
                for b in self.breaks
            ],
        }


def compare_positions(
    broker_positions: list[dict], ledger_positions: list[dict]
) -> list[PositionBreak]:
    """Symbols where the two views disagree beyond tolerance."""

    def _qty(row: dict, *names: str) -> float:
        for name in names:
            if row.get(name) is not None:
                try:
                    return float(row[name])
                except (TypeError, ValueError):
                    continue
        return 0.0

    broker = {
        str(p.get("symbol", "")).upper(): _qty(p, "qty", "net_qty")
        for p in broker_positions
        if p.get("symbol")
    }
    ledger = {
        str(p.get("symbol", "")).upper(): _qty(p, "net_qty", "qty")
        for p in ledger_positions
        if p.get("symbol")
    }

    breaks: list[PositionBreak] = []
    for symbol in sorted(set(broker) | set(ledger)):
        broker_qty = broker.get(symbol, 0.0)
        ledger_qty = ledger.get(symbol, 0.0)
        if abs(broker_qty - ledger_qty) > QTY_TOLERANCE:
            breaks.append(PositionBreak(symbol, broker_qty, ledger_qty))
    return breaks


class Reconciler:
    """Periodically checks the ledger against the broker."""

    def __init__(
        self,
        execution_url: str,
        portfolio_url: str,
        internal_key: str = "",
        breaks_before_halt: int | None = None,
    ) -> None:
        self._execution_url = execution_url.rstrip("/")
        self._portfolio_url = portfolio_url.rstrip("/")
        self._internal_key = internal_key
        self._breaks_before_halt = (
            breaks_before_halt
            if breaks_before_halt is not None
            else int(os.getenv("RECONCILE_BREAKS_BEFORE_HALT", "2"))
        )
        self._consecutive_breaks = 0
        self.last_result: ReconciliationResult | None = None

    @property
    def entries_blocked(self) -> bool:
        """True once a break has persisted long enough to be believed."""
        return self._consecutive_breaks >= self._breaks_before_halt

    @property
    def consecutive_breaks(self) -> int:
        return self._consecutive_breaks

    def _headers(self) -> dict[str, str]:
        key = self._internal_key or os.environ.get("INTERNAL_API_KEY", "")
        return {"X-Internal-Key": key} if key else {}

    async def _fetch(self, url: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, list) else []

    async def check(self) -> ReconciliationResult:
        """Compare both views once and update the halt state."""
        now = datetime.now(timezone.utc)
        try:
            broker_positions = await self._fetch(f"{self._execution_url}/v1/positions")
            ledger_positions = await self._fetch(
                f"{self._portfolio_url}/v1/portfolio/positions"
            )
        except Exception as exc:
            # An unreachable service is not a divergence. Treating it as one
            # would halt trading every time a container restarts.
            logger.warning("Reconciliation could not run: %s", exc)
            result = ReconciliationResult(
                checked_at=now,
                ok=False,
                error=str(exc),
                consecutive_breaks=self._consecutive_breaks,
                halted=self.entries_blocked,
            )
            self.last_result = result
            return result

        breaks = compare_positions(broker_positions, ledger_positions)
        if breaks:
            self._consecutive_breaks += 1
            for item in breaks:
                logger.warning(
                    "Position break (%d/%d consecutive): %s",
                    self._consecutive_breaks,
                    self._breaks_before_halt,
                    item.describe(),
                )
        else:
            if self._consecutive_breaks:
                logger.info("Positions reconciled — break cleared")
            self._consecutive_breaks = 0

        result = ReconciliationResult(
            checked_at=now,
            ok=not breaks,
            breaks=breaks,
            broker_symbols=len(broker_positions),
            ledger_symbols=len(ledger_positions),
            consecutive_breaks=self._consecutive_breaks,
            halted=self.entries_blocked,
        )
        self.last_result = result

        if self.entries_blocked:
            logger.error(
                "New entries blocked: %d position break(s) persisted across %d checks. "
                "Exits remain enabled.",
                len(breaks),
                self._consecutive_breaks,
            )
        return result

    def reset(self) -> None:
        self._consecutive_breaks = 0
        self.last_result = None
