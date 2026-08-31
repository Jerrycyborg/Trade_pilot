"""L3 of the adaptive-learning roadmap (docs/adr/0001): bounded challengers.

The first phase that proposes anything. What keeps that safe is not the
generator being careful — it is that a proposal has nowhere to go:

- A `Challenger` is frozen, carries no lifecycle state, no sleeve id and no
  environment, and has no method that writes. There is nothing on it for a
  promotion path to read.
- Parameters are clamped at construction, not validated afterwards. A validator
  lets a generator retry until something passes; a clamp records what it moved.
- A parameter with no declared bound is **refused**, not clamped to a default,
  so the bounds are not merely a constraint on the fields somebody remembered
  to list.
- Position sizing and risk ceilings are absent by design. Those are safety
  policy, and constraint 2 puts them out of reach of anything automated.

And the statistics are kept honest: `evaluate_campaign` pools every trial from
every challenger and re-deflates each result against the pooled set, because
eight searches of 81 configurations is a search of 648, and reporting each
winner against its own 81 measures a search nobody performed.
"""

from .bounds import BOUNDS_VERSION, Bound, ChallengerBounds
from .campaign import CampaignResult, ChallengerResult, evaluate_campaign
from .compare import (
    Comparison,
    Side,
    champion_of,
    compare,
    derived_strategy_id,
    is_derived,
)
from .generate import DEFAULT_STEPS, describe, perturbations
from .proposal import Challenger, Clamp, UnboundedParameter, build_challenger

__all__ = [
    "BOUNDS_VERSION",
    "Bound",
    "CampaignResult",
    "Challenger",
    "ChallengerBounds",
    "ChallengerResult",
    "Comparison",
    "Side",
    "champion_of",
    "compare",
    "derived_strategy_id",
    "is_derived",
    "Clamp",
    "DEFAULT_STEPS",
    "UnboundedParameter",
    "build_challenger",
    "describe",
    "evaluate_campaign",
    "perturbations",
]
