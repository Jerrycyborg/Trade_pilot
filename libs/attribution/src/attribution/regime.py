"""What the market was doing when a trade was taken, and whether it changed.

The exact decomposition in `attribute.py` says how much of a result came from
the signal and how much from execution. It cannot say *why* the signal was
right or wrong. The most common answer for a rule-based strategy is that the
rule was applied in conditions it was not built for — a trend-following entry
taken in a range earns nothing however well it is executed — and that is what
this module measures.

Three properties make it worth trusting:

**It is point-in-time.** A regime is classified from bars the system actually
held at the moment being classified. The entry regime is read from
`bars_as_of(entry)`, not from the exit-time series, because a revision that
arrived during the hold was not knowable when the entry was decided. Where the
caller supplies only the exit-time series, the reading says so
(`point_in_time="exit_series"`) rather than quietly claiming a rigour it does
not have.

**It refuses to guess.** `market_data.compute_adx` returns 25.0 — a value that
reads as "mildly trending" — when there is not enough history to compute one.
That sentinel is useful to a trading filter that must decide something; it is
poison to an analysis, because it turns "we did not know" into "it was
trending". Here the bar count is checked first, and a short series produces an
unavailable reading naming the shortfall.

**It reuses the indicator the live filter uses.** The strategy worker gates
trend entries on ADX < 20. Classifying with a second, subtly different ADX
would produce disagreements that are artefacts of two implementations rather
than findings. Attribution and the live filter read the same function, so when
they disagree about a trade the disagreement is real.

Regime is a **diagnostic**, never part of the identity. Its thresholds are
conventions — 20 for ADX because that is what the live filter uses, and a
volatility band measured against the symbol's own recent history rather than
an absolute number nobody can defend across a $3 stock and a $900 one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from market_data.indicators import ADX_MIN_BARS, ADX_PERIOD, compute_adx, compute_atr

logger = logging.getLogger(__name__)

#: The live trend filter's threshold, kept in one place so the two cannot drift.
ADX_TREND_THRESHOLD = 20.0

#: compute_adx needs ADX_MIN_BARS bars; below that it returns its neutral
#: sentinel, which must never reach an attribution. Imported rather than
#: restated so the classifier and the live filter cannot drift apart about
#: what counts as enough history.
MIN_BARS_FOR_ADX = ADX_MIN_BARS

#: Volatility is judged against the symbol's own recent range, so a reading
#: needs enough history for that comparison to mean anything.
MIN_BARS_FOR_VOLATILITY = 20

#: How far the current ATR must sit from the median of the window before the
#: period is called unusually calm or unusually agitated. A convention.
VOLATILITY_BAND = 0.35

UNAVAILABLE = "unknown"


@dataclass(frozen=True)
class RegimeReading:
    """The market state at one moment, from what was knowable at that moment."""

    label: str = UNAVAILABLE
    """trending_up | trending_down | ranging | unknown."""

    volatility: str = UNAVAILABLE
    """calm | normal | agitated | unknown — relative to this symbol's own
    recent range, not to an absolute number."""

    adx: float | None = None
    atr_pct: float | None = None
    """ATR as a fraction of price, which is comparable across symbols in a way
    ATR itself is not."""

    net_move_pct: float | None = None
    """Signed move over the ADX window, as a fraction of the starting price.
    This is what gives a trend its direction: compute_adx reports strength
    without sign."""

    bars_used: int = 0
    available: bool = False
    reason: str = ""
    point_in_time: str = "as_of"
    """as_of — classified from the series held at the classified moment.
    exit_series — classified from the exit-time series, filtered by timestamp.
    The second is a weaker claim: a revision to a pre-entry bar that arrived
    during the hold can shape it."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "volatility": self.volatility,
            "adx": self.adx,
            "atr_pct": self.atr_pct,
            "net_move_pct": self.net_move_pct,
            "bars_used": self.bars_used,
            "available": self.available,
            "reason": self.reason,
            "point_in_time": self.point_in_time,
        }


@dataclass
class RegimeSlice:
    """Aggregated results for every trade taken in one regime."""

    regime: str
    trades: int = 0
    realized: float = 0.0
    from_signal: float = 0.0
    execution_cost: float = 0.0
    wins: int = 0
    incomplete: int = 0
    """Trades in this regime whose decomposition could not be computed. Counted
    so the slice cannot look thinner or richer than the evidence behind it."""

    symbols: set[str] = field(default_factory=set)

    @property
    def win_rate(self) -> float | None:
        return round(self.wins / self.trades, 4) if self.trades else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "trades": self.trades,
            "realized": round(self.realized, 4),
            "from_signal": round(self.from_signal, 4),
            "execution_cost": round(self.execution_cost, 4),
            "win_rate": self.win_rate,
            "incomplete": self.incomplete,
            "symbols": sorted(self.symbols),
        }


