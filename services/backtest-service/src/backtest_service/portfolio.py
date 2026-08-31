"""Running several strategies and symbols together, and asking whether it helped.

One strategy on one symbol is a bet on one regime. The standard answer is to
run several, but "several" only helps if they lose money at different times.
Two rules that fail together are one rule with extra steps, and combining them
adds cost, complexity and trades without adding any protection at all.

So this module does not assume diversification — it measures it:

- the **correlation matrix** between sleeve returns, because that is the whole
  mechanism, and a pair above ~0.7 is one sleeve counted twice;
- the **diversification ratio**, the weighted average of sleeve volatilities
  over the volatility of the combination. Above 1.0 means the combination is
  less volatile than its parts, which is the only thing diversification buys;
- the combined result against **the best single sleeve**, which is the question
  an operator actually has: would I have been better off just running that one?

A sleeve is one (strategy, symbol, parameters) triple. Sleeves are simulated
independently and combined by weight, which assumes each gets its own capital
and they never compete for it. That is a simplification, and a favourable one —
see the caveats in the README.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from market_data.models import OHLCVBar

from .engine import _compute_signals, _max_drawdown, _profit_factor, _simulate, periods_per_year_for
from .models import (
    BacktestRequest,
    CorrelationPair,
    PortfolioResult,
    SleeveResult,
    StrategyParams,
)
from .stats import (
    annualise,
    deflated_sharpe_ratio,
    mean,
    probabilistic_sharpe_ratio,
    sharpe_ratio,
    stdev,
)
from .strategies import get_strategy

# Above this, two sleeves are describing the same thing and the second one is
# adding cost rather than protection. Not a law — a threshold worth arguing
# with, stated so it can be argued with.
HIGH_CORRELATION = 0.7

ALLOCATIONS = ("equal", "inverse_volatility")

# Sleeve Sharpes are rounded for reporting; comparisons need slack wider than
# that rounding so an exact tie is not reported as a loss.
SHARPE_COMPARISON_MARGIN = 1e-3


@dataclass(frozen=True)
class Sleeve:
    """One strategy, on one symbol, with one set of parameters."""

    strategy: str
    symbol: str
    params: StrategyParams | None = None

    def label(self) -> str:
        return f"{self.symbol}:{self.strategy}"


def _timestamped_returns(
    bars: list[OHLCVBar], start_index: int, curve: list[float]
) -> dict[datetime, float]:
    """Pair each equity step with the bar it happened on.

    Sleeves on different symbols do not share a bar sequence — one may halt,
    list late, or simply have a gap. Combining them by position in a list would
    silently align Tuesday against Thursday, so everything downstream works
    from timestamps.
    """
    stamps = [bar.timestamp for bar in bars[start_index:]]
    if not stamps:
        return {}

    values = curve
    if len(values) > len(stamps):
        # The simulation appends one extra point when it closes a position at
        # the final bar. Fold it into the last timestamp rather than inventing
        # a bar to hang it on.
        values = values[: len(stamps) - 1] + [values[-1]]

    returns: dict[datetime, float] = {}
    for k in range(1, min(len(values), len(stamps))):
        previous = values[k - 1]
        if previous > 0:
            returns[stamps[k]] = (values[k] - previous) / previous
    return returns


def _correlation(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation, or None when one series never moves."""
    if len(left) < 2 or len(left) != len(right):
        return None
    left_sd, right_sd = stdev(left), stdev(right)
    if left_sd == 0.0 or right_sd == 0.0:
        # A sleeve that never traded has no correlation with anything. Reporting
        # 0.0 would read as "usefully uncorrelated", which is the opposite of
        # what an idle sleeve is.
        return None
    left_mean, right_mean = mean(left), mean(right)
    covariance = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)
    ) / len(left)
    return covariance / (left_sd * right_sd)


