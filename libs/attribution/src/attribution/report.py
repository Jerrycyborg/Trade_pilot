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
from .regime import RegimeSlice
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
    return _bars_as_of(journal, round_trip.symbol, timeframe, round_trip.exit.at)


def _entry_bars(journal: Any, round_trip: RoundTrip, timeframe: str) -> list[dict[str, Any]]:
    """The series as it stood when the trade was opened.

    A second query per round trip, and worth it. The entry regime is a
    statement about what was knowable at the entry, and filtering the
    exit-time series by timestamp does not produce that: it removes bars from
    the future but keeps revisions of past bars that arrived during the hold.
    Classifying the conditions a decision was made in, using data that arrived
    after the decision, is the exact mistake the point-in-time archive was
    built to prevent.
    """
    return _bars_as_of(journal, round_trip.symbol, timeframe, round_trip.entry.at)


def _bars_as_of(
    journal: Any, symbol: str, timeframe: str, moment: datetime
) -> list[dict[str, Any]]:
    try:
        return journal.bars_as_of(symbol, timeframe, moment)
    except Exception as exc:  # pragma: no cover - a report must not crash
        logger.debug("Point-in-time bars unavailable for %s: %s", symbol, exc)
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
    with_regime: bool = True,
    limit: int = 5000,
) -> dict[str, Any]:
    """Attribute every closed round trip in scope, and report the coverage."""
    with_regime_or_cf = with_counterfactuals or with_regime
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
        bars = _point_in_time_bars(journal, trip, timeframe) if with_regime_or_cf else []
        entry_bars = _entry_bars(journal, trip, timeframe) if with_regime_or_cf else None
        result = attribute(trip, bars, entry_bars)
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
        "by_regime": _by_regime(attributions),
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


#: Below this many days of live trading, annualising an observed trade
#: frequency is extrapolation rather than measurement: three days of intraday
#: activity says almost nothing about a year's. The un-annualised per-trade
#: ratio is still reported; only the comparable figure is withheld.
MIN_SPAN_DAYS_TO_ANNUALISE = 5.0

DAYS_PER_YEAR = 365.25


def performance_from_trades(
    round_trips: list[RoundTrip], capital_base: float | None = None
) -> dict[str, Any]:
    """Sharpe and drawdown from realised round trips.

    Per-trade rather than per-bar, and deliberately so: a live sleeve's health
    is about the trades it took, and a bar-level curve would need a mark-to-
    market the journal does not hold.

    `sharpe` is therefore a **per-trade** ratio and must never be compared with
    the annualised, bar-based figure a backtest reports. That comparison was
    being made — the health check read a validated annualised Sharpe against
    this one — and it is not close to harmless: a per-trade 0.20 at roughly 250
    trades a year is an annualised 3.16, so a sleeve beating a validated 2.50
    read as 2.30 *below* it and demoted.

    `sharpe_annualised` is the figure that comparison actually needs, scaled by
    the trade frequency this sleeve really ran at rather than an assumed one.
    It is None when the live record is too short to estimate a frequency from,
    because a wrong scaling is worse than an absent one: it produces a number
    that looks comparable and is not.

    `max_drawdown_amount` is the peak-to-trough fall of the cumulative realised
    curve, in currency, and is always available. `max_drawdown_pct` needs a
    `capital_base` and is None without one — a drawdown percentage is a
    fraction *of the money at risk*, and the journal does not know what that
    is. This used to divide by the running peak of cumulative P&L instead,
    which is not a capital base and is often tiny: a sleeve that won $20 early
    and then bled to -$168 reported a 940% drawdown, which against a limit
    labelled "15%" meant the trigger fired on almost any losing sleeve that had
    one good trade first.

    Returns None for a figure it cannot compute rather than a zero.
    """
    series = realized_series(round_trips)
    result: dict[str, Any] = {
        "trades": len(series),
        "realized_total": round(sum(series), 4) if series else 0.0,
        "sharpe": None,
        "sharpe_annualised": None,
        "sharpe_annualised_std_error": None,
        "trades_per_year": None,
        "span_days": None,
        "max_drawdown_amount": None,
        "max_drawdown_pct": None,
        "capital_base": capital_base,
        "win_rate": None,
    }
    if len(series) < 2:
        return result

    mean = sum(series) / len(series)
    variance = sum((value - mean) ** 2 for value in series) / len(series)
    deviation = variance**0.5
    if deviation > 0:
        result["sharpe"] = round(mean / deviation, 4)

    # Drawdown on the cumulative realised curve, peak to trough, in currency.
    # Currency because it is the only form that is unambiguous here: a
    # percentage needs a denominator, and the only honest one is the capital
    # actually at risk, which the caller has to supply.
    equity = 0.0
    peak = 0.0
    worst = 0.0
    trough = 0.0
    for value in series:
        equity += value
        peak = max(peak, equity)
        trough = min(trough, equity)
        worst = max(worst, peak - equity)

    result["max_drawdown_amount"] = round(worst, 4)
    result["max_cumulative_loss"] = round(trough, 4)
    if capital_base and capital_base > 0:
        result["max_drawdown_pct"] = round(worst / capital_base, 6)
    result["win_rate"] = round(sum(1 for v in series if v > 0) / len(series), 4)
    result.update(_annualised(round_trips, result["sharpe"], len(series)))
    return result


