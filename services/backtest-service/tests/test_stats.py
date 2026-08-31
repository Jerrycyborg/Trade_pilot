"""Tests for the overfitting statistics.

The single most important test in this file is
`test_the_best_of_many_noise_runs_does_not_survive_deflation`. If it ever
fails, the machinery that is supposed to catch overfitting has stopped
catching it, and every result the system reports becomes untrustworthy.
"""

from __future__ import annotations

import math
import random

import pytest
from backtest_service.stats import (
    EULER_MASCHERONI,
    annualise,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    kurtosis,
    normal_cdf,
    normal_ppf,
    probabilistic_sharpe_ratio,
    returns_from_equity,
    sharpe_ratio,
    skewness,
    stdev,
)


class TestNormalDistribution:
    def test_cdf_at_known_points(self) -> None:
        assert normal_cdf(0.0) == pytest.approx(0.5)
        assert normal_cdf(1.959963985) == pytest.approx(0.975, abs=1e-6)
        assert normal_cdf(-1.959963985) == pytest.approx(0.025, abs=1e-6)

    def test_ppf_inverts_the_cdf(self) -> None:
        """Verified against the standard normal table, not against itself."""
        assert normal_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)
        assert normal_ppf(0.95) == pytest.approx(1.644854, abs=1e-5)
        assert normal_ppf(0.99) == pytest.approx(2.326348, abs=1e-5)
        assert normal_ppf(0.5) == pytest.approx(0.0, abs=1e-9)

    def test_ppf_round_trips(self) -> None:
        for p in (0.01, 0.25, 0.5, 0.75, 0.999):
            assert normal_cdf(normal_ppf(p)) == pytest.approx(p, abs=1e-9)

    @pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.5])
    def test_ppf_rejects_impossible_probabilities(self, p: float) -> None:
        with pytest.raises(ValueError):
            normal_ppf(p)


class TestMoments:
    def test_symmetric_data_has_no_skew(self) -> None:
        assert skewness([-2.0, -1.0, 0.0, 1.0, 2.0]) == pytest.approx(0.0, abs=1e-12)

    def test_a_long_left_tail_is_negative_skew(self) -> None:
        """The dangerous shape: wins small and often, loses catastrophically."""
        assert skewness([1.0, 1.0, 1.0, 1.0, -10.0]) < 0

    def test_kurtosis_is_not_excess(self) -> None:
        """A normal sample gives ~3, not ~0. The PSR formula depends on this."""
        random.seed(11)
        sample = [random.gauss(0.0, 1.0) for _ in range(20_000)]
        assert kurtosis(sample) == pytest.approx(3.0, abs=0.15)

    def test_degenerate_input_does_not_divide_by_zero(self) -> None:
        assert skewness([1.0, 1.0, 1.0]) == 0.0
        assert kurtosis([1.0, 1.0, 1.0, 1.0]) == 3.0
        assert stdev([2.0]) == 0.0


class TestSharpe:
    def test_sharpe_is_per_period_not_annualised(self) -> None:
        """PSR and DSR are defined on the per-period ratio; annualising first
        would inflate them by the annualisation factor."""
        # mean 0.0025 over a population sd of 0.0075.
        returns = [0.01] * 10 + [-0.005] * 10
        per_period = sharpe_ratio(returns)
        assert per_period == pytest.approx(1 / 3, abs=1e-9)
        assert annualise(per_period, 252) == pytest.approx(math.sqrt(252) / 3)

    def test_a_flat_curve_has_no_sharpe(self) -> None:
        assert sharpe_ratio([0.0, 0.0, 0.0]) == 0.0

    def test_returns_skip_a_wiped_out_account(self) -> None:
        assert returns_from_equity([100.0, 0.0, 50.0]) == [-1.0]


class TestProbabilisticSharpe:
    def test_a_long_consistent_record_is_confident(self) -> None:
        random.seed(3)
        returns = [random.gauss(0.002, 0.01) for _ in range(2_000)]
        assert probabilistic_sharpe_ratio(returns) > 0.99

    def test_the_same_ratio_over_a_short_sample_is_not(self) -> None:
        """Sample length is the point: an identical Sharpe means less over 10 bars."""
        random.seed(3)
        long_run = [random.gauss(0.002, 0.01) for _ in range(2_000)]
        short_run = long_run[:12]
        assert probabilistic_sharpe_ratio(short_run) < probabilistic_sharpe_ratio(long_run)

    def test_a_losing_record_is_below_a_coin_flip(self) -> None:
        random.seed(5)
        returns = [random.gauss(-0.002, 0.01) for _ in range(1_000)]
        assert probabilistic_sharpe_ratio(returns) < 0.5

    def test_too_few_observations_returns_none_rather_than_a_number(self) -> None:
        """None is an answer. A number here would look like confidence."""
        assert probabilistic_sharpe_ratio([0.01, 0.02, 0.01]) is None

    def test_a_flat_record_returns_none(self) -> None:
        assert probabilistic_sharpe_ratio([0.0, 0.0, 0.0, 0.0, 0.0]) is None

    def test_a_higher_benchmark_lowers_the_probability(self) -> None:
        random.seed(7)
        returns = [random.gauss(0.001, 0.01) for _ in range(500)]
        assert probabilistic_sharpe_ratio(returns, 0.0) > probabilistic_sharpe_ratio(
            returns, 0.08
        )


