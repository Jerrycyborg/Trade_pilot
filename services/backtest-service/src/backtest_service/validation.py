"""Walk-forward analysis and parameter sensitivity.

A single backtest over the whole history answers "what would have happened?".
It cannot answer "would it happen again?", and it is trivially gamed: try
enough parameter combinations and one of them fits the sample. That is not a
subtle risk. The best of 200 configurations run on *pure random noise*
routinely produces an annualised Sharpe above 10 and a naive confidence of
99.8% — see `tests/backtest/test_validation.py`, which asserts exactly that.

This module implements the two checks that catch it.

**Walk-forward.** Parameters are chosen on data that precedes the data they are
judged on, repeatedly, the way they would have been in real time. Only the
out-of-sample segments are stitched together and reported. The number worth
reading is not the out-of-sample Sharpe on its own — it is the *gap* between
in-sample and out-of-sample. A strategy that scores 2.5 in-sample and 0.1
out-of-sample has not found an edge; it has memorised a sample.

**Parameter sensitivity.** A real effect is a plateau — nearby parameters work
nearly as well. A fitted one is a spike, surrounded by configurations that
lose money. The neighbourhood of the best result says more about whether an
edge exists than the best result does.

Both are reported alongside a deflated Sharpe ratio, which prices in how many
configurations had to be tried to find the winner.
"""

from __future__ import annotations

from dataclasses import dataclass

from market_data.models import OHLCVBar

from .engine import _compute_signals, _max_drawdown, _profit_factor, _simulate, periods_per_year_for
from .models import (
    BacktestRequest,
    FoldResult,
    ParameterGrid,
    ParameterSensitivityResult,
    ParamScore,
    StrategyParams,
    WalkForwardResult,
)
from .stats import (
    annualise,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    returns_from_equity,
    sharpe_ratio,
    stdev,
)

# A configuration that never traded in the training window tells us nothing
# about how it will trade out of sample. Selecting one because its flat equity
# curve scored 0.0 while everything else scored negative is not selection.
MIN_TRADES_TO_SELECT = 3

# Below this many out-of-sample round trips, the reported statistics describe
# a few specific trades rather than a strategy. There is no clean threshold in
# the literature; 30 is the conventional rule-of-thumb point at which a sample
# mean starts to behave, and it is stated here as a convention rather than a
# derivation.
MIN_OOS_TRADES_FOR_CONFIDENCE = 30


@dataclass(frozen=True)
class Fold:
    """One train/test split, in bar indices. `test` is always after `train`."""

    index: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def train_bars(self) -> int:
        return self.train_end

    @property
    def test_bars(self) -> int:
        return self.test_end - self.test_start


def make_folds(
    n_bars: int,
    n_splits: int,
    warmup: int,
    embargo_bars: int,
    min_train_bars: int,
) -> list[Fold]:
    """Sequential expanding-window splits, with an embargo before each test window.

    Training is anchored at the start and grows: that is what an operator
    actually has available at each point in time, and it uses every bar of
    history rather than throwing the oldest away.

    The embargo drops the bars immediately before each test window from
    training. Adjacent bars carry almost the same information — an EMA at the
    boundary is mostly built from bars that are about to become test data — so
    optimising right up to the edge is close to optimising on the test set
    itself. The embargo is the cheapest available defence against that.
    """
    if n_splits < 1:
        raise ValueError(f"n_splits must be at least 1, got {n_splits}")

    tradeable = n_bars - warmup
    if tradeable <= 0:
        return []

    # Reserve the first chunk for the initial training window; split the rest
    # into equal test windows.
    test_total = tradeable - min_train_bars
    if test_total <= 0:
        return []
    window = test_total // n_splits
    if window < 2:
        return []

    folds: list[Fold] = []
    for k in range(n_splits):
        test_start = warmup + min_train_bars + k * window
        test_end = test_start + window if k < n_splits - 1 else n_bars
        train_end = max(0, test_start - embargo_bars)
        if train_end < warmup + min_train_bars // 2 or test_end - test_start < 2:
            continue
        folds.append(
            Fold(index=len(folds), train_end=train_end, test_start=test_start, test_end=test_end)
        )
    return folds


