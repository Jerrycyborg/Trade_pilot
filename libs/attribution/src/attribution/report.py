"""The L0 deliverable: can the archive explain its own trades?

Not "here is how the strategy did" — that is what the backtest and the
execution-quality endpoint already answer. This asks a narrower and more
important question before any learning phase is attempted: **when a trade went
wrong, do the recorded facts say why?**

A high coverage number means later phases have something to learn from. A low
one is not a failure of this code; it is the finding, and it names which fields
were missing so they can be recorded before anything is built on top.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any

from .attribute import attribute
from .counterfactual import run_counterfactuals
from .models import Attribution, CoverageReport, RoundTrip
from .roundtrips import load_round_trips

logger = logging.getLogger(__name__)


def _point_in_time_bars(
    journal: Any, round_trip: RoundTrip, timeframe: str
) -> list[dict[str, Any]]:
    """The series as it stood when the trade closed.

    `bars_as_of(exit)` rather than the current series: a revision that arrived
    after the exit was not knowable during the trade, and letting it into the
    diagnosis would make every trade look worse or better than it was
    decidable at the time.
    """
    try:
        return journal.bars_as_of(round_trip.symbol, timeframe, round_trip.exit.at)
    except Exception as exc:  # pragma: no cover - a report must not crash
        logger.debug("Point-in-time bars unavailable for %s: %s", round_trip.symbol, exc)
        return []


def build_report(
    journal: Any,
    *,
    strategy_id: str | None = None,
    symbol: str | None = None,
    environment: str | None = None,
    account_id: str = "default",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    timeframe: str = "15m",
    with_counterfactuals: bool = True,
    limit: int = 5000,
) -> dict[str, Any]:
    """Attribute every closed round trip in scope, and report the coverage."""
    trips = load_round_trips(
        journal,
        strategy_id=strategy_id,
        symbol=symbol,
        environment=environment,
        account_id=account_id,
        window_start=window_start,
        window_end=window_end,
        limit=limit,
    )

    coverage = CoverageReport(round_trips=len(trips))
    attributions: list[Attribution] = []
    counterfactuals: list[dict[str, Any]] = []

    for trip in trips:
        coverage.environments[trip.environment] = (
            coverage.environments.get(trip.environment, 0) + 1
        )
        bars = _point_in_time_bars(journal, trip, timeframe) if with_counterfactuals else []
        result = attribute(trip, bars)
        attributions.append(result)

        if result.complete:
            coverage.attributable += 1
            if not result.identity_holds():
                coverage.identity_failures += 1
        for field_name in result.missing:
            coverage.missing_counts[field_name] = (
                coverage.missing_counts.get(field_name, 0) + 1
            )

        if with_counterfactuals and bars:
            counterfactuals.append(
                {
                    "symbol": trip.symbol,
                    "exit_at": trip.exit.at.isoformat(),
                    "actual_per_share": trip.realized_per_share,
                    "alternatives": [
                        c.to_dict() for c in run_counterfactuals(trip, bars)
                    ],
                }
            )

    return {
        "coverage": coverage.to_dict(),
        "totals": _totals(attributions),
        "exit_reasons": dict(
            Counter(a.diagnostics.get("exit_reason", "unknown") for a in attributions)
        ),
        "attributions": [a.to_dict() for a in attributions],
        "counterfactuals": counterfactuals,
    }


def _totals(attributions: list[Attribution]) -> dict[str, Any]:
    """Where the money came from, across every explainable trade.

    Weighted by quantity, so one large trade is not one vote. Only complete
    attributions contribute: including partial ones would silently treat
    unrecorded execution cost as zero, which is the direction that flatters.
    """
    complete = [a for a in attributions if a.complete]
    if not complete:
        return {"trades": 0, "note": "no fully attributable trades"}

    def _sum(get) -> float:
        return round(sum(get(a) * a.round_trip.qty for a in complete), 4)

    signal = _sum(lambda a: a.signal or 0.0)
    entry = _sum(lambda a: a.entry_execution or 0.0)
    exit_ = _sum(lambda a: a.exit_execution or 0.0)
    fees = round(sum(a.fees for a in complete), 4)
    realized = _sum(lambda a: a.round_trip.realized_per_share or 0.0)

    captures = [
        a.diagnostics["capture_ratio"]
        for a in complete
        if a.diagnostics.get("capture_ratio") is not None
    ]

    return {
        "trades": len(complete),
        "realized": realized,
        "from_signal": signal,
        "from_entry_execution": entry,
        "from_exit_execution": exit_,
        "execution_cost_total": round(entry + exit_, 4),
        "fees": fees,
        "net_of_fees": round(realized - fees, 4),
        "mean_capture_ratio": (
            round(sum(captures) / len(captures), 4) if captures else None
        ),
        "identity": round(signal + entry + exit_, 4),
        "identity_matches_realized": abs((signal + entry + exit_) - realized) < 1e-4,
    }
