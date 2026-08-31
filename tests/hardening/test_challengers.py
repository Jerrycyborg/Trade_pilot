"""L3: bounded challengers, and the statistics that make them arguable.

Two themes.

**A proposal must have nowhere to go.** ADR 0001 constraints 1 and 4 — the
learner never deploys, never promotes itself. Those hold here because a
Challenger is frozen, carries no lifecycle state and has no method that writes,
not because every caller remembers to be careful.

**The trial count is the whole statistical argument.** The ADR names the
failure directly: eight searches of 81 configurations is a search of 648, and
reporting each winner's deflated Sharpe against its own 81 measures a search
nobody performed. `TestTheTrialCountIsPooled` is the load-bearing set.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from challengers import (
    Bound,
    ChallengerBounds,
    UnboundedParameter,
    build_challenger,
    describe,
    evaluate_campaign,
    perturbations,
)

CHAMPION = {
    "ema_fast": 20.0,
    "ema_slow": 50.0,
    "rsi_buy_min": 45.0,
    "rsi_buy_max": 70.0,
}


def _challenger(**overrides):
    kwargs = dict(
        strategy_id="ema_rsi_macd",
        symbol="AAPL",
        base_version="v1",
        parameters=dict(CHAMPION),
        rationale="a reason a reviewer can disagree with",
    )
    kwargs.update(overrides)
    return build_challenger(**kwargs)


class TestAProposalHasNowhereToGo:
    def test_a_challenger_carries_no_lifecycle_state(self) -> None:
        """Nothing on it for a promotion path to read."""
        challenger = _challenger()
        fields = set(challenger.to_dict())

        assert not fields & {"state", "sleeve_id", "environment", "live", "promoted"}
        assert not hasattr(challenger, "promote")
        assert not hasattr(challenger, "register")

    def test_a_challenger_has_no_method_that_writes(self) -> None:
        challenger = _challenger()
        forbidden = {"save", "write", "apply", "deploy", "commit", "promote", "register"}
        assert forbidden.isdisjoint(dir(challenger))

    def test_a_challenger_cannot_be_edited_after_construction(self) -> None:
        challenger = _challenger()
        with pytest.raises(dataclasses.FrozenInstanceError):
            challenger.parameters = {}  # type: ignore[misc]

    def test_the_package_never_imports_the_lifecycle_authority(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "libs/challengers/src"
        for module in root.rglob("*.py"):
            source = module.read_text()
            assert "import lifecycle" not in source
            assert "from lifecycle" not in source


class TestBoundsAreEnforcedNotSuggested:
    def test_an_out_of_range_value_is_clamped_and_the_move_recorded(self) -> None:
        """A clamp that did not say what it moved would let a challenger arrive
        looking like it chose the boundary on merit."""
        challenger = _challenger(parameters={**CHAMPION, "ema_fast": 500.0})

        assert challenger.parameters["ema_fast"] == 50.0
        assert len(challenger.clamped) == 1
        assert challenger.clamped[0].requested == 500.0
        assert challenger.clamped[0].applied == 50.0

    def test_an_undeclared_parameter_is_refused_not_defaulted(self) -> None:
        """Clamping it would mean the bounds only constrain fields somebody
        remembered to list, which is not a bound."""
        with pytest.raises(UnboundedParameter, match="position_size_pct"):
            _challenger(parameters={**CHAMPION, "position_size_pct": 0.5})

    def test_position_sizing_and_risk_ceilings_are_not_boundable(self) -> None:
        """Absent by design. Those are safety policy, and constraint 2 puts
        them out of reach of anything automated — a `max_size_pct` bound would
        be the first step to it being fillable."""
        names = set(ChallengerBounds().parameters)
        assert not names & {
            "position_size_pct", "max_position_pct", "risk_per_trade",
            "max_drawdown_pct", "leverage", "stop_loss_pct",
        }

    def test_an_integer_bound_lands_on_an_integer(self) -> None:
        assert Bound(5, 50, integer=True).clamp(20.6) == (21.0, True)

    def test_a_proposal_without_a_rationale_is_refused(self) -> None:
        """A proposal nobody can argue with is one that gets approved by
        default."""
        with pytest.raises(ValueError, match="rationale"):
            _challenger(rationale="   ")

    def test_clamping_is_visible_in_the_summary(self) -> None:
        summary = describe([_challenger(parameters={**CHAMPION, "ema_slow": 900.0})])
        assert summary["clamped_count"] == 1


class TestGeneration:
    def test_perturbations_vary_one_axis_at_a_time(self) -> None:
        """A challenger differing in one parameter has a rationale a reviewer
        can evaluate; one differing in six has a story."""
        for challenger in perturbations(
            strategy_id="ema_rsi_macd", symbol="AAPL",
            base_version="v1", champion=CHAMPION,
        ):
            differing = [
                k for k, v in challenger.parameters.items() if v != CHAMPION[k]
            ]
            assert len(differing) == 1

    def test_the_campaign_size_is_capped_by_the_bounds(self) -> None:
        """An unbounded generator does not find more good ideas — it makes all
        of them statistically indefensible."""
        bounds = ChallengerBounds(max_challengers_per_campaign=3)
        produced = perturbations(
            strategy_id="ema_rsi_macd", symbol="AAPL", base_version="v1",
            champion=CHAMPION, bounds=bounds,
        )
        assert len(produced) == 3

    def test_generation_is_reproducible(self) -> None:
        """A campaign whose membership depends on a random seed cannot be
        reproduced, and an unreproducible campaign cannot be reviewed."""
        args = dict(
            strategy_id="ema_rsi_macd", symbol="AAPL",
            base_version="v1", champion=CHAMPION,
        )
        first = [c.challenger_id for c in perturbations(**args)]
        second = [c.challenger_id for c in perturbations(**args)]
        assert first == second

    def test_identical_parameters_share_an_id(self) -> None:
        """So a campaign cannot inflate its own trial count by re-proposing the
        same configuration under a new name."""
        assert _challenger().challenger_id == _challenger(
            rationale="different words, same proposal"
        ).challenger_id


def _outcome(sharpe: float, trials: int, own_dsr: float, bars: int = 120):
    """A stand-in walk-forward result. Injected rather than imported so the
    campaign logic is testable without a price series."""
    return SimpleNamespace(
        out_of_sample_sharpe=sharpe,
        out_of_sample_trades=40,
        parameter_stability=0.8,
        deflated_sharpe_ratio=own_dsr,
        n_trials=trials,
        trial_sharpes=[0.01 * i for i in range(trials)],
        out_of_sample_returns=[0.001 * ((i % 7) - 3) for i in range(bars)],
    )


class TestTheTrialCountIsPooled:
    """The ADR's own stated failure mode: "Every challenger evaluated must
    increment the trial count for every other, or the statistic becomes
    decorative."
    """

    def test_the_pooled_count_is_the_sum_across_challengers(self) -> None:
        challengers = perturbations(
            strategy_id="ema_rsi_macd", symbol="AAPL",
            base_version="v1", champion=CHAMPION,
        )
        seen: list[int] = []

        def run(_c):
            seen.append(1)
            return _outcome(1.2, trials=81, own_dsr=0.97)

        campaign = evaluate_campaign(
            challengers, run, deflate=lambda _r, pooled: 1.0 - len(pooled) / 10_000
        )

        assert campaign.pooled_trials == 81 * len(seen)
        assert all(r.trials_campaign == campaign.pooled_trials for r in campaign.results)
        assert all(r.trials_own_search == 81 for r in campaign.results)

    def test_the_gate_reads_the_pooled_figure_not_the_per_run_one(self) -> None:
        """Eight searches of 81 is a search of 648. Reporting each winner
        against its own 81 measures a search nobody performed."""
        challengers = perturbations(
            strategy_id="ema_rsi_macd", symbol="AAPL",
            base_version="v1", champion=CHAMPION,
        )
        campaign = evaluate_campaign(
            challengers,
            lambda _c: _outcome(1.2, trials=81, own_dsr=0.99),
            # Pooling makes the bar higher, as it must.
            deflate=lambda _r, pooled: 0.90,
            min_deflated_sharpe=0.95,
        )

        assert campaign.survivors == [], "0.90 pooled must not pass on a 0.99 per-run"
        first = campaign.results[0]
        assert first.deflated_sharpe_own_search == 0.99
        assert first.deflated_sharpe_campaign == 0.90
        assert first.to_dict()["deflation_overstated_by"] == pytest.approx(0.09)

    def test_a_challenger_that_could_not_run_contributes_no_trials(self) -> None:
        """It did not search anything, so it must not raise the bar for the
        ones that did — and it must not vanish either."""
        challengers = perturbations(
            strategy_id="ema_rsi_macd", symbol="AAPL",
            base_version="v1", champion=CHAMPION,
        )
        calls = {"n": 0}

        def run(_c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("not enough bars")
            return _outcome(1.0, trials=10, own_dsr=0.9)

        campaign = evaluate_campaign(
            challengers, run, deflate=lambda _r, pooled: 0.96
        )

        assert campaign.pooled_trials == 10 * (len(challengers) - 1)
        failed = [r for r in campaign.results if not r.evaluated]
        assert len(failed) == 1
        assert "not enough bars" in failed[0].error

    def test_a_missing_pooled_figure_never_falls_back_to_the_per_run_one(self) -> None:
        """Substituting it would put the overstated number in the one field the
        gate reads."""
        campaign = evaluate_campaign(
            [_challenger()],
            lambda _c: SimpleNamespace(
                out_of_sample_sharpe=2.0, out_of_sample_trades=50,
                parameter_stability=0.9, deflated_sharpe_ratio=0.99,
                n_trials=81, trial_sharpes=[0.1] * 81,
                out_of_sample_returns=[],  # cannot re-deflate without these
            ),
            deflate=lambda _r, _p: 0.99,
        )

        assert campaign.results[0].deflated_sharpe_campaign is None
        assert campaign.survivors == []

    def test_duplicate_challengers_are_evaluated_once(self) -> None:
        """A generator re-proposing an identical configuration would otherwise
        raise the bar for its own siblings with a search it never ran."""
        one = _challenger()
        campaign = evaluate_campaign(
            [one, one, one],
            lambda _c: _outcome(1.0, trials=50, own_dsr=0.9),
            deflate=lambda _r, _p: 0.96,
        )

        assert campaign.pooled_trials == 50
        assert len(campaign.results) == 1


class TestTheCampaignReportsHonestly:
    def test_finding_nothing_is_reported_as_a_result(self) -> None:
        """The alternative is a search that always finds something."""
        campaign = evaluate_campaign(
            [_challenger()],
            lambda _c: _outcome(0.3, trials=81, own_dsr=0.4),
            deflate=lambda _r, _p: 0.31,
        )

        assert campaign.survivors == []
        assert "not a failure" in campaign.to_dict()["verdict"]

    def test_a_survivor_is_described_as_a_proposal(self) -> None:
        campaign = evaluate_campaign(
            [_challenger()],
            lambda _c: _outcome(2.0, trials=81, own_dsr=0.99),
            deflate=lambda _r, _p: 0.97,
        )

        assert len(campaign.survivors) == 1
        rendered = campaign.to_dict()
        assert "not registered and not promoted" in rendered["verdict"]
        assert "not promoted" in rendered["results"][0]["challenger"]["note"]

    def test_survivors_are_a_list_rather_than_a_winner(self) -> None:
        """Picking the best of the survivors is one more selection step, and
        doing it here would reintroduce the bias the pooling exists to price."""
        campaign = evaluate_campaign(
            perturbations(
                strategy_id="ema_rsi_macd", symbol="AAPL",
                base_version="v1", champion=CHAMPION,
            ),
            lambda _c: _outcome(2.0, trials=20, own_dsr=0.99),
            deflate=lambda _r, _p: 0.97,
        )

        assert len(campaign.survivors) > 1
        assert not hasattr(campaign, "winner")
        assert not hasattr(campaign, "best")
