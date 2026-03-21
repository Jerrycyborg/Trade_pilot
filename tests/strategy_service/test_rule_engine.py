"""Tests for deterministic rule engine."""

from __future__ import annotations

from datetime import datetime, timezone

from market_data.models import TASummary, TechnicalIndicators

from strategy_service.rule_engine import RuleSignal, evaluate_rules


def _ta(
    ema_20: float = 110.0,
    ema_50: float = 100.0,
    rsi_14: float = 55.0,
    macd_histogram: float = 0.5,
    adx: float = 20.0,
) -> TASummary:
    return TASummary(
        symbol="AAPL",
        as_of=datetime.now(timezone.utc),
        bars_count=60,
        indicators=TechnicalIndicators(
            rsi_14=rsi_14,
            macd_histogram=macd_histogram,
            ema_20=ema_20,
            ema_50=ema_50,
        ),
        adx=adx,
        current_price=ema_20,
    )


def test_buy_signal_all_conditions_met() -> None:
    """EMA20>EMA50, RSI=55, MACD_hist>0 -> BUY."""
    result = evaluate_rules(_ta(ema_20=110, ema_50=100, rsi_14=55, macd_histogram=0.5))
    assert result.action == "BUY"
    assert result.confidence >= 0.65


def test_sell_signal_all_conditions_met() -> None:
    """EMA20<EMA50, RSI=45, MACD_hist<0 -> SELL."""
    result = evaluate_rules(_ta(ema_20=90, ema_50=100, rsi_14=45, macd_histogram=-0.5))
    assert result.action == "SELL"
    assert result.confidence >= 0.65


def test_hold_when_rsi_overbought() -> None:
    """RSI=75 -> HOLD (overbought, BUY condition not met)."""
    result = evaluate_rules(_ta(ema_20=110, ema_50=100, rsi_14=75, macd_histogram=0.5))
    assert result.action == "HOLD"


def test_hold_when_signals_mixed() -> None:
    """EMA bullish but MACD bearish -> HOLD."""
    result = evaluate_rules(_ta(ema_20=110, ema_50=100, rsi_14=55, macd_histogram=-0.1))
    assert result.action == "HOLD"


def test_adx_boosts_confidence() -> None:
    """ADX=30 (trending) -> confidence >= 0.75."""
    result = evaluate_rules(_ta(ema_20=110, ema_50=100, rsi_14=55, macd_histogram=0.5, adx=30))
    assert result.confidence >= 0.75
    assert result.action == "BUY"


def test_risk_score_low_when_trending_rsi_mid() -> None:
    """ADX>25, RSI=55 -> LOW risk score."""
    result = evaluate_rules(_ta(ema_20=110, ema_50=100, rsi_14=55, macd_histogram=0.5, adx=30))
    assert result.risk_score == "LOW"


def test_risk_score_high_when_overbought() -> None:
    """RSI=75 -> HIGH risk score."""
    result = evaluate_rules(_ta(rsi_14=75))
    assert result.risk_score == "HIGH"


def test_risk_score_high_when_oversold() -> None:
    """RSI=25 -> HIGH risk score."""
    result = evaluate_rules(_ta(rsi_14=25))
    assert result.risk_score == "HIGH"


def test_size_pct_matches_risk_score() -> None:
    """LOW risk -> 0.02, MEDIUM -> 0.015, HIGH -> 0.005."""
    low = evaluate_rules(_ta(ema_20=110, ema_50=100, rsi_14=55, macd_histogram=0.5, adx=30))
    assert low.risk_score == "LOW"
    assert low.size_pct == 0.02

    high = evaluate_rules(_ta(rsi_14=75))
    assert high.risk_score == "HIGH"
    assert high.size_pct == 0.005
