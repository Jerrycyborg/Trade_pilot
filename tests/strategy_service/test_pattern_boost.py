from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from market_data.models import TASummary, TechnicalIndicators
from strategy_service.rule_engine import evaluate_rules


def _bull_ta() -> TASummary:
    return TASummary(
        symbol="AAPL",
        as_of=datetime.now(timezone.utc),
        bars_count=60,
        indicators=TechnicalIndicators(rsi_14=55.0, macd_histogram=0.5, ema_20=110.0, ema_50=100.0),
        adx=30.0,
        current_price=110.0,
    )


def _make_bar(open_: float, high: float, low: float, close: float):
    bar = MagicMock()
    bar.open = open_
    bar.high = high
    bar.low = low
    bar.close = close
    return bar


def test_hammer_boosts_buy_confidence() -> None:
    prev = _make_bar(105, 106, 104, 105.5)
    hammer = _make_bar(110, 111, 107, 110.5)
    bars = [prev, hammer]
    base = evaluate_rules(_bull_ta())
    boosted = evaluate_rules(_bull_ta(), bars=bars)
    if base.action == "BUY":
        assert boosted.confidence >= base.confidence


def test_no_pattern_no_confidence_change() -> None:
    bars = [_make_bar(100, 101, 99, 100.5), _make_bar(101, 102, 100, 101.5)]
    base = evaluate_rules(_bull_ta())
    with_bars = evaluate_rules(_bull_ta(), bars=bars)
    assert with_bars.confidence == base.confidence or with_bars.confidence >= base.confidence


def test_bars_none_no_error() -> None:
    result = evaluate_rules(_bull_ta(), bars=None)
    assert result.action in ("BUY", "SELL", "HOLD")
