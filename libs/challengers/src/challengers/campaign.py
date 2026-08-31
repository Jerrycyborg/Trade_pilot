"""Evaluating a set of challengers without making the statistics decorative.

ADR 0001 states the failure mode directly: "Every challenger evaluated must
increment the trial count for every other, or the statistic becomes
decorative."

It is worth being precise about why. A walk-forward deflates its winner against
the configurations *that run* tried. Run it once over 81 configurations and the
deflated Sharpe answers "did this beat the best of 81 random tries?" Run it
eight times over eight challengers and report each winner's own deflated ratio,
and every one of those numbers still answers the 81-question — while the thing
you actually did was search 648 configurations and keep the best. The reported
confidence is then not merely optimistic; it is measuring a search nobody
performed.

So a campaign pools every trial Sharpe from every challenger and re-deflates
each result against the pooled set. The per-run figure is kept beside it, and
the gap between them is reported, because that gap is the size of the error
that pooling corrects — and a reader who has only ever seen the per-run number
should see what it was hiding.

Nothing here registers, promotes, or writes. A surviving challenger is returned
as a proposal and reaches live only through the same gates as anything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .proposal import Challenger

logger = logging.getLogger(__name__)


@dataclass
class ChallengerResult:
    """One challenger's evaluation, on both trial counts."""

    challenger: Challenger
    out_of_sample_sharpe: float | None = None
    out_of_sample_trades: int = 0
    parameter_stability: float | None = None

    deflated_sharpe_own_search: float | None = None
    """As the walk-forward reported it: deflated against that run's grid only.
    Kept for comparison, never used as the gate."""

    deflated_sharpe_campaign: float | None = None
    """Deflated against every configuration the whole campaign evaluated. This
    is the honest one, and the one the gate reads."""

    trials_own_search: int = 0
    trials_campaign: int = 0
    error: str = ""

    @property
    def evaluated(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        own = self.deflated_sharpe_own_search
        campaign = self.deflated_sharpe_campaign
        return {
            "challenger": self.challenger.to_dict(),
            "out_of_sample_sharpe": self.out_of_sample_sharpe,
            "out_of_sample_trades": self.out_of_sample_trades,
            "parameter_stability": self.parameter_stability,
            "deflated_sharpe_own_search": own,
            "deflated_sharpe_campaign": campaign,
            "deflation_overstated_by": (
                round(own - campaign, 4)
                if own is not None and campaign is not None
                else None
            ),
            "trials_own_search": self.trials_own_search,
            "trials_campaign": self.trials_campaign,
            "error": self.error,
        }


@dataclass
class CampaignResult:
    """Every challenger, judged against the whole search that produced them."""

    results: list[ChallengerResult] = field(default_factory=list)
    pooled_trials: int = 0
    min_deflated_sharpe: float = 0.95

    @property
    def survivors(self) -> list[ChallengerResult]:
        """Challengers that clear the bar on the *campaign* trial count.

        Deliberately a list rather than a single winner. Picking the best of
        the survivors is one more selection step, and doing it here would
        reintroduce the exact bias this class exists to price in."""
        return [
            r
            for r in self.results
            if r.evaluated
            and r.deflated_sharpe_campaign is not None
            and r.deflated_sharpe_campaign >= self.min_deflated_sharpe
        ]

    def to_dict(self) -> dict[str, Any]:
        survivors = self.survivors
        return {
            "challengers_evaluated": sum(1 for r in self.results if r.evaluated),
            "pooled_trials": self.pooled_trials,
            "min_deflated_sharpe": self.min_deflated_sharpe,
            "survivors": [r.challenger.challenger_id for r in survivors],
            "verdict": _verdict(self, survivors),
            "results": [r.to_dict() for r in self.results],
        }


def _verdict(campaign: CampaignResult, survivors: list[ChallengerResult]) -> str:
    evaluated = sum(1 for r in campaign.results if r.evaluated)
    if not evaluated:
        return "nothing was evaluated"
    if not survivors:
        return (
            f"no challenger cleared a deflated Sharpe of "
            f"{campaign.min_deflated_sharpe} against {campaign.pooled_trials} "
            f"pooled trials. That is the expected outcome of most campaigns and "
            f"is a result, not a failure — the alternative is a search that "
            f"always finds something."
        )
    return (
        f"{len(survivors)} of {evaluated} challengers cleared the bar against "
        f"{campaign.pooled_trials} pooled trials. Each is a proposal: it is not "
        f"registered and not promoted, and reaches live only through the same "
        f"gates as anything else."
    )


def evaluate_campaign(
    challengers: list[Challenger],
    run_walk_forward: Callable[[Challenger], Any],
    deflate: Callable[[list[float], list[float]], float | None],
    min_deflated_sharpe: float = 0.95,
) -> CampaignResult:
    """Evaluate every challenger, then re-judge all of them together.

    `run_walk_forward` returns something with `out_of_sample_sharpe`,
    `trial_sharpes`, `out_of_sample_returns`, `n_trials`, and the usual fold
    figures — injected rather than imported so this library does not depend on
    the backtest service, and so a test can drive it without a price series.

    Two passes, and the second is the point: the pooled trial set is not known
    until every challenger has run.
    """
    campaign = CampaignResult(min_deflated_sharpe=min_deflated_sharpe)
    pooled: list[float] = []
    raw: list[tuple[ChallengerResult, Any]] = []

    # De-duplicated by content-addressed id, so re-proposing an identical
    # configuration cannot inflate the trial count it is judged against — a
    # generator that did so would be raising the bar for its own siblings with
    # searches it never really ran.
    seen: set[str] = set()
    for challenger in challengers:
        if challenger.challenger_id in seen:
            continue
        seen.add(challenger.challenger_id)

        result = ChallengerResult(challenger=challenger)
        try:
            outcome = run_walk_forward(challenger)
        except Exception as exc:
            # One challenger that cannot be evaluated must not abort the
            # campaign, and must not silently vanish from the trial count
            # either: it did not run, so it contributes no trials.
            result.error = f"{type(exc).__name__}: {exc}"
            campaign.results.append(result)
            logger.debug("Challenger %s failed: %s", challenger.challenger_id, exc)
            continue

        result.out_of_sample_sharpe = getattr(outcome, "out_of_sample_sharpe", None)
        result.out_of_sample_trades = getattr(outcome, "out_of_sample_trades", 0)
        result.parameter_stability = getattr(outcome, "parameter_stability", None)
        result.deflated_sharpe_own_search = getattr(
            outcome, "deflated_sharpe_ratio", None
        )
        trials = list(getattr(outcome, "trial_sharpes", []) or [])
        result.trials_own_search = getattr(outcome, "n_trials", len(trials))
        pooled.extend(trials)
        campaign.results.append(result)
        raw.append((result, outcome))

    campaign.pooled_trials = len(pooled)
    for result, outcome in raw:
        returns = list(getattr(outcome, "out_of_sample_returns", []) or [])
        result.trials_campaign = campaign.pooled_trials
        if not returns or not pooled:
            # No fallback to the per-run figure. Substituting it here would put
            # the overstated number in the field the gate reads, which is the
            # one place it must never appear.
            result.deflated_sharpe_campaign = None
            continue
        result.deflated_sharpe_campaign = deflate(returns, pooled)

    return campaign
