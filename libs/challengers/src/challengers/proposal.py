"""A challenger: a bounded parameter proposal that cannot become a deployment.

ADR 0001 constraint 1: the learner never deploys. Constraint 4: it never
promotes itself. Both are about the same thing — a proposal must stay a
proposal — and both are arranged here rather than relied upon:

- A `Challenger` is frozen and carries no lifecycle state, no sleeve id, no
  environment and no method that writes anything. There is nothing on it for a
  promotion path to read.
- Its parameters are already clamped at construction. A caller cannot build one
  holding an out-of-range value and clamp it later, because "later" is where
  that step gets skipped.
- Every adjustment the clamp made is recorded on the object. A challenger that
  kept pressing against a bound is visible as exactly that, rather than
  arriving looking like it chose the boundary on merit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .bounds import BOUNDS_VERSION, ChallengerBounds


@dataclass(frozen=True)
class Clamp:
    """One value the bounds refused as proposed."""

    parameter: str
    requested: float
    applied: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "requested": self.requested,
            "applied": self.applied,
        }


class UnboundedParameter(ValueError):
    """A proposal touched a parameter no bound declares.

    Refused rather than clamped to a default. Clamping an undeclared parameter
    would mean the bounds only constrain the fields somebody remembered to
    list, which is not a bound.
    """


@dataclass(frozen=True)
class Challenger:
    """A proposed parameter set. Inert by construction."""

    strategy_id: str
    symbol: str
    base_version: str
    """The champion this was derived from, so a comparison has two named
    sides rather than one side and 'the current thing'."""

    parameters: dict[str, float]
    rationale: str
    """Why this was proposed, in terms a reviewer can disagree with. Required:
    a proposal nobody can argue with is one that gets approved by default."""

    clamped: tuple[Clamp, ...] = ()
    bounds_version: str = BOUNDS_VERSION
    generated_at: datetime | None = None
    generator: str = "deterministic:perturbation/1"

    @property
    def challenger_id(self) -> str:
        """Content-addressed. Two runs proposing the same parameters produce
        the same id, so a campaign cannot inflate its own trial count by
        re-proposing an identical configuration under a new name."""
        payload = json.dumps(
            {
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "base_version": self.base_version,
                "parameters": self.parameters,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "chal-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenger_id": self.challenger_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "base_version": self.base_version,
            "parameters": self.parameters,
            "rationale": self.rationale,
            "clamped": [c.to_dict() for c in self.clamped],
            "bounds_version": self.bounds_version,
            "generator": self.generator,
            "generated_at": (
                self.generated_at.isoformat() if self.generated_at else None
            ),
            "note": (
                "A proposal. It is not registered, not promoted, and has no "
                "path to either — a challenger reaches live only by the same "
                "gates as anything else, including the ones it cannot influence."
            ),
        }


def build_challenger(
    *,
    strategy_id: str,
    symbol: str,
    base_version: str,
    parameters: dict[str, float],
    rationale: str,
    bounds: ChallengerBounds | None = None,
    generated_at: datetime | None = None,
    generator: str = "deterministic:perturbation/1",
) -> Challenger:
    """Clamp, record what moved, and freeze. The only way to make one."""
    active = bounds or ChallengerBounds()
    if not rationale.strip():
        raise ValueError(
            "a challenger needs a rationale: a proposal nobody can argue with "
            "is one that gets approved by default"
        )

    applied: dict[str, float] = {}
    clamps: list[Clamp] = []
    for name, value in sorted(parameters.items()):
        if not active.allows(name):
            raise UnboundedParameter(
                f"{name!r} has no declared bound, so it cannot be proposed. "
                f"Bounded parameters: {sorted(active.parameters)}"
            )
        pulled, moved = active.parameters[name].clamp(float(value))
        applied[name] = pulled
        if moved:
            clamps.append(Clamp(parameter=name, requested=float(value), applied=pulled))

    return Challenger(
        strategy_id=strategy_id,
        symbol=symbol,
        base_version=base_version,
        parameters=applied,
        rationale=rationale.strip(),
        clamped=tuple(clamps),
        bounds_version=BOUNDS_VERSION,
        generated_at=generated_at,
        generator=generator,
    )
