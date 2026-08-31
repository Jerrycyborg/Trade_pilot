"""Where proposals come from.

Deliberately dull: local perturbations of the champion's parameters, one axis
at a time, on a fixed grid of relative steps. Dull is the requirement rather
than a limitation of effort.

A generator's job here is not to be clever. Every challenger it emits raises
the deflation bar for every other, so cleverness that produces more candidates
actively destroys the campaign's ability to conclude anything — and a stream of
plausible proposals is the mechanism behind the review fatigue the ADR names as
a risk. A small, explainable, reproducible set is worth more than a large one.

One axis at a time, also deliberately. A challenger differing from the champion
in one parameter has a rationale a reviewer can evaluate; one differing in six
has a story. And when a perturbation wins, a single-axis change says which
axis, which is the input to the next question rather than to a shrug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .bounds import ChallengerBounds
from .proposal import Challenger, build_challenger

#: Relative steps applied to each axis. Symmetric so the generator has no
#: directional prior — it is not trying to make a parameter bigger, it is
#: asking whether the champion's value is a peak or a point on a slope.
DEFAULT_STEPS: tuple[float, ...] = (-0.2, 0.2)


def perturbations(
    *,
    strategy_id: str,
    symbol: str,
    base_version: str,
    champion: dict[str, float],
    axes: list[str] | None = None,
    steps: tuple[float, ...] = DEFAULT_STEPS,
    bounds: ChallengerBounds | None = None,
    generated_at: datetime | None = None,
) -> list[Challenger]:
    """One-axis perturbations of the champion, capped and de-duplicated.

    The cap is enforced here rather than left to the caller. A generator whose
    output size depends on how it was invoked is one that will eventually be
    invoked wrongly, and the cost of that mistake is a campaign in which
    nothing can clear the bar.
    """
    active = bounds or ChallengerBounds()
    candidates = [a for a in (axes or sorted(champion)) if active.allows(a)]

    out: list[Challenger] = []
    seen: set[str] = set()
    for axis in candidates:
        base = float(champion[axis])
        for step in steps:
            proposed = dict(champion)
            proposed[axis] = base * (1.0 + step)
            challenger = build_challenger(
                strategy_id=strategy_id,
                symbol=symbol,
                base_version=base_version,
                parameters={k: float(v) for k, v in proposed.items() if active.allows(k)},
                rationale=(
                    f"{axis} {step:+.0%} from the champion's {base:g}. Asks whether "
                    f"that value is a peak or a point on a slope — a champion "
                    f"sitting on a spike is fitted to the sample that chose it."
                ),
                bounds=active,
                generated_at=generated_at,
            )
            # A perturbation that clamps back onto the champion is not a
            # challenger, it is the champion with a story attached.
            if challenger.parameters == {
                k: float(v) for k, v in champion.items() if active.allows(k)
            }:
                continue
            if challenger.challenger_id in seen:
                continue
            seen.add(challenger.challenger_id)
            out.append(challenger)

    if len(out) > active.max_challengers_per_campaign:
        # Truncated deterministically rather than sampled. A campaign whose
        # membership depends on a random seed cannot be reproduced, and an
        # unreproducible campaign cannot be reviewed.
        out = sorted(out, key=lambda c: c.challenger_id)[
            : active.max_challengers_per_campaign
        ]
    return out


def describe(challengers: list[Challenger]) -> dict[str, Any]:
    """A summary for a reviewer, including what the bounds refused."""
    clamped = [c for c in challengers if c.clamped]
    return {
        "count": len(challengers),
        "clamped_count": len(clamped),
        "clamped": [
            {"challenger_id": c.challenger_id, "adjustments": [a.to_dict() for a in c.clamped]}
            for c in clamped
        ],
        "note": (
            "A challenger that kept pressing against a bound is shown as such "
            "rather than arriving looking like it chose the boundary on merit."
        ),
    }