def _annualised(
    round_trips: list[RoundTrip], per_trade_sharpe: float | None, n: int
) -> dict[str, Any]:
    """Put a per-trade Sharpe on the same footing as a backtest's.

    The frequency is measured, not assumed: this sleeve's own trades over this
    sleeve's own elapsed time. A strategy that fires twice a day and one that
    fires twice a month produce the same per-trade ratio from very different
    edges, and only the observed frequency separates them.

    The span covers n-1 intervals between n exits, so dividing by n would
    overstate the rate — mildly at 200 trades and by a third at four.

    The standard error is Lo (2002)'s approximation for an iid sample,
    SE(SR) = sqrt((1 + SR^2/2)/n), scaled by the same root-frequency. Trade
    results are not iid, so this understates the true uncertainty; it is used
    only to set a demotion band, where understating uncertainty makes the
    trigger *more* willing to fire, and the absolute floor beside it is what
    stops that being reckless.
    """
    blank: dict[str, Any] = {
        "sharpe_annualised": None,
        "sharpe_annualised_std_error": None,
        "trades_per_year": None,
        "span_days": None,
    }
    if n < 2 or per_trade_sharpe is None:
        return blank

    exits = sorted(t.exit.at for t in round_trips if t.realized is not None)
    if len(exits) < 2:
        return blank
    span_days = (exits[-1] - exits[0]).total_seconds() / 86400.0
    out: dict[str, Any] = dict(blank)
    out["span_days"] = round(span_days, 3)
    if span_days < MIN_SPAN_DAYS_TO_ANNUALISE:
        return out

    trades_per_year = (n - 1) * DAYS_PER_YEAR / span_days
    root = trades_per_year**0.5
    out["trades_per_year"] = round(trades_per_year, 2)
    out["sharpe_annualised"] = round(per_trade_sharpe * root, 4)
    std_error = ((1.0 + (per_trade_sharpe**2) / 2.0) / n) ** 0.5
    out["sharpe_annualised_std_error"] = round(std_error * root, 4)
    return out


def _by_regime(attributions: list[Attribution]) -> dict[str, Any]:
    """Results grouped by the regime each trade was *entered* into.

    Entry rather than exit, because that is the regime the decision was made
    in, and the question this answers is whether the rule is being applied
    where it works. A slice that is consistently negative is the strongest
    evidence L0 can produce: not "the strategy is bad" but "the strategy is
    being run in conditions it does not handle", which points at a filter
    rather than at the rule.

    Trades whose regime could not be classified go to their own slice instead
    of a residual bucket that quietly resembles a real one. So do trades whose
    decomposition is incomplete: they are counted in the slice, and counted
    again as incomplete, so a reader can see how much of it rests on trades
    that could not be fully explained.
    """
    slices: dict[str, RegimeSlice] = {}
    shifted = 0
    shift_known = 0
    shift_realized = 0.0
    steady_realized = 0.0

    for attribution in attributions:
        reading = attribution.diagnostics.get("entry_regime") or {}
        label = reading.get("label", "unknown") if reading.get("available") else "unknown"
        slot = slices.setdefault(label, RegimeSlice(regime=label))
        slot.trades += 1
        slot.symbols.add(attribution.round_trip.symbol)

        realized = attribution.round_trip.realized
        if realized is not None:
            slot.realized += realized
            if realized > 0:
                slot.wins += 1

        if attribution.complete:
            qty = attribution.round_trip.qty
            slot.from_signal += (attribution.signal or 0.0) * qty
            slot.execution_cost += (
                (attribution.entry_execution or 0.0) + (attribution.exit_execution or 0.0)
            ) * qty
        else:
            slot.incomplete += 1

        shift = attribution.diagnostics.get("regime_shift") or {}
        if shift.get("changed") is not None:
            shift_known += 1
            if shift["changed"]:
                shifted += 1
                shift_realized += realized or 0.0
            else:
                steady_realized += realized or 0.0

    return {
        "slices": [
            slices[key].to_dict() for key in sorted(slices, key=lambda k: -slices[k].trades)
        ],
        "regime_shift": {
            "classifiable_trades": shift_known,
            "changed": shifted,
            "realized_when_changed": round(shift_realized, 4),
            "realized_when_steady": round(steady_realized, 4),
            "note": (
                "A trade whose regime changed under it is the textbook way for a "
                "correct signal to lose money. Comparing the two totals is "
                "suggestive, not causal: nothing here controls for anything."
            ),
        },
    }
