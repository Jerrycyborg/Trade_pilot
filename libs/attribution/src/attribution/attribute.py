"""Decomposing a closed trade into where its result came from.

The three price components are an exact identity:

    signal            = exit_decision  - entry_decision   (direction-adjusted)
    entry_execution   = entry_decision - entry_fill
    exit_execution    = exit_fill      - exit_decision
    ------------------------------------------------------------------
    sum               = exit_fill      - entry_fill       = realised

which is the whole reason to decompose this way rather than into categories
that feel meaningful and do not add up. "Signal" is what the strategy's own
decisions would have earned with perfect fills; the other two are what trading
cost. A test asserts the identity on every attribution, because a decomposition
that does not reconstruct the result is a story about a trade rather than an
account of one.

Everything that is *not* exact — excursions, timing, regime — is reported as a
diagnostic. Folding an approximation into an identity is how the identity stops
being one.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import Attribution, RoundTrip
from .regime import classify, describe_shift

logger = logging.getLogger(__name__)


def attribute(
    round_trip: RoundTrip,
    bars: list[dict[str, Any]] | None = None,
    entry_bars: list[dict[str, Any]] | None = None,
) -> Attribution:
    """Explain one round trip, naming whatever it could not explain.

    `bars` are the point-in-time series as of the exit — what the system knew
    *then*, not the corrected series. Passing the corrected one would let a
    revision the live system never saw shape the diagnosis.

    `entry_bars` is the same series as of the *entry*, used only to classify
    the regime the trade was opened into. Filtering `bars` by timestamp would
    remove future bars but not revisions of past ones that arrived during the
    hold, so without it the entry regime is a weaker claim and says so.
    """
    result = Attribution(round_trip=round_trip)
    entry, exit_ = round_trip.entry, round_trip.exit
    direction = round_trip.direction

    if not entry.usable:
        result.missing.append("entry_fill_price")
    if not exit_.usable:
        result.missing.append("exit_fill_price")
    if entry.decision_price in (None, 0):
        result.missing.append("entry_decision_price")
    if exit_.decision_price in (None, 0):
        result.missing.append("exit_decision_price")

    result.fees = round((entry.fees or 0.0) + (exit_.fees or 0.0), 6)
    result.diagnostics.update(_diagnostics(round_trip, bars, entry_bars))

    if result.missing:
        # Partial on purpose: a zero here would read as "execution cost nothing"
        # rather than "we did not record what it cost".
        return result

    result.signal = round(direction * (exit_.decision_price - entry.decision_price), 6)
    result.entry_execution = round(direction * (entry.decision_price - entry.fill_price), 6)
    result.exit_execution = round(direction * (exit_.fill_price - exit_.decision_price), 6)
    return result


def _diagnostics(
    round_trip: RoundTrip,
    bars: list[dict[str, Any]] | None,
    entry_bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Context that explains a result without being part of the identity."""
    diagnostics: dict[str, Any] = {
        "exit_reason": round_trip.exit.outcome or "unknown",
        "held_for_minutes": round(round_trip.held_for_minutes, 2),
    }
    diagnostics.update(_regime(round_trip, bars, entry_bars))

    window = _bars_between(round_trip, bars)
    if not window:
        diagnostics["excursions_available"] = False
        return diagnostics

    diagnostics["excursions_available"] = True
    diagnostics["bars_in_hold"] = len(window)

    entry_price = round_trip.entry.fill_price
    direction = round_trip.direction
    highs = [b["high"] for b in window if b.get("high") is not None]
    lows = [b["low"] for b in window if b.get("low") is not None]
    if not (highs and lows and entry_price):
        return diagnostics

    if direction > 0:
        favourable = max(highs) - entry_price
        adverse = min(lows) - entry_price
    else:
        favourable = entry_price - min(lows)
        adverse = entry_price - max(highs)

    diagnostics["max_favourable_excursion"] = round(favourable, 6)
    diagnostics["max_adverse_excursion"] = round(adverse, 6)

    realized = round_trip.realized_per_share
    if realized is not None and favourable > 0:
        # How much of the best available move was actually captured. Not a
        # target — capturing all of it requires selling the top — but a
        # persistent 10% says the exit is leaving the trade too early.
        diagnostics["capture_ratio"] = round(realized / favourable, 4)
    return diagnostics


def _bars_between(
    round_trip: RoundTrip, bars: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """The bars covering the hold, from a point-in-time series."""
    if not bars:
        return []
    from datetime import datetime

    def _stamp(bar: dict[str, Any]) -> datetime | None:
        raw = bar.get("bar_ts")
        if isinstance(raw, datetime):
            return raw
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    out = []
    for bar in bars:
        stamp = _stamp(bar)
        if stamp is None:
            continue
        if round_trip.entry.at <= stamp <= round_trip.exit.at:
            out.append(bar)
    return out


def _regime(
    round_trip: RoundTrip,
    bars: list[dict[str, Any]] | None,
    entry_bars: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """What the market was doing at each end of the trade.

    Kept out of the identity deliberately. A regime label is a classification
    with a threshold in it, and a threshold is an opinion; the three price
    components have to add up whatever anyone thinks about ADX.

    The entry reading prefers a series fetched as of the entry. When only the
    exit-time series is available it is still used — a timestamp-filtered
    reading is worth more than no reading — but it is labelled `exit_series`
    so nobody reads it as point-in-time when it is not.
    """
    if entry_bars is not None:
        entry_regime = classify(entry_bars, round_trip.entry.at, point_in_time="as_of")
    else:
        entry_regime = classify(bars, round_trip.entry.at, point_in_time="exit_series")
    exit_regime = classify(bars, round_trip.exit.at, point_in_time="as_of")

    return {
        "entry_regime": entry_regime.to_dict(),
        "exit_regime": exit_regime.to_dict(),
        "regime_shift": describe_shift(entry_regime, exit_regime),
    }
