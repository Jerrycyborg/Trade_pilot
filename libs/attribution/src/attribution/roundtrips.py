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

_SIDE_DIRECTION = {"BUY": 1, "SELL": -1}


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

    Direction comes from netting, not from the side label: a SELL with nothing
    open (or with shorts open) opens a short lot, and the BUY that offsets it
    closes the round trip. The first live paper run's only clean trade was a
    short, and a pairing that hard-coded BUY-opens/SELL-closes reported "no
    closed round trips" over an archive that held one — every downstream model
    was already direction-aware; the trips just never reached it.

    Only filled orders participate: a limit that missed has no position to
    attribute. Unmatched openings are still open and are left out rather than
    closed at a guessed price. The netting reading has one honest limit: a
    close whose opening fill predates the archive window looks like a fresh
    opening in the opposite direction, so windows should start flat.
    """
    groups: dict[
        tuple[str, str, str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in rows:
        if not row.get("filled"):
            continue
        if row.get("fill_price") in (None, 0):
            continue
        groups[
            (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or ""),
                str(row.get("symbol") or "").upper(),
                str(row.get("environment") or ""),
                str(row.get("account_id") or ""),
            )
        ].append(row)

    trips: list[RoundTrip] = []
    for (
        strategy_id,
        strategy_version,
        symbol,
        environment,
        account_id,
    ), scoped in groups.items():
        scoped.sort(key=lambda r: r["recorded_at"])
        # All open lots in a scope share one direction at any moment: an
        # opposing fill nets against them FIFO before anything new opens.
        open_legs: deque[tuple[Leg, float]] = deque()
        open_direction = 0

        for row in scoped:
            leg = _leg(row)
            if leg.qty <= 0:
                continue
            leg_direction = _SIDE_DIRECTION.get(leg.side)
            if leg_direction is None:
                continue

            remaining = leg.qty
            while remaining > 0 and open_legs and leg_direction == -open_direction:
                entry, available = open_legs[0]
                matched = min(available, remaining)
                trips.append(
                    RoundTrip(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        environment=environment,
                        account_id=account_id,
                        strategy_version=strategy_version,
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
                # Whatever the offsets left over opens (or extends) a position
                # in this fill's own direction — including the flip through
                # flat when it closed the other side entirely.
                open_legs.append((leg, remaining))
                open_direction = leg_direction
            elif not open_legs:
                open_direction = 0

    trips.sort(key=lambda t: t.exit.at)
    return trips


def load_round_trips(
    journal: Any,
    *,
    strategy_id: str | None = None,
    strategy_version: str | None = None,
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
        strategy_version=strategy_version,
        symbol=symbol,
        environment=environment,
        account_id=account_id,
        window_start=window_start,
        window_end=window_end,
        limit=limit,
    )
    return pair_round_trips(rows)
