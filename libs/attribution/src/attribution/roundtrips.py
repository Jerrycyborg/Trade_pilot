"""Reconstructing closed round trips from the execution record.

The journal stores one row per order, not per position, so the pairing has to
be done here. FIFO within a scope, because that is what the portfolio's own
reconciliation does and two different answers to "which lot closed" would make
attribution disagree with P&L.

**Scope is not optional.** Pairing is done within one
(strategy, symbol, environment, account) group. Matching a paper entry to a
live exit would produce a round trip that never existed, with a realised result
computed across two different kinds of money.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from .models import Leg, RoundTrip

logger = logging.getLogger(__name__)

OPENING_SIDES = {"BUY"}
CLOSING_SIDES = {"SELL"}


def _leg(row: Any) -> Leg:
    return Leg(
        side=str(row.get("side", "")).upper(),
        qty=float(row.get("filled_qty") or row.get("qty") or 0.0),
        decision_price=row.get("decision_price"),
        fill_price=row.get("fill_price"),
        at=row["recorded_at"],
        fees=float(row.get("fees") or 0.0),
        order_id=str(row.get("order_id") or ""),
        outcome=str(row.get("outcome") or ""),
    )


def pair_round_trips(rows: list[dict[str, Any]]) -> list[RoundTrip]:
    """Match opening fills to closing fills, FIFO, within each scope.

    Only filled orders participate: a limit that missed has no position to
    attribute. Unmatched openings are still open and are left out rather than
    closed at a guessed price.
    """
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("filled"):
            continue
        if row.get("fill_price") in (None, 0):
            continue
        groups[
            (
                str(row.get("strategy_id") or ""),
                str(row.get("symbol") or "").upper(),
                str(row.get("environment") or ""),
                str(row.get("account_id") or ""),
            )
        ].append(row)

    trips: list[RoundTrip] = []
    for (strategy_id, symbol, environment, account_id), scoped in groups.items():
        scoped.sort(key=lambda r: r["recorded_at"])
        open_legs: deque[tuple[Leg, float]] = deque()

        for row in scoped:
            leg = _leg(row)
            if leg.qty <= 0:
                continue

            if leg.side in OPENING_SIDES:
                open_legs.append((leg, leg.qty))
                continue
            if leg.side not in CLOSING_SIDES:
                continue

            remaining = leg.qty
            while remaining > 0 and open_legs:
                entry, available = open_legs[0]
                matched = min(available, remaining)
                trips.append(
                    RoundTrip(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        environment=environment,
                        account_id=account_id,
                        entry=entry,
                        exit=leg,
                        qty=matched,
                    )
                )
                remaining -= matched
                if available - matched <= 0:
                    open_legs.popleft()
                else:
                    open_legs[0] = (entry, available - matched)

            if remaining > 0:
                # A close with nothing open in this scope. Not attributable, and
                # not something to invent an entry for.
                logger.debug(
                    "Unmatched close of %g %s in %s/%s", remaining, symbol,
                    strategy_id, environment,
                )

    trips.sort(key=lambda t: t.exit.at)
    return trips


def load_round_trips(
    journal: Any,
    *,
    strategy_id: str | None = None,
    symbol: str | None = None,
    environment: str | None = None,
    account_id: str = "default",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    limit: int = 5000,
) -> list[RoundTrip]:
    """Round trips from the journal, for one scope.

    `environment` is left optional so a report can cover everything, but the
    pairing never crosses environments regardless — see the module docstring.
    """
    rows = journal.execution_rows(
        strategy_id=strategy_id,
        symbol=symbol,
        environment=environment,
        account_id=account_id,
        window_start=window_start,
        window_end=window_end,
        limit=limit,
    )
    return pair_round_trips(rows)
