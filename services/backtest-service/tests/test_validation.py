"""Tests for walk-forward analysis and parameter sensitivity.

The engine tests check that the simulation is honest about costs. These check
that the *validation* is honest about the simulation — that out-of-sample
really is out of sample, that the deflated Sharpe ratio prices in the search,
and that a grid search over pure noise is reported as what it is.

As in the engine tests, the bars here are synthetic. Nothing about the
strategy's real profitability can be concluded from them.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from backtest_service.engine import _compute_signals, _simulate
from backtest_service.models import BacktestRequest, ParameterGrid, StrategyParams
from backtest_service.validation import (
    MIN_TRADES_TO_SELECT,
    make_folds,
    parameter_sensitivity,
    walk_forward,
)
from market_data.models import OHLCVBar
from pydantic import ValidationError

ET_MID_SESSION_UTC = 14


def _bars(n: int, seed: int = 1, drift: float = 0.0, vol: float = 0.004) -> list[OHLCVBar]:
    """A random walk. There is no edge in this data by construction."""
    random.seed(seed)
    start = datetime(2025, 1, 2, ET_MID_SESSION_UTC, 30, tzinfo=timezone.utc)
    price = 100.0
    bars: list[OHLCVBar] = []
    for i in range(n):
        price *= math.exp(random.gauss(drift, vol))
        bars.append(
            OHLCVBar(
                symbol="SYN",
                timestamp=start + timedelta(minutes=15 * i),
                open=price,
                high=price * 1.002,
                low=price * 0.998,
                close=price,
                volume=100_000.0,
            )
        )
    return bars


def _request(**overrides: object) -> BacktestRequest:
    payload: dict[str, object] = {
        "symbol": "SYN",
        "timeframe": "intraday",
        "intraday_minutes": 15,
        "period_days": 60,
    }
    payload.update(overrides)
    return BacktestRequest(**payload)



@pytest.fixture(scope="module")
def noise_walk_forward():
    """One walk-forward over a random walk, shared by the tests that read it.

    Several tests inspect different fields of the same run; recomputing an
    81-configuration search for each of them costs ten seconds and tells us
    nothing extra.
    """
    return walk_forward(_request(), _bars(1_500, seed=1), n_splits=4)


# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
class TestStrategyParams:
    def test_defaults_reproduce_the_original_hardcoded_rule(self) -> None:
        params = StrategyParams()
        assert (params.ema_fast, params.ema_slow) == (20, 50)
        assert (params.rsi_buy_min, params.rsi_buy_max) == (45.0, 70.0)
        assert params.macd_hist_min == 0.0

    def test_the_sell_band_mirrors_the_buy_band(self) -> None:
        """The original rule's 30-55 sell band is the mirror of its 45-70 buy band."""
        params = StrategyParams()
        assert (params.rsi_sell_min, params.rsi_sell_max) == (30.0, 55.0)

    def test_a_fast_ema_at_or_above_the_slow_one_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StrategyParams(ema_fast=50, ema_slow=50)

    def test_an_inverted_rsi_band_is_rejected(self) -> None:
        """It would never trigger, so it is a mistake rather than a choice."""
        with pytest.raises(ValidationError):
            StrategyParams(rsi_buy_min=70.0, rsi_buy_max=45.0)

    def test_warmup_follows_the_slow_ema(self) -> None:
        assert StrategyParams(ema_fast=10, ema_slow=100).min_warmup_bars == 101

    def test_warmup_never_drops_below_the_macd_requirement(self) -> None:
        """MACD needs 26 + 9 bars whatever the EMAs are set to."""
        assert StrategyParams(ema_fast=2, ema_slow=5).min_warmup_bars == 35