def _weights(allocation: str, volatilities: list[float]) -> list[float]:
    """Capital split across sleeves."""
    count = len(volatilities)
    if allocation == "inverse_volatility":
        inverses = [1.0 / v if v > 0 else 0.0 for v in volatilities]
        total = sum(inverses)
        if total > 0:
            return [i / total for i in inverses]
        # Every sleeve was flat; there is nothing to weight by.
    return [1.0 / count] * count


def run_portfolio(
    request: BacktestRequest,
    sleeves: list[Sleeve],
    bars_by_symbol: dict[str, list[OHLCVBar]],
    allocation: str = "equal",
    n_trials: int | None = None,
) -> PortfolioResult:
    """Simulate each sleeve, combine them, and report whether combining helped."""
    if not sleeves:
        raise ValueError("A portfolio needs at least one sleeve")
    if allocation not in ALLOCATIONS:
        raise ValueError(f"Unknown allocation {allocation!r}. Available: {', '.join(ALLOCATIONS)}")

    if not bars_by_symbol:
        raise ValueError("No bars supplied for any symbol")

    warnings: list[str] = []
    sleeve_results: list[SleeveResult] = []
    return_maps: list[dict[datetime, float]] = []
    # Bar spacing is inferred from one symbol. They are all fetched with the
    # same timeframe settings, so they share a resolution.
    periods = periods_per_year_for(request, next(iter(bars_by_symbol.values())))

    for sleeve in sleeves:
        bars = bars_by_symbol.get(sleeve.symbol)
        if not bars:
            warnings.append(f"{sleeve.label()}: no bars supplied — sleeve dropped.")
            continue

        strategy = get_strategy(sleeve.strategy)
        params = sleeve.params or request.params
        warmup = strategy.warmup_bars(params)
        if len(bars) <= warmup + 2:
            warnings.append(
                f"{sleeve.label()}: {len(bars)} bars is not enough for a "
                f"{warmup}-bar warm-up — sleeve dropped."
            )
            continue

        variant = request.model_copy(
            update={"symbol": sleeve.symbol, "strategy": sleeve.strategy, "params": params}
        )
        signals = _compute_signals(bars, variant)
        run = _simulate(variant, bars, signals, variant.cost_per_side_pct, start_index=warmup)

        returns_by_stamp = _timestamped_returns(bars, warmup, run.equity_curve)
        series = list(returns_by_stamp.values())
        capital = variant.initial_capital

        sleeve_results.append(
            SleeveResult(
                label=sleeve.label(),
                symbol=sleeve.symbol,
                strategy=sleeve.strategy,
                params=params,
                total_return_pct=round((run.final_equity - capital) / capital, 4),
                sharpe_ratio=round(annualise(sharpe_ratio(series), periods), 4),
                max_drawdown_pct=round(_max_drawdown(run.equity_curve), 4),
                total_trades=len(run.trades),
                profit_factor=round(_profit_factor(run.trades), 4),
                # Volatility annualises by sqrt(periods), same factor as Sharpe.
                volatility=round(annualise(stdev(series), periods), 6),
            )
        )
        return_maps.append(returns_by_stamp)

    if not sleeve_results:
        raise ValueError("No sleeve produced a result — check symbols and bar counts")

    # Align on the union of timestamps. A sleeve with no bar at time t was not
    # trading then, which is a zero return, not a missing one.
    timeline = sorted({stamp for mapping in return_maps for stamp in mapping})
    aligned = [[mapping.get(stamp, 0.0) for stamp in timeline] for mapping in return_maps]

    volatilities = [stdev(series) for series in aligned]
    weights = _weights(allocation, volatilities)

    combined = [
        sum(weight * series[i] for weight, series in zip(weights, aligned, strict=True))
        for i in range(len(timeline))
    ]

    equity = 1.0
    curve = [equity]
    for r in combined:
        equity *= 1.0 + r
        curve.append(equity)

    combined_sharpe = annualise(sharpe_ratio(combined), periods)
    best_sleeve = max(sleeve_results, key=lambda s: s.sharpe_ratio)
    weighted_sleeve_sharpe = sum(
        weight * result.sharpe_ratio
        for weight, result in zip(weights, sleeve_results, strict=True)
    )

    # Diversification ratio: weighted average sleeve volatility over the
    # volatility of the combination. 1.0 means the sleeves move as one.
    portfolio_vol = stdev(combined)
    weighted_vol = sum(w * v for w, v in zip(weights, volatilities, strict=True))
    diversification_ratio = (
        round(weighted_vol / portfolio_vol, 4) if portfolio_vol > 0 else None
    )

    pairs = _correlation_pairs(sleeve_results, aligned, warnings)

    # Compared with a margin: the sleeve figure is rounded for reporting, so a
    # combination that exactly equals its best sleeve would otherwise be
    # reported as having lost to it.
    if combined_sharpe < best_sleeve.sharpe_ratio - SHARPE_COMPARISON_MARGIN:
        warnings.append(
            f"The combination ({combined_sharpe:.2f} Sharpe) did worse than its best "
            f"single sleeve, {best_sleeve.label} ({best_sleeve.sharpe_ratio:.2f}). "
            "Picking that sleeve in hindsight is not a strategy, but neither is "
            "paying costs on the others for nothing."
        )
    if diversification_ratio is not None and diversification_ratio < 1.05:
        warnings.append(
            f"Diversification ratio {diversification_ratio:.2f}: the sleeves are "
            "moving together, so the portfolio is carrying the cost of several "
            "strategies and the risk of one."
        )

    trials = n_trials if n_trials is not None else len(sleeve_results)
    return PortfolioResult(
        timeframe=request.timeframe,
        allocation=allocation,
        sleeves=sleeve_results,
        weights=[round(w, 4) for w in weights],
        aligned_bars=len(timeline),
        total_return_pct=round(equity - 1.0, 4),
        sharpe_ratio=round(combined_sharpe, 4),
        max_drawdown_pct=round(_max_drawdown(curve), 4),
        best_sleeve_label=best_sleeve.label,
        best_sleeve_sharpe=best_sleeve.sharpe_ratio,
        weighted_sleeve_sharpe=round(weighted_sleeve_sharpe, 4),
        diversification_ratio=diversification_ratio,
        correlations=pairs,
        max_correlation=max((p.correlation for p in pairs), default=None),
        probabilistic_sharpe_ratio=_rounded(probabilistic_sharpe_ratio(combined)),
        deflated_sharpe_ratio=_rounded(
            deflated_sharpe_ratio(
                combined,
                [sharpe_ratio(series) for series in aligned],
                n_trials=trials,
            )
        ),
        n_trials=trials,
        warnings=warnings,
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _correlation_pairs(
    sleeve_results: list[SleeveResult],
    aligned: list[list[float]],
    warnings: list[str],
) -> list[CorrelationPair]:
    """Every pairwise correlation, worst (most correlated) first."""
    pairs: list[CorrelationPair] = []
    for i in range(len(aligned)):
        for j in range(i + 1, len(aligned)):
            value = _correlation(aligned[i], aligned[j])
            if value is None:
                continue
            pairs.append(
                CorrelationPair(
                    left=sleeve_results[i].label,
                    right=sleeve_results[j].label,
                    correlation=round(value, 4),
                )
            )
    pairs.sort(key=lambda p: p.correlation, reverse=True)

    for pair in pairs:
        if pair.correlation >= HIGH_CORRELATION:
            warnings.append(
                f"{pair.left} and {pair.right} correlate at {pair.correlation:.2f}. "
                "They will lose money at the same time, so holding both is one "
                "position in two accounts, paying two sets of costs."
            )
    return pairs


def build_sleeves(
    symbols: list[str],
    strategies: list[str],
    params: StrategyParams | None = None,
) -> list[Sleeve]:
    """The cross product of symbols and strategies."""
    for name in strategies:
        get_strategy(name)  # raise early on a typo rather than at run time
    return [
        Sleeve(strategy=strategy, symbol=symbol.upper(), params=params)
        for symbol in symbols
        for strategy in strategies
    ]
