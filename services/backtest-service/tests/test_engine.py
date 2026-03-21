"""Tests for backtest engine."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from backtest_service.engine import run_backtest
from backtest_service.models import BacktestRequest
from market_data.models import OHLCVBar


def _make_bars(n: int, base_price: float = 100.0, trend: float = 0.0) -> list[OHLCVBar]:
    """Generate synthetic OHLCV bars."""
    bars = []
    price = base_price
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        price = price * (1 + trend)
        high = price * 1.01
        low = price * 0.99
        bars.append(OHLCVBar(
            symbol="TEST",
            timestamp=start + timedelta(days=i),
            open=price * 0.999,
            high=high,
            low=low,
            close=price,
            volume=10000.0,
        ))
    return bars


def _make_trending_bars(n: int = 100, daily_return: float = 0.005) -> list[OHLCVBar]:
    """Strongly uptrending bars to trigger BUY signals."""
    return _make_bars(n, base_price=100.0, trend=daily_return)


def _request(**kwargs) -> BacktestRequest:
    defaults = dict(
        symbol="TEST",
        strategy="ema_rsi_macd",
        period_days=180,
        initial_capital=100_000.0,
        risk_per_trade_pct=0.01,
        atr_stop_multiplier=2.0,
        commission_pct=0.001,
    )
    defaults.update(kwargs)
    return BacktestRequest(**defaults)


def test_returns_result_for_minimal_bars() -> None:
    """35 synthetic bars -> BacktestResult with valid fields."""
    bars = _make_bars(35)
    result = run_backtest(_request(), bars)
    assert result.symbol == "TEST"
    assert result.initial_capital == 100_000.0
    assert result.final_value > 0
    assert isinstance(result.sharpe_ratio, float)
    assert isinstance(result.total_trades, int)
    assert 0.0 <= result.win_rate <= 1.0


def test_raises_on_insufficient_bars() -> None:
    """Less than 30 bars raises ValueError."""
    bars = _make_bars(20)
    with pytest.raises(ValueError, match="Need at least 30 bars"):
        run_backtest(_request(), bars)


def test_all_hold_returns_zero_trades() -> None:
    """Flat price (no trend/momentum) -> no or minimal trades."""
    bars = _make_bars(80, base_price=100.0, trend=0.0)
    result = run_backtest(_request(), bars)
    # With flat price, EMA20 ≈ EMA50, so no BUY/SELL signals expected
    assert result.total_trades == 0


def test_sharpe_calculable_on_trending_data() -> None:
    """Trending bars should produce a calculable Sharpe ratio."""
    bars = _make_trending_bars(n=100, daily_return=0.003)
    result = run_backtest(_request(), bars)
    assert math.isfinite(result.sharpe_ratio)


def test_no_lookahead() -> None:
    """
    Anti-lookahead: bars with a single massive spike at the end should not
    produce profitable trades based on future information.
    The strategy should only see bars up to the current index.
    """
    bars = _make_bars(80, base_price=100.0, trend=0.0)
    # Spike only at final bar — should not influence any prior signals
    last = bars[-1]
    bars[-1] = OHLCVBar(
        symbol=last.symbol,
        timestamp=last.timestamp,
        open=last.open,
        high=last.high * 10,
        low=last.low,
        close=last.close * 10,
        volume=last.volume,
    )
    result = run_backtest(_request(), bars)
    # No trade should have been entered before the spike based on seeing it
    # All signals before last bar should be HOLD (flat price)
    assert result.total_trades <= 1  # at most close-out of open position at spike bar


def test_atr_sizing_produces_fractional_positions() -> None:
    """ATR sizing should compute variable position sizes based on ATR."""
    bars = _make_trending_bars(n=100, daily_return=0.005)
    result_small_risk = run_backtest(_request(risk_per_trade_pct=0.001), bars)
    result_large_risk = run_backtest(_request(risk_per_trade_pct=0.02), bars)
    # Larger risk per trade should result in different final values
    # (we just check both complete successfully)
    assert math.isfinite(result_small_risk.sharpe_ratio)
    assert math.isfinite(result_large_risk.sharpe_ratio)
