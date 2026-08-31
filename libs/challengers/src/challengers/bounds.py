"""The ranges a proposal may not leave.

ADR 0001 constraint 7: "Parameter proposals are clamped to ranges declared in
configuration. A learner that can propose a 100x position size is one
review-fatigue error away from being catastrophic."

Two things follow from taking that seriously.

**Clamping is not validation.** A validator rejects and lets the caller retry
until something passes, which under a generator means it eventually proposes
whatever it wanted. Here an out-of-range value is pulled to the bound and the
adjustment is recorded on the proposal, so a challenger that kept pressing
against a limit is visible as exactly that rather than silently absent.

**There is no bound on position size here, because this layer cannot propose
one.** Sizing and risk ceilings are safety policy, and constraint 2 puts those
out of reach of anything automated. The absence is deliberate; a `max_size_pct`
field would be the first step to it being fillable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Bumped when a bound's meaning or value changes, so a proposal recorded
#: months ago can be read against the bounds that constrained it.
BOUNDS_VERSION = "1"


@dataclass(frozen=True)
class Bound:
    """A closed interval, and the step a proposal must land on."""

    low: float
    high: float
    integer: bool = False

    def clamp(self, value: float) -> tuple[float, bool]:
        """Return the value pulled into range, and whether it had to move."""
        pulled = min(max(value, self.low), self.high)
        if self.integer:
            pulled = float(round(pulled))
            pulled = min(max(pulled, self.low), self.high)
        return pulled, pulled != value


@dataclass(frozen=True)
class ChallengerBounds:
    """Every parameter a challenger may touch, and nothing else.

    A parameter absent from this mapping cannot be proposed at all — not
    clamped to a default, *refused*. That is the difference between a bounded
    generator and one that can reach anything as long as it stays in range on
    the fields somebody remembered to bound.
    """

    parameters: dict[str, Bound] = field(
        default_factory=lambda: {
            # Momentum. Ranges span where the idea is still recognisably itself;
            # wider mostly buys more chances for noise to win, and every extra
            # trial raises the bar the winner has to clear.
            "ema_fast": Bound(5, 50, integer=True),
            "ema_slow": Bound(20, 120, integer=True),
            "rsi_buy_min": Bound(30.0, 55.0),
            "rsi_buy_max": Bound(60.0, 85.0),
            "macd_hist_min": Bound(0.0, 0.5),
            # Mean reversion.
            "bb_period": Bound(8, 40, integer=True),
            "bb_std": Bound(1.0, 3.0),
            "rsi_oversold": Bound(15.0, 40.0),
            "rsi_overbought": Bound(60.0, 85.0),
        }
    )

    max_challengers_per_campaign: int = 8
    """A cap on how many proposals one run may produce.

    Not a performance limit. Every challenger evaluated raises the deflation
    bar for every other, so an unbounded generator does not find more good
    ideas — it makes all of them statistically indefensible, while producing
    exactly the volume of plausible proposals that trains a reviewer to approve
    them. The ADR names review fatigue as a risk; this is the part of it that
    can be bounded in code."""

    def allows(self, name: str) -> bool:
        return name in self.parameters

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": BOUNDS_VERSION,
            "max_challengers_per_campaign": self.max_challengers_per_campaign,
            "parameters": {
                name: {"low": b.low, "high": b.high, "integer": b.integer}
                for name, b in self.parameters.items()
            },
        }
