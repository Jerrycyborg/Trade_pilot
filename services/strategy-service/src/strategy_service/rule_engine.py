"""Deterministic EMA/RSI/MACD rule-based strategy engine."""

from __future__ import annotations

from dataclasses import dataclass

from market_data.models import TASummary

_SIZE_BY_RISK = {"LOW": 0.02, "MEDIUM": 0.015, "HIGH": 0.005}


@dataclass
class RuleSignal:
    action: str  # "BUY" | "SELL" | "HOLD"
    confidence: float
    risk_score: str  # "LOW" | "MEDIUM" | "HIGH"
    reasoning: str
    size_pct: float


#: The champion's thresholds, stated once. `evaluate_rules` with these values
#: is the rule as it has always run; a challenger substitutes its own recorded
#: proposal. The sell band mirrors the buy band around RSI 50, matching the
#: backtest strategy's deliberate reduction of the search space.
CHAMPION_PARAMETERS: dict[str, float] = {
    "ema_fast": 20.0,
    "ema_slow": 50.0,
    "rsi_buy_min": 45.0,
    "rsi_buy_max": 70.0,
    "macd_hist_min": 0.0,
}


def evaluate_rules(
    ta: TASummary,
    config: dict | None = None,
    sentiment_score: float | None = None,
    bars: list | None = None,
    parameters: dict | None = None,
) -> RuleSignal:
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
    adx = ta.adx

    # Parameterised thresholds (L4: a challenger trades its recorded proposal;
    # the champion trades CHAMPION_PARAMETERS, which reproduce the original
    # hardcoded rule exactly — a test asserts that identity).
    active = {**CHAMPION_PARAMETERS, **(parameters or {})}
    rsi_buy_min = float(active["rsi_buy_min"])
    rsi_buy_max = float(active["rsi_buy_max"])
    macd_hist_min = float(active["macd_hist_min"])
    ema_fast_period = int(active["ema_fast"])
    ema_slow_period = int(active["ema_slow"])

    if ema_fast_period == 20 and ema_slow_period == 50:
        # The champion's averages come from the TA summary, exactly as before.
        ema_fast = ind.ema_20
        ema_slow = ind.ema_50
    else:
        # Non-default periods need the series. Without enough of it the answer
        # is "cannot evaluate", never a rule quietly run on the wrong averages:
        # a challenger's whole identity is its parameters, and trading it on
        # the champion's would record evidence for a strategy nobody proposed.
        closes = [float(b.close) for b in (bars or []) if getattr(b, "close", None)]
        if len(closes) < ema_slow_period:
            return RuleSignal(
                action="HOLD",
                confidence=0.0,
                risk_score="HIGH",
                reasoning=(
                    f"EMA({ema_fast_period}/{ema_slow_period}) needs "
                    f"{ema_slow_period} bars, have {len(closes)} — not evaluated"
                ),
                size_pct=0.0,
            )
        from market_data.indicators import compute_ema

        ema_fast = compute_ema(closes, ema_fast_period)
        ema_slow = compute_ema(closes, ema_slow_period)

    # The sell band mirrors the buy band around RSI 50; with the champion's
    # 45-70 that is the original 30-55.
    rsi_sell_min = 100.0 - rsi_buy_max
    rsi_sell_max = 100.0 - rsi_buy_min

    # --- Determine action ---
    buy_conditions = (
        ema_fast > ema_slow
        and rsi_buy_min < rsi < rsi_buy_max
        and macd_hist > macd_hist_min
    )
    sell_conditions = (
        ema_fast < ema_slow
        and rsi_sell_min < rsi < rsi_sell_max
        and macd_hist < -macd_hist_min
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
    if action == "BUY" and rsi > 50 and macd_hist > 0 and ema_fast > ema_slow:
        confidence += 0.05
    elif action == "SELL" and rsi < 50 and macd_hist < 0 and ema_fast < ema_slow:
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
        f"EMA{ema_fast_period}={ema_fast:.2f} "
        f"{'>' if ema_fast > ema_slow else '<='} EMA{ema_slow_period}={ema_slow:.2f}, "
        f"RSI={rsi:.1f}, MACD_hist={macd_hist:.6f}, ADX={adx:.1f} -> {action}"
    )

    sentiment_block_threshold = float((config or {}).get("sentiment_block_threshold", -0.3))
    if (
        action == "BUY"
        and sentiment_score is not None
        and sentiment_score < sentiment_block_threshold
    ):
        action = "HOLD"
        reasoning = (
            reasoning
            + f" | Sentiment gate blocked BUY (score={sentiment_score:.2f} < "
            + f"{sentiment_block_threshold})"
        )
        confidence = min(confidence, 0.55)

    if bars is not None and len(bars) >= 2:
        from market_data.indicators import detect_patterns

        slice_ = bars[-3:] if len(bars) >= 3 else bars
        opens_ = [b.open for b in slice_]
        highs_ = [b.high for b in slice_]
        lows_ = [b.low for b in slice_]
        closes_ = [b.close for b in slice_]
        patterns = detect_patterns(opens_, highs_, lows_, closes_)
        bullish_patterns = {"hammer", "bullish_engulfing"}
        bearish_patterns = {"shooting_star", "bearish_engulfing"}
        matched_bullish = bullish_patterns & set(patterns)
        matched_bearish = bearish_patterns & set(patterns)
        if action == "BUY" and matched_bullish:
            confidence = min(0.95, confidence + 0.10)
            reasoning = reasoning + f" | Pattern: {sorted(matched_bullish)}"
        elif action == "SELL" and matched_bearish:
            confidence = min(0.95, confidence + 0.10)
            reasoning = reasoning + f" | Pattern: {sorted(matched_bearish)}"

    size_pct = _SIZE_BY_RISK[risk_score]

    return RuleSignal(
        action=action,
        confidence=round(confidence, 4),
        risk_score=risk_score,
        reasoning=reasoning,
        size_pct=size_pct,
    )
