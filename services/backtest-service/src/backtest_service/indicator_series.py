"""Per-bar indicator values for a whole series, in one pass.

`market_data.indicators` computes one value from one list of closes. The
backtest needs the value at *every* bar, and calling those functions on each
growing prefix is quadratic: on a year of 15-minute bars a single walk-forward
over an 81-point grid takes tens of minutes, which means it does not get run,
which means overfitting does not get caught.

Every indicator in use is recursive from a seed that is fixed once and never
revised — an SMA over the first `period` closes, then a fixed update rule. So
the value at prefix length L is a step in a single forward pass, and the whole
series costs one pass instead of L of them.

This module deliberately does **not** touch `market_data.indicators`. That code
runs in the live trading path; this is a research-time reimplementation, and
`tests/backtest/test_indicator_series.py` asserts value-for-value equality
against the original at every prefix length. If the two ever disagree, that
test fails rather than the backtest quietly diverging from what the live system
would have seen.
"""

from __future__ import annotations

from dataclasses import dataclass

RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# What the originals return when there is not enough data to compute anything.
RSI_NEUTRAL = 50.0
ADX_NEUTRAL = 25.0
ADX_PERIOD = 14


@dataclass(frozen=True)
class IndicatorSeries:
    """One value per bar, aligned to the input closes."""

    ema_fast: list[float]
    ema_slow: list[float]
    rsi: list[float]
    macd_hist: list[float]


@dataclass(frozen=True)
class BollingerSeries:
    """Bollinger bands at every bar, aligned to the input closes."""

    upper: list[float]
    middle: list[float]
    lower: list[float]


def _ema_series(closes: list[float], period: int) -> list[float]:
    """compute_ema(closes[:i+1], period) for every i."""
    out: list[float] = []
    ema = 0.0
    for i, price in enumerate(closes):
        length = i + 1
        if length < period:
            # Matches the original's fallback: not enough data to seed, so the
            # last close stands in for the average.
            out.append(price)
            continue
        if length == period:
            ema = sum(closes[:period]) / period
        else:
            ema = (price - ema) * (2.0 / (period + 1)) + ema
        out.append(ema)
    return out


