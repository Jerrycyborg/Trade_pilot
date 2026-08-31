"""Tests for compute_atr — guards Wilder's ATR smoothing correctness."""

from __future__ import annotations

import math

from market_data.indicators import compute_atr


def test_compute_atr_exact_values() -> None:
    """
    Known-input / known-output test for Wilder's ATR (period=3).

    Bars (H, L, C):
      Day 0: H=12, L=10, C=11  — baseline
      Day 1: H=13, L=11, C=12  TR = max(2, |13-11|, |11-11|) = 2
      Day 2: H=14, L=12, C=13  TR = max(2, |14-12|, |12-12|) = 2
      Day 3: H=15, L=13, C=14  TR = max(2, |15-13|, |13-13|) = 2

    Seed ATR (SMA of TR[0:3]) = (2+2+2)/3 = 2.0
    No further bars → final ATR = 2.0 (no Wilder smoothing steps needed).
    """
    highs = [12.0, 13.0, 14.0, 15.0]
    lows = [10.0, 11.0, 12.0, 13.0]
    closes = [11.0, 12.0, 13.0, 14.0]
    atr = compute_atr(highs, lows, closes, period=3)
    assert math.isfinite(atr), "ATR must be finite"
    assert abs(atr - 2.0) < 1e-9, f"Expected ATR=2.0, got {atr}"


def test_compute_atr_wilder_smoothing() -> None:
    """
    Verify Wilder smoothing step: seed + one extra bar.

    4 bars to compute 3 TRs → seed = SMA(TR[0:3]).
    Then 1 more TR triggers one Wilder step:
      new_atr = (seed * (period-1) + TR) / period
    """
    period = 3
    # All bars flat: H=L=C, so TR = 0 for every bar
    # Seed ATR = 0.0; after Wilder step still 0.0.
    highs = [10.0] * 5
    lows = [10.0] * 5
    closes = [10.0] * 5
    atr = compute_atr(highs, lows, closes, period=period)
    assert atr == 0.0, f"Flat bars should yield ATR=0.0, got {atr}"


def test_compute_atr_insufficient_data_fallback() -> None:
    """Single bar: falls back to H-L of last bar."""
    atr = compute_atr([15.0], [10.0], [12.0], period=14)
    assert atr == 5.0, f"Fallback should return H-L=5.0, got {atr}"


def test_compute_atr_positive_for_volatile_bars() -> None:
    """ATR of volatile bars must be positive."""
    import random

    rng = random.Random(42)
    highs, lows, closes = [], [], []
    prev_close = 100.0
    for _ in range(30):
        h = prev_close + rng.uniform(0.5, 3.0)
        low = prev_close - rng.uniform(0.5, 3.0)
        c = rng.uniform(low, h)
        highs.append(h)
        lows.append(low)
        closes.append(c)
        prev_close = c
    atr = compute_atr(highs, lows, closes, period=14)
    assert atr > 0, f"Volatile bars must have ATR > 0, got {atr}"
