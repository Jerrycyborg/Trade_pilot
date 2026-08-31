"""What a closed trade was made of.

L0 of the adaptive-learning roadmap (docs/adr/0001): attribution only, no
proposals. The purpose of this phase is to answer one question honestly —
**is the recorded data rich enough to explain outcomes?** If it is not, every
later phase is built on sand, and finding that out now is the point.

So these models carry their own gaps. An attribution that could not be computed
says which input was missing rather than substituting a zero, and the coverage
report counts how often that happened. A decomposition that silently treats
absent data as no-effect would make the archive look richer than it is, which
is the one outcome that would make this phase worthless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Leg:
    """One side of a round trip, as the execution record has it."""

    side: str
    qty: float
    decision_price: float | None
    fill_price: float | None
    at: datetime
    fees: float = 0.0
    order_id: str = ""
    outcome: str = ""

    @property
    def usable(self) -> bool:
        return self.fill_price is not None and self.fill_price > 0


@dataclass(frozen=True)
class RoundTrip:
    """An opening leg matched to a closing leg."""

    strategy_id: str
    symbol: str
    environment: str
    account_id: str
    entry: Leg
    exit: Leg
    qty: float

    @property
    def direction(self) -> int:
        """+1 for a long round trip, -1 for a short."""
        return 1 if self.entry.side.upper() == "BUY" else -1

    @property
    def held_for_minutes(self) -> float:
        return (self.exit.at - self.entry.at).total_seconds() / 60.0

    @property
    def realized_per_share(self) -> float | None:
        if not (self.entry.usable and self.exit.usable):
            return None
        return self.direction * (self.exit.fill_price - self.entry.fill_price)

    @property
    def realized(self) -> float | None:
        per_share = self.realized_per_share
        return None if per_share is None else per_share * self.qty


@dataclass
class Attribution:
    """Where a round trip's result came from.

    The three price components are an **exact** decomposition — they sum to the
    realised per-share result — which is what makes them worth arguing about.
    Anything approximate is reported separately as a diagnostic rather than
    folded in, so the identity always holds:

        signal + entry_execution + exit_execution == realized_per_share
    """

    round_trip: RoundTrip

    signal: float | None = None
    """What the strategy's own decisions would have earned with perfect fills:
    the move between the entry decision price and the exit decision price."""

    entry_execution: float | None = None
    """The cost of getting in — decision price minus what was actually paid.
    Negative when the fill was worse than the decision."""

    exit_execution: float | None = None
    """The cost of getting out. Same convention."""

    fees: float = 0.0

    missing: list[str] = field(default_factory=list)
    """Which inputs were absent. An attribution with anything here is partial,
    and the coverage report counts it as such."""

    diagnostics: dict[str, Any] = field(default_factory=dict)
    """Non-additive context: excursions, exit reason, hold time, regime."""

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def total(self) -> float | None:
        if not self.complete:
            return None
        return (self.signal or 0.0) + (self.entry_execution or 0.0) + (
            self.exit_execution or 0.0
        )

    def identity_holds(self, tolerance: float = 1e-6) -> bool:
        """Whether the decomposition actually reconstructs the result.

        Asserted in tests. A decomposition that does not add up is a story, not
        an attribution.
        """
        realized = self.round_trip.realized_per_share
        if realized is None or self.total is None:
            return False
        return abs(self.total - realized) <= tolerance

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.round_trip.strategy_id,
            "symbol": self.round_trip.symbol,
            "environment": self.round_trip.environment,
            "direction": "long" if self.round_trip.direction > 0 else "short",
            "qty": self.round_trip.qty,
            "entry_at": self.round_trip.entry.at.isoformat(),
            "exit_at": self.round_trip.exit.at.isoformat(),
            "held_for_minutes": round(self.round_trip.held_for_minutes, 2),
            "realized_per_share": self.round_trip.realized_per_share,
            "realized": self.round_trip.realized,
            "signal": self.signal,
            "entry_execution": self.entry_execution,
            "exit_execution": self.exit_execution,
            "fees": self.fees,
            "complete": self.complete,
            "missing": self.missing,
            "diagnostics": self.diagnostics,
        }


@dataclass
class Counterfactual:
    """What a different rule would have returned, on what was knowable then."""

    name: str
    question: str
    per_share: float | None
    difference: float | None
    """Counterfactual minus actual. Positive means the alternative did better."""
    available: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "question": self.question,
            "per_share": self.per_share,
            "difference": self.difference,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass
class CoverageReport:
    """Whether the archive can explain what happened. The L0 deliverable.

    A phase that produced only attributions would invite the reader to trust
    them. This says how much of the record could be explained at all, and what
    was missing when it could not — which is the finding L0 exists to produce.
    """

    round_trips: int = 0
    attributable: int = 0
    missing_counts: dict[str, int] = field(default_factory=dict)
    environments: dict[str, int] = field(default_factory=dict)
    identity_failures: int = 0

    @property
    def coverage(self) -> float | None:
        if self.round_trips == 0:
            return None
        return round(self.attributable / self.round_trips, 4)

    @property
    def verdict(self) -> str:
        """A plain reading, so the number is not left to interpretation."""
        if self.round_trips == 0:
            return "no closed round trips in the archive — nothing to explain yet"
        if self.identity_failures:
            return (
                f"{self.identity_failures} attribution(s) did not reconstruct the "
                "realised result — the decomposition is wrong, not merely incomplete"
            )
        share = self.coverage or 0.0
        if share >= 0.95:
            return "the archive explains essentially every closed trade"
        if share >= 0.7:
            return (
                "most trades are explainable; the gaps below are what to fix before "
                "relying on attribution"
            )
        return (
            "the archive cannot explain most of its own trades — later learning "
            "phases would be built on sand until the gaps below are closed"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_trips": self.round_trips,
            "attributable": self.attributable,
            "coverage": self.coverage,
            "identity_failures": self.identity_failures,
            "missing_counts": dict(sorted(self.missing_counts.items())),
            "environments": dict(sorted(self.environments.items())),
            "verdict": self.verdict,
        }
