"""Equivalence tests for the one-pass indicator series.

`indicator_series` exists purely for speed — it must produce exactly what
`market_data.indicators` produces on each growing prefix, because those are the
values the live system computes. A divergence here would mean the backtest
validates a strategy the live loop does not run.

These tests compare value for value at every prefix length, including the
short-data fallbacks, on several price shapes. Nothing here is approximate
beyond floating-point tolerance.
"""

from __future__ import annotations

import random

import pytest
from backtest_service import indicator_series
from market_data.indicators import compute_ema, compute_macd, compute_rsi

TOLERANCE = 1e-9


def _random_walk(n: int, seed: int) -> list[float]:
    random.seed(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + random.gauss(0.0, 0.01)))
    return closes


PRICE_SHAPES = {
    "random_walk": _random_walk(200, 7),
    "monotonic_rise": [100.0 + i for i in range(200)],
    "monotonic_fall": [300.0 - i for i in range(200)],
    "flat": [100.0] * 200,
    "single_spike": [100.0] * 99 + [150.0] + [100.0] * 100,
    "very_short": [100.0, 101.0, 99.0, 102.0],
}


@pytest.mark.parametrize("shape", sorted(PRICE_SHAPES))
def test_every_indicator_matches_at_every_prefix(shape: str) -> None:
    closes = PRICE_SHAPES[shape]
    series = indicator_series.build(closes, 20, 50)

    for i in range(len(closes)):
        prefix = closes[: i + 1]
        assert series.ema_fast[i] == pytest.approx(
            compute_ema(prefix, 20), abs=TOLERANCE
        ), f"{shape} ema_fast at bar {i}"
        assert series.ema_slow[i] == pytest.approx(
            compute_ema(prefix, 50), abs=TOLERANCE
        ), f"{shape} ema_slow at bar {i}"
        assert series.rsi[i] == pytest.approx(
            compute_rsi(prefix), abs=TOLERANCE
        ), f"{shape} rsi at bar {i}"
        assert series.macd_hist[i] == pytest.approx(
            compute_macd(prefix)[2], abs=TOLERANCE
        ), f"{shape} macd_hist at bar {i}"


@pytest.mark.parametrize("fast,slow", [(5, 10), (10, 40), (20, 50), (30, 60), (2, 200)])
def test_matches_across_the_parameter_grid(fast: int, slow: int) -> None:
    """The grid varies the EMA periods, so equivalence has to hold across them."""
    closes = _random_walk(250, 3)
    series = indicator_series.build(closes, fast, slow)

    for i in range(len(closes)):
        prefix = closes[: i + 1]
        assert series.ema_fast[i] == pytest.approx(compute_ema(prefix, fast), abs=TOLERANCE)
        assert series.ema_slow[i] == pytest.approx(compute_ema(prefix, slow), abs=TOLERANCE)


def test_series_are_aligned_to_the_input() -> None:
    closes = _random_walk(64, 1)
    series = indicator_series.build(closes, 20, 50)
    assert len(series.ema_fast) == len(closes)
    assert len(series.ema_slow) == len(closes)
    assert len(series.rsi) == len(closes)
    assert len(series.macd_hist) == len(closes)


def test_empty_input_produces_empty_series() -> None:
    series = indicator_series.build([], 20, 50)
    assert series.ema_fast == []
    assert series.macd_hist == []


def test_short_data_uses_the_same_fallbacks_as_the_originals() -> None:
    """RSI 50, MACD 0, EMA falling back to the last close."""
    closes = [100.0, 101.0, 102.0]
    series = indicator_series.build(closes, 20, 50)
    assert series.rsi == [50.0, 50.0, 50.0]
    assert series.macd_hist == [0.0, 0.0, 0.0]
    assert series.ema_fast == closes
