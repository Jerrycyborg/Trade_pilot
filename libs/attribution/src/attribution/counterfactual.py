"""What a different rule would have returned, on what was knowable then.

Every counterfactual here reads the **point-in-time** series — the bars as the
system held them during the trade, via `Journal.bars_as_of`. Running these
against the corrected series would let a revision the live system never
received decide that a different exit was better, which is hindsight wearing
the clothes of analysis.

These are diagnostics, not proposals. L0 produces no recommendation: the ADR is
explicit that the learner may not propose anything until attribution has shown
the archive can explain outcomes at all. A counterfactual that "wins" here is a
question worth asking later, not an answer.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import Counterfactual, RoundTrip

logger = logging.getLogger(__name__)


def _unavailable(name: str, question: str, reason: str) -> Counterfactual:
    return Counterfactual(
        name=name, question=question, per_share=None, difference=None,
        available=False, reason=reason,
    )


def hold_to_end_of_window(
    round_trip: RoundTrip, bars: list[dict[str, Any]]
) -> Counterfactual:
    """What if the position had been held to the end of the available series?"""
    question = "what if it had been held rather than exited when it was?"
    if not bars:
        return _unavailable("hold_to_end", question, "no point-in-time bars for the hold")

    actual = round_trip.realized_per_share
    if actual is None or not round_trip.entry.usable:
        return _unavailable("hold_to_end", question, "no usable fill prices")

    last_close = bars[-1].get("close")
    if last_close is None:
        return _unavailable("hold_to_end", question, "final bar has no close")

    alternative = round_trip.direction * (last_close - round_trip.entry.fill_price)
    return Counterfactual(
        name="hold_to_end",
        question=question,
        per_share=round(alternative, 6),
        difference=round(alternative - actual, 6),
    )


def stop_at(round_trip: RoundTrip, bars: list[dict[str, Any]], distance: float) -> Counterfactual:
    """What if a stop had sat `distance` away from the entry?

    Filled at the stop, or at the bar's open when it gapped through — a gap
    fills worse than the stop, never better. Being generous here would make
    every alternative stop look better than the one that ran.
    """
    question = f"what if the stop had been {distance:g} from entry?"
    if not bars:
        return _unavailable("stop", question, "no point-in-time bars for the hold")
    if not round_trip.entry.usable or distance <= 0:
        return _unavailable("stop", question, "no usable entry fill, or a non-positive stop")

    actual = round_trip.realized_per_share
    if actual is None:
        return _unavailable("stop", question, "no usable fill prices")

    entry = round_trip.entry.fill_price
    direction = round_trip.direction
    stop_price = entry - direction * distance

    for bar in bars:
        low, high, open_ = bar.get("low"), bar.get("high"), bar.get("open")
        if low is None or high is None:
            continue
        touched = low <= stop_price if direction > 0 else high >= stop_price
        if not touched:
            continue
        fill = stop_price
        if open_ is not None:
            fill = min(stop_price, open_) if direction > 0 else max(stop_price, open_)
        alternative = direction * (fill - entry)
        return Counterfactual(
            name=f"stop_{distance:g}",
            question=question,
            per_share=round(alternative, 6),
            difference=round(alternative - actual, 6),
        )

    # Never touched: the trade would have run to its actual exit unchanged.
    return Counterfactual(
        name=f"stop_{distance:g}",
        question=question,
        per_share=round(actual, 6),
        difference=0.0,
    )


def perfect_exit(round_trip: RoundTrip, bars: list[dict[str, Any]]) -> Counterfactual:
    """The best exit available during the hold.

    Unattainable by construction — nobody sells the high — so it is a bound on
    the opportunity rather than a target. Its value is the *gap*: a trade that
    captured 90% of what was there and one that captured 5% call for different
    conversations, and the realised number alone does not distinguish them.
    """
    question = "how much of the available move was captured?"
    if not bars:
        return _unavailable("perfect_exit", question, "no point-in-time bars for the hold")
    if not round_trip.entry.usable:
        return _unavailable("perfect_exit", question, "no usable entry fill")

    actual = round_trip.realized_per_share
    if actual is None:
        return _unavailable("perfect_exit", question, "no usable fill prices")

    entry = round_trip.entry.fill_price
    direction = round_trip.direction
    if direction > 0:
        best = max((b["high"] for b in bars if b.get("high") is not None), default=None)
    else:
        best = min((b["low"] for b in bars if b.get("low") is not None), default=None)
    if best is None:
        return _unavailable("perfect_exit", question, "bars carry no high/low")

    alternative = direction * (best - entry)
    return Counterfactual(
        name="perfect_exit",
        question=question,
        per_share=round(alternative, 6),
        difference=round(alternative - actual, 6),
    )


def run_counterfactuals(
    round_trip: RoundTrip,
    bars: list[dict[str, Any]],
    stop_distances: list[float] | None = None,
) -> list[Counterfactual]:
    """The standard set, for one trade."""
    results = [
        hold_to_end_of_window(round_trip, bars),
        perfect_exit(round_trip, bars),
    ]
    entry = round_trip.entry.fill_price
    if entry:
        for fraction in stop_distances or [0.01, 0.02, 0.03]:
            results.append(stop_at(round_trip, bars, entry * fraction))
    return results
