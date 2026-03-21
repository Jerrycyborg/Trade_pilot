"""Tests for ADX and candlestick pattern detection."""
from market_data.indicators import compute_adx, detect_patterns


def _trend_data(n: int = 30) -> tuple[list[float], list[float], list[float]]:
    closes = [100.0 + i * 0.5 for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    return highs, lows, closes


def test_compute_adx_returns_float():
    highs, lows, closes = _trend_data(40)
    result = compute_adx(highs, lows, closes)
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0


def test_compute_adx_insufficient_data():
    result = compute_adx([101.0, 102.0], [99.0, 100.0], [100.0, 101.0])
    assert result == 25.0


def test_detect_patterns_doji():
    opens = [100.0, 100.05]
    highs = [101.0, 102.0]
    lows = [99.0, 98.0]
    closes = [99.5, 100.06]
    result = detect_patterns(opens, highs, lows, closes)
    assert "doji" in result


def test_detect_patterns_hammer():
    opens = [100.0, 102.0]
    highs = [101.0, 102.5]
    lows = [99.0, 99.0]
    closes = [99.5, 102.3]
    result = detect_patterns(opens, highs, lows, closes)
    assert "hammer" in result


def test_detect_patterns_empty():
    result = detect_patterns([100.0], [101.0], [99.0], [100.5])
    assert result == []


def test_detect_patterns_bullish_engulfing():
    opens = [102.0, 99.0]
    highs = [103.0, 104.0]
    lows = [99.5, 98.5]
    closes = [100.0, 103.0]
    result = detect_patterns(opens, highs, lows, closes)
    assert "bullish_engulfing" in result
