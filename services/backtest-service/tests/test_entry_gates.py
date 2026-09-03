"""The backtest's entry gates must decide exactly what the live ones decide.

`backtest_service.entry_gates` is a deliberate second copy of the filters in
`strategy_service/worker.py`, made so the gates can be measured rather than
assumed. A second copy is only safe while something proves the two agree, and
that is this file's whole job. When ADR-006's shared strategy library lands and
the duplicate goes away, these tests should keep passing against it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from backtest_service import entry_gates, indicator_series
from market_data.indicators import compute_adx
from market_data.models import OHLCVBar


def _series(n: int = 240, seed: int = 11) -> list[OHLCVBar]:
    random.seed(seed)
    now = datetime.now(timezone.utc)
    bars: list[OHLCVBar] = []
    price = 100.0
    for i in range(n):
        price *= 1 + random.gauss(0, 0.015)
        bars.append(
            OHLCVBar(
                symbol="TEST",
                timestamp=now - timedelta(days=n - i),
                open=price * 0.999,
                high=price * 1.01,
                low=price * 0.985,
                close=price,
                volume=random.uniform(1e6, 9e6),
            )
        )
    return bars


class TestTheFastADXMatchesTheLiveOne:
    """The live gate reads one scalar ADX from the whole window it holds, so
    the backtest needs that value at every bar. Recomputing the scalar per bar
    is quadratic; this asserts the one-pass version is the same number."""

    def test_every_prefix_agrees_with_compute_adx(self) -> None:
        bars = _series()
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]

        fast = indicator_series.adx_series(highs, lows, closes)

        assert len(fast) == len(bars)
        for i in range(len(bars)):
            expected = compute_adx(highs[: i + 1], lows[: i + 1], closes[: i + 1])
            assert abs(fast[i] - expected) < 1e-9, f"prefix {i + 1} diverged"

    def test_a_short_series_is_the_sentinel_not_a_measurement(self) -> None:
        bars = _series(n=8)
        fast = indicator_series.adx_series(
            [b.high for b in bars], [b.low for b in bars], [b.close for b in bars]
        )
        assert all(value == indicator_series.ADX_NEUTRAL for value in fast)


class TestTheGatesDecideWhatLiveDecides:
    def test_an_unmeasurable_regime_is_refused_not_passed(self) -> None:
        """compute_adx returns 25.0 when it cannot measure, which sits above
        the 20 floor — so reading the number without checking measurability
        passes on thin data. The live gate checks the bar count first."""
        assert entry_gates.regime_is_tradable(indicator_series.ADX_NEUTRAL, bars_count=5) is False
        assert entry_gates.regime_is_tradable(indicator_series.ADX_NEUTRAL, bars_count=200) is True

    def test_the_regime_floor_is_inclusive_at_twenty(self) -> None:
        assert entry_gates.regime_is_tradable(19.999, bars_count=200) is False
        assert entry_gates.regime_is_tradable(20.0, bars_count=200) is True

    def test_volume_must_exceed_its_average_equality_fails(self) -> None:
        """Live suppresses on `current <= average`, so a bar exactly at its
        average is refused. The boundary is the easy thing to get wrong."""
        flat = [100.0] * 20
        assert entry_gates.volume_confirms(flat, index=19) is False
        rising = [100.0] * 19 + [101.0]
        assert entry_gates.volume_confirms(rising, index=19) is True

    def test_the_average_window_is_the_trailing_twenty_inclusive(self) -> None:
        volumes = [1.0] * 19 + [50.0] + [2.0]
        # At index 20 the window is indices 1..20, which includes the 50 spike,
        # so an average well above 2.0 and the bar is refused.
        assert entry_gates.volume_confirms(volumes, index=20) is False


class TestApplyingTheGatesToASignalSeries:
    def test_gates_off_is_the_untouched_series(self) -> None:
        bars = _series()
        signals = ["BUY"] * len(bars)
        assert entry_gates.apply(bars, signals) == signals

    def test_only_buys_are_gated(self) -> None:
        """Entries are filtered; a risk-reducing exit never is."""
        bars = _series()
        signals = ["SELL"] * len(bars)
        gated = entry_gates.apply(bars, signals, regime_gate=True, volume_gate=True)
        assert gated == signals

    def test_gating_can_only_remove_entries(self) -> None:
        bars = _series()
        signals = ["BUY"] * len(bars)
        gated = entry_gates.apply(bars, signals, regime_gate=True, volume_gate=True)
        assert len(gated) == len(signals)
        assert all(new in ("BUY", "HOLD") for new in gated)
        assert sum(s == "BUY" for s in gated) < len(signals), "the gates suppressed nothing at all"

    def test_each_gate_matches_a_hand_computed_decision(self) -> None:
        """The end-to-end check: for every bar, the gated result equals what
        the live filters would have concluded from the same inputs."""
        bars = _series()
        signals = ["BUY"] * len(bars)
        gated = entry_gates.apply(bars, signals, regime_gate=True, volume_gate=True)

        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        volumes = [b.volume for b in bars]

        for i in range(len(bars)):
            adx = compute_adx(highs[: i + 1], lows[: i + 1], closes[: i + 1])
            window = volumes[max(0, i - 19) : i + 1]
            live_allows = (
                (i + 1) >= indicator_series.ADX_PERIOD + 2
                and adx >= entry_gates.REGIME_ADX_FLOOR
                and volumes[i] > sum(window) / len(window)
            )
            assert gated[i] == ("BUY" if live_allows else "HOLD"), f"bar {i} disagreed"