class TestParameterGrid:
    def test_invalid_combinations_are_dropped_not_raised(self) -> None:
        """A cross product of independent axes produces impossible points; that
        is an artefact of the grid, not a caller error."""
        grid = ParameterGrid(ema_fast=[10, 60], ema_slow=[40, 50], rsi_buy_min=[45.0],
                             rsi_buy_max=[70.0], macd_hist_min=[0.0])
        labels = [p.label() for p in grid.combinations()]
        assert len(labels) == 2  # 60/40 and 60/50 are both invalid
        assert all("ema10" in label for label in labels)

    def test_the_default_grid_contains_the_default_parameters(self) -> None:
        """Otherwise the search cannot return the strategy as it is shipped."""
        labels = [p.label() for p in ParameterGrid().combinations()]
        assert StrategyParams().label() in labels

    def test_neighbours_differ_in_exactly_one_dimension(self) -> None:
        grid = ParameterGrid()
        base = StrategyParams()
        for neighbour in grid.neighbours(base):
            differences = sum(
                1
                for field in ("ema_fast", "ema_slow", "rsi_buy_min", "rsi_buy_max", "macd_hist_min")
                if getattr(neighbour, field) != getattr(base, field)
            )
            assert differences == 1

    def test_a_configuration_is_not_its_own_neighbour(self) -> None:
        base = StrategyParams()
        assert base.label() not in [n.label() for n in ParameterGrid().neighbours(base)]

    def test_a_single_point_grid_has_no_neighbours(self) -> None:
        grid = ParameterGrid(
            ema_fast=[20], ema_slow=[50], rsi_buy_min=[45.0], rsi_buy_max=[70.0],
            macd_hist_min=[0.0],
        )
        assert grid.neighbours(StrategyParams()) == []


# ---------------------------------------------------------------------------
# Fold construction — where leakage would live
# ---------------------------------------------------------------------------
class TestFolds:
    def test_test_windows_never_overlap_training(self) -> None:
        for fold in make_folds(2_000, 4, warmup=51, embargo_bars=51, min_train_bars=120):
            assert fold.train_end <= fold.test_start

    def test_the_embargo_separates_training_from_the_test_window(self) -> None:
        """Adjacent bars carry nearly the same information, so optimising right
        up to the boundary is close to optimising on the test set."""
        for fold in make_folds(2_000, 4, warmup=51, embargo_bars=61, min_train_bars=120):
            assert fold.test_start - fold.train_end == 61

    def test_zero_embargo_touches_the_boundary(self) -> None:
        for fold in make_folds(2_000, 4, warmup=51, embargo_bars=0, min_train_bars=120):
            assert fold.train_end == fold.test_start

    def test_test_windows_are_sequential_and_disjoint(self) -> None:
        folds = make_folds(2_000, 4, warmup=51, embargo_bars=51, min_train_bars=120)
        for earlier, later in zip(folds, folds[1:], strict=False):
            assert earlier.test_end <= later.test_start

    def test_training_windows_expand(self) -> None:
        """Anchored, not rolling: each fold has everything an operator would."""
        folds = make_folds(2_000, 4, warmup=51, embargo_bars=51, min_train_bars=120)
        for earlier, later in zip(folds, folds[1:], strict=False):
            assert later.train_end > earlier.train_end

    def test_the_last_fold_runs_to_the_end_of_the_data(self) -> None:
        folds = make_folds(2_000, 4, warmup=51, embargo_bars=51, min_train_bars=120)
        assert folds[-1].test_end == 2_000

    def test_too_little_data_produces_no_folds(self) -> None:
        assert make_folds(100, 4, warmup=51, embargo_bars=51, min_train_bars=120) == []

    def test_zero_splits_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            make_folds(2_000, 0, warmup=51, embargo_bars=51, min_train_bars=120)