class TestExpectedMaxSharpe:
    def test_more_trials_set_a_higher_bar(self) -> None:
        assert expected_max_sharpe(1_000, 0.01) > expected_max_sharpe(10, 0.01)

    def test_wider_dispersion_sets_a_higher_bar(self) -> None:
        """A search across configurations that differ a lot has more room to
        get lucky, so the winner has more to prove."""
        assert expected_max_sharpe(100, 0.04) > expected_max_sharpe(100, 0.01)

    def test_a_single_trial_deflates_nothing(self) -> None:
        """One configuration is not a search — there is no selection bias."""
        assert expected_max_sharpe(1, 0.25) == 0.0

    def test_identical_trials_deflate_nothing(self) -> None:
        assert expected_max_sharpe(500, 0.0) == 0.0

    def test_scales_linearly_with_the_standard_deviation(self) -> None:
        assert expected_max_sharpe(100, 0.04) == pytest.approx(
            2 * expected_max_sharpe(100, 0.01)
        )

    def test_uses_the_euler_mascheroni_constant(self) -> None:
        assert EULER_MASCHERONI == pytest.approx(0.5772156649, abs=1e-9)


class TestDeflatedSharpe:
    def test_the_best_of_many_noise_runs_does_not_survive_deflation(self) -> None:
        """The test this whole module exists for.

        200 strategies built from pure random noise. The best of them shows an
        annualised Sharpe above 8 and a naive confidence above 99% — numbers
        that would get a strategy funded. The deflated ratio has to see through
        it, because on real data nothing else will.
        """
        random.seed(42)
        trials = [[random.gauss(0.0, 0.01) for _ in range(500)] for _ in range(200)]
        sharpes = [sharpe_ratio(t) for t in trials]
        best = trials[max(range(len(trials)), key=lambda i: sharpes[i])]

        # The trap, stated in numbers.
        assert annualise(sharpe_ratio(best), 6_552) > 8.0
        assert probabilistic_sharpe_ratio(best) > 0.99

        # The correction.
        deflated = deflated_sharpe_ratio(best, sharpes)
        assert deflated is not None
        assert deflated < 0.95

    def test_deflation_only_lowers_confidence(self) -> None:
        random.seed(13)
        returns = [random.gauss(0.001, 0.01) for _ in range(800)]
        sharpes = [sharpe_ratio([random.gauss(0.0, 0.01) for _ in range(800)]) for _ in range(50)]
        assert deflated_sharpe_ratio(returns, sharpes) <= probabilistic_sharpe_ratio(returns)

    def test_more_trials_means_less_confidence_in_the_same_result(self) -> None:
        """Identical returns, a wider search: the result has to be better to mean
        the same thing."""
        random.seed(17)
        returns = [random.gauss(0.0015, 0.01) for _ in range(1_000)]
        pool = [sharpe_ratio([random.gauss(0.0, 0.01) for _ in range(1_000)]) for _ in range(400)]
        few = deflated_sharpe_ratio(returns, pool[:10])
        many = deflated_sharpe_ratio(returns, pool)
        assert many < few

    def test_a_single_trial_matches_the_undeflated_ratio(self) -> None:
        random.seed(19)
        returns = [random.gauss(0.001, 0.01) for _ in range(600)]
        assert deflated_sharpe_ratio(returns, [sharpe_ratio(returns)]) == pytest.approx(
            probabilistic_sharpe_ratio(returns)
        )

    def test_no_trials_falls_back_to_the_undeflated_ratio(self) -> None:
        random.seed(23)
        returns = [random.gauss(0.001, 0.01) for _ in range(600)]
        assert deflated_sharpe_ratio(returns, []) == probabilistic_sharpe_ratio(returns)

    def test_too_short_a_sample_returns_none(self) -> None:
        assert deflated_sharpe_ratio([0.01, 0.02], [0.1, 0.2]) is None
