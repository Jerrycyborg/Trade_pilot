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


def realized_series(round_trips: list[RoundTrip]) -> list[float]:
    """Per-trade realised results, oldest first.

    The input to any performance figure computed from actual trading rather
    than from an equity curve. Trades without usable fills are skipped rather
    than counted as zero: a trade whose price was never recorded is not a
    flat trade.
    """
    series = []
    for trip in sorted(round_trips, key=lambda t: t.exit.at):
        realized = trip.realized
        if realized is not None:
            series.append(realized)
    return series


def performance_from_trades(round_trips: list[RoundTrip]) -> dict[str, Any]:
    """Sharpe and drawdown from realised round trips.

    Per-trade rather than per-bar, and deliberately so: a live sleeve's health
    is about the trades it took, and a bar-level curve would need a mark-to-
    market the journal does not hold. The Sharpe here is therefore a per-trade
    ratio and is **not** comparable to the annualised, bar-based figure a
    backtest reports — the health check compares it against a validated
    out-of-sample number, so the two must be read as the same kind of thing or
    the comparison is meaningless.

    Returns None for a figure it cannot compute rather than a zero.
    """
    series = realized_series(round_trips)
    result: dict[str, Any] = {
        "trades": len(series),
        "realized_total": round(sum(series), 4) if series else 0.0,
        "sharpe": None,
        "max_drawdown_pct": None,
        "win_rate": None,
    }
    if len(series) < 2:
        return result

    mean = sum(series) / len(series)
    variance = sum((value - mean) ** 2 for value in series) / len(series)
    deviation = variance**0.5
    if deviation > 0:
        result["sharpe"] = round(mean / deviation, 4)

    # Drawdown on the cumulative realised curve, against the running peak —
    # the journal does not know the capital base, so a percentage of anything
    # else would be invented.
    equity = 0.0
    peak = 0.0
    worst = 0.0
    trough = 0.0
    for value in series:
        equity += value
        peak = max(peak, equity)
        trough = min(trough, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)

    # A sleeve that never went positive has no peak to measure against, and
    # reporting 0.0 there would read as "no drawdown" for the worst possible
    # record. None says "not measurable"; max_loss carries the fact instead.
    result["max_drawdown_pct"] = round(worst, 4) if peak > 0 else None
    result["max_cumulative_loss"] = round(trough, 4)
    result["win_rate"] = round(sum(1 for v in series if v > 0) / len(series), 4)
    return result