class _SignalCache:
    """Signals for each configuration, computed once over the whole series.

    The signal on bar i depends only on bars[:i+1], so the signals for any
    prefix of the data are the matching prefix of the signals for all of it.
    Recomputing them per fold — as the obvious implementation does — makes a
    walk-forward over an 81-point grid five times more expensive than it needs
    to be, and the indicator pass is already quadratic in the bar count.
    """

    def __init__(self, request: BacktestRequest, bars: list[OHLCVBar]) -> None:
        self._request = request
        self._bars = bars
        self._cache: dict[str, list[str]] = {}

    def for_params(self, params: StrategyParams) -> list[str]:
        key = params.label()
        if key not in self._cache:
            variant = self._request.model_copy(update={"params": params})
            self._cache[key] = _compute_signals(self._bars, variant)
        return self._cache[key]


def _score(
    request: BacktestRequest,
    bars: list[OHLCVBar],
    params: StrategyParams,
    start_index: int,
    end_index: int,
    signals: _SignalCache | None = None,
) -> tuple[float, float, int, float, list[float]]:
    """Run one configuration over bars[:end_index], trading only from start_index.

    Returns (per-period sharpe, total return, trade count, profit factor, returns).
    """
    variant = request.model_copy(update={"params": params})
    window = bars[:end_index]
    cached = (
        signals.for_params(params)
        if signals is not None
        else _compute_signals(window, variant)
    )
    run = _simulate(
        variant,
        window,
        cached[:end_index],
        variant.cost_per_side_pct,
        start_index=start_index,
    )
    returns = returns_from_equity(run.equity_curve)
    capital = variant.initial_capital
    return (
        sharpe_ratio(returns),
        (run.final_equity - capital) / capital,
        len(run.trades),
        _profit_factor(run.trades),
        returns,
    )


def _objective_value(objective: str, sharpe: float, total_return: float, pf: float) -> float:
    if objective == "return":
        return total_return
    if objective == "profit_factor":
        return pf
    return sharpe


