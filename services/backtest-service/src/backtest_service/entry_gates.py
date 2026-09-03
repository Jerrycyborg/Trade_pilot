"""The live entry gates, reproduced so they can be measured instead of assumed.

The trading worker suppresses a BUY when the regime is not trending or volume
does not confirm (`strategy_service/worker.py`, the regime and volume filters
in front of the rule). The backtest never modelled either, so every backtest
result described a strictly more permissive strategy than the one that trades:
live takes fewer entries than any published figure, and whether the gates earn
that reduction has never been tested. They have been applied on faith.

This module lets a backtest run the gates on or off, so the difference is a
measurement rather than an opinion. Both default to off, which is the backtest's
historical behaviour — turning them on is an explicit request.

**This is a deliberate second copy, and it is temporary.** ADR-006 decision 1
puts the one true strategy definition in a shared library that research and live
both import; that extraction is Phase 1 of TASK-009 and is blocked behind an
in-flight PR that is itself refactoring the live gate. Until then,
`test_entry_gates.py` asserts this implementation and the live one agree
decision-for-decision on the same inputs. That test is what makes the duplicate
safe, and it is the thing that must keep passing when the shared library lands.
"""

from __future__ import annotations

from market_data.models import OHLCVBar

from . import indicator_series

#: Below this ADX the regime is treated as ranging and a BUY is suppressed.
REGIME_ADX_FLOOR = 20.0

#: Bars in the volume average the current bar must exceed.
VOLUME_LOOKBACK = 20


def regime_is_tradable(
    adx: float, bars_count: int, period: int = indicator_series.ADX_PERIOD
) -> bool:
    """Whether the trend filter permits an entry.

    `compute_adx` returns 25.0 — above the floor — when it has too little data
    to measure anything, so a filter that reads the number without checking
    measurability passes on thin data rather than refusing. An unmeasurable
    regime is not a trending one, which is why the bar count is checked first.
    """
    if bars_count < period + 2:
        return False
    return adx >= REGIME_ADX_FLOOR


def volume_confirms(volumes: list[float], index: int, lookback: int = VOLUME_LOOKBACK) -> bool:
    """Whether the current bar's volume exceeds its trailing average.

    Mirrors the live comparison exactly, including its boundaries: the average
    is over at most `lookback` bars ending at `index` inclusive, and equality
    fails — the live gate suppresses on `current <= average`.
    """
    if not volumes or index < 0 or index >= len(volumes):
        return False
    window = volumes[max(0, index + 1 - lookback) : index + 1]
    if not window:
        return False
    return volumes[index] > sum(window) / len(window)


def apply(
    bars: list[OHLCVBar],
    signals: list[str],
    *,
    regime_gate: bool = False,
    volume_gate: bool = False,
) -> list[str]:
    """Return `signals` with gated BUYs turned to HOLD.

    Only BUY is gated, matching live: the filters sit in front of entries, and
    a risk-reducing exit is never suppressed by them.
    """
    if not (regime_gate or volume_gate) or not bars:
        return signals

    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    closes = [bar.close for bar in bars]
    volumes = [float(getattr(bar, "volume", 0.0) or 0.0) for bar in bars]
    adx = indicator_series.adx_series(highs, lows, closes) if regime_gate else []

    gated = list(signals)
    for i, signal in enumerate(gated):
        if signal != "BUY":
            continue
        if regime_gate and not regime_is_tradable(adx[i], i + 1):
            gated[i] = "HOLD"
            continue
        if volume_gate and not volume_confirms(volumes, i):
            gated[i] = "HOLD"
    return gated