def bar_timestamp(bar: dict[str, Any]) -> datetime | None:
    """The market timestamp of a bar row, or None if it cannot be read."""
    raw = bar.get("bar_ts")
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def bars_up_to(bars: list[dict[str, Any]] | None, moment: datetime) -> list[dict[str, Any]]:
    """Bars at or before `moment`, oldest first.

    Filtering by market timestamp is not the same as point-in-time: it removes
    bars from the future but not revisions of past bars that arrived later.
    Callers that can fetch `bars_as_of(moment)` should, and say so.
    """
    if not bars:
        return []
    keep = []
    for bar in bars:
        stamp = bar_timestamp(bar)
        if stamp is not None and stamp <= moment:
            keep.append((stamp, bar))
    keep.sort(key=lambda pair: pair[0])
    return [bar for _, bar in keep]


def classify(
    bars: list[dict[str, Any]] | None,
    moment: datetime,
    *,
    point_in_time: str = "as_of",
) -> RegimeReading:
    """Classify the regime at `moment` from bars knowable at `moment`.

    Returns an unavailable reading — never a default label — when the series is
    too short or malformed. "We could not tell" and "it was ranging" are
    different findings, and only one of them is honest about thin data.
    """
    window = bars_up_to(bars, moment)
    if len(window) < MIN_BARS_FOR_ADX:
        return RegimeReading(
            bars_used=len(window),
            reason=(
                f"need {MIN_BARS_FOR_ADX} bars to compute ADX, have {len(window)}"
            ),
            point_in_time=point_in_time,
        )

    highs = [_number(b.get("high")) for b in window]
    lows = [_number(b.get("low")) for b in window]
    closes = [_number(b.get("close")) for b in window]
    if any(v is None for v in highs + lows + closes):
        return RegimeReading(
            bars_used=len(window),
            reason="series has bars with missing high/low/close",
            point_in_time=point_in_time,
        )

    adx = compute_adx(highs, lows, closes, period=ADX_PERIOD)  # type: ignore[arg-type]
    start = closes[-MIN_BARS_FOR_ADX]
    end = closes[-1]
    net_move_pct = ((end - start) / start) if start else None  # type: ignore[operator]

    if adx < ADX_TREND_THRESHOLD:
        label = "ranging"
    elif net_move_pct is None:
        # Strength without a usable direction. Naming it as trending without a
        # side would invite a reader to supply one.
        label = "trending_unsigned"
    elif net_move_pct >= 0:
        label = "trending_up"
    else:
        label = "trending_down"

    volatility, atr_pct = _volatility(highs, lows, closes)  # type: ignore[arg-type]

    return RegimeReading(
        label=label,
        volatility=volatility,
        adx=round(adx, 4),
        atr_pct=None if atr_pct is None else round(atr_pct, 6),
        net_move_pct=None if net_move_pct is None else round(net_move_pct, 6),
        bars_used=len(window),
        available=True,
        point_in_time=point_in_time,
    )


def _volatility(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[str, float | None]:
    """Current ATR against the symbol's own recent ATR, not an absolute number.

    A 0.5% ATR is placid for one instrument and a storm for another, so an
    absolute threshold would be a statement about the universe rather than
    about the trade. Comparing a symbol to itself is a weaker claim and a true
    one.
    """
    atr = compute_atr(highs, lows, closes, period=ADX_PERIOD)
    price = closes[-1]
    atr_pct = (atr / price) if price else None
    if atr_pct is None or len(closes) < MIN_BARS_FOR_VOLATILITY:
        return UNAVAILABLE, atr_pct

    # The trailing distribution of single-bar ranges, which is what "recent
    # range" means without needing a second smoothed series.
    ranges = [
        (high - low) / close
        for high, low, close in zip(highs, lows, closes, strict=True)
        if close
    ]
    if len(ranges) < MIN_BARS_FOR_VOLATILITY:
        return UNAVAILABLE, atr_pct
    reference = _median(ranges)
    if reference <= 0:
        return UNAVAILABLE, atr_pct

    ratio = atr_pct / reference
    if ratio >= 1.0 + VOLATILITY_BAND:
        return "agitated", atr_pct
    if ratio <= 1.0 - VOLATILITY_BAND:
        return "calm", atr_pct
    return "normal", atr_pct


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def describe_shift(entry: RegimeReading, exit_: RegimeReading) -> dict[str, Any]:
    """Whether the regime changed under the trade.

    A trade entered in a trend and exited in a range is the textbook way for a
    correct signal to lose money, and it is invisible in the price
    decomposition: the signal component simply reads negative with no
    indication that the conditions the rule assumes had stopped holding.
    """
    if not (entry.available and exit_.available):
        return {
            "changed": None,
            "reason": "regime not classifiable at both ends",
            "from": entry.label,
            "to": exit_.label,
        }
    return {
        "changed": entry.label != exit_.label,
        "from": entry.label,
        "to": exit_.label,
        "volatility_from": entry.volatility,
        "volatility_to": exit_.volatility,
    }