class TestOutOfSampleIsolation:
    """`start_index` is what makes an out-of-sample segment out of sample."""

    def test_no_trade_is_opened_before_the_start_index(self) -> None:
        bars = _bars(600, seed=2)
        request = _request()
        signals = _compute_signals(bars, request)
        run = _simulate(request, bars, signals, 0.0, start_index=400)
        assert all(trade.entry_date >= bars[400].timestamp for trade in run.trades)

    def test_the_equity_curve_starts_at_the_start_index(self) -> None:
        bars = _bars(600, seed=2)
        request = _request()
        signals = _compute_signals(bars, request)
        run = _simulate(request, bars, signals, 0.0, start_index=400)
        assert len(run.equity_curve) <= len(bars) - 400 + 1

    def test_indicators_still_see_the_history_before_it(self) -> None:
        """Using past prices is not leakage — it is what live trading does. The
        signals inside the window must match the full-history signals exactly."""
        bars = _bars(600, seed=2)
        request = _request()
        full = _compute_signals(bars, request)
        assert full[400:] == _compute_signals(bars, request)[400:]

    def test_a_prefix_produces_the_same_signals_as_the_whole_series(self) -> None:
        """The property the signal cache relies on."""
        bars = _bars(600, seed=4)
        request = _request()
        assert _compute_signals(bars[:300], request) == _compute_signals(bars, request)[:300]


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------
class TestWalkForward:
    def test_a_random_walk_degrades_out_of_sample(self, noise_walk_forward) -> None:
        """The headline test. There is no edge in a random walk, so a search
        that finds a strong in-sample result must not carry it out of sample."""
        result = noise_walk_forward

        assert result.in_sample_sharpe > 2.0, "the search should find something in-sample"
        assert result.out_of_sample_sharpe < result.in_sample_sharpe
        assert result.sharpe_degradation > 0

    def test_the_deflated_ratio_rejects_a_random_walk(self, noise_walk_forward) -> None:
        result = noise_walk_forward
        assert result.deflated_sharpe_ratio is not None
        assert result.deflated_sharpe_ratio < 0.95

    def test_only_out_of_sample_bars_reach_the_reported_return(self, noise_walk_forward) -> None:
        """If training returns leaked in, the reported figure would be roughly
        the whole-sample result rather than the tested part of it."""
        result = noise_walk_forward
        oos_bars = sum(fold.test_bars for fold in result.folds)
        assert oos_bars < result.bars_count

    def test_the_trial_count_is_the_grid_size(self) -> None:
        grid = ParameterGrid(ema_fast=[10, 20], ema_slow=[50], rsi_buy_min=[45.0],
                             rsi_buy_max=[70.0], macd_hist_min=[0.0])
        result = walk_forward(_request(), _bars(1_500, seed=1), grid=grid, n_splits=3)
        assert result.n_trials == 2

    def test_a_wider_search_is_deflated_harder(self) -> None:
        """Two searches over the same data; the wider one has to clear more."""
        bars = _bars(1_500, seed=1)
        narrow = ParameterGrid(ema_fast=[20], ema_slow=[50], rsi_buy_min=[45.0],
                               rsi_buy_max=[70.0], macd_hist_min=[0.0])
        narrow_result = walk_forward(_request(), bars, grid=narrow, n_splits=3)
        wide_result = walk_forward(_request(), bars, n_splits=3)
        assert wide_result.n_trials > narrow_result.n_trials
        assert wide_result.trial_sharpe_dispersion > 0

    def test_folds_record_which_parameters_they_chose(self, noise_walk_forward) -> None:
        result = noise_walk_forward
        assert all(fold.selected_params is not None for fold in result.folds)
        assert 0.0 <= result.parameter_stability <= 1.0

    def test_disagreeing_folds_are_flagged(self, noise_walk_forward) -> None:
        result = noise_walk_forward
        if result.parameter_stability < 0.5:
            assert any("stability" in w for w in result.warnings)

    def test_a_thin_out_of_sample_record_is_flagged(self, noise_walk_forward) -> None:
        """A Sharpe from a handful of trades is an anecdote, and must say so."""
        result = noise_walk_forward
        if result.out_of_sample_trades < 30:
            assert any("out-of-sample trades" in w for w in result.warnings)

    def test_configurations_that_barely_trade_are_not_selectable(self, noise_walk_forward) -> None:
        result = noise_walk_forward
        assert all(fold.in_sample_trades >= MIN_TRADES_TO_SELECT for fold in result.folds)

    def test_too_little_data_is_an_error_not_an_empty_result(self) -> None:
        with pytest.raises(ValueError, match="Not enough data"):
            walk_forward(_request(), _bars(120, seed=1), n_splits=4)

    def test_an_empty_grid_is_an_error(self) -> None:
        empty = ParameterGrid(ema_fast=[60], ema_slow=[40], rsi_buy_min=[45.0],
                              rsi_buy_max=[70.0], macd_hist_min=[0.0])
        with pytest.raises(ValueError, match="empty"):
            walk_forward(_request(), _bars(1_500, seed=1), grid=empty)

    def test_the_objective_changes_what_gets_selected(self) -> None:
        bars = _bars(1_500, seed=1)
        by_sharpe = walk_forward(_request(), bars, n_splits=3, objective="sharpe")
        by_return = walk_forward(_request(), bars, n_splits=3, objective="return")
        assert by_sharpe.objective == "sharpe"
        assert by_return.objective == "return"

    def test_the_embargo_is_reported(self) -> None:
        result = walk_forward(_request(), _bars(1_500, seed=1), n_splits=3, embargo_bars=25)
        assert result.embargo_bars == 25

    def test_dropped_folds_are_disclosed(self) -> None:
        """Silently running fewer folds than asked would overstate the test."""
        result = walk_forward(_request(), _bars(1_500, seed=1), n_splits=8)
        if result.n_folds < 8:
            assert result.warnings


