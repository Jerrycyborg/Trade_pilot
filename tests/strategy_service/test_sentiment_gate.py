from __future__ import annotations

from datetime import datetime, timezone

from market_data.models import TASummary, TechnicalIndicators
from strategy_service.rule_engine import evaluate_rules


def _bull_ta() -> TASummary:
    """TA that would normally produce a BUY signal."""
    return TASummary(
        symbol="AAPL",
        as_of=datetime.now(timezone.utc),
        bars_count=60,
        indicators=TechnicalIndicators(
            rsi_14=55.0,
            macd_histogram=0.5,
            ema_20=110.0,
            ema_50=100.0,
        ),
        adx=30.0,
        current_price=110.0,
    )


def test_buy_blocked_by_negative_sentiment():
    signal = evaluate_rules(_bull_ta(), sentiment_score=-0.5)
    assert signal.action == "HOLD"
    assert "Sentiment gate" in signal.reasoning


def test_buy_allowed_with_neutral_sentiment():
    signal = evaluate_rules(_bull_ta(), sentiment_score=0.0)
    assert signal.action == "BUY"


def test_buy_allowed_when_no_sentiment():
    signal = evaluate_rules(_bull_ta(), sentiment_score=None)
    assert signal.action == "BUY"


def test_sell_not_affected_by_sentiment_gate():
    bear_ta = TASummary(
        symbol="AAPL",
        as_of=datetime.now(timezone.utc),
        bars_count=60,
        indicators=TechnicalIndicators(
            rsi_14=45.0,
            macd_histogram=-0.5,
            ema_20=90.0,
            ema_50=100.0,
        ),
        adx=30.0,
        current_price=90.0,
    )
    signal = evaluate_rules(bear_ta, sentiment_score=-0.9)
    assert signal.action == "SELL"
