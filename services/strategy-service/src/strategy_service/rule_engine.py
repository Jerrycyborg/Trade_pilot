"""Deterministic EMA/RSI/MACD rule-based strategy engine."""

from __future__ import annotations

from dataclasses import dataclass

from market_data.models import TASummary

_SIZE_BY_RISK = {"LOW": 0.02, "MEDIUM": 0.015, "HIGH": 0.005}


@dataclass
class RuleSignal:
    action: str          # "BUY" | "SELL" | "HOLD"
    confidence: float
    risk_score: str      # "LOW" | "MEDIUM" | "HIGH"
    reasoning: str
    size_pct: float


def evaluate_rules(ta: TASummary, config: dict | None = None) -> RuleSignal:
    """
    Deterministic Dual-EMA Momentum + RSI + MACD strategy.

    BUY conditions (all must hold):
      - ema_20 > ema_50  (bullish trend)
      - rsi_14 > 45 and rsi_14 < 70  (momentum, not overbought)
      - macd_histogram > 0  (momentum confirming)

    SELL conditions (all must hold):
      - ema_20 < ema_50  (bearish trend)
      - rsi_14 < 55 and rsi_14 > 30  (momentum, not oversold)
      - macd_histogram < 0

    HOLD: everything else.

    confidence: base 0.65; +0.10 if adx > 25 (trending); +0.05 if no conflicting signals.
    risk_score: LOW if adx>25 and 45<rsi<65; HIGH if rsi>70 or rsi<30; else MEDIUM.
    size_pct: LOW->0.02, MEDIUM->0.015, HIGH->0.005
    """
    ind = ta.indicators
    rsi = ind.rsi_14
    macd_hist = ind.macd_histogram
    ema_20 = ind.ema_20
    ema_50 = ind.ema_50
    adx = ta.adx

    # --- Determine action ---
    buy_conditions = (
        ema_20 > ema_50
        and 45 < rsi < 70
        and macd_hist > 0
    )
    sell_conditions = (
        ema_20 < ema_50
        and 30 < rsi < 55
        and macd_hist < 0
    )

    if buy_conditions:
        action = "BUY"
    elif sell_conditions:
        action = "SELL"
    else:
        action = "HOLD"

    # --- Confidence ---
    confidence = 0.65
    if adx > 25:
        confidence += 0.10
    # No conflicting signals: all indicators point same direction
    if action == "BUY" and rsi > 50 and macd_hist > 0 and ema_20 > ema_50:
        confidence += 0.05
    elif action == "SELL" and rsi < 50 and macd_hist < 0 and ema_20 < ema_50:
        confidence += 0.05
    confidence = min(confidence, 0.95)

    # --- Risk score ---
    if rsi > 70 or rsi < 30:
        risk_score = "HIGH"
    elif adx > 25 and 45 < rsi < 65:
        risk_score = "LOW"
    else:
        risk_score = "MEDIUM"

    # --- Reasoning ---
    reasoning = (
        f"EMA20={ema_20:.2f} {'>' if ema_20 > ema_50 else '<='} EMA50={ema_50:.2f}, "
        f"RSI={rsi:.1f}, MACD_hist={macd_hist:.6f}, ADX={adx:.1f} -> {action}"
    )

    size_pct = _SIZE_BY_RISK[risk_score]

    return RuleSignal(
        action=action,
        confidence=round(confidence, 4),
        risk_score=risk_score,
        reasoning=reasoning,
        size_pct=size_pct,
    )