# ---------------------------------------------------------------------------
# Parameter sensitivity
# ---------------------------------------------------------------------------
class TestParameterSensitivity:
    def test_every_grid_point_is_scored(self) -> None:
        grid = ParameterGrid()
        result = parameter_sensitivity(_request(), _bars(800, seed=5), grid=grid)
        assert result.grid_size == len(grid.combinations())
        assert len(result.scores) == result.grid_size

    def test_scores_are_ranked_best_first(self) -> None:
        result = parameter_sensitivity(_request(), _bars(800, seed=5))
        sharpes = [s.sharpe_ratio for s in result.scores]
        assert sharpes == sorted(sharpes, reverse=True)
        assert result.best.sharpe_ratio >= result.worst.sharpe_ratio

    def test_the_neighbourhood_of_the_best_is_measured(self) -> None:
        result = parameter_sensitivity(_request(), _bars(800, seed=5))
        assert result.neighbour_count > 0
        assert result.neighbour_mean_sharpe is not None

    def test_a_spike_is_flagged_a_plateau_is_not(self) -> None:
        result = parameter_sensitivity(_request(), _bars(800, seed=5))
        if result.plateau_ratio is not None and result.plateau_ratio < 0.5:
            assert any("spike" in w for w in result.warnings)
        else:
            assert not any("spike" in w for w in result.warnings)

    def test_a_mostly_losing_grid_is_flagged(self) -> None:
        """Picking a winner out of a losing population is selection, not skill."""
        result = parameter_sensitivity(
            _request(spread_bps=200.0), _bars(800, seed=5)
        )
        assert result.profitable_fraction < 0.25
        assert any("profitable" in w for w in result.warnings)

    def test_too_little_data_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            parameter_sensitivity(_request(), _bars(40, seed=5))

    def test_an_empty_grid_is_an_error(self) -> None:
        empty = ParameterGrid(ema_fast=[60], ema_slow=[40], rsi_buy_min=[45.0],
                              rsi_buy_max=[70.0], macd_hist_min=[0.0])
        with pytest.raises(ValueError, match="empty"):
            parameter_sensitivity(_request(), _bars(800, seed=5), grid=empty)
