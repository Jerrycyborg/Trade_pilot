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

logger = logging.getLogger(__name__)


def attribute(round_trip: RoundTrip, bars: list[dict[str, Any]] | None = None) -> Attribution:
    """Explain one round trip, naming whatever it could not explain.

    `bars` are the point-in-time series for the holding period — what the
    system knew *then*, not the corrected series. Passing the corrected one
    would let a revision the live system never saw shape the diagnosis.
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
    result.diagnostics.update(_diagnostics(round_trip, bars))

    if result.missing:
        # Partial on purpose: a zero here would read as "execution cost nothing"
        # rather than "we did not record what it cost".
        return result

    result.signal = round(direction * (exit_.decision_price - entry.decision_price), 6)
    result.entry_execution = round(direction * (entry.decision_price - entry.fill_price), 6)
    result.exit_execution = round(direction * (exit_.fill_price - exit_.decision_price), 6)
    return result


def _diagnostics(round_trip: RoundTrip, bars: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Context that explains a result without being part of the identity."""
    diagnostics: dict[str, Any] = {
        "exit_reason": round_trip.exit.outcome or "unknown",
        "held_for_minutes": round(round_trip.held_for_minutes, 2),
    }

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