def _rsi_series(closes: list[float], period: int = RSI_PERIOD) -> list[float]:
    """compute_rsi(closes[:i+1], period) for every i, using Wilder's smoothing."""
    out: list[float] = []
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(len(closes)):
        length = i + 1
        if length < period + 1:
            out.append(RSI_NEUTRAL)
            continue
        if length == period + 1:
            # gains[0:period] are the deltas across closes[0:period+1].
            gains = [max(closes[j] - closes[j - 1], 0.0) for j in range(1, period + 1)]
            losses = [abs(min(closes[j] - closes[j - 1], 0.0)) for j in range(1, period + 1)]
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
        else:
            delta = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
            avg_loss = (avg_loss * (period - 1) + abs(min(delta, 0.0))) / period

        if avg_loss == 0.0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def _macd_hist_series(
    closes: list[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal_period: int = MACD_SIGNAL,
) -> list[float]:
    """The histogram from compute_macd(closes[:i+1]) for every i.

    Note the original steps the fast EMA from index `slow`, not from index
    `fast` — closes between the two are skipped for the fast leg. That is
    reproduced rather than corrected: the point of this module is to match what
    the live system computes, not to improve on it. Changing it would silently
    move every signal.
    """
    minimum = slow + signal_period
    out: list[float] = []

    ema_fast = 0.0
    ema_slow = 0.0
    mult_fast = 2.0 / (fast + 1)
    mult_slow = 2.0 / (slow + 1)

    macd_values: list[float] = []
    sig = 0.0

    for i, price in enumerate(closes):
        length = i + 1
        if length <= slow:
            # No MACD value is produced until index `slow`.
            if length < minimum:
                out.append(0.0)
            continue

        if length == slow + 1:
            ema_fast = sum(closes[:fast]) / fast
            ema_slow = sum(closes[:slow]) / slow
        ema_fast = (price - ema_fast) * mult_fast + ema_fast
        ema_slow = (price - ema_slow) * mult_slow + ema_slow
        macd_values.append(ema_fast - ema_slow)

        if length < minimum:
            out.append(0.0)
            continue

        if len(macd_values) == signal_period:
            sig = sum(macd_values[:signal_period]) / signal_period
        else:
            sig = (macd_values[-1] - sig) * (2.0 / (signal_period + 1)) + sig
        out.append(macd_values[-1] - sig)

    return out


def build(closes: list[float], ema_fast_period: int, ema_slow_period: int) -> IndicatorSeries:
    """Every indicator the strategy reads, at every bar."""
    return IndicatorSeries(
        ema_fast=_ema_series(closes, ema_fast_period),
        ema_slow=_ema_series(closes, ema_slow_period),
        rsi=_rsi_series(closes),
        macd_hist=_macd_hist_series(closes),
    )


def bollinger(closes: list[float], period: int, std_dev: float) -> BollingerSeries:
    """compute_bollinger(closes[:i+1], period, std_dev) for every i.

    Unlike the recursive indicators this one is a plain rolling window, so the
    only thing being avoided here is the repeated slicing — but it keeps the
    whole signal pass to one shape, and the equality test covers it the same
    way.
    """
    upper: list[float] = []
    middle: list[float] = []
    lower: list[float] = []

    for i in range(len(closes)):
        if i + 1 < period:
            # Matches the original's "not enough data" return.
            upper.append(0.0)
            middle.append(0.0)
            lower.append(0.0)
            continue
        window = closes[i + 1 - period : i + 1]
        mean = sum(window) / period
        variance = sum((price - mean) ** 2 for price in window) / period
        spread = std_dev * variance**0.5
        upper.append(mean + spread)
        middle.append(mean)
        lower.append(mean - spread)

    return BollingerSeries(upper=upper, middle=middle, lower=lower)


def adx_series(
    highs: list[float], lows: list[float], closes: list[float], period: int = ADX_PERIOD
) -> list[float]:
    """ADX at every bar, where element i equals `compute_adx` over `[:i+1]`.

    The live regime gate reads one scalar ADX from the whole fetched window, so
    reproducing it in a backtest means the value the gate *would* have seen at
    each bar — an expanding window, not a rolling one. Recomputing the scalar
    per bar is O(n^2) and the walk-forward calls it once per trial per fold, so
    this runs the same Wilder recursion forward once and records the running
    value. It is a performance rewrite of `market_data.indicators.compute_adx`
    and nothing else: a test asserts the two agree bar for bar, and that test is
    the only thing keeping them honest.
    """
    n = len(closes)
    out = [ADX_NEUTRAL] * n
    if n < period + 2:
        return out

    tr_list: list[float] = []
    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []

    atr = plus_smooth = minus_smooth = 0.0
    dx_count = 0
    dx_running_sum = 0.0
    dx_seed_sum = 0.0
    adx_state = 0.0

    for i in range(1, n):
        high, low, prev_close = highs[i], lows[i], closes[i - 1]
        prev_high, prev_low = highs[i - 1], lows[i - 1]

        tr_list.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm_list.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm_list.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

        if len(tr_list) < period:
            continue
        if len(tr_list) == period:
            # Seed the Wilder sums exactly as compute_adx does.
            atr = sum(tr_list[:period])
            plus_smooth = sum(plus_dm_list[:period])
            minus_smooth = sum(minus_dm_list[:period])
            continue

        atr = atr - atr / period + tr_list[-1]
        plus_smooth = plus_smooth - plus_smooth / period + plus_dm_list[-1]
        minus_smooth = minus_smooth - minus_smooth / period + minus_dm_list[-1]

        plus_di = 100.0 * plus_smooth / atr if atr > 0 else 0.0
        minus_di = 100.0 * minus_smooth / atr if atr > 0 else 0.0
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0

        dx_count += 1
        dx_running_sum += dx
        if dx_count < period:
            # Fewer DX values than the smoothing period: compute_adx takes a
            # plain mean here rather than smoothing, so this branch must too.
            adx_state = dx_running_sum / dx_count
        elif dx_count == period:
            dx_seed_sum = dx_running_sum
            adx_state = dx_seed_sum / period
        else:
            adx_state = (adx_state * (period - 1) + dx) / period
        out[i] = adx_state

    return out
