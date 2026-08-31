"""Statistics for judging whether a backtest result is evidence of an edge.

A Sharpe ratio computed from one backtest answers a question nobody asked:
"how did this configuration do on this sample?" The question that matters is
"would it do that again?", and the gap between the two is where most retail
strategies die.

Three things corrupt a naive Sharpe:

**Selection bias.** Try 200 parameter combinations and the best one will look
good even on pure noise — the maximum of 200 draws is not a typical draw. The
deflated Sharpe ratio subtracts the level you would expect the best of N trials
to reach by luck alone.

**Non-normality.** Sharpe's usual confidence interval assumes normal returns.
Trading returns are skewed and fat-tailed — a strategy that wins small and
often but loses catastrophically has a flattering Sharpe and a real risk of
ruin. The probabilistic Sharpe ratio takes skew and kurtosis into account.

**Sample length.** A Sharpe of 2.0 over 40 bars is noise. Over 4,000 it is
worth investigating. Both corrections scale with the number of observations.

Sources:
  Bailey, D. and Lopez de Prado, M. (2012), "The Sharpe Ratio Efficient
  Frontier", Journal of Risk 15(2) — the probabilistic Sharpe ratio.
  Bailey, D. and Lopez de Prado, M. (2014), "The Deflated Sharpe Ratio:
  Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
  Journal of Portfolio Management 40(5) — the deflated Sharpe ratio.

Everything here is pure Python. scipy is not a dependency of this repo, and the
two functions that would need it (the normal CDF and its inverse) are exact
enough from `math.erf` and a bisection to be worth more than the dependency.
"""

from __future__ import annotations

import math

# Euler-Mascheroni constant, as it appears in the expected-maximum formula.
EULER_MASCHERONI = 0.5772156649015329


def normal_cdf(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_ppf(p: float, tolerance: float = 1e-12) -> float:
    """Inverse standard normal CDF, by bisection on `normal_cdf`.

    Bisection rather than a rational approximation: it is slower and completely
    transparent, and this is called a handful of times per run, not per bar. A
    mistranscribed approximation coefficient would silently bias every deflated
    Sharpe ratio the system ever reports.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"normal_ppf needs 0 < p < 1, got {p}")

    low, high = -40.0, 40.0
    while high - low > tolerance:
        mid = (low + high) / 2.0
        if normal_cdf(mid) < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    """Population standard deviation — the moment estimator, matching the papers."""
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def skewness(values: list[float]) -> float:
    """Third standardised moment. Negative means a long left tail — the shape
    of "wins small and often, loses big"."""
    if len(values) < 3:
        return 0.0
    sigma = stdev(values)
    if sigma == 0.0:
        return 0.0
    mu = mean(values)
    return sum(((v - mu) / sigma) ** 3 for v in values) / len(values)


def kurtosis(values: list[float]) -> float:
    """Fourth standardised moment, **not** excess: a normal distribution gives 3.

    The PSR formula is written against non-excess kurtosis. Passing excess
    kurtosis instead shifts every result, so the convention is stated here
    rather than left to the caller.
    """
    if len(values) < 4:
        return 3.0
    sigma = stdev(values)
    if sigma == 0.0:
        return 3.0
    mu = mean(values)
    return sum(((v - mu) / sigma) ** 4 for v in values) / len(values)


def sharpe_ratio(returns: list[float]) -> float:
    """Per-period Sharpe, risk-free = 0. **Not annualised.**

    PSR and DSR are defined on the per-period ratio and the per-period
    observation count. Feeding them an annualised Sharpe while T counts bars
    inflates the result by the annualisation factor — roughly 40x on 15-minute
    bars, which would turn any noise into a certainty.
    """
    if len(returns) < 2:
        return 0.0
    sigma = stdev(returns)
    if sigma == 0.0:
        return 0.0
    return mean(returns) / sigma


def annualise(per_period_sharpe: float, periods_per_year: float) -> float:
    return per_period_sharpe * math.sqrt(periods_per_year)


def probabilistic_sharpe_ratio(
    returns: list[float], benchmark_sharpe: float = 0.0
) -> float | None:
    """Probability that the true per-period Sharpe exceeds `benchmark_sharpe`.

    Returns None when the sample is too short or degenerate to say anything —
    which is an answer, and a more useful one than a number that looks like
    confidence.

    `benchmark_sharpe` is per-period, like the observed ratio. The default of
    0.0 answers only "is this better than not trading?", which is a low bar;
    the deflated ratio raises it to "better than the best of N random trials".
    """
    n = len(returns)
    if n < 4:
        return None

    observed = sharpe_ratio(returns)
    if observed == 0.0 and stdev(returns) == 0.0:
        return None

    skew = skewness(returns)
    kurt = kurtosis(returns)

    # Variance of the Sharpe estimator under non-normal returns.
    variance_term = 1.0 - skew * observed + ((kurt - 1.0) / 4.0) * observed**2
    if variance_term <= 0.0:
        # Extreme skew/kurtosis against a large Sharpe. The estimator's
        # distribution is not usable here; say so rather than return a number.
        return None

    z = (observed - benchmark_sharpe) * math.sqrt(n - 1) / math.sqrt(variance_term)
    return normal_cdf(z)


def expected_max_sharpe(n_trials: int, trial_sharpe_variance: float) -> float:
    """The per-period Sharpe the *best* of `n_trials` reaches by luck alone.

    This is the benchmark a strategy has to beat to be evidence of anything.
    It grows with the number of configurations tried and with how much they
    differ from each other — searching a wide grid sets a higher bar than
    searching a narrow one, which is the correct incentive.
    """
    if n_trials <= 1 or trial_sharpe_variance <= 0.0:
        # One trial is no selection, so there is nothing to deflate.
        return 0.0

    sigma = math.sqrt(trial_sharpe_variance)
    return sigma * (
        (1.0 - EULER_MASCHERONI) * normal_ppf(1.0 - 1.0 / n_trials)
        + EULER_MASCHERONI * normal_ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(
    returns: list[float],
    trial_sharpes: list[float],
) -> float | None:
    """Probability the strategy's Sharpe survives the search that found it.

    `returns` are the per-bar returns of the selected configuration.
    `trial_sharpes` are the **per-period** Sharpe ratios of every configuration
    that competed, including the selected one — that list is what sets the bar.

    Read the result as a probability, not a score. Below 0.95 the conventional
    reading is that the result is not distinguishable from the best of a random
    search; below 0.5 it is worse than the search's own noise floor.

    The count cuts both ways. Configurations that produce identical results —
    an RSI band that never binds, say — are counted as separate trials even
    though they are one, which makes the bar too high. That error is partly
    self-correcting, because duplicates also narrow the dispersion the
    benchmark scales with, and it errs toward caution either way.

    The caveat that does not err toward caution, and which no formula can fix:
    `n_trials` counts the
    configurations *this run* evaluated. Every parameter you tried by hand
    beforehand, every strategy variant abandoned along the way, and every
    symbol swapped in and out is also a trial, and none of them are in this
    number. The true burden is higher than what is reported here, so treat this
    as an upper bound on your confidence rather than a measurement of it.
    """
    if len(returns) < 4:
        return None
    if not trial_sharpes:
        return probabilistic_sharpe_ratio(returns, 0.0)

    benchmark = expected_max_sharpe(len(trial_sharpes), stdev(trial_sharpes) ** 2)
    return probabilistic_sharpe_ratio(returns, benchmark)


def returns_from_equity(equity_curve: list[float]) -> list[float]:
    """Simple per-bar returns from an equity curve, skipping non-positive equity."""
    return [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
