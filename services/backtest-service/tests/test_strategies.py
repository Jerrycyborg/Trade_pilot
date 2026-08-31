"""Tests for the strategy registry and the two rules in it.

Two things matter here. First, that the momentum rule still does exactly what
it did when it was inlined in the engine — this refactor must not have moved a
single signal. Second, that the mean-reversion rule is genuinely a different
idea, because a portfolio of two rules that agree is a portfolio of one.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from backtest_service import indicator_series
from backtest_service.models import BacktestRequest, StrategyParams
from backtest_service.strategies import (
    REGISTRY,
    get_strategy,
    momentum_signals,
    reversion_signals,
    strategy_names,
)
from market_data.models import OHLCVBar


def _bars(n: int, seed: int = 1, vol: float = 0.01) -> list[OHLCVBar]:
    random.seed(seed)
    start = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    price = 100.0
    out: list[OHLCVBar] = []
    for i in range(n):
        price *= 1 + random.gauss(0.0, vol)
        out.append(
            OHLCVBar(
                symbol="SYN",
                timestamp=start + timedelta(minutes=15 * i),
                open=price,
                high=price * 1.005,
                low=price * 0.995,
                close=price,
                volume=100_000.0,
            )
        )
    return out


class TestRegistry:
    def test_both_strategies_are_registered(self) -> None:
        assert set(strategy_names()) == {"ema_rsi_macd", "bollinger_reversion"}

    def test_an_unknown_strategy_names_the_alternatives(self) -> None:
        """A typo used to run the momentum rule silently."""
        with pytest.raises(ValueError, match="bollinger_reversion"):
            get_strategy("emma_rsi_macd")

    def test_every_strategy_declares_the_fields_it_reads(self) -> None:
        valid = set(StrategyParams.model_fields)
        for strategy in REGISTRY.values():
            assert strategy.param_fields
            assert set(strategy.param_fields) <= valid

    def test_no_two_strategies_read_the_same_parameters(self) -> None:
        """Overlapping parameter sets would make the two rules variants of one."""
        momentum = set(REGISTRY["ema_rsi_macd"].param_fields)
        reversion = set(REGISTRY["bollinger_reversion"].param_fields)
        assert momentum & reversion == set()

    def test_the_engine_routes_through_the_registry(self) -> None:
        from backtest_service.engine import _compute_signals

        bars = _bars(300)
        request = BacktestRequest(symbol="SYN", strategy="bollinger_reversion")
        assert _compute_signals(bars, request) == reversion_signals(bars, request.params)

    def test_an_unknown_strategy_on_a_request_raises(self) -> None:
        from backtest_service.engine import _compute_signals

        with pytest.raises(ValueError, match="Unknown strategy"):
            _compute_signals(_bars(300), BacktestRequest(symbol="SYN", strategy="nope"))


class TestMomentumUnchanged:
    """The rule was inlined in the engine before this refactor. It must be
    identical now — a moved signal is a different strategy."""

    def test_signals_match_the_rule_computed_independently(self) -> None:
        bars = _bars(400, seed=3)
        params = StrategyParams()
        closes = [b.close for b in bars]
        series = indicator_series.build(closes, 20, 50)

        expected: list[str] = []
        for i in range(len(bars)):
            if i + 1 < 51:
                expected.append("HOLD")
                continue
            buy = (
                series.ema_fast[i] > series.ema_slow[i]
                and 45 < series.rsi[i] < 70
                and series.macd_hist[i] > 0
            )
            sell = (
                series.ema_fast[i] < series.ema_slow[i]
                and 30 < series.rsi[i] < 55
                and series.macd_hist[i] < 0
            )
            expected.append("BUY" if buy else "SELL" if sell else "HOLD")

        assert momentum_signals(bars, params) == expected

    def test_nothing_fires_before_the_warm_up(self) -> None:
        bars = _bars(400, seed=3)
        assert set(momentum_signals(bars, StrategyParams())[:50]) == {"HOLD"}

    def test_a_slower_ema_pushes_the_warm_up_out(self) -> None:
        bars = _bars(400, seed=3)
        signals = momentum_signals(bars, StrategyParams(ema_fast=20, ema_slow=100))
        assert set(signals[:100]) == {"HOLD"}


class TestReversion:
    def test_it_buys_below_the_lower_band_when_oversold(self) -> None:
        bars = _bars(400, seed=4)
        params = StrategyParams()
        closes = [b.close for b in bars]
        bands = indicator_series.bollinger(closes, params.bb_period, params.bb_std)
        rsi = indicator_series._rsi_series(closes)

        signals = reversion_signals(bars, params)
        for i, signal in enumerate(signals):
            if signal == "BUY":
                assert closes[i] < bands.lower[i]
                assert rsi[i] < params.rsi_oversold

    def test_it_exits_at_the_mean(self) -> None:
        """Holding past the mean turns a reversion bet into a momentum one."""
        bars = _bars(400, seed=4)
        params = StrategyParams()
        closes = [b.close for b in bars]
        bands = indicator_series.bollinger(closes, params.bb_period, params.bb_std)
        rsi = indicator_series._rsi_series(closes)

        signals = reversion_signals(bars, params)
        for i, signal in enumerate(signals):
            if signal == "SELL":
                assert closes[i] > bands.middle[i] or rsi[i] > params.rsi_overbought

    def test_it_warms_up_faster_than_the_momentum_rule(self) -> None:
        """It does not use MACD, so it needs less history."""
        params = StrategyParams()
        assert REGISTRY["bollinger_reversion"].warmup_bars(params) < REGISTRY[
            "ema_rsi_macd"
        ].warmup_bars(params)

    def test_a_wider_band_trades_less(self) -> None:
        """More standard deviations means fewer prices count as stretched."""
        bars = _bars(600, seed=6)
        tight = reversion_signals(bars, StrategyParams(bb_std=1.0))
        wide = reversion_signals(bars, StrategyParams(bb_std=3.0))
        assert tight.count("BUY") >= wide.count("BUY")


class TestTheTwoRulesDisagree:
    """If they agreed there would be nothing to diversify."""

    def test_they_produce_different_signals(self) -> None:
        bars = _bars(600, seed=8)
        params = StrategyParams()
        momentum = momentum_signals(bars, params)
        reversion = reversion_signals(bars, params)
        assert momentum != reversion

    def test_they_rarely_both_want_to_buy_at_once(self) -> None:
        """One buys strength and the other buys weakness, so simultaneous BUYs
        should be rare rather than merely non-identical."""
        bars = _bars(600, seed=8)
        params = StrategyParams()
        momentum = momentum_signals(bars, params)
        reversion = reversion_signals(bars, params)

        both = sum(1 for m, r in zip(momentum, reversion, strict=True) if m == r == "BUY")
        either = sum(1 for m, r in zip(momentum, reversion, strict=True) if "BUY" in (m, r))
        assert either > 0
        assert both / either < 0.1