def walk_forward(
    request: BacktestRequest,
    bars: list[OHLCVBar],
    grid: ParameterGrid | None = None,
    n_splits: int = 4,
    embargo_bars: int | None = None,
    objective: str = "sharpe",
) -> WalkForwardResult:
    """Select parameters on past data, judge them on the data that followed.

    The result reports only out-of-sample performance. The in-sample figures
    are included for one purpose: to show the size of the drop.
    """
    grid = grid or ParameterGrid()
    combos = grid.combinations()
    if not combos:
        raise ValueError("Parameter grid is empty — every combination was invalid")

    warmup = max(p.min_warmup_bars for p in combos)
    embargo = warmup if embargo_bars is None else embargo_bars
    # A training window shorter than a few multiples of the warm-up cannot
    # produce enough trades to choose between configurations.
    min_train = max(warmup * 2, 60)

    folds = make_folds(len(bars), n_splits, warmup, embargo, min_train)
    warnings: list[str] = []
    if not folds:
        raise ValueError(
            f"Not enough data for {n_splits} walk-forward folds: {len(bars)} bars, "
            f"{warmup} needed for warm-up and {min_train} for the first training "
            f"window. Increase period_days or reduce n_splits."
        )
    if len(folds) < n_splits:
        warnings.append(
            f"Only {len(folds)} of {n_splits} folds had enough data; the rest were dropped."
        )

    periods = periods_per_year_for(request, bars)
    signals = _SignalCache(request, bars)

    fold_results: list[FoldResult] = []
    oos_returns: list[float] = []
    in_sample_sharpes: list[float] = []
    selected: list[StrategyParams] = []

    for fold in folds:
        best: tuple[float, StrategyParams, float, int] | None = None
        for params in combos:
            sharpe, total_return, trades, pf, _ = _score(
                request, bars, params, start_index=warmup, end_index=fold.train_end,
                signals=signals,
            )
            if trades < MIN_TRADES_TO_SELECT:
                continue
            value = _objective_value(objective, sharpe, total_return, pf)
            if best is None or value > best[0]:
                best = (value, params, sharpe, trades)

        if best is None:
            warnings.append(
                f"Fold {fold.index}: no configuration traded at least "
                f"{MIN_TRADES_TO_SELECT} times in training — fold skipped."
            )
            continue

        _, params, is_sharpe, is_trades = best
        oos_sharpe, oos_return, oos_trades, oos_pf, fold_returns = _score(
            request, bars, params, start_index=fold.test_start, end_index=fold.test_end,
            signals=signals,
        )

        selected.append(params)
        in_sample_sharpes.append(is_sharpe)
        oos_returns.extend(fold_returns)
        fold_results.append(
            FoldResult(
                fold=fold.index,
                train_bars=fold.train_bars,
                test_bars=fold.test_bars,
                train_end=bars[max(0, fold.train_end - 1)].timestamp,
                test_start=bars[fold.test_start].timestamp,
                test_end=bars[min(fold.test_end, len(bars)) - 1].timestamp,
                selected_params=params,
                in_sample_sharpe=round(annualise(is_sharpe, periods), 4),
                in_sample_trades=is_trades,
                out_of_sample_sharpe=round(annualise(oos_sharpe, periods), 4),
                out_of_sample_return_pct=round(oos_return, 4),
                out_of_sample_trades=oos_trades,
                out_of_sample_profit_factor=round(oos_pf, 4),
            )
        )

    if not fold_results:
        raise ValueError(
            "No fold produced a usable result — no configuration traded enough in "
            "any training window. The strategy may be too selective for this data."
        )

    # Stitch the out-of-sample segments into one continuous record.
    equity = 1.0
    curve = [equity]
    for r in oos_returns:
        equity *= 1.0 + r
        curve.append(equity)

    oos_sharpe_per_period = sharpe_ratio(oos_returns)
    is_mean = sum(in_sample_sharpes) / len(in_sample_sharpes)

    # The trial count for deflation: every configuration that competed for
    # selection. Computed on the full sample so the trial Sharpes are
    # comparable to each other.
    trial_sharpes = [
        _score(
            request, bars, params, start_index=warmup, end_index=len(bars), signals=signals
        )[0]
        for params in combos
    ]

    total_oos_trades = sum(f.out_of_sample_trades for f in fold_results)
    if total_oos_trades < MIN_OOS_TRADES_FOR_CONFIDENCE:
        warnings.append(
            f"Only {total_oos_trades} out-of-sample trades. A Sharpe ratio computed "
            "from a handful of round trips is dominated by which ones they happened "
            "to be — treat every figure here as an anecdote, not a measurement."
        )

    stability = _parameter_stability(selected)
    if stability < 0.5 and len(selected) > 1:
        warnings.append(
            f"Parameter stability {stability:.0%}: the folds disagree about which "
            "configuration is best, which is what fitting to noise looks like."
        )

    return WalkForwardResult(
        symbol=request.symbol,
        timeframe=request.timeframe,
        bars_count=len(bars),
        n_folds=len(fold_results),
        n_trials=len(combos),
        objective=objective,
        embargo_bars=embargo,
        folds=fold_results,
        in_sample_sharpe=round(annualise(is_mean, periods), 4),
        out_of_sample_sharpe=round(annualise(oos_sharpe_per_period, periods), 4),
        out_of_sample_return_pct=round(equity - 1.0, 4),
        out_of_sample_max_drawdown_pct=round(_max_drawdown(curve), 4),
        out_of_sample_trades=sum(f.out_of_sample_trades for f in fold_results),
        sharpe_degradation=round(annualise(is_mean - oos_sharpe_per_period, periods), 4),
        probabilistic_sharpe_ratio=_rounded(probabilistic_sharpe_ratio(oos_returns)),
        deflated_sharpe_ratio=_rounded(deflated_sharpe_ratio(oos_returns, trial_sharpes)),
        trial_sharpe_dispersion=round(annualise(stdev(trial_sharpes), periods), 4),
        parameter_stability=round(stability, 4),
        warnings=warnings,
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _parameter_stability(selected: list[StrategyParams]) -> float:
    """Share of folds that chose the single most-chosen configuration.

    1.0 means every fold independently landed on the same parameters, which is
    weak evidence that they describe something real. A low value means the
    "optimal" parameters are a property of the window, not of the market.
    """
    if not selected:
        return 0.0
    counts: dict[str, int] = {}
    for params in selected:
        key = params.label()
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(selected)


def parameter_sensitivity(
    request: BacktestRequest,
    bars: list[OHLCVBar],
    grid: ParameterGrid | None = None,
) -> ParameterSensitivityResult:
    """Score every configuration in the grid, then look at the shape of the result.

    This is deliberately in-sample: the question is not "how much does the best
    one make?" but "does the surface around it hold up, or is the winner alone
    on a peak?" A spike surrounded by losses is a fitted artefact whatever its
    Sharpe says.
    """
    grid = grid or ParameterGrid()
    combos = grid.combinations()
    if not combos:
        raise ValueError("Parameter grid is empty — every combination was invalid")

    warmup = max(p.min_warmup_bars for p in combos)
    if len(bars) <= warmup + 2:
        raise ValueError(
            f"Need more than {warmup + 2} bars to score this grid, got {len(bars)}"
        )

    periods = periods_per_year_for(request, bars)
    signals = _SignalCache(request, bars)
    scores: dict[str, ParamScore] = {}
    for params in combos:
        sharpe, total_return, trades, pf, _ = _score(
            request, bars, params, start_index=warmup, end_index=len(bars), signals=signals
        )
        scores[params.label()] = ParamScore(
            params=params,
            sharpe_ratio=round(annualise(sharpe, periods), 4),
            total_return_pct=round(total_return, 4),
            total_trades=trades,
            profit_factor=round(pf, 4),
        )

    ranked = sorted(scores.values(), key=lambda s: s.sharpe_ratio, reverse=True)
    best = ranked[0]

    neighbours = [
        scores[n.label()] for n in grid.neighbours(best.params) if n.label() in scores
    ]
    neighbour_sharpes = [n.sharpe_ratio for n in neighbours]
    neighbour_mean = sum(neighbour_sharpes) / len(neighbour_sharpes) if neighbour_sharpes else None

    profitable = sum(1 for s in scores.values() if s.total_return_pct > 0)

    # How much of the best result survives one step away from it. Near 1.0 is a
    # plateau; near 0 or negative is a spike, and a spike is an artefact.
    plateau_ratio = (
        round(neighbour_mean / best.sharpe_ratio, 4)
        if neighbour_mean is not None and best.sharpe_ratio > 0
        else None
    )

    warnings: list[str] = []
    if plateau_ratio is not None and plateau_ratio < 0.5:
        warnings.append(
            f"The best configuration's neighbours average {neighbour_mean:.2f} Sharpe "
            f"against its own {best.sharpe_ratio:.2f}. That is a spike, not a plateau: "
            "one step away in any single parameter and most of the result is gone."
        )
    if profitable / len(scores) < 0.25:
        warnings.append(
            f"Only {profitable}/{len(scores)} configurations were profitable. The "
            "winner is being drawn from a mostly losing population."
        )

    return ParameterSensitivityResult(
        symbol=request.symbol,
        timeframe=request.timeframe,
        grid_size=len(combos),
        best=best,
        worst=ranked[-1],
        scores=ranked,
        profitable_count=profitable,
        profitable_fraction=round(profitable / len(scores), 4),
        neighbour_count=len(neighbours),
        neighbour_mean_sharpe=None if neighbour_mean is None else round(neighbour_mean, 4),
        plateau_ratio=plateau_ratio,
        sharpe_dispersion=round(
            stdev([s.sharpe_ratio for s in scores.values()]), 4
        ),
        warnings=warnings,
    )


def default_grid() -> ParameterGrid:
    """A deliberately small grid.

    Every extra configuration raises the Sharpe the winner must clear to mean
    anything — that is what the deflated ratio does — so a wide grid is not a
    more thorough search, it is a more expensive one. These axes span the range
    where the momentum idea is still recognisably itself.
    """
    return ParameterGrid()


__all__ = [
    "Fold",
    "ParameterGrid",
    "default_grid",
    "make_folds",
    "parameter_sensitivity",
    "walk_forward",
]
